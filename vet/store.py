"""The database: one SQLite file (corpus/vetriever.db) holding every codebook table. Each
document and each study is stored once, whichever topic or import brought it in.

    vet import <folder>     # add a folder of tables (a delivery, or an older layout)
    vet export [folder]     # write every table back out as CSV
    vet check               # check the database

Keys:
- A document's source_id is set once, when it first arrives (DOI first, see codebook.md),
  and never changes. Later arrivals are matched by any of DOI, PMID, PMCID or the agency key,
  so the same paper is never stored twice.
- A study's ID is VET-<n>, numbered in arrival order and not built from any document, so it
  stays the same when more documents about the study arrive.

An import adds whole documents: a document already in the database is skipped together with
its studies and their rows (updating a stored document comes with re-extraction, later).
Every value that comes in is accounted for: stored, skipped because its document is already
stored, or listed as dropped.
"""
import csv
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .codebook import TABLES, Codebook
from .settings import DB
from .validate import PRIMARY_KEY

# Older schema v2 deliveries keep the topic in studies.indication and the topic fields in one
# table per topic.
LEGACY_TOPIC_TABLES = {"ext_pain": "surgical_pain", "ext_imha": "imha", "ext_cie": "cie"}
SOURCE_COLUMNS = {"source_id", "primary_source_id", "secondary_source_id"}
STUDY_KEY_COLUMNS = {"ID", "arm_id", "dose_id", "result_id", "reference_arm_id"}
# columns whose values are keys or structure, renamed by an import; all others are counted
NOT_COUNTED = SOURCE_COLUMNS | STUDY_KEY_COLUMNS | {"row_key", "table", "topic", "field"}


def norm_doi(doi: str) -> str:
    return doi.strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")


def document_key(row: dict) -> str:
    """The source_id a new document gets: DOI first, then PMID, else the key it came with
    (agency, registry, conference abstract or web page)."""
    if row.get("doc_type") != "conference_abstract":
        if row.get("doi"):
            return "DOI-" + re.sub(r"[^A-Z0-9]+", "-", norm_doi(row["doi"]).upper()).strip("-")
        if row.get("pmid"):
            return f"PMID{row['pmid']}"
    return row["source_id"]


def _identifiers(row: dict) -> set:
    doi = "" if row.get("doc_type") == "conference_abstract" else norm_doi(row.get("doi", ""))
    ids = {("doi", doi), ("pmid", row.get("pmid", "")), ("pmcid", row.get("pmcid", "").upper()),
           ("key", row.get("source_id", ""))}
    return {(name, value) for name, value in ids if value}


def _values(table: str, rows: list) -> Counter:
    return Counter((table, c, v) for r in rows for c, v in r.items()
                   if v and c not in NOT_COUNTED)


def _plain(counts: Counter) -> Counter:
    """(table, column, value) counts -> value counts, for comparing across layouts."""
    out = Counter()
    for (_, _, v), n in counts.items():
        out[v] += n
    return out


@dataclass
class ImportReport:
    rows_in: Counter = field(default_factory=Counter)
    rows_added: Counter = field(default_factory=Counter)
    rows_skipped: Counter = field(default_factory=Counter)
    new_documents: list = field(default_factory=list)
    known_documents: list = field(default_factory=list)    # (key in the folder, stored key)
    new_studies: dict = field(default_factory=dict)        # ID in the folder -> new ID
    topic_from_document: list = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)       # (table, column) -> filled cells
    values_in: int = 0
    values_stored: int = 0
    values_skipped: int = 0
    values_dropped: int = 0
    missing: Counter = field(default_factory=Counter)       # must stay empty


