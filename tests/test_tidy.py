"""Repairs by rule (vet tidy) and study links (vet link): each settles only what its rule can,
for any drug, unit or species, logs what it changed, and changes nothing more a second time."""
import pytest

from tests import fixture
from tests.test_store import second_study, write_any
from vet.codebook import Codebook
from vet.duplicates import possible_duplicates
from vet.links import link
from vet.store import Store
from vet.tidy import read_dose, tidy
from vet.validate import validate

CB = Codebook()
A2 = fixture.A2


@pytest.mark.parametrize("drug, written, others, want", [
    ("prednisone", "prednisone (2.0 ± 0.1 mg/kg, [mean ± SD], PO, q24h; Prednisone, Acme Pharma)",
     ["rivaroxaban"], ("2.0", "", "mg/kg", "mean")),
    ("MMF", ("The median starting dosage of MMF was 17.5 mg/kg/day (interquartile range [IQR] "
             "13.9-20.0 mg/kg/day)"), [], ("17.5", "", "mg/kg/day", "median")),
    ("azathioprine", "azathioprine (median 2 mg/kg [range, 1.5-2.2 mg/kg] q24h)", [],
     ("2", "", "mg/kg", "median")),
    ("clopidogrel", "clopidogrel (2‐3 mg/kg q24h) plus placebo", ["placebo"],
     ("2", "3", "mg/kg", "stated")),
    ("prednisone", "prednisone at a dose range of 1 to 2 mg/kg/day, tapered over 3 months", [],
     ("1", "2", "mg/kg/day", "stated")),
    ("carboplatin", "carboplatin 300 mg/m2 IV every 3 weeks", [], ("300", "", "mg/m2", "stated")),
    ("dexmedetomidine", "dexmedetomidine CRI 1-2 µg/kg/h", [], ("1", "2", "µg/kg/h", "stated")),
    ("insulin", "insulin glargine 0.25 U/kg SC q12h", [], ("0.25", "", "U/kg", "stated")),
    ("meloxicam", "meloxicam 1.5 mg per cat", [], ("1.5", "", "mg", "stated")),
    ("prednisolone", "prednisolone (5 mg tablets; Acme) at 1 mg/kg q12h", [],
     ("1", "", "mg/kg", "stated")),                         # the tablet's strength is not the dose
    ("cyclosporine", "cyclosporine (100 mg/mL oral solution) 5 mg/kg PO q24h", [],
     ("5", "", "mg/kg", "stated")),                         # nor is a concentration
    ("aspirin", ("combined low‐dose aspirin and cyclosporine (1.0 ± 0.03 mg/kg PO q24h and "
                 "9.9 ± 0.4 mg/kg PO q12h, respectively)"), ["cyclosporine"],
     ("1.0", "", "mg/kg", "mean")),                         # in the order the drugs are named
    ("cyclosporine", ("combined low‐dose aspirin and cyclosporine (1.0 ± 0.03 mg/kg PO q24h and "
                      "9.9 ± 0.4 mg/kg PO q12h, respectively)"), ["aspirin"],
     ("9.9", "", "mg/kg", "mean")),
])
def test_a_written_dose_gives_its_one_dose(drug, written, others, want):
    dose, why = read_dose(drug, written, others)
    assert dose and (dose.lo, dose.hi, dose.unit, dose.statistic) == want, why
    assert dose.lo in dose.span and dose.hi in dose.span


@pytest.mark.parametrize("drug, written, others", [
    ("corticosteroid", "All dogs were started on immunosuppressive corticosteroids", []),
    ("prednisolone", "prednisolone (5 mg tablets; Acme)", []),
    ("gabapentin", "gabapentin 10 mg/kg PO q8h … then 5 mg/kg q12h for 7 days", []),
    ("butorphanol", "sedated with a dose of 0 · 3 mg/kg butorphanol", []),
    ("aspirin", "aspirin and cyclosporine (1.0 ± 0.03 mg/kg PO q24h and 9.9 ± 0.4 mg/kg PO q12h)",
     ["cyclosporine"]),                                     # whose dose is whose: a person decides
])
def test_a_written_dose_that_rule_cannot_settle_is_left(drug, written, others):
    dose, why = read_dose(drug, written, others)
    assert dose is None and why


