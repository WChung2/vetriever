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
    REPAIR_BATCH,
    STEPS,
    TABLE_MORE,
    TABLE_PART,
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


def old_flow(llm):
    """The flow before each-table-once, kept as a setting (results_by_table = false): a call per
    kind of result, then one for tables no result cites. The tests up to "results by table" use
    it, so both flows stay tested."""
    llm.cfg = {**llm.cfg, "pipeline": {**llm.cfg["pipeline"], "results_by_table": False}}
    return llm


def extract(tmp_path, fake=None, store=None, **kw):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / f"{DOC}.txt").write_text(TEXT, "utf-8")
    fake = fake or Fake()
    llm = old_flow(LLM(FakeGemini(fake), save_calls=False, corpus=tmp_path / "corpus"))
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
    sent = fake.held + [c["prompt"] for c in fake.calls]      # held once, or in each call
    assert any("Sixty dogs were enrolled." in s for s in sent)
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
    llm = old_flow(LLM(FakeGemini(fake), save_calls=False, corpus=tmp_path / "corpus"))
    store = Store(tmp_path / "v.db", CB)
    extract_document(DOC, TOPIC, CB.topics[TOPIC], llm, store, tmp_path)
    assert fake.seen == ["study", "arms", *RESULTS, "tables", "people"]
    assert [r["endpoint"] for r in store.rows("results")][-1] == "vomiting"


# -- results by table -------------------------------------------------------------------------

def result(row, column, value, kind="outcome", columns=None):
    return {"result_kind": kind, "arm_id": "VET-1-A2", "endpoint": row, "value": value,
            "statistic_type": "count", "location": "Table 1", "as_written": f"{row} {value}",
            "table_row": row, "table_columns": columns or [column]}


class ByTable(Fake):
    """Answers the plan and each table call. tables: the plan; cells(prompt): one call's answer."""

    def __init__(self, tables, cells):
        super().__init__()
        self.tables, self.cells, self.asked, self.prose = tables, cells, [], 0

    def __call__(self, step, prompt, schema):
        props = schema["properties"]
        if "tables" in props:
            self.seen.append("plan")
            return {"tables": self.tables}
        if "blank_cells" in props:
            self.seen.append("table")
            self.asked.append(prompt.rsplit("## This call", 1)[1])
            return self.cells(prompt)
        if "results" in props and "This call: results in the running text" in prompt:
            self.seen.append("prose")
            self.prose += 1
            return copy.deepcopy(ANSWERS["results"])
        return super().__call__(step, prompt, schema)


PLAN = [{"number": "1", "reports_results": True, "rows": ["Pain", "Rescue"],
         "columns": ["Day 1", "Day 2"]}]
TEXT_TABLE = TEXT + "Table 1.\nPain 7\nPain 8\nRescue 3\nRescue 4\n"


def by_table(tmp_path, fake):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / f"{DOC}.txt").write_text(TEXT_TABLE, "utf-8")
    llm = LLM(FakeGemini(fake), save_calls=False, corpus=tmp_path / "corpus")
    llm.cfg = {**llm.cfg, "pipeline": {**llm.cfg["pipeline"], "results_by_table": True}}
    store = Store(tmp_path / "v.db", CB)
    return extract_document(DOC, TOPIC, CB.topics[TOPIC], llm, store, tmp_path), store, fake


def whole(prompt):
    return {"results": [result(r, c, v) for r, c, v in (
        ("Pain", "Day 1", "7"), ("Pain", "Day 2", "8"), ("Rescue", "Day 1", "3"),
        ("Rescue", "Day 2", "4"))], "blank_cells": []}


def test_each_table_once_is_the_shipped_default(tmp_path):
    llm = LLM(FakeGemini(), save_calls=False, corpus=tmp_path / "corpus")
    assert llm.cfg["pipeline"]["results_by_table"] is True


def test_the_old_flow_is_still_one_call_per_kind(tmp_path):
    _, _, fake = extract(tmp_path)
    assert fake.seen == ["study", "arms", *RESULTS, "people"]


def test_each_table_is_read_once_then_the_running_text(tmp_path):
    rep, store, fake = by_table(tmp_path, ByTable(PLAN, whole))
    assert fake.seen == ["study", "arms", "plan", "table", "prose", "people"]
    assert rep.partly == 0
    assert len({r["result_id"] for r in store.rows("results")}) == 5


def test_a_table_with_no_results_is_not_read(tmp_path):
    plan = [{"number": "1", "reports_results": False, "rows": ["Drug A"], "columns": ["Dose"]}]
    _, _, fake = by_table(tmp_path, ByTable(plan, whole))
    assert "table" not in fake.seen


