"""How two studies relate (study_links): one decision per pair, for any topic.

    vet link VET-248 VET-219 same_experiment --source DOI-10-3389-... --words "..." --where "Methods"
    vet link VET-141 VET-175 distinct --note "two case reports, one dog each"

same_experiment and shared_animals rest on a document's own words: the document must be one of
the two studies' own, and the words must be in its stored text (spacing, case and hyphens
don't count, as in `vet check`). A pair is decided once; deciding it again replaces the row.
"""
import re

from .store import Store
from .validate import STUDY_ID, _flat

RELATIONS = ("same_experiment", "shared_animals", "distinct")
BY = ("vet", "curator")


def link(store: Store, a: str, b: str, relation: str, source_id: str = "", words: str = "",
         where: str = "", by: str = "curator", note: str = "") -> dict:
    """Store the decision for the pair a, b; returns the row. Raises ValueError, storing
    nothing, when the decision isn't supported."""
    for sid in (a, b):
        if not STUDY_ID.fullmatch(sid):
            raise ValueError(f"{sid!r} is not a study key (VET-<n>)")
    if a == b:
        raise ValueError("a study can't be linked to itself")
    if relation not in RELATIONS:
        raise ValueError(f"relation must be one of {', '.join(RELATIONS)}")
    if by not in BY:
        raise ValueError(f"decided by must be one of {', '.join(BY)}")
    a, b = sorted((a, b), key=lambda s: int(s[4:]))
    have = {r["ID"] for r in store.rows("studies")}
    missing = [s for s in (a, b) if s not in have]
    if missing:
        raise ValueError(f"no such study: {', '.join(missing)}")
    words = " ".join(words.split())
    if relation == "distinct":
        if not note.strip():
            raise ValueError("distinct needs a note: what differs")
        source_id = words = where = ""
    else:
        if not (source_id and words):
            raise ValueError(f"{relation} needs the document's words (--words) and the "
                             "document (--source)")
        docs = {r["source_id"]: r for r in store.rows("study_sources") if r["ID"] in (a, b)}
        if source_id not in docs:
            raise ValueError(f"{source_id} is not a document of {a} or {b}")
        src = next((r for r in store.rows("sources") if r["source_id"] == source_id), {})
        path = store.path.parent / src.get("text_file", "") if src.get("text_file") else None
        if not (path and path.is_file()):
            raise ValueError(f"{source_id} has no stored text to check the words against")
        parts = [_flat(x) for x in re.split(r"…|\.\.\.", words) if _flat(x)]
        text = _flat(path.read_text("utf-8"))
        if not all(p in text for p in parts):
            raise ValueError(f"the words are not in {source_id}'s text")
    row = {"ID": a, "linked_ID": b, "relation": relation, "source_id": source_id,
           "statement_verbatim": words, "location": where, "decided_by": by, "note": note.strip()}
    cols = store.cb.columns("study_links")
    names, marks = ", ".join(f'"{c}"' for c in cols), ", ".join("?" for _ in cols)
    with store.db:
        store.db.execute('DELETE FROM study_links WHERE "ID" = ? AND linked_ID = ?', (a, b))
        store.db.execute(f"INSERT INTO study_links ({names}) VALUES ({marks})",
                         [row.get(c, "") for c in cols])
    return row