def a_store(tmp_path, t=None) -> Store:
    store = Store(tmp_path / "v.db", CB)
    store.import_folder(write_any(t or fixture.clean(CB), tmp_path / "in"))
    return store


def test_tidy_fills_a_dose_from_its_written_dose_once(tmp_path):
    t = fixture.clean(CB)
    t["arm_doses"][0].update(dose_value="", dose_unit="",
                             dose_as_written="analgesic X (2.0 ± 0.1 mg/kg [mean ± SD]) SC at extubation")
    t["evidence"] = [e for e in t["evidence"] if e["table"] != "arm_doses"] + fixture.evidence_for(
        CB, {**{k: [] for k in t}, "arm_doses": t["arm_doses"], "topic_fields": []})
    store = a_store(tmp_path, t)
    rep = tidy(store, numbers=True)
    dose = store.rows("arm_doses")[0]
    assert (dose["dose_value"], dose["dose_unit"], dose["dose_statistic"]) == ("2.0", "mg/kg", "mean")
    assert rep.doses == 1 and rep.statistics == 1
    issues = {i.check for i in validate(store.tables(), CB)}
    assert not issues & {"missing_evidence", "quote_missing"}, issues
    logged = store.db.execute("SELECT field FROM extraction_log WHERE step = 'tidy'").fetchall()
    assert {f for (f,) in logged} == {"dose_value", "dose_unit", "dose_statistic"}
    again = tidy(store, numbers=True)
    assert (again.doses, again.statistics, again.citations, again.repeats) == (0, 0, 0, 0)


def test_tidy_says_what_a_stated_dose_is_and_keeps_the_extractors_number(tmp_path):
    store = a_store(tmp_path)                    # fixture: dose 2, "2 mg/kg SC once at extubation"
    tidy(store, numbers=True)
    assert store.rows("arm_doses")[0]["dose_statistic"] == "stated"
    store.db.execute("UPDATE arm_doses SET dose_value = '3', dose_statistic = ''")
    tidy(store, numbers=True)                                  # 3 is not what the words say: left alone
    assert store.rows("arm_doses")[0]["dose_statistic"] == ""


def test_tidy_takes_citation_details_from_the_search_record(tmp_path):
    t = fixture.clean(CB)
    t["sources"][0].update(year="", journal_or_agency="", first_author="")
    store = a_store(tmp_path, t)
    store.db.execute("CREATE TABLE records (key TEXT, pmid TEXT, doi TEXT, pmcid TEXT, title TEXT, "
                     "year TEXT, journal TEXT, first_author TEXT)")
    store.db.execute("INSERT INTO records VALUES ('k', '', ?, '', 'Fixture', '2021', "
                     "'J Fict Vet Med', 'Doe J')", (t["sources"][0]["doi"].upper(),))
    rep = tidy(store, numbers=True)
    src = next(r for r in store.rows("sources") if r["source_id"] == fixture.DOC)
    assert (src["year"], src["journal_or_agency"], src["first_author"]) == ("2021", "J Fict Vet Med",
                                                                             "Doe J")
    assert src["title"] == t["sources"][0]["title"]          # filled only where empty
    assert rep.citations == 3


def test_tidy_removes_a_row_repeated_under_a_new_key(tmp_path):
    t = fixture.clean(CB)
    t["results"].append({**t["results"][0], "result_id": f"{fixture.SID}-R3"})
    store = a_store(tmp_path, t)
    assert tidy(store, numbers=True).repeats == 1
    assert len(store.rows("results")) == 2
    assert "duplicate_row" not in {i.check for i in possible_duplicates(store.tables())}


# -- study links ----------------------------------------------------------------------------
def two_studies(tmp_path) -> Store:
    store = Store(tmp_path / "v.db", CB)
    store.import_folder(write_any(fixture.clean(CB), tmp_path / "a"))
    store.import_folder(write_any(second_study(fixture.clean(CB)), tmp_path / "b"))
    docs = tmp_path / "docs"
    docs.mkdir()
    for src in store.rows("sources"):
        (tmp_path / src["text_file"]).write_text(
            "Methods. Blood samples were collected from these dogs during a related study of "
            "analgesic X.", "utf-8")
    return store


def same_trial(store):
    return [i.key for i in possible_duplicates(store.tables()) if i.check == "possible_same_trial"]


