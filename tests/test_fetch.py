"""Full-text routes against fake services (offline, free). scripts/smoke.sh tries the real ones."""
import csv
import json
import shutil
import sqlite3

import httpx
import pytest

from vet import connectors as c
from vet import fetch as f

TITLE = "Cyclophosphamide for immune-mediated hemolytic anemia in dogs"
WORDS = " ".join(["dogs received prednisone and were followed for relapse"] * 70)


def pubmed_xml(pmid: str, doi: str, pmcid: str = "") -> bytes:
    pmc = f'<ArticleId IdType="pmc">{pmcid}</ArticleId>' if pmcid else ""
    return f"""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>
<Journal><JournalIssue><PubDate><Year>2001</Year></PubDate></JournalIssue><Title>J</Title></Journal>
<ArticleTitle>{TITLE}</ArticleTitle></Article></MedlineCitation><PubmedData><ArticleIdList>
<ArticleId IdType="doi">{doi}</ArticleId>{pmc}</ArticleIdList></PubmedData></PubmedArticle>
</PubmedArticleSet>""".encode()


def make_pdf(pages: list[str], font: str = "Helvetica") -> bytes:
    """A real PDF with one text line per 12 words, so pypdf reads the words back."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            3: f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} >>".encode()}
    kids = []
    for i, text in enumerate(pages):
        page, content = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{page} 0 R")
        words = text.split()
        lines = [" ".join(words[k:k + 12]) for k in range(0, len(words), 12)]
        stream = ("BT /F1 9 Tf 11 TL 40 760 Td " + "".join(f"({x}) Tj T* " for x in lines)
                  + "ET").encode()
        objs[page] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
                      f"{content} 0 R /Resources << /Font << /F1 3 0 R >> >> >>").encode()
        objs[content] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()
    out, offsets = b"%PDF-1.4\n", {}
    for k in sorted(objs):
        offsets[k] = len(out)
        out += b"%d 0 obj\n" % k + objs[k] + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % offsets[k] for k in sorted(objs))
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, start)


FULL_PDF = make_pdf([TITLE + " " + WORDS[:1800], WORDS[1800:]])
PREVIEW_PDF = make_pdf([TITLE + " " + WORDS[:600]])
OTHER_PDF = make_pdf(["Canine osteoarthritis pain " + WORDS[:1800], WORDS[1800:]])
PMC_XML = f"""<article><front><article-meta><title-group><article-title>{TITLE}</article-title>
</title-group><contrib><name><surname>Agnoli</surname><given-names>Chiara</given-names></name>
</contrib></article-meta></front><body><sec><title>Methods</title><p>{WORDS}</p>
<table-wrap><table><tr><td>Prednisone</td><td>2 mg/kg</td></tr></table></table-wrap></sec></body>
<back><ref-list><ref>Smith 1999</ref></ref-list></back></article>""".encode()
ELSEVIER_NO_BODY = b"<full-text-retrieval-response><coredata/></full-text-retrieval-response>"


class Services:
    """Fake PubMed, OpenAlex, Crossref and publishers; records every URL asked for."""

    def __init__(self, pmcid="", free=False, free_pdfs=(), links=(), answers=None, member="",
                 doi="10.1/x", openalex_doi=""):
        self.pmcid, self.free, self.free_pdfs, self.links = pmcid, free, free_pdfs, links
        self.member, self.doi, self.openalex_doi = member, doi, openalex_doi
        self.answers = answers or {}
        self.asked = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.asked.append(url)
        if "efetch.fcgi" in url:
            return httpx.Response(200, content=pubmed_xml("111", self.doi, self.pmcid))
        if "api.openalex.org" in url:
            locs = [{"is_oa": True, "pdf_url": u} for u in self.free_pdfs]
            return httpx.Response(200, json={"results": [
                {"doi": self.openalex_doi, "open_access": {"is_oa": self.free}, "locations": locs}]})
        if "api.crossref.org" in url:
            return httpx.Response(200, json={"message": {"member": self.member, "link": [
                {"URL": u, "content-type": t, "intended-application": "text-mining"}
                for t, u in self.links]}})
        for start, answer in self.answers.items():
            if url.startswith(start):
                return answer(req) if callable(answer) else answer
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "env", lambda name: "")
    keys = {"ELSEVIER_API_KEY": "k", "WILEY_TDM_TOKEN": "t"}
    monkeypatch.setattr(f, "env", lambda name: keys.get(name, ""))
    return keys


def run(services: Services, folder):
    http = c.Http(httpx.Client(transport=httpx.MockTransport(services)))
    return f.fetch(["111"], folder, http, say=lambda s: None)[0]


def to_download(folder) -> list[dict]:
    with open(folder / "reports" / "to_download.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def table(db) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM fetches ORDER BY rowid")]
    finally:
        con.close()


def pmc_services() -> Services:
    return Services(pmcid="PMC9",
                    answers={"https://www.ebi.ac.uk/": httpx.Response(200, content=PMC_XML)})


def test_pubmed_central_first_and_text_keeps_headings_and_tables(tmp_path):
    s = Services(pmcid="PMC9", links=[("application/pdf", "https://api.wiley.com/x")],
                 answers={"https://www.ebi.ac.uk/": httpx.Response(200, content=PMC_XML)})
    p = run(s, tmp_path)
    assert (p.route, p.access, p.file_name) == ("pmc", "open_access", "docs/DOI-10-1-X.xml")
    text = (tmp_path / p.text_file).read_text("utf-8")
    assert "## Methods" in text and "Prednisone | 2 mg/kg" in text
    assert "Smith 1999" not in text                        # references left out
    assert "Agnoli Chiara" in text                         # names keep their space
    assert not any("wiley" in u for u in s.asked)          # stopped at the first good route


def test_one_page_preview_is_refused_and_listed_to_download(tmp_path):
    s = Services(links=[("text/xml", "https://api.elsevier.com/content/article/PII:S1")],
                 answers={"https://api.elsevier.com/": lambda req: httpx.Response(
                     200, content=PREVIEW_PDF if req.headers["Accept"] == "application/pdf"
                     else ELSEVIER_NO_BODY)})
    p = run(s, tmp_path)
    assert p.route == "" and "elsevier: preview only (1 page)" in p.tried
    assert not list((tmp_path / "docs").glob("DOI-*"))
    assert [r["link"] for r in to_download(tmp_path)] == ["https://doi.org/10.1/x"]


def test_wrong_paper_is_refused(tmp_path):
    s = Services(links=[("unspecified", "https://api.wiley.com/onlinelibrary/tdm/v1/articles/x")],
                 answers={"https://api.wiley.com/": httpx.Response(200, content=OTHER_PDF)})
    p = run(s, tmp_path)
    assert p.route == "" and "wiley: title words not found" in p.tried


def test_subscription_paper_through_wiley(tmp_path):
    s = Services(links=[("unspecified", "https://api.wiley.com/onlinelibrary/tdm/v1/articles/x")],
                 answers={"https://api.wiley.com/": httpx.Response(200, content=FULL_PDF)})
    p = run(s, tmp_path)
    assert (p.route, p.access, p.pages) == ("wiley", "paywalled", "2")
    assert TITLE.split()[0] in (tmp_path / p.text_file).read_text("utf-8")


def test_missing_key_is_said_and_nothing_is_asked(tmp_path, offline):
    offline.clear()
    s = Services(links=[("unspecified", "https://api.wiley.com/onlinelibrary/tdm/v1/articles/x")])
    p = run(s, tmp_path)
    assert p.tried == "wiley: no WILEY_TDM_TOKEN in .env"
    assert not any("api.wiley.com" in u for u in s.asked)


def test_publisher_web_sites_with_an_api_are_never_used(tmp_path):
    s = Services(free=True, free_pdfs=["https://onlinelibrary.wiley.com/doi/pdf/10.1/x"],
                 links=[("application/pdf", "https://onlinelibrary.wiley.com/doi/pdf/10.1/x")])
    p = run(s, tmp_path)
    assert p.route == "" and not any("onlinelibrary.wiley.com" in u for u in s.asked)
    assert p.tried.startswith("no route")


def test_free_copy_from_a_repository(tmp_path):
    s = Services(free=True, free_pdfs=["https://repository.example.edu/1.pdf"],
                 answers={"https://repository.example.edu/": httpx.Response(200, content=FULL_PDF)})
    p = run(s, tmp_path)
    assert (p.route, p.access) == ("free_copy", "open_access")


def test_rerun_keeps_what_it_has(tmp_path):
    s = Services(pmcid="PMC9", answers={"https://www.ebi.ac.uk/": httpx.Response(200, content=PMC_XML)})
    run(s, tmp_path)
    again = Services(pmcid="PMC9")
    assert run(again, tmp_path).route == "pmc"
    assert not any("ebi.ac.uk" in u for u in again.asked)


def test_not_a_pmid_or_doi(tmp_path):
    with pytest.raises(ValueError, match="not a PMID or DOI"):
        f.fetch(["imha"], tmp_path, c.Http(httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=json.dumps({}).encode())))))


def test_newer_wiley_dois_go_to_the_api_by_publisher(tmp_path):
    """Newer Wiley DOIs list only web-site links at Crossref; the member number says Wiley."""
    s = Services(member="311", links=[("application/pdf", "https://onlinelibrary.wiley.com/doi/pdf/x")],
                 answers={"https://api.wiley.com/": httpx.Response(200, content=FULL_PDF)})
    assert run(s, tmp_path).route == "wiley"
    assert not any("onlinelibrary.wiley.com" in u for u in s.asked)


def test_doi_missing_in_pubmed_comes_from_openalex(tmp_path):
    s = Services(doi="", openalex_doi="https://doi.org/10.2460/JAVMA.1999.214.07.1026")
    p = run(s, tmp_path)
    assert (p.doi, p.source_id) == ("10.2460/javma.1999.214.07.1026",
                                    "DOI-10-2460-JAVMA-1999-214-07-1026")
    assert any("api.crossref.org" in u for u in s.asked)


def test_a_retry_replaces_the_earlier_line_even_under_a_new_key(tmp_path):
    run(Services(doi=""), tmp_path)                                    # no DOI anywhere yet
    run(Services(doi="", openalex_doi="10.2460/javma.x"), tmp_path)    # now OpenAlex has one
    with open(tmp_path / "docs" / "index.csv", encoding="utf-8") as fh:
        assert [r["source_id"] for r in csv.DictReader(fh)] == ["DOI-10-2460-JAVMA-X"]
    assert len(to_download(tmp_path)) == 1


def test_pdf_symbol_codes_become_symbols_and_symbol_fonts_are_flagged(tmp_path):
    pdf = make_pdf([TITLE + " relapse (PCV /H11021 25%) " + WORDS[:1800], WORDS[1800:]])
    s = Services(links=[("unspecified", "https://api.wiley.com/onlinelibrary/tdm/v1/articles/x")],
                 answers={"https://api.wiley.com/": httpx.Response(200, content=pdf)})
    p = run(s, tmp_path)
    assert "PCV < 25%" in (tmp_path / p.text_file).read_text("utf-8") and p.text_warning == ""
    shutil.rmtree(tmp_path)
    s.answers = {"https://api.wiley.com/": httpx.Response(200, content=make_pdf(
        [TITLE + " " + WORDS[:1800], WORDS[1800:]], font="AdvP4C4E74"))}
    assert "AdvP4C4E74" in run(s, tmp_path).text_warning


def test_outcomes_are_in_the_database_and_the_csvs_are_written_from_it(tmp_path):
    p = run(pmc_services(), tmp_path)
    rows = table(tmp_path / "vetriever.db")
    assert rows == [vars(p)]
    with open(tmp_path / "docs" / "index.csv", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh)) == rows
    assert to_download(tmp_path) == []
    con = sqlite3.connect(tmp_path / "vetriever.db")
    assert [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")] \
        == ["fetches"]                                    # no other table is made or changed
    con.close()


def test_rerun_reads_the_table_not_the_csv(tmp_path):
    run(pmc_services(), tmp_path)
    (tmp_path / "docs" / "index.csv").unlink()
    again = Services(pmcid="PMC9")
    assert run(again, tmp_path).route == "pmc"
    assert not any("ebi.ac.uk" in u for u in again.asked)
    assert (tmp_path / "docs" / "index.csv").exists()     # written again from the table


def test_an_older_index_csv_is_loaded_once(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "DOI-10-1-X.xml").write_bytes(PMC_XML)
    cols = [k for k in f.COLUMNS if k != "text_warning"]           # an older layout

    def write_index(*papers):
        f._write(docs / "index.csv", cols, [{k: getattr(p, k) for k in cols} for p in papers])

    write_index(f.Paper(source_id="DOI-10-1-X", pmid="111", doi="10.1/x", route="pmc",
                        file_name="docs/DOI-10-1-X.xml", text_file="docs/DOI-10-1-X.txt"),
                f.Paper(source_id="PMID222", pmid="222", title="Older", tried="no route: no DOI"))
    def keys():
        with open(docs / "index.csv", encoding="utf-8") as fh:
            return ([r["source_id"] for r in table(tmp_path / "vetriever.db")],
                    [r["source_id"] for r in csv.DictReader(fh)])

    s = Services(pmcid="PMC9")
    assert run(s, tmp_path).route == "pmc" and not any("ebi.ac.uk" in u for u in s.asked)
    assert keys() == (["DOI-10-1-X", "PMID222"], ["DOI-10-1-X", "PMID222"])
    assert [r["pmid"] for r in to_download(tmp_path)] == ["222"]
    write_index(f.Paper(source_id="PMID333", pmid="333"))             # the table has rows now
    run(Services(pmcid="PMC9"), tmp_path)
    assert keys() == (["DOI-10-1-X", "PMID222"], ["DOI-10-1-X", "PMID222"])


def test_fetched_says_which_papers_have_a_full_text(tmp_path):
    s = pmc_services()
    s.doi = "10.1/X"                                       # PubMed's DOIs keep their case
    run(s, tmp_path / "a")
    got = f.fetched(["111", "10.1/x", "PMID999"], tmp_path / "a" / "vetriever.db")
    assert set(got) == {"111", "10.1/x"} and got["111"] == got["10.1/x"]
    assert (got["111"].route, got["111"].access, got["111"].text_file) == (
        "pmc", "open_access", "docs/DOI-10-1-X.txt")
    http = c.Http(httpx.Client(transport=httpx.MockTransport(Services())))     # no route
    f.fetch(["111"], tmp_path / "b", http, say=lambda s: None, db=tmp_path / "b" / "other.db")
    assert f.fetched(["111"], tmp_path / "b" / "other.db")["111"].route == ""
    assert not (tmp_path / "b" / "vetriever.db").exists()
