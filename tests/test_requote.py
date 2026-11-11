"""Stored quotes realigned with a re-read text: layout only, never words. Made-up text only."""
import sqlite3

from tests.test_extract import CB, DOC, TEXT, extract
from vet.requote import realign, requote
from vet.validate import validate

NEW = ("Length of hospitalization (days) | 8 (4–17) / n = 14 | 8 (4–13) / n = 12 |\n"
       "Platelets | 3 / 8 | 12 |\nRescue | 18 (4–17) |\n")


def test_a_cell_split_over_lines_is_found_with_its_new_separators():
    old = "Length of hospitalization (days) |\n8 (4–17)\n\nn = 14\n|\n8 (4–13)"
    assert realign(NEW, old) == "Length of hospitalization (days) | 8 (4–17) / n = 14 | 8 (4–13)"


def test_a_number_the_old_reader_ran_together_never_matches():
    assert realign(NEW, "Platelets | 38 | 12") is None


def test_a_quote_never_matches_inside_a_longer_number():
    assert realign("Rescue | 18 (4–17) |", "8 (4–17)") is None


def test_a_quote_never_matches_part_of_a_decimal_but_may_end_a_sentence():
    assert realign("mean 0.5 (0.1–0.9) days", "5 (0.1–0.9)") is None
    assert realign("dose 5.2 mg", "dose 5") is None
    assert realign("It was 5. Then", "It was 5") == "It was 5"


def test_a_quote_found_twice_is_left_alone():
    assert realign("a 1 b\na 1 b\n", "a 1 b") is None


def test_a_slash_inside_a_value_stays_part_of_it():
    assert realign("BUN | 2/7 | 0/2 |", "BUN | 2/7 | 0/2") == "BUN | 2/7 | 0/2"
    assert realign("BUN | 2 / 7 | 0/2 |", "BUN | 2/7 | 0/2") is None
    assert realign("BUN | 2/7 |", "BUN | 2 7") is None          # two numbers are not a fraction


def test_joined_passages_are_each_found():
    text = "First part here. Other words. Second part | 4 / 5 |"
    assert realign(text, "First part here … Second part | 4 5") == "First part here … Second part | 4 / 5"


def test_requote_fixes_a_quote_whose_layout_changed_and_leaves_the_rest(tmp_path):
    _, store, _ = extract(tmp_path)
    store.close()
    db = tmp_path / "v.db"
    path = tmp_path / "docs" / f"{DOC}.txt"
    path.write_text(TEXT.replace("Sixty client-owned dogs", "Sixty client-owned / dogs")
                        .replace("Mixed breed dogs (n = 20)", "Mixed breed dogs (n = 21)"), "utf-8")
    con = sqlite3.connect(db)
    values = con.execute("SELECT value FROM evidence ORDER BY rowid").fetchall()
    con.close()
    done = requote(db, tmp_path, CB)
    assert done == {"realigned": 1, "left": 1}          # the changed number stays flagged
    con = sqlite3.connect(db)
    quotes = [q for (q,) in con.execute("SELECT quote FROM evidence")]
    assert "Sixty client-owned / dogs" in quotes
    assert con.execute("SELECT value FROM evidence ORDER BY rowid").fetchall() == values
    con.close()
    from vet.store import Store
    s = Store(db, CB)
    left = [i for i in validate(s.tables(), CB, root=tmp_path) if "document" in i.check]
    s.close()
    assert len(left) == 1 and "20" in left[0].message