def test_a_link_rests_on_the_documents_own_words(tmp_path):
    store = two_studies(tmp_path)
    assert same_trial(store) == ["VET-1|VET-2"]
    with pytest.raises(ValueError, match="not in"):
        link(store, "VET-2", "VET-1", "same_experiment", "DOI-10-9999-FIXTURE-2",
             "the same dogs were used")
    with pytest.raises(ValueError, match="not a document"):
        link(store, "VET-2", "VET-1", "same_experiment", "DOI-10-1111-OTHER-1", "during a related")
    with pytest.raises(ValueError, match="needs a note"):
        link(store, "VET-1", "VET-2", "distinct")
    row = link(store, "VET-2", "VET-1", "same_experiment", "DOI-10-9999-FIXTURE-2",
               "collected from these dogs during a related study", "Methods")
    assert (row["ID"], row["linked_ID"]) == ("VET-1", "VET-2")       # lower number first
    assert same_trial(store) == []                                  # decided: not listed again
    assert validate(store.tables(), CB) == []
    assert not [i for i in validate(store.tables(), CB, root=tmp_path) if i.table == "study_links"]
    link(store, "VET-1", "VET-2", "distinct", note="different dogs after all")
    assert [r["relation"] for r in store.rows("study_links")] == ["distinct"]  # replaced


def test_the_checker_holds_links_to_their_rules(tmp_path):
    store = two_studies(tmp_path)
    t = store.tables()
    base = {c: "" for c in CB.columns("study_links")}
    t["study_links"] = [{**base, "ID": "VET-2", "linked_ID": "VET-1", "relation": "distinct",
                         "decided_by": "curator"},
                        {**base, "ID": "VET-1", "linked_ID": "VET-2", "relation": "shared_animals",
                         "decided_by": "curator"},
                        {**base, "ID": "VET-1", "linked_ID": "VET-3", "relation": "shared_animals",
                         "source_id": "DOI-10-9999-FIXTURE-2", "decided_by": "curator",
                         "statement_verbatim": "words that are nowhere in the paper"}]
    checks = {i.check for i in validate(t, CB, root=tmp_path)}
    assert {"link_order", "link_unsupported", "orphan", "not_in_document"} <= checks


def test_an_import_renumbers_both_studies_of_a_link(tmp_path):
    store = two_studies(tmp_path)
    link(store, "VET-1", "VET-2", "distinct", note="different dogs")
    store.export(tmp_path / "out")
    other = Store(tmp_path / "other.db", CB)
    other.import_tables({"studies": [], "study_sources": [], "sources": []})
    other.db.execute("INSERT OR REPLACE INTO meta VALUES ('last_study', '10')")
    from vet.store import _read_folder
    tables = {k: rows for k, (_, rows) in _read_folder(tmp_path / "out").items()}
    other.import_tables(tables)
    assert [(r["ID"], r["linked_ID"]) for r in other.rows("study_links")] == [("VET-11", "VET-12")]


# -- the checker's plausibility rules --------------------------------------------------------
def _p(t):
    t["results"][1]["p_value"] = "44"


def _percent(t):
    t["results"][0].update(statistic_type="proportion", value="140", value_unit="%")


def _proportion(t):
    t["results"][0].update(statistic_type="proportion", value="3", value_unit="")


def _range(t):
    t["arm_doses"][0]["dose_value_max"] = "1"


def _kind_without_dose(t):
    t["arm_doses"][0].update(dose_value="", dose_statistic="stated")


@pytest.mark.parametrize("check, break_it", [
    ("p_value_range", _p), ("proportion_range", _percent), ("proportion_range", _proportion),
    ("dose_range", _range), ("missing_value", _kind_without_dose)])
def test_values_that_cannot_be_right_are_caught(check, break_it):
    t = fixture.clean(CB)
    break_it(t)
    assert check in {i.check for i in validate(t, CB)}


def test_a_percent_and_a_proportion_each_on_their_own_scale_pass():
    t = fixture.clean(CB)
    t["results"][0].update(statistic_type="proportion", value="45", value_unit="%")
    t["results"][1].update(statistic_type="proportion", value="0.45", value_unit="", p_value="<0.001")
    checks = {i.check for i in validate(t, CB)}
    assert not checks & {"proportion_range", "p_value_range"}


