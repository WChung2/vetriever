"""The table benchmark's key, hidden checks and scoring, on made-up tables only."""
import random

import pytest

from vet import tablebench as tb

JATS = """<article><body><sec><p>Text 99.1.</p>
<table-wrap><label>Table 1</label><caption><p>Doses and results</p></caption>
<table><thead>
<tr><th rowspan="2">Variable</th><th colspan="2">Treated</th><th>Control</th></tr>
<tr><th>Day 0</th><th>Day 7</th><th>Day 7</th></tr>
</thead><tbody>
<tr><td colspan="4">Blood</td></tr>
<tr><td>PCV (%)</td><td>21.5</td><td>30.2</td><td>28.8</td></tr>
<tr><td>Platelets</td><td>3<break/>8</td><td></td><td>131</td></tr>
<tr><td rowspan="2">Sex</td><td>Male</td><td>12</td><td>9</td></tr>
<tr><td>Female</td><td>14 (54%)</td><td>P &lt; .05</td></tr>
</tbody></table>
<table-wrap-foot><p>Note 77.7</p></table-wrap-foot></table-wrap>
<table-wrap><label>Table 2</label><table><tr><th>Only</th></tr><tr><td>one</td></tr></table>
</table-wrap>
<table-wrap><label>Table 3</label><graphic/></table-wrap>
</sec></body></article>"""


@pytest.fixture
def table(tmp_path):
    f = tmp_path / "paper.xml"
    f.write_text(JATS)
    tables = tb.read_tables(f)
    assert len(tables) == 1           # no numbers in Table 2, no cells in the picture Table 3
    return tables[0]


def by_value(t, value):
    return next(c for c in t.cells if c.value == value)


def test_merged_header_cells_reach_every_column(table):
    assert table.head[0] == ["Variable", "Treated", "Treated", "Control"]
    assert by_value(table, "30.2").header == "Treated › Day 7"
    assert by_value(table, "28.8").header == "Control › Day 7"


def test_a_section_row_labels_the_rows_below_it(table):
    assert by_value(table, "21.5").label == "Blood › PCV (%)"
    assert not any(c.value == "Blood" for c in table.cells)


def test_a_line_break_in_a_cell_keeps_two_values_apart(table):
    assert by_value(table, "3 / 8").col == 1
    assert "38" not in [c.value for c in table.cells]


def test_a_blank_cell_keeps_its_column(table):
    assert by_value(table, "131").col == 3
    assert table.body[2][2] == ""


def test_a_merged_row_label_reaches_both_rows_and_words_join_the_label(table):
    assert by_value(table, "9").label == "Blood › Sex › Male"
    assert by_value(table, "14 (54%)").label == "Blood › Sex › Female"


def test_the_draw_is_the_same_every_time_and_capped(table):
    assert [c.id for c in tb.picked("p", table)] == [c.id for c in tb.picked("p", table)]
    assert len(tb.picked("p", table, n=3)) == 3
    assert len(tb.picked("p", table)) == len(table.cells)


def test_the_region_is_the_table_not_the_prose_around_it(table):
    prose = "\n".join(f"Line {i} of the methods section." for i in range(80))
    output = f"{prose}\n{tb.clean_copy(table)}\n{prose}"
    region = tb.find_region(output, table)
    assert "21.5" in region and "Table 1" in region and "methods section" not in region[:40]
    assert region.count("Line") <= 2


def test_a_table_a_reader_lost_has_no_region(table):
    assert tb.find_region("Nothing here but 12 words and 9 more.", table) == ""
    assert tb.find_region("", table) == ""


def test_an_html_table_comes_one_row_per_line_and_keeps_its_spans():
    out = tb.tidy('<table class="x"><tr><td colspan="2" style="a">1<br>2</td></tr>'
                  "<tr><td>3</td></tr></table>")
    assert out.split("\n")[0] == '<table><tr><td colspan="2">1 / 2</td></tr>'


