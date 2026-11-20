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
STUDY_KEY_COLUMNS = {"ID", "arm_id", "dose_id", "result_id", "reference_arm_id", "linked_ID"}
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
                if "ID" in self.cb.columns(t):
                    self.db.execute(f'CREATE INDEX IF NOT EXISTS "{t}_ID" ON "{t}" ("ID")')
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT)")

    def close(self):
        self.db.close()

    def rows(self, table: str) -> list[dict]:
        cols = self.cb.columns(table)
        names = ", ".join(f'"{c}"' for c in cols)
        return [dict(zip(cols, r)) for r in
                self.db.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid')]

    def rows_of(self, table: str, ids: set, docs: set) -> list[dict]:
        """The rows of these studies, and the rows without a study that belong to these
        documents (a source, a consensus definition)."""
        cols = self.cb.columns(table)
        names = ", ".join(f'"{c}"' for c in cols)
        wanted = []
        if "ID" in cols:
            wanted.append(("ID", sorted(ids), ""))
        if "source_id" in cols:
            wanted.append(("source_id", sorted(docs), ' AND "ID" = \'\'' if "ID" in cols else ""))
        out = []
        for col, values, extra in wanted:
            for k in range(0, len(values), 500):
                part = values[k:k + 500]
                marks = ", ".join("?" for _ in part)
                out += [dict(zip(cols, r)) for r in self.db.execute(
                    f'SELECT {names} FROM "{table}" WHERE "{col}" IN ({marks}){extra}', part)]
        return out

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
        return self.import_tables(t, rep, where=str(folder))

    def import_tables(self, tables: dict, rep: ImportReport | None = None,
                      where: str = "the tables") -> ImportReport:
        """Add tables in today's layout ({table: [row dicts]}), in one transaction: all of
        them or nothing. Study IDs are renumbered VET-<n> from the database's count."""
        unknown = sorted(set(tables) - set(TABLES))
        if unknown:
            raise ValueError(f"nothing imported: {unknown} are not codebook tables")
        t = {name: tables.get(name, []) for name in TABLES}
        if rep is None:
            rep = ImportReport()
            rep.values_in = sum(sum((_values(n, rows) for n, rows in t.items()), Counter()).values())
        for name, rows in t.items():
            rep.rows_in[name] = len(rows)
        in_folder = {r["ID"] for r in t["studies"]}
        orphans = sorted({r[c] for rows in t.values() for r in rows for c in ("ID", "linked_ID")
                          if r.get(c) and r[c] not in in_folder})
        if orphans:
            raise ValueError(f"nothing imported: rows refer to studies that {where} has no "
                             f"studies row for, e.g. {orphans[:3]}")

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
                skipped = (r.get("ID") in skip_studies or r.get("linked_ID") in skip_studies
                           if r.get("ID") else r.get("source_id") in skip_docs)
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
            raise ValueError(f"nothing imported: two rows in {where} share a key ({e})") from e

        # read back what was stored and compare value by value, column by column
        new_ids, new_docs = set(id_map.values()), set(rep.new_documents)
        stored = Counter()
        for name in TABLES:
            stored.update(_values(name, self.rows_of(name, new_ids, new_docs)))
        rep.values_stored = sum(stored.values())
        rep.values_skipped = sum(values_skipped.values())
        rep.missing += values_in - stored                      # lost while storing
        return rep


    def _breeds_of_gone_groups(self, study_id: str, groups: set) -> tuple[int, int]:
        """A breeds row whose group is no longer among the study's groups is kept for the whole
        study, with its evidence; when that breed is already recorded for the whole study the row
        adds nothing and goes, with its evidence. Returns (kept for the whole study, gone)."""
        loose = doubled = 0
        rows = self.db.execute("SELECT arm_id, Breed FROM breeds WHERE ID = ? AND arm_id != ''",
                               (study_id,)).fetchall()
        for arm, breed in rows:
            if arm in groups:
                continue
            mine = (study_id, f"{arm}|{breed}")
            if self.db.execute("SELECT 1 FROM breeds WHERE ID = ? AND arm_id = '' AND Breed = ?",
                               (study_id, breed)).fetchone():
                self.db.execute("DELETE FROM breeds WHERE ID = ? AND arm_id = ? AND Breed = ?",
                                (study_id, arm, breed))
                self.db.execute("DELETE FROM evidence WHERE ID = ? AND \"table\" = 'breeds' "
                                "AND row_key = ?", mine)
                doubled += 1
            else:
                self.db.execute("UPDATE breeds SET arm_id = '' WHERE ID = ? AND arm_id = ? AND "
                                "Breed = ?", (study_id, arm, breed))
                self.db.execute("UPDATE evidence SET row_key = ? WHERE ID = ? AND \"table\" = "
                                "'breeds' AND row_key = ?", (f"|{breed}", *mine))
                loose += 1
        return loose, doubled

    # -- a study read again ------------------------------------------------------------
    def replace_reading(self, study_id: str, tables: dict) -> dict:
        """Put a new reading of one stored study in place of the old one, in one transaction:
        the values of its studies row, its groups, doses and results, and the evidence of
        those. The study keeps its ID, documents, topics, people, breeds, definitions, topic
        fields and links. A group keeps its id when its name is unchanged, or when the study
        has one group before and after, so rows that point at it still do; a breeds row whose
        group is gone is kept for the whole study. Sites are replaced only when no person is
        tied to one. tables: {"studies": [one row], "arms", "arm_doses", "results", "sites",
        "evidence"} with this study's ID. Returns counts."""
        if not self.db.execute("SELECT 1 FROM studies WHERE ID = ?", (study_id,)).fetchone():
            raise ValueError(f"no study {study_id} in the database")
        (study,) = tables["studies"]

        def flat(s) -> str:
            return " ".join(str(s).lower().split())

        old = [{"arm_id": a, "arm_label": b} for a, b in self.db.execute(
            "SELECT arm_id, arm_label FROM arms WHERE ID = ? ORDER BY arm_id", (study_id,))]
        new = tables.get("arms", [])
        by_name = {flat(r["arm_label"]): r["arm_id"] for r in old}
        if len(old) == 1 and len(new) == 1:
            ids = {new[0]["arm_id"]: old[0]["arm_id"]}
        else:
            ids = {r["arm_id"]: by_name[flat(r["arm_label"])] for r in new
                   if flat(r["arm_label"]) in by_name}
        used = set(ids.values())
        n = max([int(m.group(1)) for a in [r["arm_id"] for r in old + new]
                 if (m := re.search(r"-A(\d+)$", a))] or [0])
        for r in new:                                # a group with a new name: a number not used before
            if r["arm_id"] not in ids:
                n += 1
                ids[r["arm_id"]] = f"{study_id}-A{n}"
                used.add(ids[r["arm_id"]])

        def key(value: str) -> str:
            """A group's id, or a dose's (<group id>-D<n>), as it will be stored."""
            for was, now in ids.items():
                if value == was or value.startswith(was + "-D"):
                    return now + value[len(was):]
            return value

        keep_sites = bool(self.db.execute(
            "SELECT 1 FROM people WHERE ID = ? AND site_no != ''", (study_id,)).fetchone())
        replaced = ["arms", "arm_doses", "results"] + ([] if keep_sites else ["sites"])
        rows = {name: [dict(r) for r in tables.get(name, [])] for name in replaced}
        for name in ("arms", "arm_doses", "results"):
            for r in rows[name]:
                for c in ("arm_id", "dose_id", "reference_arm_id"):
                    if r.get(c):
                        r[c] = key(r[c])
        evidence = []
        for e in tables.get("evidence", []):
            if e["table"] != "studies" and e["table"] not in replaced:
                continue
            evidence.append({**e, "row_key": key(e["row_key"]) if e["table"] in ("arms", "arm_doses")
                             else e["row_key"]})
        with self.db:
            cols = [c for c in self.cb.columns("studies") if c != "ID"]
            self.db.execute("UPDATE studies SET " + ", ".join(f'"{c}" = ?' for c in cols)
                            + " WHERE ID = ?", [str(study.get(c, "")) for c in cols] + [study_id])
            marks = ", ".join("?" for _ in replaced + ["studies"])
            self.db.execute(f'DELETE FROM evidence WHERE ID = ? AND "table" IN ({marks})',
                            [study_id, *replaced, "studies"])
            for name in replaced:
                self.db.execute(f'DELETE FROM "{name}" WHERE ID = ?', (study_id,))
            for name, add in [*rows.items(), ("evidence", evidence)]:
                cols = self.cb.columns(name)
                self.db.executemany(
                    f'INSERT INTO "{name}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                    f'VALUES ({", ".join("?" for _ in cols)})',
                    [[str(r.get(c, "")) for c in cols] for r in add])
            loose, doubled = self._breeds_of_gone_groups(study_id, used)
        return {**{name: len(r) for name, r in rows.items()}, "evidence": len(evidence),
                "groups_that_kept_their_id": sum(v in {r["arm_id"] for r in old} for v in used),
                "breeds_set_to_the_whole_study": loose,
                "breeds_already_there_for_the_whole_study": doubled, "sites_kept": keep_sites}


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
    promoted = {}           # (ID, column) of an ext_* column that is now a studies field
    for name, (header, rows) in raw.items():
        if name in LEGACY_TOPIC_TABLES:
            topic = LEGACY_TOPIC_TABLES[name]
            for r in rows:
                for c, v in r.items():
                    if c == "ID" or not v:
                        continue
                    if cb.field("studies", c):
                        promoted[r["ID"], c] = v
                    else:
                        t["topic_fields"].append({"ID": r["ID"], "topic": topic, "field": c,
                                                  "value": v})
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
    for r in t["studies"]:
        for c in r:
            if not r[c] and (r["ID"], c) in promoted:
                r[c] = promoted[r["ID"], c]
    for r in t["evidence"] + t["conflicts"]:
        if r["table"] in LEGACY_TOPIC_TABLES and cb.field("studies", r["field"]):
            r["table"], r["row_key"] = "studies", ""
        elif r["table"] in LEGACY_TOPIC_TABLES:
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
