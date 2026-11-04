"""Extraction with the fake model (free): every field has one form, code assigns every key,
failing values go back once and are then emptied and logged, the gate stops only on a backed
"no", abstract-only papers are marked, and a document goes in whole or not at all."""
import copy
import json
from collections import Counter

import pytest

from tests import fixture
from vet.codebook import TABLES, Codebook
from vet.extract import (
    STEPS,
    extract_document,
    form,
    form_fields,
    read_log,
    run_extract,
    schema,
    write_abstract,
)
from vet.llm import LLM, FakeGemini
from vet.store import Store
from vet.validate import validate

CB = Codebook()
TOPIC = "surgical_pain"
DOC = "DOI-10-9999-EXTRACT-1"
TEXT = """FICTIONAL: Analgesic X for pain after ovariohysterectomy in dogs, a randomised, \
double-blind trial
J Doe, R Roe. Fictional Veterinary Journal 2020. doi:10.9999/extract.1
Corresponding author: J Doe, Fictional Veterinary Hospital.
Objective: To compare analgesic X with placebo after ovariohysterectomy in dogs.
Sixty client-owned dogs undergoing ovariohysterectomy at Fictional Veterinary Hospital, Bern,
Switzerland, were enrolled between March 2019 and May 2019.
Dogs were randomly assigned to placebo (n = 30) or analgesic X (n = 30), given at 2 mg/kg SC
once at extubation. Anaesthesia: propofol induction, isoflurane maintenance.
3/29 dogs given analgesic X needed rescue within 24 h.
Mixed breed dogs (n = 20) were the most common breed.
Rescue analgesia was defined as a CMPS-SF score of 6 or more.
"""


def q(value, quote, location="Methods"):
    return {"value": value, "quote": quote, "location": location}


def at(value, location="Methods"):
    return {"value": value, "location": location}


# what a careful model answers for TEXT; the keys it adds (ID, arm_id, …) must be ignored
ANSWERS = {
    "study": {
        "gate": {"answer": "yes", "quote": "Sixty client-owned dogs undergoing ovariohysterectomy"},
        "sources": {"source_id": "PMID1", "doc_type": "journal_article",
                    "title": "FICTIONAL: Analgesic X for pain after ovariohysterectomy in dogs",
                    "first_author": "J Doe", "last_author": "R Roe", "year": "2020",
                    "journal_or_agency": "Fictional Veterinary Journal",
                    "doi": "10.9999/extract.1", "language": "en"},
        "studies": {"ID": "VET-77", "study_kind": q("prospective_trial", "double-blind trial"),
                    "randomised": q("yes", "Dogs were randomly assigned"),
                    "blinding": q("double", "double-blind trial"),
                    "n_enrolled": q("60", "Sixty client-owned dogs"),
                    "Sex": q("FEMALE", "dogs undergoing ovariohysterectomy"),
                    "Country": q("Switzerland", "Bern, Switzerland"),
                    "Started date": q("2019-03", "between March 2019 and May 2019"),
                    "species": "dogs", "disease_name": "pain after ovariohysterectomy",
                    "Summary": at("To compare analgesic X with placebo after "
                                  "ovariohysterectomy in dogs.", "Abstract")},
        "sites": [{"site_no": "9", "Facility": "Fictional Veterinary Hospital", "City": "Bern",
                   "Country": q("Switzerland", "Bern, Switzerland")}]},
    "arms": {"arms": [
        {"arm_id": "B7", "arm_label": "placebo", "is_control": q("yes", "placebo (n = 30)"),
         "n_assigned": q("30", "placebo (n = 30)")},
        {"arm_id": "B8", "arm_label": "analgesic X", "is_control": q("no", "analgesic X (n = 30)"),
         "n_assigned": q("30", "analgesic X (n = 30)"),
         "arm_doses": [{"dose_id": "D99", "drug": "analgesic X", "dose_unit": "mg/kg",
                        "dose_value": q("2", "given at 2 mg/kg SC once at extubation"),
                        "route": q("SC", "2 mg/kg SC"), "frequency": "once",
                        "timing": "at extubation",
                        "dose_as_written": at("2 mg/kg SC once at extubation")}]}]},
    "results": {"results": [
        {"result_id": "R42", "arm_id": "VET-1-A2", "result_kind": "outcome",
         "endpoint": "rescue analgesia", "timepoint_value": "24", "timepoint_unit": "hours",
         "timepoint_as_written": "24 h", "n": "29", "events": "3", "statistic_type": "count",
         "value": "3", "as_written": "3/29 dogs given analgesic X needed rescue within 24 h",
         "location": "Results"}]},
    "people": {
        "people": [{"person_no": "5", "Name": "J Doe",
                    "Affiliation": "Fictional Veterinary Hospital",
                    "role": q("author", "J Doe, R Roe."),
                    "author_position": q("first", "J Doe, R Roe."),
                    "is_corresponding": q("yes", "Corresponding author: J Doe"),
                    "site_no": "1"}],
        "breeds": [{"Breed": "Mixed breed", "n_dogs": q("20", "Mixed breed dogs (n = 20)")}],
        "definitions": [{"term": "rescue analgesia",
                         "definition_verbatim": "a CMPS-SF score of 6 or more"}],
        "topic_fields": {"surgery_types": "ovariohysterectomy",
                         "surgery_category": q("soft_tissue", "undergoing ovariohysterectomy"),
                         "anesthesia_protocol": at("propofol induction, isoflurane maintenance")},
        "conflicts": []},
}