def test_a_less_than_sign_is_data_not_a_tag():
    text = "Lysis <0.0001 and 3.5\nnext line 9.9\nn >10 dogs"
    assert tb.tidy(text) == text


def test_a_whole_structured_table_is_taken_and_not_the_paper_around_it(table):
    prose = "A long paragraph with 21.5 and 30.2 and 131 in it. " * 40
    html = ("<table><tr><th>Variable</th><th>Day 0</th></tr>"
            + "".join(f"<tr><td>{c.label}</td><td>{c.value}</td></tr>" for c in table.cells)
            + "</table>")
    region = tb.find_region(f"{prose}\n\nTable 1. Doses\n{html}\n\n{prose}", table)
    assert region.startswith("Table 1") and region.endswith("</table>")
    assert "paragraph" not in region


def test_padding_in_a_pipe_table_is_dropped():
    assert tb.tidy("|  a     |   12 |\n|------|-----|") == "| a | 12 |\n|---|---|"


def test_a_table_split_over_two_pages_comes_back_whole(table):
    half = len(table.cells) // 2
    rows = [f"| {c.label} | {c.value} |" for c in table.cells]
    output = "\n".join([*rows[:half], "Page footer", "", *rows[half:], "", "End of it"])
    region = tb.find_region(output, table)
    assert all(c.value in region for c in table.cells)


def long_table(rows):
    body = [[f"Item {chr(97 + i % 26)}{i}", f"{100 + i}.5"] for i in range(rows)]
    t = tb.Table(1, "Table 1", "Long", [["Item", "Value"]], body)
    t.cells = tb._number_cells(t)
    return t


def plain(t, rows):
    return [f"{t.body[i][0]} {t.body[i][1]}" for i in rows]


def filler(n):
    return [f"Words on the next page, line {i}." for i in range(5000, 5000 + n)]


def test_a_long_plain_table_runs_on_over_a_page(table):
    t = long_table(24)
    closing = ["Closing words of the paper."] * 60
    lines = [*plain(t, range(12)), *filler(90), *plain(t, range(12, 24)), *closing]
    region = tb.find_region("\n".join(lines), t)
    assert all(c.value in region for c in t.cells)
    assert region.count("Closing words") <= 2


def test_the_first_part_of_a_long_table_is_not_lost_when_the_second_is_bigger(table):
    t = long_table(24)
    lines = [*plain(t, range(6)), *filler(100), *plain(t, range(6, 24))]
    region = tb.find_region("\n".join(lines), t)
    assert all(c.value in region for c in t.cells)


def test_stray_numbers_in_plain_prose_do_not_stretch_the_region():
    t = long_table(6)
    before = [f"The methods, line {i}." for i in range(5000, 5040)]
    after = [f"As seen earlier, {t.body[i % 6][1]} was noted on line {5000 + i}." for i in range(30)]
    region = tb.find_region("\n".join([*before, "Table 1 Long", *plain(t, range(6)), *after]), t)
    assert all(c.value in region for c in t.cells)
    assert region.count("As seen earlier") <= 2


def test_a_less_than_sign_inside_an_html_table_stays():
    out = tb.tidy("<table><tr><td>P <0.05</td><td>n >10 of <3 dogs</td></tr></table>")
    assert "P <0.05" in out and "n >10 of <3 dogs" in out


def test_the_draw_stays_the_same_when_only_some_cells_are_checked(table):
    draws = {tuple(c.id for c in tb.picked("p", table, n=3)) for _ in range(20)}
    assert len(draws) == 1
    assert {c.id for c in tb.picked("p", table, n=3)} != {c.id for c in tb.picked("q", table, n=3)}


def test_the_clean_copy_holds_every_value(table):
    copy = tb.clean_copy(table)
    assert all(c.value in copy for c in table.cells)


def test_damage_is_real_and_reported(table):
    cells = tb.picked("p", table)
    text, planted = tb.damaged_copy("p", table, cells)
    assert planted and set(planted.values()) <= {"wrong", "missing"}
    assert text != tb.clean_copy(table)
    assert set(planted) <= {c.id for c in cells}
    assert tb.damaged_copy("p", table, cells) == (text, planted)


