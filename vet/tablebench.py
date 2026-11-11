"""The table reader benchmark: answer key, blind packets, hidden checks and scoring.

The question: which reader puts table numbers in the right row and column? A paper's PubMed
Central XML is the answer key, because it holds each table as cells.

    read_tables   the XML's tables as grids, merged cells filled in, line breaks kept
    picked        up to 20 number cells per table, the same ones for every reader
    find_region   the part of a reader's output that holds a table
    clean_copy    the key written out as a reader would: a hidden check the judge must pass
    damaged_copy  the same with planted errors: a hidden check the judge must catch
    packets       what a judge sees: tables and outputs, reader names hidden, order shuffled
    score         marks back to readers: correct, wrong and missing per reader

Papers, keys, outputs and marks stay in corpus/tablebench/. Nothing here calls a service.
"""
from __future__ import annotations

import random
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

CELLS_PER_TABLE = 20
SEED = "vetriever-tablebench-2026-11-05"
BREAK = " / "                     # a line break inside a cell: two values, not one
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?|(?<![\w\d])\.\d+")
CAPTION = re.compile(r"^\W*(?:table|tab\.?)\s*(\d+)\b", re.IGNORECASE)
HTML_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b([^<>]*)>")
SPAN = re.compile(r"\b(colspan|rowspan)\s*=\s*[\"']?(\d+)")
MARKS = ("correct", "wrong", "missing")
CLEAN, DAMAGED = "check-clean", "check-damaged"
PLANTED_RATE = 0.4
PAGE_GAP = 40                     # lines of prose a table may jump over at a page break


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


@dataclass
class Cell:
    id: str
    row: int
    col: int
    label: str
    header: str
    value: str


@dataclass
class Table:
    n: int
    label: str
    caption: str
    head: list[list[str]]
    body: list[list[str]]
    foot: str = ""
    cells: list[Cell] = field(default_factory=list)


def _text(el) -> str:
    """A cell's words: a line break or a second paragraph becomes BREAK, never nothing."""
    out = [el.text or ""]
    for i, kid in enumerate(el):
        if _local(kid) in ("break", "br") or (_local(kid) == "p" and i > 0):
            out.append(BREAK)
        out.append(_text(kid))
        out.append(kid.tail or "")
    return "".join(out)


def _clean(s: str) -> str:
    s = " ".join(unicodedata.normalize("NFC", s).split())
    return re.sub(rf"^(?:{re.escape(BREAK.strip())}\s*)+|(?:\s*{re.escape(BREAK.strip())})+$", "",
                  s).strip()