RESULTS = ["results"] * 3     # baseline, outcome, adverse events


class Fake:
    """Answers each step from ANSWERS; change(step, answer) edits an answer, repair(prompt,
    schema) answers the repair calls (default: no fixes)."""

    def __init__(self, change=None, repair=None):
        self.change, self.repair, self.seen = change, repair, []

    def __call__(self, step, prompt, schema):
        props = schema["properties"]
        kind = ("study" if "gate" in props else
                next((s for s in ("arms", "results", "people") if s in props), "repair"))
        self.seen.append(kind)
        if kind == "repair":
            return self.repair(prompt, schema) if self.repair else {}
        answer = copy.deepcopy(ANSWERS[kind])
        if kind == "results":          # one call per kind of result: answer that kind only
            kinds = props["results"]["items"]["properties"]["result_kind"]["enum"]
            answer["results"] = [r for r in answer["results"] if r["result_kind"] in kinds]
        if self.change:
            self.change(kind, answer)
        return answer


def extract(tmp_path, fake=None, store=None, **kw):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / f"{DOC}.txt").write_text(TEXT, "utf-8")
    fake = fake or Fake()
    llm = LLM(FakeGemini(fake), save_calls=False, corpus=tmp_path / "corpus")
    store = store or Store(tmp_path / "v.db", CB)
    rep = extract_document(DOC, TOPIC, CB.topics[TOPIC], llm, store, tmp_path, **kw)
    return rep, store, fake


# -- forms ------------------------------------------------------------------------------------

@pytest.mark.parametrize("topic", sorted(CB.topics))
def test_every_field_is_on_exactly_one_form(topic):
    homes = Counter(t for tables in STEPS.values() for t in tables)
    assert set(homes) | {"evidence"} == set(TABLES) and set(homes.values()) == {1}
    listed = Counter((t, f.name) for step in STEPS for t, f, _ in form_fields(step, CB, topic))
    want = {(t, f.name) for t in TABLES if t != "evidence" for f in CB.tables[t]}
    want = want - {("topic_fields", "value")} | {("topic_fields", n) for n in CB.topic_fields[topic]}
    assert set(listed) == want and set(listed.values()) == {1}


def test_forms_give_each_field_its_rules():
    for step in STEPS:
        text = form(step, CB, TOPIC, CB.topics[TOPIC])
        for table, f, by_code in form_fields(step, CB, TOPIC):
            if not by_code:
                assert f"- {f.name} (" in text and f"evidence: {f.evidence}" in text
    study = form("study", CB, TOPIC, CB.topics[TOPIC])
    assert "pain after surgery" in study and "### gate" in study
    assert "only when study_kind = prospective_trial or other" in study
    assert "Code fills: ID." in study and "Switzerland" in study      # the country names


