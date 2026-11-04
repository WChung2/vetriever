"""Full texts. `vet fetch <PMID or DOI>…` finds, downloads and checks each paper's full text,
trying legal routes from free to subscription:

1. pmc        PubMed Central, as Europe PMC XML (free).
2. free_copy  a free PDF that OpenAlex lists (repositories, open-access journals).
3. elsevier   Elsevier's text-mining API (ELSEVIER_API_KEY in .env).
   wiley      Wiley's text-mining API (WILEY_TDM_TOKEN in .env).
   publisher  another text-mining PDF link the publisher registered at Crossref (e.g. Springer).

The subscription routes use the institution's licence, so they work only on its network or
VPN, and only for non-commercial research. Publisher web pages are never scraped: where a
publisher has a text-mining API, its web site is not used at all.

A file is kept only if it is the whole paper (a PDF needs at least 2 pages, and any file at
least 500 words of main text, so a one-page preview is refused) and the record's title words
are in it. Files go to <db folder>/docs/<source_id>.<pdf|xml>, with the text the structuring
step reads in <source_id>.txt. Each paper's outcome, with why every route failed, is a row of
the fetches table in the database beside docs/ (vetriever.db). After each run the table is
written out to docs/index.csv, and the papers no route got to reports/to_download.csv with a
link, to download by hand. A re-run skips the papers the table has a file for; an older
docs/index.csv is loaded into the table once, when the table is still empty. `fetched` tells
other steps which papers have a full text.
"""
import csv
import io
import logging
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from . import connectors as c
from .settings import DB, env
from .store import document_key, norm_doi

EUROPEPMC_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{}/fullTextXML"
ELSEVIER = "https://api.elsevier.com/content/article/doi/"
WILEY = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/"
WILEY_GAP = 10.0          # Wiley allows 60 requests per 10 minutes
# Crossref member → the publisher's text-mining API (newer Wiley DOIs register no API link)
API_OF_MEMBER = {"78": "elsevier", "311": "wiley"}
API_HOST = {"elsevier": "api.elsevier.com", "wiley": "api.wiley.com"}
# publishers whose web sites code must not use: their text-mining API is the way in
API_ONLY = ("wiley.com", "sciencedirect.com", "elsevier.com")
MIN_PAGES, MIN_WORDS, MIN_TITLE = 2, 500, 0.8
# symbol codes older PDFs give as text (e.g. JVIM 2000), checked against the page images;
# "=" from its place in statistics ("P /H11005 .017")
PI_CODES = {"/H11005": "=", "/H11021": "<", "/H11022": ">", "/H11034": "°", "/H9262": "µ",
            "/H17015": "©"}
# fonts whose symbols come out as ordinary letters ("o" for "<", "4" for ">"): the text can't
# be trusted for <, >, = and the like, so the paper is flagged for a person to check
SYMBOL_FONT = re.compile(r"AdvP4C4E|AdvPi\d|AdvPSMP|AdvPSMSAM|MathematicalPi|Symbol")
COLUMNS = ["source_id", "pmid", "doi", "pmcid", "title", "route", "access", "file_name",
           "text_file", "pages", "words", "title_match", "text_warning", "url",
           "retrieved_date", "tried"]

logging.getLogger("pypdf").setLevel(logging.ERROR)     # font warnings on older PDFs


class NoText(Exception):
    """A route did not give the full paper; the message says why."""


@dataclass
class Paper:
    """One row of the fetches table (and of docs/index.csv). route is empty when no route got
    the paper."""
    source_id: str = ""
    pmid: str = ""
    doi: str = ""
    pmcid: str = ""
    title: str = ""
    route: str = ""
    access: str = ""          # open_access | paywalled, as in sources.access
    file_name: str = ""
    text_file: str = ""
    pages: str = ""
    words: str = ""
    title_match: str = ""
    text_warning: str = ""    # why parts of the text can't be trusted (PDF symbols)
    url: str = ""
    retrieved_date: str = ""
    tried: str = ""           # route: why it failed | …