class Store:
    def __init__(self, path: Path = DB, cb: Codebook | None = None):
        self.cb = cb or Codebook()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        with self.db:
            for t in TABLES:
                cols = ", ".join(f'"{c}" TEXT NOT NULL DEFAULT \'\'' for c in self.cb.columns(t))
                pk = ", ".join(f'"{c}"' for c in PRIMARY_KEY[t])
                self.db.execute(f'CREATE TABLE IF NOT EXISTS "{t}" ({cols}, PRIMARY KEY ({pk}))')
                have = [r[1] for r in self.db.execute(f'PRAGMA table_info("{t}")')]
                gone = [c for c in have if c not in self.cb.columns(t)]
                if gone:
                    raise RuntimeError(f"{self.path}: {t} has columns the codebook no longer "
                                       f"has: {gone}; export the data before removing them")
                for c in self.cb.columns(t):
                    if c not in have:
                        self.db.execute(f'ALTER TABLE "{t}" ADD COLUMN "{c}" TEXT NOT NULL DEFAULT \'\'')
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT)")

    def close(self):
        self.db.close()

    def rows(self, table: str) -> list[dict]:
        cols = self.cb.columns(table)
        names = ", ".join(f'"{c}"' for c in cols)
        return [dict(zip(cols, r)) for r in
                self.db.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid')]

    def tables(self) -> dict[str, list[dict]]:
        return {t: self.rows(t) for t in TABLES}

    def export(self, folder: Path) -> int:
        folder.mkdir(parents=True, exist_ok=True)
        n = 0
        for t in TABLES:
            rows = self.rows(t)
            with open(folder / f"{t}.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=self.cb.columns(t), lineterminator="\n")
                w.writeheader()
                w.writerows(rows)
            n += len(rows)
        return n

    def _take_study_numbers(self, n: int) -> list[str]:
        """Numbers are never reused, even if a study is deleted later."""
        row = self.db.execute("SELECT value FROM meta WHERE name = 'last_study'").fetchone()
        last = int(row[0]) if row else 0
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('last_study', ?)", (str(last + n),))
        return [f"VET-{i}" for i in range(last + 1, last + n + 1)]

    # -- import ------------------------------------------------------------------------
    def import_folder(self, folder: Path) -> ImportReport:
        rep = ImportReport()
        raw = _read_folder(Path(folder))
        t, dropped = _current_layout(raw, self.cb, rep)
        raw_values = _plain(sum((_values(name, rows) for name, (_, rows) in raw.items()),
                                Counter()))
        raw_values -= Counter(r["indication"] for r in raw.get("studies", ([], []))[1]
                              if r.get("indication"))           # became study_topics
        layout_values = _plain(sum((_values(name, rows) for name, rows in t.items()), Counter()))
        rep.values_in, rep.values_dropped = sum(raw_values.values()), sum(dropped.values())
        rep.missing = raw_values - layout_values - dropped     # lost while changing layout
        for name, rows in t.items():
            rep.rows_in[name] = len(rows)
        in_folder = {r["ID"] for r in t["studies"]}
        orphans = sorted({r["ID"] for rows in t.values() for r in rows
                          if r.get("ID") and r["ID"] not in in_folder})
        if orphans:
            raise ValueError(f"nothing imported: rows refer to studies that {folder}/studies.csv "
                             f"does not have, e.g. {orphans[:3]}")

        # documents: match each one against the stored ones (and the ones before it here)
        known_ids = {}
        for row in self.rows("sources"):
            for ident in _identifiers(row):
                known_ids[ident] = row["source_id"]
        taken = {row["source_id"] for row in self.rows("sources")}
        doc_map, skip_docs = {}, set()
        for row in t["sources"]:
            match = next((known_ids[i] for i in _identifiers(row) if i in known_ids), None)
            if match:
                doc_map[row["source_id"]] = match
                skip_docs.add(row["source_id"])
                rep.known_documents.append((row["source_id"], match))
                continue
            key, n = document_key(row), 1
            while key in taken:
                n += 1
                key = f"{document_key(row)}-{n}"
            taken.add(key)
            doc_map[row["source_id"]] = key
            rep.new_documents.append(key)
            for ident in _identifiers(row) | {("key", key)}:
                known_ids[ident] = key

        # studies: skipped with their primary document when it is already stored
        primary = {r["ID"]: r["source_id"] for r in t["study_sources"] if r["role"] == "primary"}
        skip_studies = {sid for sid, doc in primary.items() if doc in skip_docs}
        new = [r["ID"] for r in t["studies"] if r["ID"] not in skip_studies]
        id_map = dict(zip(new, self._take_study_numbers(len(new))))
        rep.new_studies = id_map

        def one(token: str) -> str:
            for k in [len(token)] + [m.start() for m in re.finditer("-", token)][::-1]:
                if token[:k] in id_map:
                    return id_map[token[:k]] + token[k:]
            return token

        def swap(value: str) -> str:
            """Study keys inside a value: VET-<old>-A1 -> VET-<new>-A1, also within a
            |-separated or free-text row_key."""
            if value in id_map:
                return id_map[value]
            return re.sub(r"VET-[A-Za-z0-9-]+", lambda m: one(m.group(0)), value)

        add, values_in, values_skipped = {}, Counter(), Counter()
        for name, rows in t.items():
            keep = []
            for r in rows:
                skipped = (r.get("ID") in skip_studies if r.get("ID")
                           else r.get("source_id") in skip_docs)
                if name == "sources":
                    skipped = r["source_id"] in skip_docs
                (values_skipped if skipped else values_in).update(_values(name, [r]))
                if skipped:
                    rep.rows_skipped[name] += 1
                    continue
                r = dict(r)
                for c in r:
                    if c in SOURCE_COLUMNS and r[c]:
                        r[c] = doc_map.get(r[c], r[c])
                    elif c in STUDY_KEY_COLUMNS or (c == "row_key" and name in ("evidence", "conflicts")):
                        r[c] = swap(r[c])
                keep.append(r)
            add[name] = keep

        try:
            with self.db:
                for name, rows in add.items():
                    cols = self.cb.columns(name)
                    marks = ", ".join("?" for _ in cols)
                    names = ", ".join(f'"{c}"' for c in cols)
                    self.db.executemany(f'INSERT INTO "{name}" ({names}) VALUES ({marks})',
                                        [[r.get(c, "") for c in cols] for r in rows])
                    rep.rows_added[name] = len(rows)
        except sqlite3.IntegrityError as e:
            raise ValueError(f"nothing imported: two rows in {folder} share a key ({e})") from e

        # read back what was stored and compare value by value, column by column
        new_ids, new_docs = set(id_map.values()), set(rep.new_documents)
        stored = Counter()
        for name in TABLES:
            mine = [r for r in self.rows(name)
                    if (r.get("ID") in new_ids if r.get("ID") else r.get("source_id") in new_docs)
                    or (name == "sources" and r["source_id"] in new_docs)]
            stored.update(_values(name, mine))
        rep.values_stored = sum(stored.values())
        rep.values_skipped = sum(values_skipped.values())
        rep.missing += values_in - stored                      # lost while storing
        return rep


def _read_folder(folder: Path) -> dict:
    out = {}
    for path in sorted(folder.glob("*.csv")):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            rows = [{k: v or "" for k, v in r.items() if k is not None} for r in reader]
            out[path.stem] = (reader.fieldnames or [], rows)
    if not out:
        raise ValueError(f"no CSV tables in {folder}")
    return out


def _current_layout(raw: dict, cb: Codebook, rep: ImportReport) -> tuple[dict, Counter]:
    """Tables in today's layout, and the values left behind. An older layout's
    studies.indication becomes study_topics and its ext_* tables become topic_fields; any
    other table or column the codebook doesn't have is dropped and reported."""
    t = {name: [] for name in TABLES}
    dropped = Counter()
    for name, (header, rows) in raw.items():
        if name in LEGACY_TOPIC_TABLES:
            topic = LEGACY_TOPIC_TABLES[name]
            t["topic_fields"] += [{"ID": r["ID"], "topic": topic, "field": c, "value": v}
                                  for r in rows for c, v in r.items() if c != "ID" and v]
            continue
        cols = set(cb.columns(name)) if name in TABLES else set()
        for c in header:
            if c not in cols and (name, c) != ("studies", "indication"):
                cells = [r[c] for r in rows if r.get(c)]
                rep.dropped[name, c] += len(cells)
                dropped += _plain(_values(name, [{c: v} for v in cells]))
        if name not in TABLES:
            continue
        t[name] += [{c: r.get(c, "") for c in cb.columns(name)} for r in rows]
        if name == "studies":
            t["study_topics"] += [{"ID": r["ID"], "topic": r["indication"]}
                                  for r in rows if r.get("indication")]
    for r in t["evidence"] + t["conflicts"]:
        if r["table"] in LEGACY_TOPIC_TABLES:
            r["row_key"] = LEGACY_TOPIC_TABLES[r["table"]]
            r["table"] = "topic_fields"
    # the topic now comes from the search, not from the document, so evidence for
    # studies.indication has nothing left to support
    old = [r for r in t["evidence"] if (r["table"], r["field"]) == ("studies", "indication")]
    if old:
        t["evidence"] = [r for r in t["evidence"] if r not in old]
        rep.dropped["evidence", "rows for studies.indication"] += len(old)
        dropped += _plain(_values("evidence", old))
    # a study with no topic gets the topics of the other studies in its primary document
    has_topic = {r["ID"] for r in t["study_topics"]}
    doc = {r["ID"]: r["source_id"] for r in t["study_sources"] if r["role"] == "primary"}
    doc_topics = {}
    for r in t["study_topics"]:
        doc_topics.setdefault(doc.get(r["ID"]), set()).add(r["topic"])
    for r in t["studies"]:
        if r["ID"] not in has_topic:
            for topic in sorted(doc_topics.get(doc.get(r["ID"]), ())):
                t["study_topics"].append({"ID": r["ID"], "topic": topic})
                rep.topic_from_document.append((r["ID"], topic))
    return t, dropped
