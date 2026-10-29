"""Checks tables against the codebook (schema v2 section 10). Nothing is fixed silently:
every problem becomes one Issue.

    vet check                               # the database
    vet check corpus/out/PMID31234567       # one document's folder

A document folder holds that document's tables; sources.csv and study_sources.csv are read
from the corpus folder when the checked folder has none.
"""
import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .codebook import TABLES, Codebook

SOURCE_ID = re.compile(r"(?:PMID\d+|DOI(?:-[A-Z0-9]+)+|FDA-A?NADA\d+|EMA-[A-Z0-9]+"
                       r"|REG(?:-[A-Z0-9]+){2,}|ABS(?:-[A-Z0-9]+){2,}|WEB-[A-Z0-9]+-\d{8}-\d+)"
                       r"(?:-\d+)?")
STUDY_ID = re.compile(r"VET-[1-9]\d*")
INTEGER = re.compile(r"\d+")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
YEAR = re.compile(r"\d{4}")
DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
SENTINELS = {"na", "n/a", "n.a.", "nan", "null", "none", "-", "--", "nr", "unknown",
             "not reported", "not stated", "not available", "not applicable"}
HEADERS = ("Inclusion Criteria:", "Exclusion Criteria:")
QUOTE_MAX = 500

PRIMARY_KEY = {"sources": ("source_id",), "study_sources": ("ID", "source_id"),
               "studies": ("ID",), "study_topics": ("ID", "topic"),
               "topic_fields": ("ID", "topic", "field"), "arms": ("arm_id",),
               "arm_doses": ("dose_id",), "results": ("result_id",), "sites": ("ID", "site_no"),
               "people": ("ID", "person_no"), "breeds": ("ID", "arm_id", "Breed"),
               "definitions": ("source_id", "ID", "term"),
               "conflicts": ("ID", "table", "row_key", "field", "secondary_source_id"),
               # a value may have several evidence rows: one per document, or one per part
               # of a list (e.g. a quote for each country)
               "evidence": ("ID", "table", "row_key", "field", "source_id", "quote", "location")}
CELL = ("ID", "table", "row_key", "field")       # the value an evidence row supports
# key columns that may be empty: whole-cohort result, whole-study breed, person with no
# stated site, definition from a consensus statement, evidence or conflict for a studies value
OPTIONAL_KEYS = {("results", "arm_id"), ("results", "reference_arm_id"),
                 ("breeds", "arm_id"), ("people", "site_no"), ("definitions", "ID"),
                 ("evidence", "row_key"), ("evidence", "quote"), ("evidence", "location"),
                 ("conflicts", "row_key")}
# (child, child columns, parent, parent columns, check name); skipped when a child column is empty
LINKS = [
    ("study_sources", ("source_id",), "sources", ("source_id",), "orphan"),
    ("studies", ("ID",), "study_sources", ("ID",), "orphan"),
    ("study_topics", ("ID",), "studies", ("ID",), "orphan"),
    ("topic_fields", ("ID", "topic"), "study_topics", ("ID", "topic"), "orphan"),
    ("arms", ("ID",), "studies", ("ID",), "orphan"),
    ("arm_doses", ("ID", "arm_id"), "arms", ("ID", "arm_id"), "orphan"),
    ("results", ("ID",), "studies", ("ID",), "orphan"),
    ("results", ("ID", "arm_id"), "arms", ("ID", "arm_id"), "orphan"),
    ("results", ("ID", "reference_arm_id"), "arms", ("ID", "arm_id"), "orphan"),
    ("results", ("ID", "source_id"), "study_sources", ("ID", "source_id"), "unlinked_source"),
    ("sites", ("ID",), "studies", ("ID",), "orphan"),
    ("people", ("ID",), "studies", ("ID",), "orphan"),
    ("people", ("ID", "source_id"), "study_sources", ("ID", "source_id"), "unlinked_source"),
    ("people", ("ID", "site_no"), "sites", ("ID", "site_no"), "orphan"),
    ("breeds", ("ID",), "studies", ("ID",), "orphan"),
    ("breeds", ("ID", "arm_id"), "arms", ("ID", "arm_id"), "orphan"),
    ("definitions", ("source_id",), "sources", ("source_id",), "orphan"),
    ("definitions", ("ID", "source_id"), "study_sources", ("ID", "source_id"), "unlinked_source"),
    ("evidence", ("ID", "source_id"), "study_sources", ("ID", "source_id"), "unlinked_source"),
    ("conflicts", ("ID",), "studies", ("ID",), "orphan"),
    ("conflicts", ("ID", "primary_source_id"), "study_sources", ("ID", "source_id"),
     "unlinked_source"),
    ("conflicts", ("ID", "secondary_source_id"), "study_sources", ("ID", "source_id"),
     "unlinked_source"),
]
# tables whose values evidence rows support, and how their row_key is built; topic_fields
# rows are supported too, with row_key = the topic (see _Run.evidence)
ROW_KEY = {"studies": (), "arms": ("arm_id",), "arm_doses": ("dose_id",), "sites": ("site_no",),
           "people": ("person_no",), "breeds": ("arm_id", "Breed")}