def fetch(ids: list[str], folder: Path, http: c.Http | None = None,
          say: Callable[[str], None] = print, db: Path | None = None) -> list[Paper]:
    """Fetch each paper into <folder>/docs; db is the database (default <folder>/vetriever.db)."""
    http = http or c.Http()
    (folder / "docs").mkdir(parents=True, exist_ok=True)
    con = _connect(db or folder / DB.name, folder)
    papers = []
    try:
        for p in _records(http, ids):
            where, args = _same(p.pmid, p.doi)
            done = next((q for q in _select(con, where, args)
                         if q.file_name and (folder / q.file_name).exists()), None)
            if done:
                papers.append(done)
                say(f"  had   {done.source_id}  ({done.route})")
                continue
            _fetch_one(http, p, folder)
            with con:           # replaces an earlier try, maybe under another key
                con.execute(f"DELETE FROM fetches {where}", args)
                _insert(con, [p])
            papers.append(p)
            say(f"  {'got ' if p.route else 'none'}  {p.source_id}  "
                f"({p.route or p.tried})")
        _export(con, folder)
    finally:
        con.close()
    return papers


def fetched(ids: list[str], db: Path) -> dict[str, Paper]:
    """What fetch stored for each PMID or DOI, by the id as given; ids never fetched are left
    out. A paper has its full text when route is set: text_file (beside db) is the text."""
    con = _connect(db, db.parent)
    try:
        out = {}
        for raw in ids:
            kind, value = paper_id(raw)
            rows = _select(con, *_same(*((value, "") if kind == "pmid" else ("", value))))
            if rows:
                out[raw] = next((q for q in rows if q.route), rows[0])
        return out
    finally:
        con.close()


def paper_id(raw: str) -> tuple[str, str]:
    """("pmid", digits) or ("doi", the DOI in lower case); ValueError for anything else."""
    i = raw.strip().removeprefix("PMID")
    if re.fullmatch(r"\d+", i):
        return "pmid", i
    if norm_doi(i).startswith("10."):
        return "doi", norm_doi(i)
    raise ValueError(f"not a PMID or DOI: {raw!r}")


def _records(http: c.Http, ids: list[str]) -> list[Paper]:
    """PubMed's record (title, DOI, PMCID) for each PMID or DOI, in the order given."""
    order = [paper_id(raw) for raw in ids]
    dois = [v for kind, v in order if kind == "doi"]
    found = {}
    for k in range(0, len(dois), 150):
        found |= {norm_doi(d): v["pmid"] for d, v in c.convert_ids(http, dois[k:k + 150]).items()
                  if v.get("pmid")}
    pmids = [v if kind == "pmid" else found.get(v, "") for kind, v in order]
    recs = {r.pmid: r for r in c.pubmed_records(http, [p for p in pmids if p])}
    out = []
    for (kind, v), pmid in zip(order, pmids):
        r = recs.get(pmid)
        if r:
            p = Paper(pmid=pmid, doi=r.doi or (v if kind == "doi" else ""), pmcid=r.pmcid,
                      title=r.title)
        elif kind == "doi":
            r = c.crossref_work(http, v)
            p = Paper(doi=v, title=r.title if r else "")
        else:
            raise ValueError(f"PMID {v} is not in PubMed")
        out.append(p)
    return out