def test_every_kind_of_damage_turns_up():
    rng = random.Random(1)
    assert tb._digit("12.5", rng) != "12.5"
    assert tb._symbol("P < .05") == "P > .05"
    assert tb._symbol("3.1 ± 0.2") == "3.1 + 0.2"
    assert tb._symbol("12") is None


def test_packets_hide_who_is_who(table):
    shown, sealed = tb.packets("p", [table], {"reader-a": tb.clean_copy(table), "reader-b": ""})
    packet = shown[0]
    assert set(packet["entries"]) == {"A", "B", "C"}
    assert "reader" not in str(packet) and "check-" not in str(packet)
    info = sealed["p-1"]
    assert sorted(info["who"].values()) == sorted([tb.CLEAN, tb.DAMAGED, "reader-a"])
    assert info["absent"] == ["reader-b"]


def result_for(table, wrong=None):
    _, sealed = tb.packets("p", [table], {"reader-a": tb.clean_copy(table)})
    who = {name: letter for letter, name in sealed["p-1"]["who"].items()}
    marks = {"p-1": {who[tb.DAMAGED]: dict(sealed["p-1"]["planted"])}}
    if wrong:
        marks["p-1"][who["reader-a"]] = {wrong: "wrong"}
    return tb.score(sealed, marks), sealed


def test_scoring_counts_a_cell_not_listed_as_correct(table):
    cell = tb.picked("p", table)[0].id
    result, _ = result_for(table, cell)
    assert result["totals"]["reader-a"]["wrong"] == 1
    assert result["totals"]["reader-a"]["correct"] == len(table.cells) - 1


def test_a_table_nobody_judged_stops_the_scoring(table):
    _, sealed = tb.packets("p", [table], {"reader-a": "x"})
    with pytest.raises(ValueError, match="not been judged"):
        tb.score(sealed, {})


def test_an_unknown_mark_stops_the_scoring(table):
    _, sealed = tb.packets("p", [table], {})
    letter = next(iter(sealed["p-1"]["who"]))
    cell = sealed["p-1"]["cells"][0]
    with pytest.raises(ValueError, match="unknown mark"):
        tb.score(sealed, {"p-1": {letter: {cell: "maybe"}}})


def test_a_reader_that_lost_a_table_scores_missing_for_every_cell(table):
    _, sealed = tb.packets("p", [table], {"reader-b": None})
    result = tb.score(sealed, {"p-1": {}})
    assert result["totals"]["reader-b"]["missing"] == len(table.cells)
    assert result["totals"]["reader-b"]["correct"] == 0


def test_the_checks_pass_only_for_a_judge_that_caught_the_planted_errors(table):
    result, _ = result_for(table)
    assert tb.checks_pass(result)[0]
    _, sealed = tb.packets("p", [table], {})
    lazy = tb.score(sealed, {"p-1": {}})
    assert not tb.checks_pass(lazy)[0]


def test_the_checks_fail_a_judge_that_marks_the_clean_copy_down(table):
    _, sealed = tb.packets("p", [table], {})
    who = {name: letter for letter, name in sealed["p-1"]["who"].items()}
    harsh = {who[tb.DAMAGED]: dict(sealed["p-1"]["planted"]),
             who[tb.CLEAN]: {sealed["p-1"]["cells"][0]: "wrong"}}
    assert not tb.checks_pass(tb.score(sealed, {"p-1": harsh}))[0]


def test_the_gain_compares_the_same_papers():
    def papers(correct):
        return {f"p{i}": tb.Counter(correct=correct, wrong=100 - correct) for i in range(10)}
    result = {"papers": {"new": papers(95), "old": papers(90)}}
    gain, low, high = tb.paired_gain(result, "new", "old", rounds=200)
    assert (gain, low, high) == (5, 5, 5)
