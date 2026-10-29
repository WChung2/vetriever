"""Connectors against canned answers (offline, free). `vet ping` checks the live services."""
import httpx
import pytest

from vet import connectors as c

PUBMED_XML = b"""<?xml version="1.0"?><PubmedArticleSet><PubmedArticle>
<MedlineCitation><PMID>111</PMID><Article>
<Journal><JournalIssue><PubDate><MedlineDate>2019 Jan-Feb</MedlineDate></PubDate></JournalIssue>
<Title>Vet Anaesth Analg</Title></Journal>
<ArticleTitle>Two doses of <i>X</i> after surgery</ArticleTitle>
<Abstract><AbstractText Label="OBJECTIVE">Compare doses.</AbstractText>
<AbstractText Label="RESULTS">12/20 dogs.</AbstractText></Abstract>
<AuthorList><Author><LastName>Doe</LastName><Initials>J</Initials></Author>
<Author><CollectiveName>Study Group</CollectiveName></Author></AuthorList>
<PublicationTypeList><PublicationType>Randomized Controlled Trial, Veterinary</PublicationType>
</PublicationTypeList></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/x</ArticleId>
<ArticleId IdType="pmc">PMC9</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "env", lambda name: "")       # ignore any real .env


def http(handler) -> c.Http:
    return c.Http(httpx.Client(transport=httpx.MockTransport(handler)))


def test_pubmed_search_rejects_ignored_terms():
    body = {"esearchresult": {"count": "0", "idlist": [],
                              "warninglist": {"quotedphrasesnotfound": ['"Pain, Postoperative"[Mesh]']}}}
    h = http(lambda req: httpx.Response(200, json=body))
    with pytest.raises(c.QueryProblem, match="Pain, Postoperative"):
        c.pubmed_search(h, '"Pain, Postoperative"[Mesh]')


def test_pubmed_records_parse():
    rec = c.pubmed_records(http(lambda req: httpx.Response(200, content=PUBMED_XML)), ["111"])[0]
    assert (rec.pmid, rec.year, rec.doi, rec.pmcid) == ("111", "2019", "10.1/x", "PMC9")
    assert rec.title == "Two doses of X after surgery"
    assert rec.abstract == "OBJECTIVE: Compare doses. RESULTS: 12/20 dogs."
    assert (rec.first_author, rec.last_author) == ("Doe J", "Study Group")


def test_openalex_abstract_is_rebuilt_in_order():
    assert c._abstract({"dogs": [1], "Twenty": [0], "enrolled.": [2]}) == "Twenty dogs enrolled."


def test_crossref_unknown_doi_is_none():
    assert c.crossref_work(http(lambda req: httpx.Response(404)), "10.9/none") is None


def test_retries_rate_limit_then_succeeds():
    answers = iter([httpx.Response(429, headers={"Retry-After": "1"}),
                    httpx.Response(200, json={"ok": 1})])
    assert http(lambda req: next(answers)).get("https://example.org/x").json() == {"ok": 1}


def test_gives_up_after_repeated_server_errors():
    with pytest.raises(httpx.HTTPStatusError):
        http(lambda req: httpx.Response(503)).get("https://example.org/x")