def _fetch_one(http: c.Http, p: Paper, folder: Path):
    free, free_pdfs, doi = _openalex(http, p)
    p.doi = p.doi or doi                     # PubMed lacks the DOI of some older papers
    p.source_id = document_key({"doi": p.doi, "pmid": p.pmid, "source_id": ""})
    routes = []
    if p.pmcid:
        routes.append(("pmc", lambda: _pmc(http, p.pmcid)))
    for url in free_pdfs:
        if not _api_only(url):
            routes.append(("free_copy", lambda url=url: _pdf(http, url)))
    member, links = _crossref(http, p.doi) if p.doi else ("", [])
    apis = {API_OF_MEMBER.get(member)} | {name for _, url in links
                                          for name, host in API_HOST.items() if host in url}
    if "elsevier" in apis:
        routes.append(("elsevier", lambda: _elsevier(http, p.doi)))
    if "wiley" in apis:
        routes.append(("wiley", lambda: _wiley(http, p.doi)))
    for kind, url in links:
        if kind == "application/pdf" and not _api_only(url) and not any(
                host in url for host in API_HOST.values()):
            routes.append(("publisher", lambda url=url: _pdf(http, url)))
    tried = []
    for name, get in routes:
        try:
            ext, data, url = get()
            text, pages, words = _read(ext, data)
            match = _title_match(p.title, text)
            if pages and pages < MIN_PAGES:
                raise NoText(f"preview only ({pages} page)")
            if words < MIN_WORDS:
                raise NoText(f"too little main text ({words} words): an abstract, a preview "
                             "or a scan without text")
            if match < MIN_TITLE:
                raise NoText(f"title words not found ({match:.0%}): wrong paper?")
        except NoText as e:
            tried.append(f"{name}: {e}")
            continue
        except httpx.HTTPStatusError as e:
            tried.append(f"{name}: refused ({e.response.status_code})" +
                         (": not subscribed, or not on the institution's network/VPN?"
                          if e.response.status_code in (401, 403) and name in
                          ("elsevier", "wiley") else ""))
            continue
        except httpx.HTTPError as e:
            tried.append(f"{name}: {type(e).__name__}")
            continue
        (folder / "docs" / f"{p.source_id}.{ext}").write_bytes(data)
        (folder / "docs" / f"{p.source_id}.txt").write_text(text, "utf-8")
        p.route, p.url = name, url
        p.access = "open_access" if free or name in ("pmc", "free_copy") else "paywalled"
        p.file_name, p.text_file = f"docs/{p.source_id}.{ext}", f"docs/{p.source_id}.txt"
        p.pages, p.words, p.title_match = str(pages or ""), str(words), f"{match:.2f}"
        p.text_warning = _symbol_warning(data, text) if ext == "pdf" else ""
        p.retrieved_date = date.today().isoformat()
        break
    if not routes:
        tried.append("no route: no free copy, and " + ("the publisher has no text-mining "
                     "route" if p.doi else "no DOI"))
    p.tried = " | ".join(tried)


# ---- where the full text might be ------------------------------------------------------

def _openalex(http: c.Http, p: Paper) -> tuple[bool, list[str], str]:
    """Is the paper free to read, the free PDFs OpenAlex lists for it, and its DOI."""
    params = {"filter": f"pmid:{p.pmid}" if p.pmid else f"doi:{p.doi}",
              "select": "doi,open_access,locations"}
    if env("OPENALEX_API_KEY"):
        params["api_key"] = env("OPENALEX_API_KEY")
    try:
        works = http.get(c.OPENALEX, params).json()["results"]
    except httpx.HTTPError:
        return False, [], ""
    if not works:
        return False, [], ""
    w = works[0]
    pdfs = [loc["pdf_url"] for loc in w.get("locations") or [] if loc.get("is_oa") and
            loc.get("pdf_url")]
    return (bool(w.get("open_access", {}).get("is_oa")), list(dict.fromkeys(pdfs)),
            norm_doi(w.get("doi") or ""))


def _crossref(http: c.Http, doi: str) -> tuple[str, list[tuple[str, str]]]:
    """The DOI's Crossref member (publisher) and the full-text links it registered for text
    mining: (content type, URL)."""
    params = {"mailto": env("CONTACT_EMAIL")} if env("CONTACT_EMAIL") else None
    try:
        m = http.get(c.CROSSREF + doi, params).json()["message"]
    except httpx.HTTPError:
        return "", []
    return str(m.get("member", "")), [
        (x.get("content-type", ""), x["URL"]) for x in m.get("link", [])
        if x.get("intended-application") in ("text-mining", "unspecified")]