def test_the_answer_schema_follows_the_form():
    s = schema("study", CB, TOPIC)["properties"]
    assert next(iter(s)) == "gate"
    studies = s["studies"]["properties"]
    assert set(studies) == {f.name for t, f, code in form_fields("study", CB, TOPIC)
                            if t == "studies" and not code}
    assert studies["n_enrolled"]["required"] == ["value", "quote"]
    assert studies["Summary"]["required"] == ["value", "location"]
    assert studies["disease_name"] == {"type": "string"}
    assert studies["blinding"]["properties"]["value"]["enum"] == ["", "none", "single", "double",
                                                                 "blinded_unspecified"]
    arms = schema("arms", CB, TOPIC)["properties"]["arms"]["items"]["properties"]
    assert "arm_doses" in arms and "arm_id" not in arms
    results = schema("results", CB, TOPIC, {"arms": ["VET-1-A1"]})["properties"]["results"]
    assert results["items"]["properties"]["arm_id"]["enum"] == ["", "VET-1-A1"]
    assert "result_id" not in results["items"]["properties"]


def test_a_topic_without_its_screen_section_is_refused(tmp_path):
    llm = LLM(FakeGemini(), save_calls=False, corpus=tmp_path)
    with pytest.raises(ValueError, match=r"\[screen\]"):
        extract_document(DOC, TOPIC, {"question": "q"}, llm, Store(tmp_path / "v.db", CB))


# -- one document -----------------------------------------------------------------------------

def test_a_clean_document_goes_in_whole(tmp_path):
    rep, store, fake = extract(tmp_path)
    assert fake.seen == ["study", "arms", *RESULTS, "people"]          # nothing to repair
    assert (rep.stopped, rep.study, rep.stored_as, rep.emptied) == ("", "VET-1", DOC, 0)
    assert rep.values > 50 and rep.calls == 6
    assert validate(store.tables(), CB, root=tmp_path) == [] and read_log(store) == []
    study = store.rows("studies")[0]
    assert (study["n_enrolled"], study["Sex"], study["Country"]) == ("60", "FEMALE", "Switzerland")
    assert store.rows("study_topics") == [{"ID": "VET-1", "topic": TOPIC}]
    cited = {(r["table"], r["field"]) for r in store.rows("evidence")}
    assert {("studies", "n_enrolled"), ("arm_doses", "route"),
            ("topic_fields", "surgery_category")} <= cited


def test_keys_come_from_code_whatever_the_model_says(tmp_path):
    store = Store(tmp_path / "v.db", CB)
    store.import_tables(fixture.clean(CB))                     # VET-1 is taken
    rep, store, _ = extract(tmp_path, store=store)
    assert rep.study == "VET-2"

    def mine(table):
        return [r for r in store.rows(table) if r.get("ID") == "VET-2"]
    assert [r["arm_id"] for r in mine("arms")] == ["VET-2-A1", "VET-2-A2"]
    assert [r["dose_id"] for r in mine("arm_doses")] == ["VET-2-A2-D1"]
    assert [(r["result_id"], r["arm_id"]) for r in mine("results")] == [("VET-2-R1", "VET-2-A2")]
    assert [r["site_no"] for r in mine("sites")] == ["1"]
    assert [(r["person_no"], r["site_no"]) for r in mine("people")] == [("1", "1")]
    assert DOC in {r["source_id"] for r in store.rows("sources")}
    stored = json.dumps([mine(t) for t in TABLES if "ID" in CB.columns(t)])
    assert not [k for k in ("VET-77", "B7", "B8", "D99", "R42", "PMID1") if k in stored]


def test_a_wrong_quote_goes_back_once_then_is_emptied_and_logged(tmp_path):
    def change(step, answer):
        if step == "study":
            answer["studies"]["n_enrolled"]["quote"] = "Sixty dogs from Antarctica"

    def repair(prompt, schema):
        assert list(schema["properties"]) == ["studies.n_enrolled"]
        assert "quote_not_in_document" in prompt and "Sixty dogs from Antarctica" in prompt
        return {"studies.n_enrolled": q("60", "Sixty dogs from the Moon")}

    rep, store, fake = extract(tmp_path, Fake(change, repair))
    assert fake.seen == ["study", "repair", "arms", *RESULTS, "people"]
    study = store.rows("studies")[0]
    assert study["n_enrolled"] == "" and study["Sex"] == "FEMALE"      # the rest goes in
    assert not [r for r in store.rows("evidence") if r["field"] == "n_enrolled"]
    log = read_log(store)
    assert [(r["source_id"], r["ID"], r["step"], r["table"], r["field"], r["value"])
            for r in log] == [(DOC, "VET-1", "study", "studies", "n_enrolled", "60")]
    assert "quote_not_in_document" in log[0]["problem"]
    assert (rep.emptied, rep.repaired) == (1, 0)
    assert validate(store.tables(), CB, root=tmp_path) == []