def test_a_big_table_goes_in_parts_each_row_once(tmp_path):
    rows = [f"Row {i}" for i in range(100)]
    plan = [{"number": "1", "reports_results": True, "rows": rows, "columns": ["A", "B", "C"]}]

    def cells(prompt):
        mine = [r for r in rows if f"- {r}\n" in prompt]
        return {"results": [], "blank_cells": [f"{r} | {c}" for r in mine for c in "ABC"]}

    _, _, fake = by_table(tmp_path, ByTable(plan, cells))
    assert fake.seen.count("table") == 5              # 20 rows of 3 columns fit one call
    listed = [r for ask in fake.asked for r in rows if f"- {r}\n" in ask]
    assert sorted(listed) == sorted(rows)


def test_cells_the_count_finds_missing_are_asked_for_once_by_name(tmp_path):
    def cells(prompt):
        if "still missing" in prompt:
            return {"results": [result("Rescue", "Day 2", "4")], "blank_cells": []}
        return {"results": whole(prompt)["results"][:3], "blank_cells": []}

    rep, _, fake = by_table(tmp_path, ByTable(PLAN, cells))
    assert fake.seen.count("table") == 2
    assert "- Rescue | Day 2" in fake.asked[1] and "- Pain | Day 1" not in fake.asked[1]
    assert rep.partly == 0


def test_a_complete_table_gets_no_second_call(tmp_path):
    _, _, fake = by_table(tmp_path, ByTable(PLAN, whole))
    assert fake.seen.count("table") == 1


def test_a_listed_blank_counts_as_covered(tmp_path):
    def cells(prompt):
        return {"results": whole(prompt)["results"][:3], "blank_cells": ["Rescue | Day 2"]}

    rep, _, fake = by_table(tmp_path, ByTable(PLAN, cells))
    assert fake.seen.count("table") == 1 and rep.partly == 0


def test_one_result_can_cover_a_value_and_its_p_value_column(tmp_path):
    plan = [{"number": "1", "reports_results": True, "rows": ["Pain"],
             "columns": ["Day 1", "P value"]}]

    def cells(prompt):
        return {"results": [result("Pain", "", "7", columns=["Day 1", "P value"])],
                "blank_cells": []}

    rep, _, fake = by_table(tmp_path, ByTable(plan, cells))
    assert fake.seen.count("table") == 1 and rep.partly == 0


def test_a_table_still_short_is_marked_partly_stored(tmp_path):
    rep, store, _ = by_table(tmp_path, ByTable(PLAN, lambda p: {"results": [], "blank_cells": []}))
    assert rep.partly == 1
    log = [r["problem"] for r in read_log(store)]
    assert any("partly stored: 4 of 4 cells" in p for p in log)


def test_labels_match_whatever_their_case_and_spacing(tmp_path):
    def cells(prompt):
        got = whole(prompt)["results"]
        for r in got:
            r["table_row"], r["table_columns"] = f" {r['table_row'].upper()} ", ["DAY  1", "day 2"]
        return {"results": got, "blank_cells": []}

    rep, _, fake = by_table(tmp_path, ByTable(PLAN, cells))
    assert fake.seen.count("table") == 1 and rep.partly == 0




def test_a_broken_table_answer_loses_that_part_not_the_paper(tmp_path):
    fake = ByTable(PLAN, whole)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / f"{DOC}.txt").write_text(TEXT_TABLE, "utf-8")

    class Cut(FakeGemini):
        def generate(self, model, system, prompt, schema, thinking, max_output, step="", cache=""):
            text, usage, thoughts = super().generate(model, system, prompt, schema, thinking,
                                                     max_output, step, cache)
            return (text[:20] if "blank_cells" in schema["properties"] else text), usage, thoughts

    llm = LLM(Cut(fake), save_calls=False, corpus=tmp_path / "corpus")
    llm.cfg = {**llm.cfg, "pipeline": {**llm.cfg["pipeline"], "results_by_table": True}}
    store = Store(tmp_path / "v.db", CB)
    rep = extract_document(DOC, TOPIC, CB.topics[TOPIC], llm, store, tmp_path)
    assert rep.study and not rep.stopped
    assert rep.partly == 1
    assert any("not read" in r["problem"] for r in read_log(store))


def test_a_long_repair_goes_in_batches_and_each_value_is_asked_once(tmp_path):
    rows = [f"Row {i}" for i in range(50)]
    plan = [{"number": "1", "reports_results": True, "rows": rows, "columns": ["A"]}]

    def cells(prompt):
        got = [result(r, "A", "7") for r in rows]
        return {"results": got, "blank_cells": []}

    sizes, asked = [], []

    def repair(prompt, schema):
        sizes.append(len(schema["properties"]))
        asked.extend(schema["properties"])
        return {}

    fake = ByTable(plan, cells)
    fake.repair = repair
    by_table(tmp_path, fake)
    assert len(sizes) >= 2 and max(sizes) <= REPAIR_BATCH
    assert len(asked) == len(set(asked)) >= 50