def _grid(rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """Rows of (text, colspan, rowspan) into a full grid, merged cells repeated."""
    out, pending = [], {}
    for cells in rows:
        line, col, queue = {}, 0, list(cells)
        while queue or any(c >= col for c in pending):
            if col in pending:
                left, text = pending[col]
                line[col] = text
                if left > 1:
                    pending[col] = (left - 1, text)
                else:
                    del pending[col]
                col += 1
            elif queue:
                text, colspan, rowspan = queue.pop(0)
                for _ in range(colspan):
                    line[col] = text
                    if rowspan > 1:
                        pending[col] = (rowspan - 1, text)
                    col += 1
            else:
                col += 1
        out.append([line.get(i, "") for i in range(max(line) + 1)] if line else [])
    return out


def _span(el, name: str) -> int:
    try:
        return max(1, int(el.get(name, "1")))
    except ValueError:
        return 1


def _rows(el) -> list[list[tuple[str, int, int]]]:
    return [[(_clean(_text(c)), _span(c, "colspan"), _span(c, "rowspan"))
             for c in tr if _local(c) in ("td", "th")]
            for tr in el.iter() if _local(tr) == "tr"]


def read_tables(path: Path) -> list[Table]:
    """Every table in a JATS file that has cells and at least two rows holding numbers."""
    tables = []
    for wrap in (e for e in ET.parse(path).getroot().iter() if _local(e) == "table-wrap"):
        table = next((k for k in wrap.iter() if _local(k) == "table"), None)
        if table is None:
            continue
        head_el = next((k for k in table.iter() if _local(k) == "thead"), None)
        head_rows = _rows(head_el) if head_el is not None else []
        all_rows = _rows(table)
        body_rows = all_rows[len(head_rows):]
        if not head_rows and body_rows:
            head_rows, body_rows = body_rows[:1], body_rows[1:]
        grid = _grid(head_rows + body_rows)
        width = max((len(r) for r in grid), default=0)
        grid = [r + [""] * (width - len(r)) for r in grid]
        t = Table(n=0, label="", caption="", head=grid[:len(head_rows)],
                  body=grid[len(head_rows):])
        for kid in wrap:
            if _local(kid) == "label":
                t.label = _clean(_text(kid))
            elif _local(kid) == "caption":
                t.caption = _clean(_text(kid))
            elif _local(kid) == "table-wrap-foot":
                t.foot = _clean(_text(kid))
        t.cells = _number_cells(t)
        if len({c.row for c in t.cells}) >= 2:
            t.n = len(tables) + 1
            tables.append(t)
    return tables


def _is_subheading(row: list[str]) -> bool:
    filled = [c for c in row if c]
    return bool(filled) and len(set(filled)) == 1 and (not row[0] or len(filled) < len(row)
                                                       or len(filled) > 1)


def _number_cells(t: Table) -> list[Cell]:
    """A table's cells that hold a number, each with its row label and column header."""
    cells, section = [], ""
    for r, row in enumerate(t.body):
        if not any(row):
            continue
        if _is_subheading(row):
            section = next(c for c in row if c)
            continue
        end = 1                    # the first column is a label; so are wordy ones after it
        while end < len(row) and row[end] and not re.search(r"\d", row[end]):
            end += 1
        label = " › ".join(x for x in [section, *dict.fromkeys(row[:end])] if x)
        for c in range(end, len(row)):
            if re.search(r"\d", row[c]):
                stack = dict.fromkeys(h[c] for h in t.head if c < len(h) and h[c])
                cells.append(Cell(f"{t.n or 0}.{r}.{c}", r, c, label, " › ".join(stack), row[c]))
    return cells


def picked(paper: str, t: Table, n: int = CELLS_PER_TABLE) -> list[Cell]:
    """The cells checked in this table: a fixed draw, the same for every reader."""
    cells = list(t.cells)
    random.Random(f"{SEED}/{paper}/{t.label}/{t.caption[:40]}").shuffle(cells)
    return sorted(cells[:n], key=lambda c: (c.row, c.col))


def _numbers(s: str) -> list[str]:
    s = unicodedata.normalize("NFKC", s).replace("·", ".")
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)
    out = []
    for m in NUMBER.findall(s):
        m = "0" + m if m.startswith(".") else m
        out.append(m.rstrip("0").rstrip(".") if "." in m else m)
    return out


def _html_rows(m: re.Match) -> str:
    def tag(t):
        close, name, attrs = t.groups()
        if name.lower() == "br":
            return BREAK
        if name.lower() not in ("table", "tr", "td", "th", "thead", "tbody"):
            return ""
        spans = "".join(f' {a.lower()}="{v}"' for a, v in SPAN.findall(attrs) if v != "1")
        end = "\n" if close and name.lower() in ("tr", "table") else ""
        return f"<{close}{name.lower()}{spans}>{end}"
    return TAG.sub(tag, " ".join(m.group().split()))


def tidy(output: str) -> str:
    """HTML tables one row per line, keeping only what places a cell; pipe rows without padding.

    Touches nothing outside a table: "P <0.05 and n >10" is data, not a tag.
    """
    lines = []
    for line in HTML_TABLE.sub(_html_rows, output).split("\n"):
        if line.lstrip().startswith("|"):
            line = re.sub(r"<br\s*/?>", BREAK, line, flags=re.IGNORECASE)
            line = re.sub(r"-{3,}", "---", re.sub(r"\s{2,}", " ", line.strip()))
        lines.append(line)
    return "\n".join(lines)


