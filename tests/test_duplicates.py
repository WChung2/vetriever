"""Possible duplicates: suggestions for a person, never decisions."""
from tests import fixture
from tests.test_store import second_study, write_any
from vet.codebook import Codebook
from vet.duplicates import _person, _same_person, possible_duplicates
from vet.store import Store

CB = Codebook()


def checks(t):
    return [i.check for i in possible_duplicates(t)]


def test_the_clean_fixture_has_none():
    assert checks(fixture.clean(CB)) == []


def test_a_row_repeated_under_a_new_key():
    t = fixture.clean(CB)
    t["results"].append({**t["results"][0], "result_id": f"{fixture.SID}-R3"})
    assert checks(t) == ["duplicate_row"]


def test_breeds_with_the_same_count_are_not_duplicates():
    t = fixture.clean(CB)
    t["breeds"].append({**t["breeds"][0], "Breed": "Beagle"})       # also 20 dogs
    assert checks(t) == []


def test_names_match_whatever_their_order_or_titles():
    assert _same_person(_person("Watari Toshihiro"), _person("Toshihiro Watari, DVM"))
    assert _same_person(_person("Steven A. Martinez"), _person("Steven Martinez, DVM, MS, DACVS"))
    assert not _same_person(_person("John Smith"), _person("John Brown"))


def test_two_documents_with_the_same_title_and_year():
    t = fixture.clean(CB)
    t["sources"].append({**t["sources"][0], "source_id": "PMID99999909", "doi": "", "pmid": "",
                         "title": "Fixture -- fictional analgesic TRIAL"})
    assert checks(t) == ["possible_same_document"]


def test_one_trial_reported_in_two_documents(tmp_path):
    db = Store(tmp_path / "test.db", CB)
    db.import_folder(write_any(fixture.clean(CB), tmp_path / "a"))
    db.import_folder(write_any(second_study(fixture.clean(CB)), tmp_path / "b"))
    found = [i for i in possible_duplicates(db.tables()) if i.check == "possible_same_trial"]
    assert [i.key for i in found] == ["VET-1|VET-2"]
    assert "both enrolled 60" in found[0].message and "analgesic" in found[0].message


def test_different_drugs_are_not_a_match(tmp_path):
    other = second_study(fixture.clean(CB))
    for r in other["arm_doses"]:
        r["drug"] = "meloxicam"
    db = Store(tmp_path / "test.db", CB)
    db.import_folder(write_any(fixture.clean(CB), tmp_path / "a"))
    db.import_folder(write_any(other, tmp_path / "b"))
    assert "possible_same_trial" not in checks(db.tables())
