"""Authors and institutions against a fake OpenAlex (offline, free)."""
import sqlite3

import httpx
import pytest

from vet import connectors as c
from vet import who as w

OA = "https://openalex.org/"


def inst(i: str, name: str, lineage: list[str], country: str = "CH") -> dict:
    return {"id": OA + i, "display_name": name, "country_code": country, "type": "education",
            "lineage": [OA + x for x in lineage]}


# OpenAlex does not say which end of a lineage is the top, so both orders are here
UNI = inst("I1", "University One", ["I1"])
DEPT = inst("I10", "Department of Clinical Veterinary Medicine", ["I10", "I1"])
SCHOOL = inst("I11", "Vetsuisse Faculty", ["I1", "I11"])
LAB = inst("I12", "Clinical Pathology Lab", ["I12", "I10", "I1"], "FR")   # a campus abroad
ANAESTHESIA = inst("I13", "Anaesthesia Lab", ["I1", "I10", "I13"])
OTHER = inst("I2", "Other University", ["I2"], "US")
HOSPITAL = inst("I3", "Animal Hospital", ["I3"], "US")
INSTITUTIONS = {i["id"]: i for i in (UNI, DEPT, SCHOOL, LAB, ANAESTHESIA, OTHER, HOSPITAL)}


def authorship(aid: str, name: str, insts: list[dict], position: str = "middle",
               orcid: str | None = None) -> dict:
    return {"author_position": position, "institutions": insts,
            "author": {"id": OA + aid, "display_name": name, "orcid": orcid}}


W1 = {"id": OA + "W1", "doi": "https://doi.org/10.1/a",
      "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/111"}, "authorships": [
          authorship("A1", "Ann Vet", [DEPT], "first", "https://orcid.org/0000-0001-2345-6789"),
          authorship("A2", "Ben Vet", [SCHOOL]),
          authorship("A3", "Cay Vet", [OTHER, HOSPITAL]),
          authorship("A5", "Eve Vet", [OTHER], "last")]}
W2 = {"id": OA + "W2", "doi": "https://doi.org/10.2/b", "ids": {}, "authorships": [
    authorship("A1", "Ann Vet", [LAB], "first"),
    authorship("A6", "Fay Vet", [ANAESTHESIA]),
    authorship("A4", "Dee Vet", [], "last")]}


class OpenAlex:
    """Fake works and institutions endpoints. Like the real one it gives 25 works a page
    unless per-page says otherwise, and refuses a filter of more than 50 values."""

    def __init__(self, works=(W1, W2)):
        self.works = list(works)
        self.asked: list[httpx.URL] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.asked.append(req.url)
        if req.url.path == "/works":
            kind, _, values = req.url.params["filter"].partition(":")
            values = [v.lower() for v in values.split("|")]
            if len(values) > 50:
                return httpx.Response(400)
            hits = [x for x in self.works if (
                (x.get("ids") or {}).get("pmid", "").rsplit("/", 1)[-1] if kind == "pmid"
                else (x.get("doi") or "").removeprefix("https://doi.org/").lower()) in values]
            return httpx.Response(200, json={"meta": {"count": len(hits)}, "results":
                                             hits[:int(req.url.params.get("per-page", 25))]})
        if req.url.path.startswith("/institutions/"):
            found = INSTITUTIONS.get(OA + req.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        return httpx.Response(404)

    def looked_up(self) -> list[str]:
        return [u.path.rsplit("/", 1)[-1] for u in self.asked
                if u.path.startswith("/institutions/")]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "env", lambda name: "")
    keys = {}
    monkeypatch.setattr(w, "env", lambda name: keys.get(name, ""))
    return keys


def http(service: OpenAlex) -> c.Http:
    return c.Http(httpx.Client(transport=httpx.MockTransport(service)))


def rows(db, *cols) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT {', '.join(cols)} FROM authors "
                           "ORDER BY paper, author_no, institution_id").fetchall()
    finally:
        con.close()


def test_authors_are_stored_with_institutions_rolled_up_to_the_university(tmp_path):
    db = tmp_path / "vetriever.db"
    assert w.who(["111", "10.2/B"], db, http(OpenAlex())) == []
    assert rows(db, "paper", "author_no", "author_name", "institution_id", "root_id",
                "root_name") == [
        ("10.2/b", 1, "Ann Vet", "I12", "I1", "University One"),   # lab, department, university
        ("10.2/b", 2, "Fay Vet", "I13", "I1", "University One"),   # the same, top first
        ("10.2/b", 3, "Dee Vet", "", "", ""),                      # no institution: one row
        ("111", 1, "Ann Vet", "I10", "I1", "University One"),
        ("111", 2, "Ben Vet", "I11", "I1", "University One"),
        ("111", 3, "Cay Vet", "I2", "I2", "Other University"),     # two institutions, two rows
        ("111", 3, "Cay Vet", "I3", "I3", "Animal Hospital"),
        ("111", 4, "Eve Vet", "I2", "I2", "Other University")]
    assert rows(db, "author_position", "orcid", "author_id", "institution_name", "country_code",
                "work_id")[3] == ("first", "0000-0001-2345-6789", "A1",
                                  "Department of Clinical Veterinary Medicine", "CH", "W1")


