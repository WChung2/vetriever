"""The golden test's draw, comparison and scoring, on made-up tables only."""
import pytest

from vet import golden as g
from vet import tablebench as tb


def table(n=1):
    t = tb.Table(n, f"Table {n}", "Made up",
                 [["Variable", "Treated", "Treated", "Control"],
                  ["Variable", "Day 0", "Day 7", "Day 7"]],
                 [["Blood", "Blood", "Blood", "Blood"],
                  ["PCV", "21.5", "30.2", "28.8"],
                  ["Platelets", "", "131", "140"],
                  ["Glucose", "4.1 ± 0.2", "", "P < .05"],
                  ["Sodium", "141", "142", "143"]])
    t.cells = tb._number_cells(t)
    return t


def cell(t, value):
    return next(c for c in t.cells if c.value == value)


def test_a_blank_cell_is_a_cell_with_its_row_and_column():
    found = {(c.label, c.header) for c in g.blanks(table())}
    assert ("Blood › Platelets", "Treated › Day 0") in found
    assert ("Blood › Glucose", "Treated › Day 7") in found


def test_a_row_with_no_numbers_gives_no_blank():
    t = table()
    t.body.append(["Note", "", "", ""])
    assert not any(c.label == "Note" for c in g.blanks(t))


def test_a_column_that_never_holds_numbers_gives_no_blank():
    t = tb.Table(1, "Table 1", "Made up", [["Variable", "Note", "Value"]],
                 [["PCV", "", "21.5"], ["Platelets", "low", "131"]])
    t.cells = tb._number_cells(t)
    assert not g.blanks(t)


def test_kinds_come_from_the_table_alone():
    t = table()
    assert g.kind_of(t, cell(t, "131")) == "after_blank"
    assert g.kind_of(t, cell(t, "4.1 ± 0.2")) == "symbol"
    assert g.kind_of(t, cell(t, "21.5")) == "deep"
    assert g.kind_of(t, g.blanks(t)[0]) == "blank"


def test_a_value_after_a_blank_outranks_its_symbol():
    t = table()
    assert g.kind_of(t, cell(t, "P < .05")) == "after_blank"


def test_a_leading_minus_is_a_symbol_and_a_range_is_not():
    assert g.SYMBOL.search("-16.4") and g.SYMBOL.search("−3")
    assert not g.SYMBOL.search("12-34")


def test_the_draw_is_fixed_and_covers_each_kind_the_table_has():
    a, b = g.draw("p", [table()]), g.draw("p", [table()])
    assert a == b
    assert {"blank", "after_blank", "symbol", "deep"} <= {q.kind for q in a}


def no_blanks(n):
    t = tb.Table(n, f"Table {n}", "Made up", [["Variable", "A", "B"]],
                 [[f"Row {i}", str(10 + i), str(20 + i)] for i in range(9)])
    t.cells = tb._number_cells(t)
    return t


def test_the_draw_caps_tables_and_facts():
    out = g.draw("p", [no_blanks(i) for i in range(1, 6)])
    assert len({q.table for q in out}) == g.TABLES_PER_PAPER
    assert len(out) == g.TABLES_PER_PAPER * g.FACTS_PER_TABLE


def test_a_table_with_few_tables_gives_more_facts():
    assert len(g.draw("p", [no_blanks(1)])) == g.FACTS_PER_TABLE_FEW


def test_a_table_with_blanks_is_always_chosen():
    plain = [no_blanks(i) for i in range(1, 8)]
    assert "Table 8" in {q.table for q in g.draw("p", [*plain, table(8)])}


def test_a_table_with_blanks_gives_its_traps_and_still_two_others():
    kinds = [q.kind for q in g.draw("p", [table()])]
    assert kinds.count("blank") == 2 and kinds.count("after_blank") == 3
    assert len(kinds) == 7


def test_a_question_names_exactly_one_cell():
    t = table()
    t.body.append(["PCV", "1", "2", "3"])          # a second row with the same label
    t.cells = tb._number_cells(t)
    assert not any(q.row == "Blood › PCV" for q in g.draw("p", [t]))


