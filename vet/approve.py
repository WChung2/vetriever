"""The vet's list (step 6) and the papers worth downloading by hand (after step 8).

The list shows each kept record's title and one-sentence summary, with its design; the
reasoning, flags and the dropped records with their reasons are in a second file. Approval
is stored in the screening table. After fetching, the approved papers with no full text are
ranked (stronger designs first, then larger studies) and the top few are recommended to the
vet; the rest are extracted from their abstracts.
"""
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from .search import add_rejected

DESIGN_ORDER = ["randomised_trial", "prospective", "retrospective", "case_series", "other",
                "unclear", "single_case", ""]


def _rows(db: sqlite3.Connection, name: str, where: str) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT s.*, r.title, r.year, r.journal, r.pmid, r.doi FROM screening s "
        f"JOIN records r ON r.key = s.key WHERE s.topic = ? AND {where} ORDER BY r.year DESC",
        (name,))]


def write_list(name: str, db: sqlite3.Connection, folder: Path) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    kept = _rows(db, name, "s.decision IN ('keep', 'lead')")
    out = [f"# {name}: {len(kept)} records kept", "",
           ("Title, then the one-sentence summary: studies of the condition first, then "
            "healthy animals given a treatment, then records kept for lack of a quote, then "
            "reviews (leads)."), ""]
    for r in sorted(kept, key=lambda r: (r["decision"] == "lead", "research_animals" in r["flags"],
                                         "unbacked_drop" in r["flags"],
                                         DESIGN_ORDER.index(r["design"]) if r["design"] in
                                         DESIGN_ORDER else 9)):
        tag = "lead" if r["decision"] == "lead" else r["design"] or "?"
        tag += "".join(f", {f}" for f in r["flags"].split("|") if f in ("research_animals",
                                                                      "unbacked_drop"))
        out += [f"- **{r['title']}** ({r['year']}, {tag}, {r['key']})", f"  {r['summary']}"]
    kept_path = folder / f"{name}_kept.md"
    kept_path.write_text("\n".join(out) + "\n", "utf-8")
    more = [f"# {name}: reasoning and flags, and the dropped records", ""]
    for title, where in (("Kept", "s.decision IN ('keep', 'lead')"),
                         ("Dropped", "s.decision = 'off_topic'"),
                         ("Set aside (letters, comments, errata)", "s.decision = 'aside'")):
        rows = _rows(db, name, where)
        more += [f"## {title} ({len(rows)})", ""]
        for r in rows:
            a = json.loads(r["answers"] or "{}")
            more += [f"- **{r['key']}** {r['title']} ({r['year']})",
                     f"  {r['decision']} {r['rule']} {r['flags']}: {a.get('reasoning', '')}"]
        more.append("")
    detail_path = folder / f"{name}_details.md"
    detail_path.write_text("\n".join(more), "utf-8")
    return kept_path, detail_path


def approve(name: str, db: sqlite3.Connection, keys: list[str] | None = None,
            leave_out: list[str] = ()) -> int:
    """Approve the kept records (all, or these keys), minus the ones left out and the ones the
    vet rejected."""
    add_rejected(db)
    rows = _rows(db, name, "s.decision = 'keep' AND COALESCE(s.rejected, '') = ''")
    chosen = [r["key"] for r in rows if (keys is None or r["key"] in keys) and r["key"] not in leave_out]
    db.executemany("UPDATE screening SET approved = ? WHERE topic = ? AND key = ?",
                   [(date.today().isoformat(), name, k) for k in chosen])
    db.commit()
    return len(chosen)


ACTIONS = {"approve", "reject", "undo"}


def set_decision(name: str, db: sqlite3.Connection, keys: list[str], action: str) -> int:
    """The vet's review of records, from the screens; returns how many changed. Each change is
    written to the topic log.
    - approve: a record the screening kept, or one it dropped ("keep anyway");
    - reject: a record the screening kept;
    - undo: back to undecided.
    Overviews and guidelines (leads) and letters set aside take no decision."""
    if action not in ACTIONS:
        raise ValueError(f"action {action!r}: one of {sorted(ACTIONS)}")
    if not keys:
        return 0
    add_rejected(db)
    allowed = {"approve": "('keep', 'off_topic')", "reject": "('keep')",
               "undo": "('keep', 'off_topic')"}[action]
    marks = ", ".join("?" for _ in keys)
    rows = db.execute(
        f"SELECT key, decision, approved, COALESCE(rejected, '') FROM screening WHERE topic = ? "
        f"AND decision IN {allowed} AND key IN ({marks})", (name, *keys)).fetchall()
    today = date.today().isoformat()
    new = {"approve": (today, ""), "reject": ("", today), "undo": ("", "")}[action]
    changed = [(k, d) for k, d, a, r in rows
               if (bool(a), bool(r)) != (bool(new[0]), bool(new[1]))]
    with db:
        db.executemany("UPDATE screening SET approved = ?, rejected = ? WHERE topic = ? AND key = ?",
                       [(*new, name, k) for k, _ in changed])
        db.executemany("INSERT INTO topic_log (topic, round, what, detail, outcome, date) "
                       "VALUES (?, 0, ?, ?, 'screens', ?)",
                       [(name, "keep anyway" if action == "approve" and d == "off_topic"
                         else action, k, today) for k, d in changed])
    return len(changed)


def approved(name: str, db: sqlite3.Connection) -> list[dict]:
    return _rows(db, name, "s.approved != ''")


def _size(summary: str) -> int:
    nums = [int(n) for n in re.findall(r"\b(\d{1,4})\b", summary) if not 1900 < int(n) < 2100]
    return max(nums, default=0)


def without_text(name: str, db: sqlite3.Connection) -> list[dict]:
    """Approved records with no full text, best first: design, then size."""
    have = set()
    if db.execute("SELECT name FROM sqlite_master WHERE name = 'fetches'").fetchone():
        for r in db.execute("SELECT pmid, doi FROM fetches WHERE route != ''"):
            have |= {r[0], (r[1] or "").lower()} - {"", None}
    rows = [r for r in approved(name, db) if r["pmid"] not in have and (r["doi"] or "").lower() not in have]
    return sorted(rows, key=lambda r: (DESIGN_ORDER.index(r["design"]) if r["design"] in DESIGN_ORDER
                                       else 9, -_size(r["summary"])))
