"""The golden test: facts confirmed on the printed page, for comparing ways of reading a paper.

The question: does Gemini get a table's facts right more often from the PDF itself, or from text
a local reader made first? A fact is golden only when two sources agree: the paper's XML, and a
blind reading of the printed page by someone who never saw the XML's value.

    draw     the questions for one paper, by a fixed seed, from the table's structure alone
    compare  the XML's value against the page's: they agree, or the fact goes to a ruling
    score    answers against the golden facts: correct, wrong and missing per route

No question depends on what any reader got right or wrong. Scoring keeps the judge blind: a ruling
is made on a fact and an answer's wording, so it can't depend on which route gave that answer.

Papers, facts and answers stay in corpus/golden/. Nothing here calls a service.
"""
from __future__ import annotations

import random
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass

from vet.tablebench import Cell, Table

SEED = "vetriever-golden-2026-11-06"
KINDS = ("blank", "after_blank", "symbol", "deep", "plain")
TABLES_PER_PAPER = 3
FACTS_PER_TABLE = 4
FACTS_PER_TABLE_FEW = 6           # a paper with one or two tables gives more from each
TRAPS = ("blank", "after_blank")  # few tables have blanks, and that is where values move
TRAP_FACTS = 3                    # so a table with blanks gives up to this many of each
SYMBOL = re.compile(r"[±<>≤≥µμ−]|^\s*-\s*\.?\d")
NOT_REPORTED = "NOT REPORTED"     # what a route answers for a blank cell or a fact it can't find
NO_ROW = "NO ROW"                 # what a page reader writes when the row or column isn't there
UNREADABLE = "UNREADABLE"
MARKS = ("correct", "wrong", "missing")


@dataclass
class Question:
    id: str
    paper: str
    table: str
    row: str
    column: str
    kind: str
    key: str                      # the XML's value; empty for a blank cell

    def blind(self) -> dict:
        """What a page reader or a route sees: never the key, never the kind."""
        return {k: v for k, v in asdict(self).items() if k not in ("key", "kind", "paper")}


def _header(t: Table, col: int) -> str:
    return " › ".join(dict.fromkeys(h[col] for h in t.head if col < len(h) and h[col]))


def _first_data_col(row: list[str]) -> int:
    end = 1
    while end < len(row) and row[end] and not re.search(r"\d", row[end]):
        end += 1
    return end


def blanks(t: Table) -> list[Cell]:
    """Empty cells in a data column, on a row that holds numbers: nothing is printed there."""
    number_cols = {c.col for c in t.cells}
    labels = {c.row: c.label for c in t.cells}
    out = []
    for r, label in labels.items():
        row = t.body[r]
        out += [Cell(f"{t.n}.{r}.{c}", r, c, label, _header(t, c), "")
                for c in range(_first_data_col(row), len(row))
                if not row[c] and c in number_cols]
    return out


def kind_of(t: Table, cell: Cell) -> str:
    """The hardest thing about a cell, from the table's structure alone."""
    if not cell.value:
        return "blank"
    row = t.body[cell.row]
    if any(not row[c] for c in range(_first_data_col(row), cell.col)):
        return "after_blank"      # a reader that drops the blank moves this value left
    if SYMBOL.search(cell.value):
        return "symbol"
    if " › " in cell.label or " › " in cell.header:
        return "deep"
    return "plain"


def _clear(cells: list[Cell]) -> list[Cell]:
    """Cells a question can name: a row and a column, and only one cell has that pair."""
    pairs = Counter((c.label, c.header) for c in cells)
    return [c for c in cells if c.label and c.header and pairs[(c.label, c.header)] == 1]


def draw(paper: str, tables: list[Table]) -> list[Question]:
    """Tables with blanks first. From each, the traps, then one of each other kind, then more at
    random, up to the cap. Fixed by SEED, and the table's structure is all it looks at."""
    chosen = sorted(tables, key=lambda t: t.n)
    random.Random(f"{SEED}/{paper}").shuffle(chosen)
    chosen = sorted(chosen, key=lambda t: not _clear(blanks(t)))[:TABLES_PER_PAPER]
    cap = FACTS_PER_TABLE_FEW if len(chosen) <= 2 else FACTS_PER_TABLE
    out = []
    for t in sorted(chosen, key=lambda t: t.n):
        rng = random.Random(f"{SEED}/{paper}/{t.n}")
        cells = sorted(_clear([*t.cells, *blanks(t)]), key=lambda c: (c.row, c.col))
        rng.shuffle(cells)
        by_kind = {k: [c for c in cells if kind_of(t, c) == k] for k in KINDS}
        traps = [c for k in TRAPS for c in by_kind[k][:TRAP_FACTS]]
        others = [by_kind[k][0] for k in KINDS if k not in TRAPS and by_kind[k]]
        others += [c for c in cells if c not in others and c not in traps]
        take = traps + others[:max(cap - len(traps), 2)]
        out += [Question(f"{paper}-{t.n}-{c.row}-{c.col}", paper, t.label, c.label, c.header,
                         kind_of(t, c), c.value)
                for c in sorted(take, key=lambda c: (c.row, c.col))]
    return out


def normal(value: str) -> str:
    """A value with only printing differences removed. Every digit and symbol stays."""
    s = unicodedata.normalize("NFKC", value).replace("·", ".")
    s = re.sub(r"[−–—‐‑]", "-", s).replace("×", "x")
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)
    return re.sub(r"\s+", "", s).lower()


def compare(questions: list[Question], entries: dict[str, str]) -> tuple[list, list]:
    """Each fact's XML value against the blind page reading. Differences go to a ruling."""
    agreed, differ = [], []
    for q in questions:
        page = entries[q.id]
        (agreed if normal(page) == normal(q.key) else differ).append((q, page))
    return agreed, differ


def mark(truth: str, answer: str, rulings: dict) -> str:
    """One answer against one golden fact. A blank fact is right only if nothing is claimed."""
    said = "" if answer.strip().upper() == NOT_REPORTED else answer.strip()
    if not truth:
        return "correct" if not said else "wrong"
    if not said:
        return "missing"
    if normal(said) == normal(truth):
        return "correct"
    ruled = rulings.get(f"{normal(truth)}|{normal(said)}")
    if ruled is None:
        raise ValueError(f"needs a ruling: {truth!r} against {answer!r}")
    if ruled not in ("correct", "wrong"):
        raise ValueError(f"unknown ruling: {ruled!r}")
    return ruled


def score(golden: dict[str, dict], answers: dict[str, dict[str, str]], rulings: dict) -> dict:
    """Answers by route, each {fact id: answer}. A fact a route left out counts as missing."""
    result = {"totals": {}, "papers": {}, "kinds": {}}
    for route, got in answers.items():
        totals, papers, kinds = Counter(), {}, {}
        for fid, fact in golden.items():
            m = mark(fact["truth"], got[fid], rulings) if fid in got else "missing"
            totals[m] += 1
            papers.setdefault(fact["paper"], Counter())[m] += 1
            kinds.setdefault(fact["kind"], Counter())[m] += 1
        result["totals"][route], result["papers"][route], result["kinds"][route] = \
            totals, papers, kinds
    return result