@dataclass(frozen=True)
class Issue:
    check: str
    table: str
    line: int        # CSV line (header = 1); 0 = the whole table
    key: str
    field: str
    message: str


def _key(row: dict, cols: tuple) -> tuple:
    return tuple(row.get(c, "") for c in cols)


def _valid_date(v: str) -> bool:
    m = DATE.fullmatch(v)
    if not m:
        return False
    y, mo, d = (int(x) if x else 1 for x in m.groups())
    try:
        date(y, mo, d)
        return True
    except ValueError:
        return False


def _criteria_problem(v: str) -> str:
    lines = [ln for ln in v.split("\n") if ln.strip()]
    if not lines or lines[0] not in HEADERS:
        return "must start with 'Inclusion Criteria:' or 'Exclusion Criteria:'"
    for h in HEADERS:
        if lines.count(h) > 1:
            return f"{h!r} appears twice"
    if HEADERS[1] in lines and HEADERS[0] in lines and lines.index(HEADERS[1]) < lines.index(HEADERS[0]):
        return "Exclusion Criteria come before Inclusion Criteria"
    bad = next((ln for ln in lines if ln not in HEADERS and not ln.startswith("* ")), None)
    return f"line is not a header or a '* ' criterion: {bad[:60]!r}" if bad else ""


