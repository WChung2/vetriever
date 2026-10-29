"""The database: an import stores every value exactly once, finds a document again by any of
its identifiers, numbers new studies VET-<n>, and turns an older layout into today's."""
import copy
import csv

import pytest

from tests import fixture
from vet.codebook import TABLES, Codebook
from vet.store import Store, document_key
from vet.validate import SOURCE_ID, load_folder, validate

CB = Codebook()


def write_any(tables: dict, folder):
    """Write tables with whatever columns their rows have (for older layouts)."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with open(folder / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["ID"],
                               lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    return folder


def replace_everywhere(t: dict, old: str, new: str) -> dict:
    return {name: [{c: v.replace(old, new) for c, v in r.items()} for r in rows]
            for name, rows in t.items()}


def second_study(t: dict) -> dict:
    """The fixture again, as another paper and FOI summary (same trial design)."""
    for old, new in ((fixture.DOC, "DOI-10-9999-FIXTURE-2"), ("10.9999/fixture.1", "10.9999/fixture.2"),
                     ("99999901", "99999902"), (fixture.FDA, "FDA-NADA999998"),
                     ("FIXTURE: fictional", "FIXTURE 2: fictional")):
        t = replace_everywhere(t, old, new)
    return t


def old_layout(t: dict) -> dict:
    """The fixture as an older delivery: PMID-first document key, VET-<source_id>-<n> study
    ID, studies.indication and an ext_pain table instead of study_topics and topic_fields."""
    t = replace_everywhere(replace_everywhere(t, fixture.SID, "VET-PMID99999901-1"),
                           fixture.DOC, "PMID99999901")
    t["studies"] = [{"ID": r["ID"], "indication": "surgical_pain",
                     **{c: v for c, v in r.items() if c != "ID"}} for r in t["studies"]]
    t["ext_pain"] = [{"ID": t["studies"][0]["ID"],
                      **{r["field"]: r["value"] for r in t.pop("topic_fields")}}]
    t.pop("study_topics")
    for r in t["evidence"]:
        if r["table"] == "topic_fields":
            r["table"], r["row_key"] = "ext_pain", ""
    t["evidence"].append({**t["evidence"][0], "field": "indication", "value": "surgical_pain",
                          "quote": "dogs undergoing ovariohysterectomy", "location": "Methods"})
    return t


def as_set(rows):
    return {tuple(sorted(r.items())) for r in rows}


def test_round_trip_keeps_every_value(tmp_path):
    t = fixture.clean(CB)
    db = Store(tmp_path / "test.db", CB)
    rep = db.import_folder(write_any(t, tmp_path / "in"))
    assert not rep.missing and not rep.dropped
    assert rep.values_in == rep.values_stored > 0
    assert db.tables() == t            # the fixture already uses VET-1 and DOI-first keys
    assert validate(db.tables(), CB) == []
    db.export(tmp_path / "out")
    exported, headers = load_folder(tmp_path / "out", tmp_path / "out")
    assert exported == t and all(headers[n] == CB.columns(n) for n in TABLES)


def test_importing_the_same_folder_twice_adds_nothing(tmp_path):
    db = Store(tmp_path / "test.db", CB)
    folder = write_any(fixture.clean(CB), tmp_path / "in")
    db.import_folder(folder)
    before = db.tables()
    rep = db.import_folder(folder)
    assert len(rep.known_documents) == 2 and not rep.new_documents and not rep.new_studies
    assert sum(rep.rows_added.values()) == 0 and rep.values_skipped == rep.values_in
    assert db.tables() == before


def test_the_same_paper_under_another_key_is_recognised(tmp_path):
    db = Store(tmp_path / "test.db", CB)
    db.import_folder(write_any(fixture.clean(CB), tmp_path / "a"))
    t = replace_everywhere(fixture.clean(CB), fixture.DOC, "PMID99999901")
    t["sources"][0]["doi"] = "10.9999/FIXTURE.1"              # DOIs ignore case
    rep = db.import_folder(write_any(t, tmp_path / "b"))
    assert ("PMID99999901", fixture.DOC) in rep.known_documents
    assert not rep.new_studies and len(db.rows("sources")) == 2


def test_new_studies_get_the_next_number(tmp_path):
    db = Store(tmp_path / "test.db", CB)
    db.import_folder(write_any(fixture.clean(CB), tmp_path / "a"))
    rep = db.import_folder(write_any(second_study(fixture.clean(CB)), tmp_path / "b"))
    assert rep.new_studies == {"VET-1": "VET-2"} and not rep.missing
    assert {r["arm_id"] for r in db.rows("arms")} == {"VET-1-A1", "VET-1-A2", "VET-2-A1", "VET-2-A2"}
    assert "VET-2-A2-D1" in {r["dose_id"] for r in db.rows("arm_doses")}
    assert {r["row_key"] for r in db.rows("evidence") if r["table"] == "arms"} >= {"VET-2-A2"}
    assert validate(db.tables(), CB) == []


def test_an_older_layout_becomes_todays(tmp_path):
    db = Store(tmp_path / "test.db", CB)
    rep = db.import_folder(write_any(old_layout(fixture.clean(CB)), tmp_path / "old"))
    assert not rep.missing and rep.new_documents[0] == fixture.DOC
    # the topic now comes from the search, so evidence for the old indication column goes
    assert rep.dropped == {("evidence", "rows for studies.indication"): 1}
    assert rep.values_in == rep.values_stored + rep.values_dropped
    want = fixture.clean(CB)
    # file paths are kept as given: they point at files on disk
    want["sources"][0].update(file_name="docs/PMID99999901.xml", text_file="docs/PMID99999901.txt")
    for name in TABLES:
        assert as_set(db.rows(name)) == as_set(want[name]), name
    assert validate(db.tables(), CB) == []


def test_a_study_without_topic_takes_its_documents_topic(tmp_path):
    t = old_layout(fixture.clean(CB))
    t["studies"].append({**{c: "" for c in t["studies"][0]}, "ID": "VET-PMID99999901-2",
                         "study_kind": "other"})
    t["study_sources"].append({"ID": "VET-PMID99999901-2", "source_id": "PMID99999901",
                               "role": "primary"})
    db = Store(tmp_path / "test.db", CB)
    rep = db.import_folder(write_any(t, tmp_path / "old"))
    assert rep.topic_from_document == [("VET-PMID99999901-2", "surgical_pain")]
    assert {"ID": "VET-2", "topic": "surgical_pain"} in db.rows("study_topics")


def test_columns_the_codebook_lacks_are_reported(tmp_path):
    t = fixture.clean(CB)
    for r in t["arms"]:
        r["colour"] = "blue"
    db = Store(tmp_path / "test.db", CB)
    rep = db.import_folder(write_any(t, tmp_path / "in"))
    assert rep.dropped == {("arms", "colour"): 2} and not rep.missing
    assert rep.values_in == rep.values_stored + rep.values_dropped


@pytest.mark.parametrize("break_it", [
    lambda t: t["arms"].append(dict(t["arms"][0])),                         # same key twice
    lambda t: t["arms"].append({**t["arms"][0], "ID": "VET-9", "arm_id": "VET-9-A1"}),  # no study
], ids=["duplicate_key", "unknown_study"])
def test_a_broken_folder_is_refused_whole(tmp_path, break_it):
    t = fixture.clean(CB)
    break_it(t)
    db = Store(tmp_path / "test.db", CB)
    with pytest.raises(ValueError, match="nothing imported"):
        db.import_folder(write_any(t, tmp_path / "in"))
    assert all(not rows for rows in db.tables().values())


def test_document_keys_put_the_doi_first():
    paper = {"source_id": "PMID1", "doc_type": "journal_article", "doi": "10.1111/vaa.12345",
             "pmid": "1"}
    assert document_key(paper) == "DOI-10-1111-VAA-12345"
    assert document_key({**paper, "doi": ""}) == "PMID1"
    odd = document_key({**paper, "doi": "10.1002/(SICI)1099-1573(199911)13:7<580::AID-PTR509>3.0.CO;2-B"})
    assert SOURCE_ID.fullmatch(odd), odd
    abstract = {"source_id": "ABS-ACVIM2023-GI12", "doc_type": "conference_abstract",
                "doi": "10.1111/jvim.99999"}           # one DOI for a whole supplement
    assert document_key(abstract) == "ABS-ACVIM2023-GI12"
    assert document_key({"source_id": "FDA-NADA141337", "doc_type": "fda_foi_summary"}) == "FDA-NADA141337"


def test_conference_abstracts_sharing_a_doi_stay_apart(tmp_path):
    t = fixture.clean(CB)
    two = copy.deepcopy(t)
    for tables, key in ((t, "ABS-FORUM2020-1"), (two, "ABS-FORUM2020-2")):
        tables.update(replace_everywhere(tables, fixture.DOC, key))
        tables["sources"][0].update(doc_type="conference_abstract", pmid="")
    two = replace_everywhere(two, fixture.FDA, "FDA-NADA999998")
    db = Store(tmp_path / "test.db", CB)
    db.import_folder(write_any(t, tmp_path / "a"))
    rep = db.import_folder(write_any(two, tmp_path / "b"))
    assert rep.new_documents == ["ABS-FORUM2020-2", "FDA-NADA999998"]