def _last_new(span, hits: list[set], seen=()) -> int:
    """The last line in span to add a number not seen before it."""
    seen, last = set(seen), span[0]
    for i in span:
        if hits[i] - seen:
            last = i
        seen |= hits[i]
    return last


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Runs of lines that are a table already: HTML rows or pipe rows."""
    out, start = [], None
    for i, line in enumerate([*lines, ""]):
        is_row = line.lstrip().startswith(("|", "<table", "<tr", "<thead", "<tbody", "</t"))
        if is_row and start is None:
            start = i
        elif not is_row and start is not None:
            out.append((start, i))
            start = None
    return out


def _caption_above(lines: list[str], first: int, reach: int) -> int:
    """Where the region starts: the table's caption if it is near, or a few lines above."""
    for i in range(first, max(first - reach, -1), -1):
        if CAPTION.match(lines[i].strip()):
            return i
    return max(first - reach // 2, 0)


def _from_blocks(lines: list[str], hits: list[set], enough: int) -> tuple[str, int]:
    """The table block holding the most numbers, with any block that continues it."""
    blocks = sorted(_blocks(lines), key=lambda b: -len(set().union(*hits[b[0]:b[1]])))
    if not blocks:
        return "", 0
    start, end = blocks[0]
    got = set().union(*hits[start:end])
    if len(got) < enough:
        return "", 0
    for other_start, other_end in blocks[1:]:          # a table split over two pages
        new = set().union(*hits[other_start:other_end]) - got
        if len(new) >= max(len(got) // 5, 2) and min(abs(other_start - end),
                                                     abs(start - other_end)) <= PAGE_GAP:
            start, end = min(start, other_start), max(end, other_end)
            got |= new
    return "\n".join(lines[_caption_above(lines, start, 4):end]), len(got)


def find_region(output: str, t: Table, margin: int = 2) -> str:
    """The part of a reader's output that holds this table, or nothing.

    A structured table is taken whole. Otherwise it is the stretch of lines with the most of
    the table's numbers, starting at its caption if that holds as many, so the column headers
    come with it. Finds nothing if under a fifth of the numbers are there.
    """
    wanted = set()
    for c in t.cells:
        wanted.update(_numbers(c.value))
    if not wanted:
        return ""
    lines = tidy(output).split("\n")
    hits = [set(_numbers(line)) & wanted for line in lines]
    enough = 0.2 * len(wanted)
    size = max(len(t.body) * 4 + 12, 30)
    number = re.search(r"\d+", t.label or "")

    def seen(start: int) -> int:
        return len(set().union(*hits[start:start + size]))

    best = max(range(len(lines)), key=seen)
    if number:
        for i, line in enumerate(lines):
            m = CAPTION.match(line.strip())
            if m and m.group(1) == number.group() and seen(i) >= seen(best):
                best = i
                break
    block, found = _from_blocks(lines, hits, enough)
    if block and found * 1.5 >= seen(best):
        return block.strip()
    if seen(best) < enough:
        return ""
    last = _last_new(range(best, min(best + size, len(lines))), hits)
    first = _last_new(range(last, best - 1, -1), hits)  # stray numbers around it add nothing
    got = set().union(*hits[first:last + 1])
    grown = True
    while grown:                                        # a long table runs over a page
        grown = False
        for span in (range(last + 1, min(last + 1 + size, len(lines))),
                     range(first - 1, max(first - 1 - size, -1), -1)):
            more = set().union(*(hits[i] for i in span)) - got
            if len(more) >= max(len(wanted) // 5, 2):
                edge = _last_new(span, hits, got)
                first, last = min(first, edge), max(last, edge)
                got |= more
                grown = True
    return "\n".join(lines[_caption_above(lines, first, 12):last + 1 + margin]).strip()


def _render(t: Table, body: list[list[str]]) -> str:
    def line(row):
        return "| " + " | ".join(row) + " |"
    rows = [line(r) for r in t.head]
    rule = "|" + " --- |" * (len(t.head[0]) if t.head else 1)
    return "\n".join([f"{t.label} {t.caption}".strip(), *rows, rule, *(line(r) for r in body)])


def clean_copy(t: Table) -> str:
    """The key itself, written as a reader would. A fair judge marks all of it correct."""
    return _render(t, t.body)


def _digit(value: str, rng: random.Random) -> str:
    at = [i for i, ch in enumerate(value) if ch.isdigit()]
    i = rng.choice(at)
    new = rng.choice([d for d in "0123456789" if d != value[i]])
    return value[:i] + new + value[i + 1:]


def _symbol(value: str) -> str | None:
    for old, new in (("<", ">"), (">", "<"), ("≤", "≥"), ("≥", "≤"), ("±", "+"), ("−", ""),
                     ("-", "+"), ("%", "‰")):
        if old in value:
            return value.replace(old, new, 1)
    return None


def damaged_copy(paper: str, t: Table, cells: list[Cell]) -> tuple[str, dict[str, str]]:
    """The key with errors planted in some checked cells: the text, and what each should get.

    digit: one digit changed. symbol: a sign changed. swap: two values in a row trade places.
    blank: the value removed. All but blank are "wrong"; blank is "missing".
    """
    rng = random.Random(f"{SEED}/damage/{paper}/{t.label}/{t.caption[:40]}")
    body = [list(r) for r in t.body]
    targets = rng.sample(cells, max(2, round(len(cells) * PLANTED_RATE))) if cells else []
    planted: dict[str, str] = {}
    touched = set()
    for c in targets:
        if (c.row, c.col) in touched:
            continue
        kinds = ["digit", "blank", "swap", "symbol"]
        rng.shuffle(kinds)
        for kind in kinds:
            if kind == "digit":
                body[c.row][c.col] = _digit(c.value, rng)
                planted[c.id] = "wrong"
            elif kind == "blank":
                body[c.row][c.col] = ""
                planted[c.id] = "missing"
            elif kind == "symbol":
                new = _symbol(c.value)
                if new is None or new == c.value:
                    continue
                body[c.row][c.col] = new
                planted[c.id] = "wrong"
            else:
                others = [o for o in range(len(body[c.row])) if o != c.col
                          and re.search(r"\d", body[c.row][o]) and (c.row, o) not in touched
                          and body[c.row][o] != c.value and o > 0]
                if not others:
                    continue
                other = rng.choice(others)
                row = body[c.row]
                row[c.col], row[other] = row[other], row[c.col]
                planted[c.id] = "wrong"
                touched.add((c.row, other))
                for mate in cells:
                    if (mate.row, mate.col) == (c.row, other):
                        planted[mate.id] = "wrong"
            touched.add((c.row, c.col))
            break
    return _render(t, body), planted


JUDGE = """You check how faithfully several unlabelled copies of a table were made. Each table
comes with the cells to check: their row, column and true value. Its "entries" are copies made
by different tools, in plain text, Markdown or HTML. Some contain planted errors.

Take each entry on its own. For each cell, read only that entry and ask: what value sits at this
row and column? Compare digit by digit; don't trust an entry because it looks tidy.

- correct: the same value. Ignore spacing, line wraps, footnote marks, HTML or Markdown, "-" for
  "\u2212", ".05" for "0.05", "0.50" for "0.5", and "/" or a line break between two values.
- wrong: a different value sits there. That includes a changed digit, a lost or changed sign or
  symbol (<, >, \u2264, \u2265, \u00b1, \u2212, %), a lost decimal point, and a value that has
  moved to another row or column.
- missing: no value sits there, or the value can't be tied to this row and column.

Plain text has no column lines, so read each row left to right: the first value belongs to the
first column, and so on. If a blank cell was dropped and that puts a value under the wrong column,
that cell is wrong; if no value is left for a column, that cell is missing.

Report only the cells that are not correct, as JSON in this shape, with every table present and
{} for a table or entry with no problems:
{"<table>": {"<entry letter>": {"<cell id>": "wrong"}}}
"""


def _letters(n: int) -> list[str]:
    return [chr(ord("A") + i) for i in range(n)]


def packets(paper: str, tables: list[Table], outputs: dict[str, str | None]) -> tuple[list, dict]:
    """What the judges see, and the sealed answers that say who is who.

    outputs: reader name to its whole output, or None if the reader gave nothing. A reader
    whose output doesn't hold a table is not shown: all its cells count as missing.
    """
    shown, sealed = [], {}
    for t in tables:
        cells = picked(paper, t)
        entries = {name: find_region(text or "", t) for name, text in outputs.items()}
        absent = sorted(name for name, region in entries.items() if not region)
        entries = {name: region for name, region in entries.items() if region}
        entries[CLEAN] = clean_copy(t)
        entries[DAMAGED], planted = damaged_copy(paper, t, cells)
        names = sorted(entries)
        random.Random(f"{SEED}/order/{paper}/{t.n}").shuffle(names)
        key = f"{paper}-{t.n}"
        who = dict(zip(_letters(len(names)), names))
        shown.append({
            "table": key,
            "caption": f"{t.label} {t.caption}".strip()[:300],
            "cells": [{"id": c.id, "row": c.label, "column": c.header, "value": c.value}
                      for c in cells],
            "entries": {letter: entries[name] for letter, name in who.items()},
        })
        sealed[key] = {"who": who, "planted": planted, "absent": absent,
                       "cells": [c.id for c in cells]}
    return shown, sealed


def score(sealed: dict, marks: dict) -> dict:
    """Marks into counts per reader, and per paper for the error ranges.

    marks: table key to {letter: {cell id: "wrong" or "missing"}}; a cell not listed is correct.
    A table with no marks at all has not been judged, and raises rather than counting as perfect.
    """
    totals: dict[str, Counter] = {}
    papers: dict[str, dict[str, Counter]] = {}
    planted = Counter()
    for key, info in sealed.items():
        if key not in marks:
            raise ValueError(f"table {key} has not been judged")
        paper = key.rsplit("-", 1)[0]
        for letter, name in info["who"].items():
            for cell in info["cells"]:
                mark = marks[key].get(letter, {}).get(cell, "correct")
                if mark not in MARKS:
                    raise ValueError(f"unknown mark {mark!r} in {key}")
                totals.setdefault(name, Counter())[mark] += 1
                papers.setdefault(name, {}).setdefault(paper, Counter())[mark] += 1
                if name == DAMAGED and cell in info["planted"]:
                    planted["planted"] += 1
                    planted["caught"] += mark != "correct"
                elif name == DAMAGED:
                    planted["untouched"] += 1
                    planted["false_alarm"] += mark != "correct"
        for name in info["absent"]:
            totals.setdefault(name, Counter())["missing"] += len(info["cells"])
            papers.setdefault(name, {}).setdefault(paper, Counter())["missing"] += len(info["cells"])
    return {"totals": totals, "papers": papers, "planted": planted}


def checks_pass(result: dict) -> tuple[bool, str]:
    """The judging counts only if the clean copy scores 99% and 95% of planted errors are caught."""
    clean = result["totals"].get(CLEAN, Counter())
    total = sum(clean.values())
    share = clean["correct"] / total if total else 0
    planted = result["planted"]
    caught = planted["caught"] / planted["planted"] if planted["planted"] else 0
    ok = share >= 0.99 and caught >= 0.95
    return ok, f"clean copy {share:.1%} correct; planted errors caught {caught:.1%}"


def paired_gain(result: dict, reader: str, baseline: str, seed: int = 20261105,
                rounds: int = 10000) -> tuple[float, float, float]:
    """Points gained in correct cells over the baseline, with a 95% range, resampling papers."""
    a, b = result["papers"][reader], result["papers"][baseline]
    names = sorted(set(a) & set(b))

    def share(group, chosen):
        done = sum(group[p]["correct"] for p in chosen)
        return 100 * done / max(sum(sum(group[p].values()) for p in chosen), 1)

    rng = random.Random(seed)
    gains = sorted(share(a, s) - share(b, s)
                   for s in ([rng.choice(names) for _ in names] for _ in range(rounds)))
    return share(a, names) - share(b, names), gains[int(rounds * .025)], gains[int(rounds * .975)]
