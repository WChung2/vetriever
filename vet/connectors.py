"""Literature services. Code calls them; the agents only write the queries.

Every service gets the same polite access: requests to one host are spaced (1 per second;
PubMed 3/s, or 10/s with an NCBI key), 429 and 5xx answers are retried with back-off, and
the User-Agent names the project and the contact email from .env. `log_search` appends
each search to corpus/search_log.csv so anyone can repeat it.
"""
import csv
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

import httpx

from .settings import CORPUS, env

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works/"
UNPAYWALL = "https://api.unpaywall.org/v2/"


@dataclass
class Record:
    """One search hit, whatever the service. Empty string = not given by the service."""
    service: str
    title: str = ""
    abstract: str = ""
    year: str = ""
    journal: str = ""
    first_author: str = ""
    last_author: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    openalex_id: str = ""
    pub_types: str = ""       # pipe list
    oa_url: str = ""


class Http:
    def __init__(self, client: httpx.Client | None = None):
        email = env("CONTACT_EMAIL")
        agent = "vetriever/0.1 (open research project" + (f"; mailto:{email}" if email else "") + ")"
        self.client = client or httpx.Client(headers={"User-Agent": agent}, timeout=30,
                                             follow_redirects=True)
        self.last: dict[str, float] = {}

    def get(self, url: str, params: dict | None = None, gap: float = 1.0) -> httpx.Response:
        host = httpx.URL(url).host
        for attempt in range(4):
            wait = self.last.get(host, 0.0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.monotonic()
            r = self.client.get(url, params=params)
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                break
            retry = r.headers.get("Retry-After", "")
            time.sleep(int(retry) if retry.isdigit() else 2 ** (attempt + 1))
        r.raise_for_status()
        return r


def log_search(service: str, query: str, hits: int, returned: int):
    path = CORPUS / "search_log.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        if new:
            w.writerow(["date", "service", "query", "hits", "returned"])
        w.writerow([date.today().isoformat(), service, query, hits, returned])


# ---- PubMed (NCBI E-utilities) -----------------------------------------------------------

def _ncbi(http: Http, endpoint: str, **params) -> httpx.Response:
    params |= {"tool": "vetriever"}
    if env("CONTACT_EMAIL"):
        params["email"] = env("CONTACT_EMAIL")
    if env("NCBI_API_KEY"):
        params["api_key"] = env("NCBI_API_KEY")
    return http.get(EUTILS + endpoint, params, gap=0.11 if env("NCBI_API_KEY") else 0.34)


class QueryProblem(ValueError):
    """The service ignored part of a query (e.g. a MeSH term that doesn't exist), so its
    result would silently mean something else. The message goes back to the Search agent."""


def pubmed_search(http: Http, query: str, limit: int = 100) -> tuple[int, list[str]]:
    """PubMed query syntax, e.g. '"Dogs"[Mesh] AND "Postoperative Pain"[Mesh]'."""
    res = _ncbi(http, "esearch.fcgi", db="pubmed", term=query, retmax=limit,
                retmode="json").json()["esearchresult"]
    warn = res.get("warninglist", {}) | res.get("errorlist", {})
    ignored = [p for k in ("quotedphrasesnotfound", "phrasesnotfound", "phrasesignored",
                           "fieldsnotfound") for p in warn.get(k, [])]
    if ignored:
        raise QueryProblem(f"PubMed ignored {ignored} in {query!r}")
    return int(res["count"]), res["idlist"]


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _pubmed_record(art) -> Record:
    cit = art.find("MedlineCitation")
    a = cit.find("Article")
    authors = [_text(x.find("CollectiveName")) or
               f"{_text(x.find('LastName'))} {_text(x.find('Initials'))}".strip()
               for x in a.findall("AuthorList/Author")]
    parts = [(p.get("Label", ""), _text(p)) for p in a.findall("Abstract/AbstractText")]
    date_el = a.find("Journal/JournalIssue/PubDate")
    year = _text(date_el.find("Year")) or _text(date_el.find("MedlineDate"))[:4]
    ids = {i.get("IdType"): _text(i) for i in art.findall("PubmedData/ArticleIdList/ArticleId")}
    return Record("pubmed", title=_text(a.find("ArticleTitle")),
                  abstract=" ".join(f"{lab}: {t}" if lab else t for lab, t in parts),
                  year=year, journal=_text(a.find("Journal/Title")),
                  first_author=authors[0] if authors else "",
                  last_author=authors[-1] if len(authors) > 1 else "",
                  doi=ids.get("doi", ""), pmid=_text(cit.find("PMID")), pmcid=ids.get("pmc", ""),
                  pub_types="|".join(_text(p) for p in a.findall("PublicationTypeList/PublicationType")))


def pubmed_records(http: Http, pmids: list[str]) -> list[Record]:
    out = []
    for i in range(0, len(pmids), 200):
        xml = _ncbi(http, "efetch.fcgi", db="pubmed", id=",".join(pmids[i:i + 200]),
                    retmode="xml").content
        out += [_pubmed_record(a) for a in ET.fromstring(xml).findall("PubmedArticle")]
    return out


def pubmed_similar(http: Http, pmid: str, limit: int = 20) -> list[str]:
    """PubMed's 'similar articles' for one PMID, best first."""
    res = _ncbi(http, "elink.fcgi", dbfrom="pubmed", db="pubmed", id=pmid,
                cmd="neighbor_score", retmode="json").json()
    for db in res["linksets"][0].get("linksetdbs", []):
        if db["linkname"] == "pubmed_pubmed":
            return [str(x["id"] if isinstance(x, dict) else x) for x in db["links"]
                    if str(x["id"] if isinstance(x, dict) else x) != pmid][:limit]
    return []


def convert_ids(http: Http, ids: list[str]) -> dict[str, dict]:
    """PMID / PMCID / DOI → all three (PubMed Central ID converter)."""
    params = {"ids": ",".join(ids), "format": "json", "tool": "vetriever"}
    if env("CONTACT_EMAIL"):
        params["email"] = env("CONTACT_EMAIL")
    recs = http.get(IDCONV, params, gap=0.34).json().get("records", [])
    return {r.get("requested-id", ""): {k: str(r.get(k, "")) for k in ("pmid", "pmcid", "doi")}
            for r in recs if "errmsg" not in r}


# ---- Europe PMC ------------------------------------------------------------------------

def europepmc_search(http: Http, query: str, limit: int = 100) -> tuple[int, list[Record]]:
    """Europe PMC syntax; covers PubMed, AGRICOLA (SRC:AGR), preprints, and full-text
    search of open-access papers."""
    res = http.get(EUROPEPMC, {"query": query, "format": "json", "resultType": "core",
                               "pageSize": min(limit, 1000)}).json()
    out = []
    for r in res.get("resultList", {}).get("result", []):
        authors = [a.get("fullName", "") for a in r.get("authorList", {}).get("author", [])]
        oa = next((u["url"] for u in r.get("fullTextUrlList", {}).get("fullTextUrl", [])
                   if u.get("availabilityCode") == "OA"), "")
        out.append(Record("europepmc", title=r.get("title", ""), abstract=r.get("abstractText", ""),
                          year=r.get("pubYear", ""),
                          journal=r.get("journalInfo", {}).get("journal", {}).get("title", ""),
                          first_author=authors[0] if authors else "",
                          last_author=authors[-1] if len(authors) > 1 else "",
                          doi=r.get("doi", ""), pmid=r.get("pmid", ""), pmcid=r.get("pmcid", ""),
                          pub_types="|".join(r.get("pubTypeList", {}).get("pubType", [])),
                          oa_url=oa))
    return int(res.get("hitCount", 0)), out[:limit]


# ---- OpenAlex --------------------------------------------------------------------------

def _abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    words = sorted((pos, w) for w, positions in inverted.items() for pos in positions)
    return " ".join(w for _, w in words)


def _openalex_record(w: dict) -> Record:
    ids = w.get("ids", {})
    authors = [a.get("author", {}).get("display_name", "") for a in w.get("authorships", [])]
    loc = (w.get("primary_location") or {}).get("source") or {}
    best = w.get("best_oa_location") or {}
    return Record("openalex", title=w.get("title") or "", abstract=_abstract(w.get("abstract_inverted_index")),
                  year=str(w.get("publication_year") or ""), journal=loc.get("display_name", ""),
                  first_author=authors[0] if authors else "",
                  last_author=authors[-1] if len(authors) > 1 else "",
                  doi=(w.get("doi") or "").removeprefix("https://doi.org/"),
                  pmid=(ids.get("pmid") or "").rsplit("/", 1)[-1],
                  pmcid=(ids.get("pmcid") or "").rsplit("/", 1)[-1],
                  openalex_id=(w.get("id") or "").rsplit("/", 1)[-1],
                  pub_types=w.get("type") or "", oa_url=best.get("pdf_url") or best.get("landing_page_url") or "")


def openalex_works(http: Http, limit: int = 100, **params) -> tuple[int, list[Record]]:
    """search="...", or filter="cites:W123" (cited by) / "cited_by:W123" (references)."""
    params |= {"per-page": min(limit, 200)}
    if env("OPENALEX_API_KEY"):
        params["api_key"] = env("OPENALEX_API_KEY")
    if env("CONTACT_EMAIL"):
        params["mailto"] = env("CONTACT_EMAIL")
    res = http.get(OPENALEX, params).json()
    return res["meta"]["count"], [_openalex_record(w) for w in res["results"]][:limit]


# ---- Crossref and Unpaywall ------------------------------------------------------------

def crossref_work(http: Http, doi: str) -> Record | None:
    """The DOI's registered record, or None when the DOI does not exist."""
    params = {"mailto": env("CONTACT_EMAIL")} if env("CONTACT_EMAIL") else None
    try:
        m = http.get(CROSSREF + doi, params).json()["message"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    authors = [f"{a.get('family', '')} {a.get('given', '')}".strip() for a in m.get("author", [])]
    year = (m.get("issued", {}).get("date-parts") or [[None]])[0][0]
    return Record("crossref", title=" ".join(m.get("title", [])), year=str(year or ""),
                  journal=" ".join(m.get("container-title", [])), doi=m.get("DOI", ""),
                  first_author=authors[0] if authors else "",
                  last_author=authors[-1] if len(authors) > 1 else "", pub_types=m.get("type", ""))


def unpaywall_url(http: Http, doi: str) -> str:
    """Legal open-access copy of a DOI ('' if none). Needs CONTACT_EMAIL."""
    email = env("CONTACT_EMAIL")
    if not email:
        raise RuntimeError("Unpaywall needs CONTACT_EMAIL in .env")
    try:
        best = http.get(UNPAYWALL + doi, {"email": email}).json().get("best_oa_location") or {}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return ""
        raise
    return best.get("url_for_pdf") or best.get("url") or ""