def test_a_value_fixed_in_the_repair_round_is_kept(tmp_path):
    def change(step, answer):
        if step == "study":
            answer["studies"]["blinding"] = q("triple", "double-blind trial")

    rep, store, fake = extract(tmp_path, Fake(change, lambda prompt, schema: {
        "studies.blinding": q("double", "double-blind trial")}))
    assert fake.seen.count("repair") == 1
    assert store.rows("studies")[0]["blinding"] == "double"
    assert (rep.repaired, rep.emptied) == (1, 0) and read_log(store) == []


@pytest.mark.parametrize("field, value", [("arm_id", "VET-1-A9"), ("as_written", "")],
                         ids=["unknown_arm", "no_sentence"])
def test_a_result_that_still_fails_is_dropped_not_moved_or_kept_unbacked(tmp_path, field, value):
    def change(step, answer):
        if step == "results" and answer["results"]:
            answer["results"][0][field] = value

    _, store, fake = extract(tmp_path, Fake(change))
    assert fake.seen == ["study", "arms", *RESULTS, "repair", "people"]
    assert store.rows("results") == [] and store.rows("arms")      # the rest goes in
    log = read_log(store)
    assert [(r["step"], r["row"], r["field"]) for r in log] == [("results", "R1", field)]
    assert "row dropped" in log[0]["problem"]


def test_a_doi_not_in_the_document_is_not_stored(tmp_path):
    def change(step, answer):
        if step == "study":
            answer["sources"]["doi"] = "10.9999/somewhere.else"

    rep, store, _ = extract(tmp_path, Fake(change))
    assert store.rows("sources")[0]["doi"] == "" and rep.stored_as == DOC
    assert "doi_not_in_document" in read_log(store)[0]["problem"]


def test_the_gate_stops_on_a_no_backed_by_its_quote(tmp_path):
    def change(step, answer):
        answer["gate"] = {"answer": "no", "quote": "Rescue analgesia was defined"}

    rep, store, fake = extract(tmp_path, Fake(change))
    assert fake.seen == ["study"] and rep.gate == "no" and rep.stopped.startswith("gate")
    assert all(not rows for rows in store.tables().values())
    assert [(r["step"], r["field"], r["value"]) for r in read_log(store)] == [("study", "gate", "no")]


def test_a_no_without_its_quote_does_not_stop(tmp_path):
    def change(step, answer):
        if step == "study":
            answer["gate"] = {"answer": "no", "quote": "twelve cats were studied"}

    rep, store, fake = extract(tmp_path, Fake(change))
    assert fake.seen == ["study", "arms", *RESULTS, "people"]
    assert rep.gate == "unbacked_no" and rep.study == "VET-1"
    assert "not in the document" in read_log(store)[0]["problem"]


