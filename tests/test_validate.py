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


def _swap_primary(t):
    t["study_sources"][0]["role"], t["study_sources"][1]["role"] = "regulatory", "primary"


def _unlinked(t):
    t["sources"].append({**t["sources"][1], "source_id": "PMID99999902"})
    t["results"][0]["source_id"] = "PMID99999902"


def _wrong_indication(t):
    t["studies"][0]["indication"] = "imha"
    ev(t, "indication")["value"] = "imha"


def _drop_evidence(t):
    t["evidence"].remove(ev(t, "n_enrolled"))


def _evidence_for_empty_field(t):
    t["evidence"].append({**ev(t, "n_enrolled"), "field": "Complete Date", "value": "2020"})


def _retrospective_randomised(t):
    t["studies"][0]["study_kind"] = "retrospective_cohort"
    ev(t, "study_kind")["value"] = "retrospective_cohort"


def _orphan_study_source(t):
    t["sources"].append({**t["sources"][0], "source_id": "PMID99999902"})
    t["study_sources"].append({"ID": "VET-PMID99999902-1", "source_id": "PMID99999902",
                               "role": "primary"})


CASES = [
    ("key_format", lambda t: t["results"][0].update(result_id="R1")),
    ("key_format", lambda t: t["sources"][0].update(source_id="pmid99999901")),
    ("key_missing", lambda t: t["results"][0].update(result_id="")),
    ("duplicate_key", _add_result_dup),
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
    ("primary_count", _fda_primary),
    ("id_not_primary", _swap_primary),
    ("unlinked_source", _unlinked),
    ("missing_study_row", _orphan_study_source),
    ("ext_wrong_indication", _wrong_indication),
    ("missing_evidence", _drop_evidence),
    ("evidence_unmatched", _evidence_for_empty_field),
    ("evidence_value_mismatch", lambda t: ev(t, "n_enrolled").update(value="61")),
    ("quote_missing", lambda t: ev(t, "n_enrolled").update(quote="")),
    ("location_missing", lambda t: ev(t, "Summary").update(location="")),
    ("quote_too_long", lambda t: ev(t, "n_enrolled").update(quote="x" * 501)),
    ("not_applicable_filled", _retrospective_randomised),
    ("not_applicable_filled", lambda t: t["studies"][0].update(not_applicable="randomised")),
]


@pytest.mark.parametrize("check,break_it", CASES, ids=[c for c, _ in CASES])
def test_each_error_is_caught(check, break_it):
    t = fixture.clean(CB)
    break_it(t)
    assert check in checks(t)


def test_truncated_quote_is_allowed():
    t = fixture.clean(CB)
    ev(t, "n_enrolled").update(quote="x" * 501, quote_truncated="yes")
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
    assert "wrong_source" in checks(t, source_id="PMID99999901")   # FIXTURE R2 is from FDA
    t["results"].pop()
    assert checks(t, source_id="PMID99999901") == set()
    t2 = copy.deepcopy(t)
    t2["study_sources"] = [r for r in t2["study_sources"] if r["source_id"] != "PMID99999901"]
    t2["study_sources"][0]["role"] = "primary"
    assert "unlinked_source" in checks(t2, source_id="PMID99999901")


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
