"""The checker: the clean fixture passes, and each deliberate error is caught (schema v2
section 10: 'test the validator on a fixture with one deliberate error of each type')."""
import copy

import pytest

from tests import fixture
from vet.codebook import Codebook
from vet.validate import load_folder, validate

CB = Codebook()
SID = fixture.SID


def checks(tables, source_id=None, headers=None):
    return {i.check for i in validate(tables, CB, source_id=source_id, headers=headers)}


def ev(t, field):
    return next(r for r in t["evidence"] if r["field"] == field and r["table"] == "studies")


def test_clean_fixture_passes(tmp_path):
    t = fixture.clean(CB)
    assert validate(t, CB) == []
    fixture.write(t, CB, tmp_path)                     # round trip through CSV
    tables, headers = load_folder(tmp_path, tmp_path)
    assert validate(tables, CB, headers=headers) == []


def _add_result_dup(t):
    t["results"].append(dict(t["results"][0]))


def _unsorted_countries(t):
    t["studies"][0]["Country"] = "United States|Switzerland"
    ev(t, "Country")["value"] = "United States|Switzerland"


def _fda_primary(t):
    t["study_sources"][1]["role"] = "primary"


def _unlinked(t):
    t["sources"].append({**t["sources"][1], "source_id": "PMID99999902"})
    t["results"][0]["source_id"] = "PMID99999902"


def _drop_evidence(t):
    t["evidence"].remove(ev(t, "n_enrolled"))


def _evidence_for_empty_field(t):
    t["evidence"].append({**ev(t, "n_enrolled"), "field": "Complete Date", "value": "2020"})


def _retrospective_randomised(t):
    t["studies"][0]["study_kind"] = "retrospective_cohort"
    ev(t, "study_kind")["value"] = "retrospective_cohort"


def _orphan_study_source(t):
    t["sources"].append({**t["sources"][1], "source_id": "PMID99999902"})
    t["study_sources"].append({"ID": "VET-2", "source_id": "PMID99999902", "role": "primary"})


def topic_ev(t, field):
    return next(r for r in t["evidence"] if r["table"] == "topic_fields" and r["field"] == field)


def _bad_topic_value(t):
    next(r for r in t["topic_fields"] if r["field"] == "surgery_category")["value"] = "soft"
    topic_ev(t, "surgery_category")["value"] = "soft"


def _same_paper_twice(t):
    t["sources"].append({**t["sources"][0], "source_id": "PMID99999901"})


CASES = [
    ("key_format", lambda t: t["results"][0].update(result_id="R1")),
    ("key_format", lambda t: t["sources"][0].update(source_id="pmid99999901")),
    ("key_missing", lambda t: t["results"][0].update(result_id="")),
    ("duplicate_key", _add_result_dup),
    ("duplicate_key", lambda t: t["evidence"].append(dict(ev(t, "n_enrolled")))),
    ("enum", lambda t: t["studies"][0].update(blinding="triple")),
    ("enum", lambda t: t["studies"][0].update(not_applicable="random")),
    ("enum", lambda t: t["sites"][0].update(Country="Korea, Republic of")),
    ("number", lambda t: t["arm_doses"][0].update(dose_value="2 mg/kg")),
    ("number", lambda t: t["studies"][0].update(n_enrolled="~60")),
    ("date", lambda t: t["studies"][0].update(**{"Started date": "2019-13"})),
    ("list", lambda t: t["studies"][0].update(keywords="analgesia | dog")),
    ("list", _unsorted_countries),
    ("criteria_layout", lambda t: t["studies"][0].update(
        **{"Eligibility Criteria": "Dogs undergoing ovariohysterectomy"})),
    ("sentinel", lambda t: t["studies"][0].update(disease_name="N/A")),
    ("sentinel", lambda t: t["studies"][0].update(Sponsor="not reported")),
    ("too_long", lambda t: t["results"][0].update(as_written="x" * 501)),
    ("orphan", lambda t: t["breeds"][0].update(arm_id=f"{SID}-A9")),
    ("orphan", lambda t: t["people"][0].update(site_no="2")),
    ("key_format", lambda t: t["studies"][0].update(ID="VET-PMID99999901-1")),
    ("key_format", lambda t: t["studies"][0].update(ID="VET-01")),
    ("primary_count", _fda_primary),
    ("unlinked_source", _unlinked),
    ("unlinked_source", lambda t: t["conflicts"][0].update(secondary_source_id="PMID99999902")),
    ("missing_study_row", _orphan_study_source),
    ("orphan", lambda t: t["conflicts"][0].update(ID="VET-9")),
    ("duplicate_document", _same_paper_twice),
    ("unknown_topic", lambda t: t["study_topics"][0].update(topic="cats")),
    ("orphan", lambda t: t["study_topics"][0].update(topic="imha")),
    ("unknown_field", lambda t: t["topic_fields"][0].update(field="colour")),
    ("enum", _bad_topic_value),
    ("missing_evidence", lambda t: t["evidence"].remove(topic_ev(t, "surgery_category"))),
    ("evidence_value_mismatch", lambda t: topic_ev(t, "surgery_category").update(value="mixed")),
    ("evidence_unmatched", lambda t: topic_ev(t, "surgery_category").update(row_key="imha")),
    ("missing_evidence", _drop_evidence),
    ("evidence_unmatched", _evidence_for_empty_field),
    ("evidence_value_mismatch", lambda t: ev(t, "n_enrolled").update(value="61")),
    ("quote_missing", lambda t: ev(t, "n_enrolled").update(quote="")),
    ("location_missing", lambda t: ev(t, "Summary").update(location="")),
    ("quote_too_long", lambda t: ev(t, "n_enrolled").update(quote="x" * 501)),
    ("not_applicable_filled", _retrospective_randomised),
    ("not_applicable_filled", lambda t: t["studies"][0].update(not_applicable="randomised")),
    ("not_applicable_filled", lambda t: t["studies"][0].update(not_applicable="surgery_category")),
]