def test_each_parent_is_looked_up_once_and_a_top_institution_not_at_all(tmp_path):
    db = tmp_path / "vetriever.db"
    s = OpenAlex()
    w.who(["111", "10.2/b"], db, http(s))
    assert sorted(s.looked_up()) == ["I1", "I10"]
    again = OpenAlex()
    w.who(["111"], db, http(again))
    assert again.looked_up() == []                     # the university is known from the table


def test_batches_of_50_and_more_works_than_a_page(tmp_path):
    db = tmp_path / "vetriever.db"
    s = OpenAlex([{"id": OA + f"W{n}", "ids": {"pmid": f"https://pubmed.ncbi.nlm.nih.gov/{n}"},
                   "authorships": [authorship("A1", "Ann Vet", [OTHER])]}
                  for n in range(1000, 1060)])
    assert w.who([str(n) for n in range(1000, 1061)], db, http(s)) == ["1060"]   # not in OpenAlex
    assert len(rows(db, "paper")) == 60
    assert [len(u.params["filter"].split("|")) for u in s.asked if u.path == "/works"] == [50, 11]


def test_key_and_contact_go_with_every_request(tmp_path, offline):
    offline.update({"OPENALEX_API_KEY": "key-for-test", "CONTACT_EMAIL": "vet@example.org"})
    s = OpenAlex()
    w.who(["111"], tmp_path / "vetriever.db", http(s))
    assert len(s.asked) == 2 and all(u.params.get("api_key") == "key-for-test" and
                                     u.params.get("mailto") == "vet@example.org" for u in s.asked)


def test_a_rerun_replaces_the_papers_rows(tmp_path):
    db = tmp_path / "vetriever.db"
    w.who(["111"], db, http(OpenAlex()))
    w.who(["111"], db, http(OpenAlex([W1 | {"authorships": W1["authorships"][:1]}])))
    assert rows(db, "paper", "author_name") == [("111", "Ann Vet")]


def test_institutions_ranked_by_papers_then_authors(tmp_path):
    db = tmp_path / "vetriever.db"
    w.who(["111", "10.1/A", "10.2/b"], db, http(OpenAlex()))      # 111 and 10.1/a: one paper
    keys = ("root_id", "root_name", "countries", "papers", "authors")
    assert [{k: g[k] for k in keys} for g in w.institutions(db)] == [
        {"root_id": "I1", "root_name": "University One", "countries": "CH|FR", "papers": 2,
         "authors": 3},
        {"root_id": "I2", "root_name": "Other University", "countries": "US", "papers": 1,
         "authors": 2},
        {"root_id": "I3", "root_name": "Animal Hospital", "countries": "US", "papers": 1,
         "authors": 1}]
    assert w.institutions(db)[0]["names"][0] == "Ann Vet"        # most papers first
    only = w.institutions(db, ["10.2/B"])
    assert [(g["root_id"], g["papers"], g["authors"]) for g in only] == [("I1", 1, 2)]


def test_lead_only_counts_first_and_last_authors(tmp_path):
    """A paper's first and last authors usually lead it; middle authors often help."""
    db = tmp_path / "vetriever.db"
    w.who(["111", "10.1/A", "10.2/b"], db, http(OpenAlex()))
    con = w._connect(db)
    lead = {(r[0], r[1]) for r in con.execute(
        "SELECT root_id, author_name FROM authors WHERE author_position IN ('first', 'last') "
        "AND root_id != ''")}                                  # no institution: nothing to rank
    every = w.rank(con)
    led = w.rank(con, lead_only=True)
    assert sum(g["authors"] for g in led) <= sum(g["authors"] for g in every)
    assert {(g["root_id"], n) for g in led for n in g["names"]} == lead


def test_not_a_pmid_or_doi_stops_before_any_request(tmp_path):
    s = OpenAlex()
    with pytest.raises(ValueError, match="not a PMID or DOI"):
        w.who(["111", "imha"], tmp_path / "vetriever.db", http(s))
    assert s.asked == []