def _api_only(url: str) -> bool:
    return any(httpx.URL(url).host.endswith(h) for h in API_ONLY)


# ---- the routes: each gives (extension, bytes, url) or raises --------------------------

def _pmc(http: c.Http, pmcid: str):
    url = EUROPEPMC_XML.format(pmcid)
    return "xml", http.get(url).content, url


def _pdf(http: c.Http, url: str):
    r = http.get(url)
    if not r.content.startswith(b"%PDF"):
        raise NoText(f"not a PDF ({r.headers.get('content-type', '?').split(';')[0]})")
    return "pdf", r.content, url


def _elsevier(http: c.Http, doi: str):
    key = env("ELSEVIER_API_KEY")
    if not key:
        raise NoText("no ELSEVIER_API_KEY in .env")
    url = ELSEVIER + doi
    r = http.get(url, headers={"X-ELS-APIKey": key, "Accept": "text/xml"})
    if re.search(rb"<(?:\w+:)?sections\b", r.content):
        return "xml", r.content, url
    # older papers have no XML body, only a PDF (a one-page preview if not subscribed)
    r = http.get(url, headers={"X-ELS-APIKey": key, "Accept": "application/pdf"})
    return "pdf", r.content, url


def _wiley(http: c.Http, doi: str):
    token = env("WILEY_TDM_TOKEN")
    if not token:
        raise NoText("no WILEY_TDM_TOKEN in .env")
    url = WILEY + quote(doi, safe="")
    r = http.get(url, gap=WILEY_GAP, headers={"Wiley-TDM-Client-Token": token})
    if not r.content.startswith(b"%PDF"):
        raise NoText("Wiley sent no PDF")
    return "pdf", r.content, url


# ---- reading and checking a file -------------------------------------------------------

def _read(ext: str, data: bytes) -> tuple[str, int, int]:
    """(text, pages, words of main text); pages is 0 for XML."""
    if ext == "pdf":
        try:
            pdf = PdfReader(io.BytesIO(data))
            text = "\n\n".join(page.extract_text() or "" for page in pdf.pages)
        except PyPdfError as e:
            raise NoText(f"unreadable PDF ({e})") from None
        for code, symbol in PI_CODES.items():
            text = text.replace(code, symbol)
        return _clean(text), len(pdf.pages), len(text.split())
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise NoText("unreadable XML") from None
    article = next((e for e in root.iter() if _local(e) == "article"), root)
    body = next((e for e in article.iter() if _local(e) == "body"), None)
    out = []
    _walk(article, out)
    return _clean("".join(out)), 0, len(" ".join(body.itertext()).split()) if body is not None else 0


def _symbol_warning(data: bytes, text: str) -> str:
    """Why <, >, = and the like may be wrong in a PDF's text; empty when they can be trusted."""
    fonts = set()
    try:
        for page in PdfReader(io.BytesIO(data)).pages:
            for ref in ((page.get("/Resources") or {}).get("/Font") or {}).values():
                font = ref.get_object()
                name = re.sub(r"^/?[A-Z]{6}\+", "", str(font.get("/BaseFont", "")))
                if SYMBOL_FONT.search(name) and "/ToUnicode" not in font:
                    fonts.add(name)
    except (PyPdfError, AttributeError):
        fonts.add("(fonts unreadable)")
    codes = sorted(set(re.findall(r"/H\d{4,5}", text)))
    why = ([f"symbol fonts {', '.join(sorted(fonts))}"] if fonts else []) + \
          ([f"unknown symbol codes {', '.join(codes[:5])}"] if codes else [])
    return "symbols such as <, >, = may be wrong in the text: " + "; ".join(why) if why else ""