@pytest.mark.parametrize("check,break_it", CASES, ids=[c for c, _ in CASES])
def test_each_error_is_caught(check, break_it):
    t = fixture.clean(CB)
    break_it(t)
    assert check in checks(t)


def test_truncated_quote_is_allowed():
    t = fixture.clean(CB)
    e = ev(t, "n_enrolled")
    e.update(quote=f"{e['value']} dogs " + "x" * 501, quote_truncated="yes")
    assert checks(t) == set()


def test_a_value_may_have_several_quotes():
    t = fixture.clean(CB)
    t["evidence"].append({**ev(t, "Country"), "quote": "a second fixture quote for Country"})
    t["evidence"].append({**ev(t, "n_enrolled"), "source_id": fixture.FDA,
                          "quote": "the FOI summary's quote for n_enrolled"})
    assert checks(t) == set()


def test_an_unstated_design_does_not_rule_fields_out():
    t = fixture.clean(CB)
    t["studies"][0]["study_kind"] = ""                 # randomised etc. now can't be ruled out
    t["evidence"].remove(ev(t, "study_kind"))
    assert checks(t) == set()


def test_listed_values_may_say_unknown_or_none():
    t = fixture.clean(CB)
    for field, value in (("trial_outcome", "unknown"), ("Overall Status", "UNKNOWN")):
        t["studies"][0][field] = value
        ev(t, field)["value"] = value
    t["studies"][0]["control_type"] = "none"
    ev(t, "control_type")["value"] = "none"
    assert checks(t) == set()


def test_document_scope():
    t = fixture.clean(CB)
    doc = fixture.DOC
    assert "wrong_source" in checks(t, source_id=doc)   # FIXTURE R2 is from FDA
    t["results"].pop()
    assert checks(t, source_id=doc) == set()
    t2 = copy.deepcopy(t)
    t2["study_sources"] = [r for r in t2["study_sources"] if r["source_id"] != doc]
    t2["study_sources"][0]["role"] = "primary"
    assert "unlinked_source" in checks(t2, source_id=doc)


def test_header_order_is_checked(tmp_path):
    t = fixture.clean(CB)
    fixture.write(t, CB, tmp_path)
    path = tmp_path / "arms.csv"
    lines = path.read_text().splitlines()
    cols = lines[0].split(",")
    cols[2], cols[3] = cols[3], cols[2]
    path.write_text("\n".join([",".join(cols)] + lines[1:]) + "\n")
    tables, headers = load_folder(tmp_path, tmp_path)
    assert "header" in checks(tables, headers=headers)


# -- against the documents' own text --------------------------------------------------------