def cached_run(tmp_path, backend):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / f"{DOC}.txt").write_text(TEXT_TABLE, "utf-8")
    llm = LLM(backend, save_calls=False, corpus=tmp_path / "corpus")
    llm.cfg = {**llm.cfg, "pipeline": {**llm.cfg["pipeline"], "results_by_table": True}}
    return extract_document(DOC, TOPIC, CB.topics[TOPIC], llm, Store(tmp_path / "v.db", CB),
                            tmp_path), llm


def test_the_paper_is_sent_once_and_deleted_after(tmp_path):
    backend = FakeGemini(ByTable(PLAN, whole))
    cached_run(tmp_path, backend)
    assert len(backend.held) == 1 and TEXT_TABLE in backend.held[0]
    assert backend.closed == ["cache-1"]
    assert all(c["cache"] == "cache-1" and "Sixty client-owned" not in c["prompt"]
               for c in backend.calls)


def test_a_paper_that_cant_be_held_is_sent_with_each_call(tmp_path):
    class NoCache(FakeGemini):
        def open_cache(self, model, system, text):
            raise ValueError("too short")

    backend = NoCache(ByTable(PLAN, whole))
    rep, _ = cached_run(tmp_path, backend)
    assert rep.study and all("Sixty client-owned" in c["prompt"] for c in backend.calls)
    assert backend.closed == []


def test_a_held_paper_is_deleted_even_when_extraction_fails(tmp_path):
    class Boom(FakeGemini):
        def generate(self, *a, **k):
            raise RuntimeError("no")

    backend = Boom(ByTable(PLAN, whole))
    with pytest.raises(RuntimeError):
        cached_run(tmp_path, backend)
    assert backend.closed == ["cache-1"]


def test_the_old_flow_holds_nothing(tmp_path):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / f"{DOC}.txt").write_text(TEXT, "utf-8")
    backend = FakeGemini(Fake())
    llm = old_flow(LLM(backend, save_calls=False, corpus=tmp_path / "corpus"))
    extract_document(DOC, TOPIC, CB.topics[TOPIC], llm, Store(tmp_path / "v.db", CB), tmp_path)
    assert backend.held == [] and all(not c["cache"] for c in backend.calls)


class ReadTimeout(Exception):
    pass


def test_a_call_that_times_out_is_tried_again(tmp_path, monkeypatch):
    monkeypatch.setattr("vet.llm.time.sleep", lambda s: None)

    class Slow(FakeGemini):
        tries = 0

        def generate(self, *a, **k):
            Slow.tries += 1
            if Slow.tries == 1:
                raise ReadTimeout("silent connection")
            return super().generate(*a, **k)

    llm = LLM(Slow(lambda *a: {}), save_calls=False, corpus=tmp_path)
    assert llm.call("structure", "p", {}) == {}
    assert Slow.tries == 2


def test_a_call_that_keeps_timing_out_stops_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr("vet.llm.time.sleep", lambda s: None)

    class Dead(FakeGemini):
        def generate(self, *a, **k):
            raise ReadTimeout("silent connection")

    with pytest.raises(ReadTimeout):
        LLM(Dead(), save_calls=False, corpus=tmp_path).call("structure", "p", {})


def test_every_real_call_has_a_time_limit(monkeypatch):
    from google import genai

    from vet.llm import CALL_TIMEOUT_MS, Gemini
    made = {}
    monkeypatch.setattr(genai, "Client", lambda **k: made.update(k))
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    Gemini()
    assert made["http_options"].timeout == CALL_TIMEOUT_MS


def test_holding_a_paper_is_paid_for(tmp_path):
    backend = FakeGemini()
    llm = LLM(backend, save_calls=False, corpus=tmp_path)
    before = llm.ledger.spent
    name = llm.open_cache("structure", "s", "x" * 4000, "doc")
    price = llm.price(llm.cfg["models"]["structure"])
    assert llm.ledger.spent - before == pytest.approx(1000 * price["input"] / 1e6, rel=.01)
    llm.close_cache(name, "structure", "doc")
    assert not llm.held


def test_a_table_answer_is_asked_to_quote_the_column_heading():
    assert "printed column heading" in TABLE_PART and "printed column heading" in TABLE_MORE


def test_cached_input_is_charged_at_the_cached_price(tmp_path):
    class Cached(FakeGemini):
        def generate(self, *a, **k):
            return "{}", {"input": 1_000_000, "output": 0, "thoughts": 0, "cached": 800_000}, ""

    llm = LLM(Cached(), save_calls=False, corpus=tmp_path)
    before = llm.ledger.spent
    llm.call("structure", "p", {})
    price = llm.price(llm.cfg["models"]["structure"])
    want = 0.2 * price["input"] + 0.8 * price["cached"]
    assert llm.ledger.spent - before == pytest.approx(want, abs=1e-6)