# -- values a row's own passage doesn't support ----------------------------------------------
def adverse(t, fatal, passage):
    t["results"][0].update(result_kind="adverse_event", endpoint="vomiting", ae_fatal=fatal,
                           as_written=passage, statistic_type="count", value="3", n="29",
                           events="3", timepoint_value="", timepoint_unit="",
                           timepoint_as_written="")
    return t


@pytest.mark.parametrize("fatal, passage, flagged", [
    ("no", "3/29 dogs vomited after analgesic X", True),           # silence is not "no"
    ("no", "3/29 dogs vomited; all survived to discharge", False),
    ("died", "3/29 dogs vomited and 1 died", False),
    ("", "3/29 dogs vomited", False),
])
def test_whether_an_adverse_event_was_fatal_needs_words_about_it(fatal, passage, flagged):
    t = adverse(fixture.clean(CB), fatal, passage)
    assert ("ae_fatal_unsupported" in {i.check for i in validate(t, CB)}) is flagged


@pytest.mark.parametrize("value, written, flagged", [
    ("3", "t3", True), ("2", "T2", True), ("2", "visit 2", True), ("1", "timepoint 1", True),
    ("14", "D14", False), ("28", "day 28", False), ("24", "24 h", False), ("6", "6h", False),
    ("2", "wk 2", False),
])
def test_a_numbered_label_is_not_a_time(value, written, flagged):
    t = fixture.clean(CB)
    t["results"][0].update(timepoint_value=value, timepoint_unit="days", timepoint_as_written=written,
                           as_written=t["results"][0]["as_written"] + f" ({written})")
    assert ("timepoint_from_label" in {i.check for i in validate(t, CB)}) is flagged


def test_tidy_empties_what_the_passage_does_not_support_and_logs_it(tmp_path):
    t = adverse(fixture.clean(CB), "no", "3/29 dogs vomited after analgesic X")
    t["results"][1].update(timepoint_value="3", timepoint_unit="days", timepoint_as_written="t3")
    store = a_store(tmp_path, t)
    assert tidy(store, numbers=True).unsupported == 3                   # ae_fatal, timepoint value and unit
    r1, r2 = store.rows("results")
    assert r1["ae_fatal"] == "" and (r2["timepoint_value"], r2["timepoint_unit"]) == ("", "")
    assert r2["timepoint_as_written"] == "t3"             # the label stays
    checks = {i.check for i in validate(store.tables(), CB)}
    assert not checks & {"ae_fatal_unsupported", "timepoint_from_label"}
    logged = {f for (f,) in store.db.execute(
        "SELECT field FROM extraction_log WHERE step = 'tidy' AND problem LIKE 'emptied%'")}
    assert logged == {"ae_fatal", "timepoint_value", "timepoint_unit"}
    assert tidy(store, numbers=True).unsupported == 0


def test_tidy_leaves_a_stated_death_it_cannot_find_for_a_person(tmp_path):
    store = a_store(tmp_path, adverse(fixture.clean(CB), "died", "3/29 dogs vomited"))
    tidy(store, numbers=True)
    assert store.rows("results")[0]["ae_fatal"] == "died"
    assert "ae_fatal_unsupported" in {i.check for i in validate(store.tables(), CB)}


def test_a_taper_says_what_kind_the_extractors_dose_is(tmp_path):
    t = fixture.clean(CB)
    t["arm_doses"][0].update(dose_value="2.19", dose_unit="mg/kg/day", dose_as_written=(
        "analgesic X at a median dosage of 2.19 mg/kg/day (IQR, 1.92-2.55), reduced to a median "
        "of 1.67 mg/kg/day at the first reduction, and to 1.12 mg/kg/day at the second"))
    t["evidence"] = [e for e in t["evidence"] if e["table"] != "arm_doses"] + fixture.evidence_for(
        CB, {**{k: [] for k in t}, "arm_doses": t["arm_doses"], "topic_fields": []})
    store = a_store(tmp_path, t)
    tidy(store, numbers=True)
    dose = store.rows("arm_doses")[0]
    assert (dose["dose_value"], dose["dose_statistic"]) == ("2.19", "median")
    assert not {i.check for i in validate(store.tables(), CB)} & {"missing_evidence", "quote_missing"}