class _Run:
    def __init__(self, tables: dict, cb: Codebook, source_id: str | None):
        self.t = {name: tables.get(name, []) for name in TABLES}
        self.cb = cb
        self.source_id = source_id
        self.issues: list[Issue] = []
        self.studies = {r.get("ID", ""): r for r in self.t["studies"]}
        self.topic_values = {_key(r, PRIMARY_KEY["topic_fields"]): r.get("value", "")
                             for r in self.t["topic_fields"]}

    def add(self, check, table, i, row, field, message):
        key = "|".join(_key(row, PRIMARY_KEY[table])) if row else ""
        self.issues.append(Issue(check, table, i + 2 if row else 0, key, field, message))

    def rows(self, table):
        return enumerate(self.t[table])

    # -- structure ---------------------------------------------------------------------
    def headers(self, headers: dict):
        for table, got in headers.items():
            want = self.cb.columns(table)
            if got != want:
                missing = [c for c in want if c not in got]
                extra = [c for c in got if c not in want]
                detail = (f"missing {missing}" if missing else "") + \
                         (f" extra {extra}" if extra else "") or "columns in the wrong order"
                self.add("header", table, 0, None, "", f"header must match the template: {detail}")
        for table in TABLES:
            for i, row in self.rows(table):
                if None in row:
                    self.add("header", table, i, row, "", "row has more cells than the header")

    def cells(self):
        for table in TABLES:
            for f in self.cb.tables[table]:
                if (table, f.name) == ("topic_fields", "value"):
                    continue                  # checked against the topic's own definition
                for i, row in self.rows(table):
                    v = row.get(f.name, "")
                    if v:
                        self.cell(table, i, row, f, v)
        for i, row in self.rows("topic_fields"):
            f = self.cb.topic_field(row.get("topic", ""), row.get("field", ""))
            if f and row.get("value"):
                self.cell("topic_fields", i, row, f, row["value"])

    def cell(self, table, i, row, f, v):
        add = lambda check, msg: self.add(check, table, i, row, f.name, msg)
        copied = table == "evidence" and f.name == "value"   # mirrors a field, checked there
        if v.strip().lower() in SENTINELS and v not in f.allowed and not copied:
            return add("sentinel", f"{v!r} is a missing-value marker; leave the cell empty")
        if f.kind == "K":
            self.key_format(table, i, row, f.name, v)
        if f.type in ("integer", "number", "year"):
            pattern = {"integer": INTEGER, "number": NUMBER, "year": YEAR}[f.type]
            if not pattern.fullmatch(v):
                add("number", f"{v!r} is not a plain {f.type}")
        elif f.type == "date" and not _valid_date(v):
            add("date", f"{v!r} is not an ISO date (YYYY, YYYY-MM or YYYY-MM-DD)")
        elif f.type == "enum" and v not in f.allowed:
            add("enum", f"{v!r} is not an allowed value")
        elif f.type == "pipe_list":
            items = v.split("|")
            if any(not x or x != x.strip() for x in items):
                add("list", "pipe list has an empty item or spaces around a pipe")
            elif f.allowed and any(x not in f.allowed for x in items):
                add("enum", f"not allowed: {[x for x in items if x not in f.allowed]}")
            elif f.allowed == self.cb.countries and items != sorted(items):
                add("list", "countries must be sorted alphabetically")
        elif f.type == "criteria":
            problem = _criteria_problem(v)
            if problem:
                add("criteria_layout", problem)
        if f.max_len and len(v) > f.max_len:
            add("too_long", f"{len(v)} characters; at most {f.max_len}")

    def key_format(self, table, i, row, name, v):
        sid = row.get("ID", "")
        bad = ""
        if name == "source_id" and not SOURCE_ID.fullmatch(v):
            bad = "source_id does not follow the key rules"
        elif name == "ID" and not STUDY_ID.fullmatch(v):
            bad = "ID must be VET-<n>"
        elif name in ("arm_id", "reference_arm_id") and not re.fullmatch(re.escape(sid) + r"-A[1-9]\d*", v):
            bad = f"{name} must be <ID>-A<n> of this row's study"
        elif name == "dose_id" and not re.fullmatch(re.escape(row.get("arm_id", "")) + r"-D[1-9]\d*", v):
            bad = "dose_id must be <arm_id>-D<n>"
        elif name == "result_id" and not re.fullmatch(re.escape(sid) + r"-R[1-9]\d*", v):
            bad = "result_id must be <ID>-R<n>"
        if bad:
            self.add("key_format", table, i, row, name, bad)

    # -- integrity ---------------------------------------------------------------------
    def keys(self):
        for table, cols in PRIMARY_KEY.items():
            seen = {}
            for i, row in self.rows(table):
                for c in cols:
                    if not row.get(c) and (table, c) not in OPTIONAL_KEYS:
                        self.add("key_missing", table, i, row, c, "key is empty")
                k = _key(row, cols)
                if k in seen:
                    self.add("duplicate_key", table, i, row, "", f"same key as line {seen[k]}")
                seen.setdefault(k, i + 2)

    def links(self):
        for child, ccols, parent, pcols, check in LINKS:
            have = {_key(r, pcols) for r in self.t[parent]}
            for i, row in self.rows(child):
                k = _key(row, ccols)
                if all(k) and k not in have:
                    self.add(check, child, i, row, ccols[-1],
                             f"{'/'.join(k)} not found in {parent}.{'/'.join(pcols)}")

    def study_documents(self):
        roles = {}
        for row in self.t["study_sources"]:
            roles.setdefault(row.get("ID", ""), []).append(row)
        for sid, rows in roles.items():
            primaries = [r for r in rows if r.get("role") == "primary"]
            i = self.t["study_sources"].index(rows[0])
            if len(primaries) != 1:
                self.add("primary_count", "study_sources", i, rows[0], "role",
                         f"{len(primaries)} primary documents; exactly one needed")
            if self.source_id is None and sid not in self.studies:
                self.add("missing_study_row", "study_sources", i, rows[0], "ID",
                         "study listed in study_sources has no studies row")

    def documents(self):
        """The same paper twice under two keys: any shared DOI, PMID or PMCID. Abstracts in
        one conference supplement can share a DOI, so their DOI is not compared."""
        seen = {}
        for i, row in self.rows("sources"):
            doi = "" if row.get("doc_type") == "conference_abstract" else row.get("doi", "")
            for name, value in (("doi", doi.strip().lower()), ("pmid", row.get("pmid", "")),
                                ("pmcid", row.get("pmcid", "").upper())):
                if not value:
                    continue
                if (name, value) in seen:
                    self.add("duplicate_document", "sources", i, row, name,
                             f"same {name} as {seen[name, value]}: one document stored twice")
                seen.setdefault((name, value), row.get("source_id", ""))

    def topics(self):
        for table in ("study_topics", "topic_fields"):
            for i, row in self.rows(table):
                if row.get("topic") and row["topic"] not in self.cb.topics:
                    self.add("unknown_topic", table, i, row, "topic",
                             f"no topics/{row['topic']}.toml")
        for i, row in self.rows("topic_fields"):
            topic, name = row.get("topic", ""), row.get("field", "")
            if topic in self.cb.topics and name and not self.cb.topic_field(topic, name):
                self.add("unknown_field", "topic_fields", i, row, "field",
                         f"{name!r} is not a field of topic {topic}")

    def document_scope(self):
        """In out/<source_id>/ every row belongs to that document (working rule 4)."""
        linked = {r.get("ID") for r in self.t["study_sources"] if r.get("source_id") == self.source_id}
        for table in TABLES:
            if table in ("sources", "study_sources"):
                continue
            for i, row in self.rows(table):
                if row.get("ID") and row["ID"] not in linked:
                    self.add("unlinked_source", table, i, row, "ID",
                             f"study is not linked to {self.source_id} in study_sources")
                if row.get("source_id") and row["source_id"] != self.source_id:
                    self.add("wrong_source", table, i, row, "source_id",
                             f"this folder holds only {self.source_id}")

    # -- evidence ----------------------------------------------------------------------
    def _supported(self) -> dict:
        """Every cell an evidence row can support, keyed like evidence rows:
        (ID, table, row_key, field) -> (Field, value, table, i, row)."""
        out = {}
        for table, cols in ROW_KEY.items():
            for i, row in self.rows(table):
                row_key = "|".join(_key(row, cols))
                for f in self.cb.tables[table]:
                    out[row.get("ID", ""), table, row_key, f.name] = (f, row.get(f.name, ""),
                                                                      table, i, row)
        for i, row in self.rows("topic_fields"):
            f = self.cb.topic_field(row.get("topic", ""), row.get("field", ""))
            if f:
                out[row.get("ID", ""), "topic_fields", row["topic"], f.name] = (
                    f, row.get("value", ""), "topic_fields", i, row)
        return out

    def _target_problem(self, cell, ev) -> tuple | None:
        """What is wrong with the cell an evidence row points at: (check, column, message)."""
        table, field, row_key = ev.get("table", ""), ev.get("field", ""), ev.get("row_key", "")
        if table in ROW_KEY and not self.cb.field(table, field):
            return "evidence_unmatched", "field", f"{table} has no field {field!r}"
        if table == "topic_fields" and not self.cb.topic_field(row_key, field):
            return "evidence_unmatched", "field", f"topic {row_key!r} has no field {field!r}"
        if table not in ROW_KEY and table != "topic_fields":
            return None                      # an unknown table is reported by the enum check
        if cell is None and table == "topic_fields":
            return "evidence_unmatched", "field", f"no {field} value for topic {row_key!r}"
        if cell is None:
            return "evidence_unmatched", "row_key", f"no {table} row with this key"
        if not cell[1]:
            return "evidence_unmatched", "field", f"{table}.{field} is empty"
        if cell[1] != ev.get("value", ""):
            return ("evidence_value_mismatch", "value",
                    f"evidence says {ev.get('value')!r}, table has {cell[1]!r}")
        return None

    def evidence(self):
        cells = self._supported()
        index = {}
        for i, ev in self.rows("evidence"):
            key = _key(ev, CELL)
            index.setdefault(key, []).append(ev)
            quote = ev.get("quote", "")
            if len(quote) > QUOTE_MAX and ev.get("quote_truncated") != "yes":
                self.add("quote_too_long", "evidence", i, ev, "quote",
                         f"{len(quote)} characters; cut to {QUOTE_MAX} and set quote_truncated = yes")
            problem = self._target_problem(cells.get(key), ev)
            if problem:
                self.add(problem[0], "evidence", i, ev, problem[1], problem[2])
        for key, (f, value, table, i, row) in cells.items():
            if f.evidence == "none" or not value:
                continue
            evs = index.get(key, [])
            if not evs:
                self.add("missing_evidence", table, i, row, f.name,
                         f"needs an evidence row ({f.evidence})")
            elif f.evidence == "quote" and not any(ev.get("quote") for ev in evs):
                self.add("quote_missing", table, i, row, f.name, "evidence row has no quote")
            elif f.evidence == "location" and not any(ev.get("location") for ev in evs):
                self.add("location_missing", table, i, row, f.name, "evidence row has no location")

    # -- applicability -----------------------------------------------------------------
    def applicability(self):
        for table in TABLES:
            if table in ("sources", "study_sources"):
                continue
            limited = [f for f in self.cb.tables[table] if f.applies and f.kind != "K"]
            for i, row in self.rows(table):
                study = self.studies.get(row.get("ID", ""))
                if study is None:
                    continue
                for f in limited:
                    if row.get(f.name) and not f.applies_to(study):
                        self.add("not_applicable_filled", table, i, row, f.name,
                                 "field does not apply to this study ("
                                 + ", ".join(f"{c} = {study.get(c, '')!r}" for c, _ in f.applies) + ")")
        topic_filled = {(sid, name) for (sid, _, name), v in self.topic_values.items() if v}
        for i, row in self.rows("studies"):
            for name in filter(None, row.get("not_applicable", "").split("|")):
                if row.get(name) or (row.get("ID", ""), name) in topic_filled:
                    self.add("not_applicable_filled", "studies", i, row, name,
                             "listed in not_applicable but filled")


def validate(tables: dict, cb: Codebook, source_id: str | None = None,
             headers: dict | None = None) -> list[Issue]:
    """source_id=None checks a merged set; a source_id checks that document's folder."""
    run = _Run(tables, cb, source_id)
    if headers:
        run.headers(headers)
    run.cells()
    run.keys()
    run.links()
    run.documents()
    run.topics()
    run.study_documents()
    if source_id:
        run.document_scope()
    run.evidence()
    run.applicability()
    return run.issues


def load_folder(folder: Path, corpus: Path) -> tuple[dict, dict]:
    tables, headers = {}, {}
    for t in TABLES:
        path = folder / f"{t}.csv"
        if not path.exists() and t in ("sources", "study_sources"):
            path = corpus / f"{t}.csv"
        if not path.exists():
            tables[t] = []
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            tables[t] = [{k: v if v is not None else "" for k, v in row.items()} for row in reader]
            headers[t] = reader.fieldnames or []
    return tables, headers


def write_report(issues: list[Issue], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["check", "table", "line", "key", "field", "message"])
        w.writerows([i.check, i.table, i.line, i.key, i.field, i.message] for i in issues)