def test_a_cell_with_no_row_label_or_no_header_is_never_asked():
    t = table()
    t.body.append(["", "7", "8", "9"])
    t.head[0][3] = t.head[1][3] = ""
    t.cells = tb._number_cells(t)
    questions = g.draw("p", [t])
    assert all(q.row and q.column for q in questions)


def test_a_blind_question_hides_the_answer_and_the_kind():
    q = g.draw("p", [table()])[0]
    assert set(q.blind()) == {"id", "table", "row", "column"}


@pytest.mark.parametrize("a, b", [("1,200", "1200"), ("3 ± 0.2", "3±0.2"), ("−5", "-5"),
                                  ("12–34", "12-34"), ("7·4", "7.4"), ("10 µg", "10 μg")])
def test_printing_differences_do_not_count(a, b):
    assert g.normal(a) == g.normal(b)


@pytest.mark.parametrize("a, b", [("1.7", "1.1"), ("P < .05", "P > .05"), ("3 ± 0.2", "3 + 0.2"),
                                  ("12", "12.0"), ("-5", "5"), ("1,20", "120")])
def test_a_real_difference_always_counts(a, b):
    assert g.normal(a) != g.normal(b)


def test_compare_sends_every_planted_error_to_a_ruling():
    questions = g.draw("p", [table()])
    entries = {q.id: q.key for q in questions}
    wrong = questions[-1]
    entries[wrong.id] = wrong.key + "1"
    agreed, differ = g.compare(questions, entries)
    assert [q for q, _ in differ] == [wrong] and len(agreed) == len(questions) - 1


def test_a_blank_read_as_a_value_goes_to_a_ruling():
    q = g.Question("x", "p", "Table 1", "r", "c", "blank", "")
    assert g.compare([q], {"x": "12"})[1] and g.compare([q], {"x": ""})[0]


def facts():
    return {"a": {"paper": "p1", "kind": "plain", "truth": "1.7"},
            "b": {"paper": "p1", "kind": "blank", "truth": ""},
            "c": {"paper": "p2", "kind": "symbol", "truth": "P < .05"}}


def test_scoring_a_perfect_route():
    r = g.score(facts(), {"x": {"a": "1.7", "b": "NOT REPORTED", "c": "P<.05"}}, {})
    assert r["totals"]["x"] == {"correct": 3}


def test_a_value_claimed_for_a_blank_cell_is_wrong():
    assert g.mark("", "12", {}) == "wrong"
    assert g.mark("", "not reported", {}) == "correct"


def test_saying_not_reported_for_a_printed_value_is_missing_not_wrong():
    assert g.mark("1.7", "NOT REPORTED", {}) == "missing"


def test_a_different_answer_stops_until_it_has_a_ruling():
    with pytest.raises(ValueError, match="needs a ruling"):
        g.mark("1.7", "1.1", {})


def test_a_ruling_is_on_the_words_so_it_cannot_favour_a_route():
    key = f"{g.normal('1.7a')}|{g.normal('1.7')}"
    r = g.score({"a": {"paper": "p", "kind": "plain", "truth": "1.7a"}},
                {"x": {"a": "1.7"}, "y": {"a": "1.7"}}, {key: "correct"})
    assert r["totals"]["x"] == r["totals"]["y"] == {"correct": 1}


def test_an_unknown_ruling_stops_the_scoring():
    with pytest.raises(ValueError, match="unknown ruling"):
        g.mark("1.7", "1.1", {f"{g.normal('1.7')}|{g.normal('1.1')}": "maybe"})


def test_a_fact_a_route_left_out_is_missing_even_for_a_blank():
    r = g.score(facts(), {"x": {}}, {})
    assert r["totals"]["x"] == {"missing": 3}


def test_counts_add_up_by_paper_and_kind():
    r = g.score(facts(), {"x": {"a": "1.7", "b": "9", "c": "NOT REPORTED"}}, {})
    assert r["papers"]["x"]["p1"] == {"correct": 1, "wrong": 1}
    assert r["kinds"]["x"]["symbol"] == {"missing": 1}