def with_texts(t: dict, root):
    """Give each document a text holding exactly the quotes and copies that cite it."""
    from collections import defaultdict

    from vet.validate import VERBATIM
    by_doc = defaultdict(list)
    for e in t["evidence"]:
        by_doc[e["source_id"]].append(e["quote"])
    for table, rows in t.items():
        for f in CB.tables.get(table, []):
            if VERBATIM.search(f.name) and table not in ("evidence", "conflicts"):
                for r in rows:
                    by_doc[r.get("source_id") or fixture.DOC].append(r.get(f.name, ""))
    for src in t["sources"]:
        path = root / src["text_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(by_doc[src["source_id"]]), "utf-8")
    return {i.check for i in validate(t, CB, root=root)}


def test_quotes_and_copies_found_in_the_documents_pass(tmp_path):
    assert with_texts(fixture.clean(CB), tmp_path) == set()


def test_pdf_line_breaks_and_hyphenation_do_not_matter(tmp_path):
    t = fixture.clean(CB)
    with_texts(t, tmp_path)
    path = tmp_path / f"docs/{fixture.DOC}.txt"
    path.write_text(path.read_text().replace("rescue within", "res- cue\nwith in"), "utf-8")
    assert {i.check for i in validate(t, CB, root=tmp_path)} == set()


def test_a_quote_missing_from_its_document_is_caught(tmp_path):
    t = fixture.clean(CB)
    with_texts(t, tmp_path)
    ev(t, "n_enrolled")["quote"] += " in Antarctica"
    assert "quote_not_in_document" in {i.check for i in validate(t, CB, root=tmp_path)}


def test_a_copied_field_missing_from_the_documents_is_caught(tmp_path):
    t = fixture.clean(CB)
    with_texts(t, tmp_path)
    t["results"][0]["as_written"] = "3/29 dogs given analgesic X needed no rescue"
    assert "not_in_document" in {i.check for i in validate(t, CB, root=tmp_path)}


def test_a_missing_text_file_is_reported_not_skipped(tmp_path):
    t = fixture.clean(CB)
    with_texts(t, tmp_path)
    (tmp_path / f"docs/{fixture.FDA}.txt").unlink()
    assert "no_text" in {i.check for i in validate(t, CB, root=tmp_path)}


def test_a_value_must_be_in_its_own_quote():
    t = fixture.clean(CB)
    ev(t, "n_enrolled")["quote"] = "dogs were enrolled"
    assert checks(t) == {"value_not_in_quote"}


@pytest.mark.parametrize("field, value, quote", [
    ("n_enrolled", "43", "Forty-three dogs were enrolled"),
    ("n_enrolled", "120", "one hundred and twenty dogs"),
    ("n_enrolled", "1200", "1,200 dogs"),
    ("Started date", "2018-04", "from April 2018 to October 2020")])
def test_numbers_in_words_and_dates_as_written_count(field, value, quote):
    t = fixture.clean(CB)
    t["studies"][0][field] = value
    ev(t, field).update(value=value, quote=quote)
    assert "value_not_in_quote" not in checks(t)


def test_adverse_event_fields_only_on_adverse_event_rows():
    t = fixture.clean(CB)
    t["results"][0].update(result_kind="outcome", ae_fatal="died")
    assert checks(t) == {"not_applicable_filled"}
    t["results"][0].update(result_kind="adverse_event", endpoint="vomiting")
    assert checks(t) == set()


def test_one_dose_may_have_several_routes():
    t = fixture.clean(CB)
    t["arm_doses"][0]["route"] = "IV|PO"
    ev_rows = [e for e in t["evidence"] if e["field"] == "route"]
    for e in ev_rows:
        e.update(value="IV|PO")
    assert checks(t) == set()


@pytest.mark.parametrize("change, check", [
    ({"value": "4"}, "value_not_in_quote"),             # the number isn't in its sentence
    ({"p_value": "<0.01"}, "value_not_in_quote"),
    ({"events": "31"}, "events_more_than_n"),
    ({"result_kind": ""}, "missing_value")])
def test_results_numbers_are_checked_against_their_sentence(change, check):
    t = fixture.clean(CB)
    t["results"][0].update(change)
    assert check in checks(t)


def test_a_denominator_from_a_table_header_counts_when_it_is_the_arm_size():
    t = fixture.clean(CB)
    t["results"][0]["as_written"] = "needed rescue within 24 h | 3 |"     # n=29 only in the header
    t["results"][0]["n"] = t["arms"][1]["n_analysed"]
    assert "value_not_in_quote" not in checks(t)


def test_copied_passages_may_be_joined_and_each_is_checked(tmp_path):
    t = fixture.clean(CB)
    with_texts(t, tmp_path)
    path = tmp_path / f"docs/{fixture.DOC}.txt"
    path.write_text(path.read_text() + "\nFifty dogs were screened.\nSee Figure 1.\nTwo were withdrawn.", "utf-8")
    t["studies"][0]["flow_as_written"] = "Fifty dogs were screened. … Two were withdrawn."
    t["evidence"].append({**ev(t, "age_as_written"), "field": "flow_as_written",
                          "value": t["studies"][0]["flow_as_written"]})
    assert {i.check for i in validate(t, CB, root=tmp_path)} == set()
    t["studies"][0]["flow_as_written"] = "Fifty dogs were screened. Two were withdrawn."
    ev(t, "flow_as_written")["value"] = t["studies"][0]["flow_as_written"]
    assert "not_in_document" in {i.check for i in validate(t, CB, root=tmp_path)}


@pytest.mark.parametrize("value, quote, found", [
    ("4", "rescue within 24 h", False), ("0.1", "CI 0.15-0.9", False), ("3", "3.5 mg", False),
    ("0.017", "P = .017", True), ("87400", "87 400 cells", True), ("-16.4", "−16.4", True),
    ("0", "no dogs relapsed", True), ("29", "3/29 dogs", True)])
def test_numbers_stand_on_their_own(value, quote, found):
    from vet.validate import _in_quote
    assert _in_quote(value, "number", quote) is found
