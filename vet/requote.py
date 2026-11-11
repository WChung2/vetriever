"""After a reader improves, realign stored quotes with the new text: layout only, never words.

A text re-read with a better reader can lay a table out differently: a cell's two lines become
"8 (4–17) / n = 14" where they were two lines. A quote copied from the old text then no longer
matches, though every word and number in it is still there, in the same order. This finds such a
quote in the new text and stores the passage as it now reads.

What may differ: line breaks and spaces, a "|" between cells, and a " / " between a cell's parts.
What may not: any other character. A quote with a number the old reader had run together ("38"
for "3 / 8") never matches, and stays flagged, because its value was read wrong. A quote must
match exactly once, and never inside a longer number or word. Values are never touched.
"""
from __future__ import annotations

import re
from pathlib import Path

from .codebook import Codebook
from .store import Store
from .validate import PRIMARY_KEY, validate

LAYOUT = r"(?:\s|\||(?<=\s)/(?=\s))+"     # between two words: spaces, a cell's | or its " / "
PASSAGES = re.compile(r"…|\.\.\.")


def realign(text: str, quote: str) -> str | None:
    """The passage of text this quote is, with only the layout changed; None unless exactly one."""
    found = []
    for part in PASSAGES.split(quote):
        words = [w for w in re.split(r"[\s|]+", part) if w and w != "/"]
        if not words:
            continue
        # never inside a longer word or number: "8" is not in "18", "5" is not in "0.5" or "5.2"
        start = r"(?<!\w)(?<!\d\.)" if re.match(r"\w", words[0]) else ""
        end = r"(?!\w)(?!\.\d)" if re.search(r"\w$", words[-1]) else ""
        hits = list(re.finditer(start + LAYOUT.join(map(re.escape, words)) + end, text))
        if len(hits) != 1:
            return None
        found.append(hits[0].group())
    return " … ".join(found) if found else None


def requote(db: Path, root: Path, cb: Codebook | None = None) -> dict:
    """Realign every stored quote that no longer matches its document. Returns the counts."""
    cb = cb or Codebook()
    store = Store(db, cb)
    done = {"realigned": 0, "left": 0}
    try:
        tables = store.tables()
        texts = {s["source_id"]: (root / s["text_file"]).read_text("utf-8")
                 for s in tables["sources"] if s.get("text_file") and (root / s["text_file"]).is_file()}
        docs: dict[str, list[str]] = {}
        for r in tables["study_sources"]:
            docs.setdefault(r.get("ID", ""), []).append(r.get("source_id", ""))
        for issue in validate(tables, cb, root=root):
            if issue.check not in ("quote_not_in_document", "not_in_document"):
                continue
            row = tables[issue.table][issue.line - 2]
            mine = [row["source_id"]] if row.get("source_id") else docs.get(row.get("ID", ""), [])
            new = next((q for d in mine if d in texts
                        for q in [realign(texts[d], row[issue.field])] if q), None)
            if new is None:
                done["left"] += 1
                continue
            keys = PRIMARY_KEY[issue.table]
            with store.db:
                store.db.execute(f'UPDATE "{issue.table}" SET "{issue.field}" = ? WHERE '
                                 + " AND ".join(f'"{k}" = ?' for k in keys),
                                 (new, *(row[k] for k in keys)))
            done["realigned"] += 1
    finally:
        store.close()
    return done