def test_the_import_is_all_or_nothing(tmp_path):
    store = Store(tmp_path / "v.db", CB)
    store.db.execute("CREATE TRIGGER fail BEFORE INSERT ON people "
                     "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    store.db.commit()
    rep, store, fake = extract(tmp_path, store=store)
    assert len(fake.seen) == 6 and rep.study == ""
    assert rep.stopped.startswith("nothing imported")
    assert all(not rows for rows in store.tables().values())         # tables before people too
    assert store.db.execute("SELECT * FROM meta WHERE name = 'last_study'").fetchone() is None
    assert "disk full" in read_log(store)[-1]["problem"]


def test_a_document_already_stored_costs_nothing(tmp_path):
    extract(tmp_path)
    rep, _, fake = extract(tmp_path, store=Store(tmp_path / "v.db", CB))
    assert fake.seen == [] and rep.stopped == f"already in the database as {DOC}"


# -- abstract only ----------------------------------------------------------------------------

ABSTRACT = {"title": "FICTIONAL: Analgesic X after ovariohysterectomy in dogs",
            "abstract": "Sixty dogs were enrolled.", "pmid": "99999955", "doi": ""}


def test_a_paper_without_full_text_is_extracted_from_its_abstract(tmp_path):
    fake = FakeGemini()                      # the smallest valid answer: gate yes, all empty
    llm = LLM(fake, save_calls=False, corpus=tmp_path / "corpus")
    reps = run_extract(["PMID99999955"], TOPIC, llm, tmp_path / "v.db",
                       abstracts={"PMID99999955": ABSTRACT}, say=lambda line: None)
    assert (tmp_path / "docs" / "PMID99999955.txt").read_text("utf-8") == (
        "FICTIONAL: Analgesic X after ovariohysterectomy in dogs\n\nSixty dogs were enrolled.\n")
    assert "Sixty dogs were enrolled." in fake.calls[0]["prompt"]
    assert reps[0].study == "VET-1"
    src = Store(tmp_path / "v.db", CB).rows("sources")[0]
    assert (src["source_id"], src["pmid"], src["access"], src["text_file"]) == (
        "PMID99999955", "99999955", "abstract_only", "docs/PMID99999955.txt")


def test_an_abstract_never_replaces_another_text(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PMID99999955.txt").write_text("the full text", "utf-8")
    with pytest.raises(ValueError, match="not overwritten"):
        write_abstract(ABSTRACT, tmp_path)
    with pytest.raises(ValueError, match="no abstract"):
        write_abstract({**ABSTRACT, "abstract": ""}, tmp_path)


# -- results: one call per kind, then the tables no row cites ---------------------------------

BASELINE = {"result_kind": "baseline", "arm_id": "VET-1-A1", "endpoint": "dogs enrolled",
            "n": "30", "as_written": "placebo (n = 30)", "location": "Methods"}
TABLE_ROW = {"result_kind": "adverse_event", "arm_id": "VET-1-A1", "endpoint": "vomiting",
             "n": "30", "events": "4", "as_written": "Vomiting: placebo 4/30",
             "location": "Table 1"}


class ByKind(Fake):
    """Answers each results call with the rows of the kind it asks for, plus a stray outcome
    row in the other calls; the tables call gets TABLE_ROW."""

    def __call__(self, step, prompt, schema):
        answer = super().__call__(step, prompt, schema)
        if self.seen[-1] != "results":
            return answer
        if "No result row cites" in prompt:
            self.seen[-1] = "tables"
            return {"results": [TABLE_ROW]}
        kinds = schema["properties"]["results"]["items"]["properties"]["result_kind"]["enum"]
        rows = [BASELINE] if kinds == ["baseline"] else answer["results"]
        stray = [] if kinds == ["outcome"] else [ANSWERS["results"]["results"][0]]
        return {"results": rows + stray}


def test_each_kind_of_result_is_asked_for_alone_and_numbered_on(tmp_path):
    _, store, fake = extract(tmp_path, ByKind())
    assert fake.seen == ["study", "arms", *RESULTS, "people"]
    got = [(r["result_id"], r["result_kind"], r["endpoint"]) for r in store.rows("results")]
    assert got == [("VET-1-R1", "baseline", "dogs enrolled"),            # the stray outcome
                   ("VET-1-R2", "outcome", "rescue analgesia")]          # rows are not taken


def test_a_table_no_result_cites_is_asked_for(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / f"{DOC}.txt").write_text(
        TEXT + "Table 1. Vomiting: placebo 4/30, analgesic X 2/30.\n", "utf-8")
    fake = ByKind()
    llm = LLM(FakeGemini(fake), save_calls=False, corpus=tmp_path / "corpus")
    store = Store(tmp_path / "v.db", CB)
    extract_document(DOC, TOPIC, CB.topics[TOPIC], llm, store, tmp_path)
    assert fake.seen == ["study", "arms", *RESULTS, "tables", "people"]
    assert [r["endpoint"] for r in store.rows("results")][-1] == "vomiting"
