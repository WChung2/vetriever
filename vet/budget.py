"""What a search may spend, and its rounds, one at a time.

Code never widens a search on its own. Each round ends with a proposal for the next one; the
vet reads it, with what the round found and cost, and decides. So the tables hold:
- `search_state`: a topic's budget, when it was set (spending counts from then), and the
  proposal waiting for the vet;
- `search_rounds`: each round, started and ended. A round with no end was cut off (by the
  budget, an error or a stop), and running it again carries on where it stopped.

The money comes from corpus/spend.csv, where each model call is written as it finishes, so
the figures hold even if the app is stopped. The budget is a hard limit: the job's ledger
refuses any call that could pass it, counting each call in flight at its worst case. So a
round is only started with that margin left (`needed`), and then it can't stop half-way.
"""
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .screen import WORKERS
from .settings import settings

BATCH_CHARS = 25_000     # a batch's prompt and system text, generously


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- what a round costs ----------------------------------------------------------------

def round_cost(records: int) -> float:
    """Up to what a round costs: its records, its proposal, and one revision of it."""
    spend = settings()["spending"]
    return records * spend["screen_usd_per_1000"] / 1000 + 2 * spend["review_usd_per_call"]


def margin() -> float:
    """The most that calls in flight can be holding back at once (see llm.LLM.call)."""
    cfg = settings()
    price = cfg["prices"][cfg["models"]["screen"]]
    worst = (BATCH_CHARS / 3 * price["input"]
             + cfg["spending"]["max_output_tokens"] * price["output"]) / 1e6
    return WORKERS * worst


def needed(records: int) -> float:
    """The budget that must be left before a round starts."""
    return round_cost(records) + margin()


def ask_needed() -> float:
    """The budget that must be left to ask for a proposal: one review call at its worst."""
    return margin() / 2


def spent(corpus: Path, name: str, since: str, until: str = "") -> float:
    """Money spent on a topic's search (screening and review) in a period of time."""
    path = Path(corpus) / "spend.csv"
    if not path.exists():
        return 0.0
    docs = {f"screen_{name}", f"review_{name}"}
    with open(path, newline="", encoding="utf-8") as fh:
        return sum(float(r["usd"] or 0) for r in csv.DictReader(fh)
                   if r["doc"] in docs and r["time"] >= since
                   and (not until or r["time"] <= until))


# ---- a topic's budget and rounds -------------------------------------------------------

def state(con: sqlite3.Connection, name: str) -> dict:
    """A topic's budget and waiting proposal; empty when it has no budget yet."""
    row = con.execute("SELECT budget, since, next_round, proposal FROM search_state "
                      "WHERE topic = ?", (name,)).fetchone()
    if row is None:
        return {}
    return dict(zip(("budget", "since", "next_round", "proposal"), row))


def set_budget(con: sqlite3.Connection, name: str, usd: float):
    """Set a topic's budget. Spending counts from the first time it is set."""
    with con:
        con.execute("INSERT INTO search_state (topic, budget, since) VALUES (?, ?, ?) "
                    "ON CONFLICT (topic) DO UPDATE SET budget = excluded.budget",
                    (name, usd, now()))


def left(con: sqlite3.Connection, corpus: Path, name: str) -> float:
    st = state(con, name)
    return st["budget"] - spent(corpus, name, st["since"]) if st else 0.0


def save_proposal(con: sqlite3.Connection, name: str, rnd: int, proposal: dict | None):
    """Keep the proposal for round `rnd` for the vet (None: there is none)."""
    with con:
        con.execute("UPDATE search_state SET next_round = ?, proposal = ? WHERE topic = ?",
                    (rnd, json.dumps(proposal, ensure_ascii=False) if proposal else "", name))


def rounds(con: sqlite3.Connection, name: str) -> list[dict]:
    cols = ("round", "started", "ended", "relevant")
    return [dict(zip(cols, r)) for r in con.execute(
        "SELECT round, started, ended, relevant FROM search_rounds WHERE topic = ? "
        "ORDER BY round", (name,))]


def next_round(con: sqlite3.Connection, name: str) -> int:
    """The number the next round takes, also after rounds run before this table existed."""
    a = con.execute("SELECT MAX(round) FROM found WHERE topic = ?", (name,)).fetchone()[0]
    b = con.execute("SELECT MAX(round) FROM search_rounds WHERE topic = ?", (name,)).fetchone()[0]
    return 1 + max(a or 0, b or 0)


def begin_round(con: sqlite3.Connection, name: str, rnd: int):
    with con:
        con.execute("INSERT OR IGNORE INTO search_rounds (topic, round, started) VALUES (?, ?, ?)",
                    (name, rnd, now()))


def end_round(con: sqlite3.Connection, name: str, rnd: int, relevant: int):
    with con:
        con.execute("UPDATE search_rounds SET ended = ?, relevant = relevant + ? WHERE topic = ? "
                    "AND round = ?", (now(), relevant, name, rnd))


def next_steps(answer: dict) -> list[str]:
    """What a proposal would do, in plain words."""
    out = [f"add the search term “{t['term']}”" for t in answer.get("term_changes", [])
           if t.get("action") == "add"]
    out += [f"take out the search term “{t['term']}”" for t in answer.get("term_changes", [])
            if t.get("action") == "remove"]
    how = {"references": "the references of", "cited_by": "the papers citing",
           "similar": "the articles similar to"}
    for kind, words in how.items():
        n = sum(f.get("how") == kind for f in answer.get("follow", []))
        if n:
            out.append(f"follow {words} {n} paper{'' if n == 1 else 's'}")
    out += [f"run the extra query {q['query']}" for q in answer.get("extra_queries", [])]
    return out