BLOCK = {"p", "para", "simple-para", "title", "section-title", "article-title", "caption",
         "label", "list-item", "fn", "abstract", "tr", "row", "table-wrap", "table", "sec"}
CELL = {"td", "th", "entry"}
HEADING = {"title", "section-title", "article-title"}
# inline items the XML gives with no space between them (names, keywords)
SPACED = {"surname", "given-names", "given-name", "kwd", "keyword", "contrib", "author", "aff"}
SKIP = {"ref-list", "bibliography", "meta", "coredata", "objects"}


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _walk(el, out: list):
    """XML → text: headings as '## ', one paragraph per line, table cells joined by ' | '."""
    name = _local(el)
    if name not in SKIP:
        out.append(("\n## " if name in HEADING else "") + (el.text or ""))
        for kid in el:
            _walk(kid, out)
        out.append("\n" if name in BLOCK else " | " if name in CELL else
                   " " if name in SPACED else "")
    out.append(el.tail or "")


def _clean(text: str) -> str:
    lines = [" ".join(line.split()) for line in unicodedata.normalize("NFKC", text).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def _title_words(s: str) -> set:
    s = unicodedata.normalize("NFKC", s).lower()
    return set(re.findall(r"[a-z]{4,}", re.sub(r"-\s*\n\s*", "", s)))


def _title_match(title: str, text: str) -> float:
    want = _title_words(title)
    return len(want & _title_words(text)) / len(want) if want else 0.0


# ---- the fetches table and its exports ------------------------------------------------

def _connect(db: Path, folder: Path) -> sqlite3.Connection:
    """The database with the fetches table; an older <folder>/docs/index.csv goes into the
    table while the table is empty, so nothing fetched before is lost."""
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    cols = ", ".join(f"{k} TEXT NOT NULL DEFAULT ''" for k in COLUMNS)
    with con:
        con.execute(f"CREATE TABLE IF NOT EXISTS fetches ({cols}, PRIMARY KEY (source_id))")
        if not con.execute("SELECT 1 FROM fetches LIMIT 1").fetchone():
            _insert(con, _read_index(folder / "docs" / "index.csv").values())
    return con


def _same(pmid: str, doi: str) -> tuple[str, tuple]:
    """WHERE clause for the rows with this PMID or this DOI (any case)."""
    return ("WHERE (pmid != '' AND pmid = ?) OR (doi != '' AND lower(doi) = lower(?))",
            (pmid, doi))


def _select(con: sqlite3.Connection, where: str = "", args: tuple = ()) -> list[Paper]:
    return [Paper(*r) for r in con.execute(
        f"SELECT {', '.join(COLUMNS)} FROM fetches {where} ORDER BY rowid", args)]


def _insert(con: sqlite3.Connection, papers):
    con.executemany(f"INSERT OR REPLACE INTO fetches ({', '.join(COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                    [[getattr(p, k) for k in COLUMNS] for p in papers])


def _export(con: sqlite3.Connection, folder: Path):
    """docs/index.csv (every paper) and reports/to_download.csv (the ones no route got)."""
    papers = _select(con)
    _write(folder / "docs" / "index.csv", COLUMNS, [vars(p) for p in papers])
    _write(folder / "reports" / "to_download.csv", ["source_id", "pmid", "title", "link", "why"],
           [{"source_id": p.source_id, "pmid": p.pmid, "title": p.title, "why": p.tried,
             "link": f"https://doi.org/{p.doi}" if p.doi else
             f"https://pubmed.ncbi.nlm.nih.gov/{p.pmid}/"}
            for p in papers if not p.route])


def _read_index(path: Path) -> dict[str, Paper]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["source_id"]: Paper(**{k: r.get(k) or "" for k in COLUMNS})
                for r in csv.DictReader(fh)}


def _write(path: Path, columns: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
