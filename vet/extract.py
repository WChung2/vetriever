"""Extraction (pipeline step 9): one document's text into the codebook's tables, every value
checked against the document, straight into the database.

    vet extract <topic> <source_id>…

The model fills four forms in turn, each generated from codebook.csv and the topic's
[[fields]], and answers in a JSON schema generated from the same form. Every table has one
home:

    1 study    sources, study_sources, studies, study_topics, sites; and the gate
    2 arms     arms, arm_doses (each arm's doses inside it)
    3 results  results (outcomes, condition at entry, adverse events)
    4 people   people, breeds, definitions, topic_fields (the topic's own fields), conflicts

evidence has no step of its own: code writes its rows from the quote or location given with
each value. Code fills every key (study, arm, dose and result IDs, site and person numbers);
the model only picks among the keys it is shown, and any key it makes up is ignored. The study
is VET-1 until the import gives it the database's next number.

The gate: step 1 also asks whether the document reports a study of the topic's condition in
the topic's species ([screen] in the topic file). A "no" whose quote is in the document stops
the extraction: nothing is stored and the reason is logged.

After each step every value is checked with validate(). The values that fail go back to the
model once, in one repair call naming each problem; what still fails is emptied with its
evidence (or its row dropped, when the row means nothing without it) and logged. Then the
whole document is imported in one transaction: all of it or none of it. Problems and gate
stops go to the extraction_log table in the same database.

A paper with no full text is extracted from its title and abstract (`write_abstract`), with
sources.access = abstract_only; what the abstract doesn't give stays empty.
"""
import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .codebook import KINDS, TABLES, Codebook, Field
from .settings import DB, ROOT
from .store import Store, _identifiers, document_key
from .validate import ROW_KEY, SOURCE_ID, _flat, validate

STEPS = {
    "study": ("sources", "study_sources", "studies", "study_topics", "sites"),
    "arms": ("arms", "arm_doses"),
    "results": ("results",),
    "people": ("people", "breeds", "definitions", "topic_fields", "conflicts"),
}
TITLES = {"study": "the study", "arms": "arms and doses", "results": "results",
          "people": "people, breeds, definitions and the topic's own fields"}
HOW = {
    "sources": "one row, the document itself",
    "studies": "one row, the study this document reports",
    "sites": "one row per site where animals were enrolled",
    "arms": "one row per arm (treatment group), each with its doses in arm_doses",
    "arm_doses": "inside each arm, one row per drug and dose",
    "results": "one row per number or finding, each with its own sentence or table row in "
               "as_written; every number in the row must be in its as_written",
    "people": "one row per person named as author, investigator or study director",
    "breeds": "one row per breed (per arm, when the document gives breeds per arm)",
    "definitions": "one row per term the document defines",
    "topic_fields": "this topic's own fields, one value each",
    "conflicts": "one row per value the document states two different ways",
}
PROVISIONAL = "VET-1"        # the study's ID until the import numbers it
# fields code fills; every other field of a step's tables is on the model's form
CODE = {
    "sources": {"source_id", "pmid", "pmcid", "url", "access", "file_name", "text_file",
                "retrieved_date"},
    "study_sources": {"ID", "source_id", "role"},
    "studies": {"ID"},
    "study_topics": {"ID", "topic"},
    "topic_fields": {"ID", "topic", "field"},
    "arms": {"ID", "arm_id"},
    "arm_doses": {"ID", "arm_id", "dose_id"},
    "results": {"result_id", "ID", "source_id"},
    "sites": {"ID", "site_no"},
    "people": {"ID", "source_id", "person_no"},
    "breeds": {"ID"},
    "definitions": {"source_id", "ID"},
    "conflicts": {"ID", "primary_source_id", "secondary_source_id", "resolution"},
}
# key fields the model fills by choosing one of the keys it is shown
REFS = {("results", "arm_id"): "arms", ("results", "reference_arm_id"): "arms",
        ("breeds", "arm_id"): "arms", ("people", "site_no"): "sites"}
REF_NAMES = {"arms": "arm IDs", "sites": "site numbers"}
ONE_ROW = ("sources", "studies", "topic_fields")
INSIDE = {"arm_doses": "arms"}           # rows given inside their parent row
RESULT_REQUIRED = ("result_kind", "arm_id", "endpoint", "as_written", "location")
# a row is dropped, not just emptied, when one of these still fails: nothing backs or
# identifies it without them (an unknown arm would turn a result into a whole-cohort one)
MUST_FILL = {("results", "as_written"), ("definitions", "definition_verbatim"),
             ("arm_doses", "drug"), ("people", "Name")}
ROW_FIELDS = MUST_FILL | {("results", "result_kind"), ("results", "arm_id"),
                          ("results", "reference_arm_id"), ("breeds", "arm_id"),
                          ("breeds", "Breed"), ("definitions", "term"), ("conflicts", "table"),
                          ("conflicts", "field"), ("topic_fields", "value")}
LOG_COLUMNS = ("source_id", "ID", "step", "table", "row", "field", "value", "problem", "date")
# results come in one call per kind, so a long results section fits in the answers, then one
# call for the tables no result row cites yet
RESULT_PARTS = {
    "baseline": "the animals' condition at entry: every characteristic the document reports, "
                "per arm or for the whole study (each row of a baseline table, each such "
                "number in the text)",
    "outcome": "every outcome: each endpoint for each arm, subgroup and timepoint, each "
               "comparison between arms, deaths counted as an endpoint, response and remission "
               "rates (each row of every results table, each such number in the text). Values "
               "measured at entry or at diagnosis are baseline, not outcomes: leave them out",
    "adverse_event": "every adverse event, complication or death reported as a harm, per arm",
}
PART_FORM = """## This call: result_kind = {kind} only
Give {what}. Don't choose among them: the database needs every one. Leave out the fields you
leave empty."""
TABLES_FORM = """## This call: tables with no result yet
No result row cites {tables} yet. Give every result they report (any result_kind; values at
entry or at diagnosis are baseline), or none for a table that reports no results (for
example a drug protocol). Leave out the fields you
leave empty."""
TABLE_REF = re.compile(r"\btable\s+(\d+)\b", re.IGNORECASE)
REPAIR = """## Form: corrections
Code checked your answer against the document and the codebook, and the values below failed.
Give each one again, corrected, with the evidence its field needs, or leave it empty if the
document doesn't state it. Each item names the value (row.field), what you gave, the problem,
and the field's form entry.
"""


class ExtractionFailed(Exception):
    """Nothing of the document can be stored; the message says why."""


@dataclass
class Cell:
    value: str = ""
    quote: str = ""
    location: str = ""


@dataclass(eq=False)
class Row:
    table: str
    ref: str           # the row's name in prompts and the log: studies, VET-1-A2, person 1…
    keys: dict         # filled by code
    cells: dict        # field name -> Cell, filled by the model


@dataclass
class ExtractReport:
    source_id: str
    gate: str = ""         # yes | no | unclear, or unbacked_no: a "no" whose quote isn't there
    stopped: str = ""      # why nothing was stored; empty when the document was imported
    stored_as: str = ""    # the document's key in the database
    study: str = ""        # the study's ID in the database
    values: int = 0        # values stored
    repaired: int = 0      # values the repair round fixed
    emptied: int = 0       # values given but not stored (each one in extraction_log)
    calls: int = 0
    usd: float = 0.0

    def line(self) -> str:
        if self.stopped:
            return f"  {self.source_id}: nothing stored: {self.stopped}"
        return (f"  {self.stored_as}: {self.study}, {self.values} values stored, "
                f"{self.repaired} repaired, {self.emptied} emptied; {self.calls} calls, "
                f"${self.usd:.4f}")


# -- forms and answer schemas -------------------------------------------------------------

def _model_fields(cb: Codebook, table: str, topic_name: str) -> list[Field]:
    if table == "topic_fields":
        return list(cb.topic_fields[topic_name].values())
    return [f for f in cb.tables[table] if f.name not in CODE[table]]


def form_fields(step: str, cb: Codebook, topic_name: str) -> list[tuple[str, Field, bool]]:
    """(table, field, filled by code) for every field of the step's tables; the topic's own
    fields stand in for topic_fields.value."""
    out = []
    for table in STEPS[step]:
        out += [(table, f, True) for f in cb.tables[table] if f.name in CODE[table]]
        out += [(table, f, False) for f in _model_fields(cb, table, topic_name)]
    return out


def _screen(topic: dict) -> dict:
    s = topic.get("screen", {})
    if not s.get("species") or not s.get("condition"):
        raise ValueError("the topic file's [screen] needs species and condition: the gate "
                         "asks whether a document reports a study of them")
    return s


def _gate_form(topic: dict) -> str:
    s = _screen(topic)
    also = f" (also called {'; '.join(s['also_called'])})" if s.get("also_called") else ""
    related = (f" A related condition counts as yes: {'; '.join(s['related'])}."
               if s.get("related") else "")
    return (f"\n### gate (answer it first)\nDoes the document report a study of "
            f"{s['condition']}{also} in {s['species']}: animals with the condition, as a group "
            f"or as a case?{related}\n- answer: yes | no | unclear\n- quote: the shortest "
            "exact passage that shows the answer (\"\" when unclear)\nCode stops on a no whose "
            "quote is in the document. Fill the tables below whatever you answer.")


def _line(cb: Codebook, table: str, f: Field, topic_name: str) -> str:
    bits = [KINDS[f.kind], f.type]
    if (table, f.name) in REFS:
        bits.append(f"one of the {REF_NAMES[REFS[table, f.name]]} under Keys, or empty")
    elif f.allowed == cb.countries:
        bits.append("names from the Country names list at the end")
    elif f.name == "not_applicable":
        bits.append("names of studies fields or of the topic's fields: "
                    + ", ".join(cb.topic_fields[topic_name]))
    elif f.allowed:
        bits.append("one of: " + " | ".join(f.allowed))
    if f.applies:
        bits.append("only when " + "; ".join(f"{c} = {' or '.join(v)}" for c, v in f.applies))
    if f.max_len:
        bits.append(f"at most {f.max_len} characters")
    bits.append(f"evidence: {f.evidence}")
    return (f"- {f.name} ({', '.join(bits)})\n  {f.description}"
            + (f" Rule: {f.rule}" if f.rule else ""))


def form(step: str, cb: Codebook, topic_name: str, topic: dict) -> str:
    """The step's form: each field with its kind, type, allowed values, rule, evidence and
    the studies it applies to."""
    out = [f"## Form, step {list(STEPS).index(step) + 1} of {len(STEPS)}: {TITLES[step]}"]
    if step == "study":
        out.append(_gate_form(topic))
    entries = form_fields(step, cb, topic_name)
    for table in STEPS[step]:
        model = [f for t, f, by_code in entries if t == table and not by_code]
        code = ", ".join(f.name for t, f, by_code in entries if t == table and by_code)
        if not model:
            out.append(f"\n### {table}: filled by code ({code})")
            continue
        out.append(f"\n### {table}: {HOW[table]}")
        out += [_line(cb, table, f, topic_name) for f in model]
        if code:
            out.append(f"Code fills: {code}.")
    if any(f.allowed == cb.countries for _, f, by_code in entries if not by_code):
        out.append("\n### Country names\n" + " | ".join(cb.countries))
    return "\n".join(out)


def _object(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": list(props) if required is None else required}


def _value(cb: Codebook, table: str, f: Field, keys: dict) -> dict:
    """One value: a string, or the value with its quote or location when it needs one."""
    v = {"type": "string"}
    ref = REFS.get((table, f.name))
    if ref and keys.get(ref):
        v["enum"] = ["", *keys[ref]]
    elif f.type == "enum" and f.allowed != cb.countries:
        v["enum"] = ["", *f.allowed]
    if f.evidence == "none":
        return v
    evidence = {"quote": {"type": "string"}} if f.evidence == "quote" else {}
    return _object({"value": v, **evidence, "location": {"type": "string"}},
                   ["value", f.evidence])


def _row_schema(cb: Codebook, table: str, topic_name: str, keys: dict) -> dict:
    fields = _model_fields(cb, table, topic_name)
    props = {f.name: _value(cb, table, f, keys) for f in fields}
    for child, parent in INSIDE.items():
        if parent == table:
            props[child] = {"type": "array", "items": _row_schema(cb, child, topic_name, keys)}
    return _object(props, list(RESULT_REQUIRED) if table == "results" else None)


GATE = _object({"answer": {"type": "string", "enum": ["yes", "no", "unclear"]},
                "quote": {"type": "string"}})


def schema(step: str, cb: Codebook, topic_name: str, keys: dict | None = None) -> dict:
    """The JSON the model answers in, from the same fields as the form. keys: the arm IDs
    and site numbers assigned so far, which the model may pick from."""
    props = {"gate": GATE} if step == "study" else {}
    for table in STEPS[step]:
        if table in INSIDE or not _model_fields(cb, table, topic_name):
            continue
        row = _row_schema(cb, table, topic_name, keys or {})
        props[table] = row if table in ONE_ROW else {"type": "array", "items": row}
    return _object(props)


# -- one document -------------------------------------------------------------------------

def _cell(raw, f: Field | None, cb: Codebook) -> Cell:
    """An answer's value as a cell. Code only tidies lists: no spaces around |, countries
    sorted (the codebook's format); everything else is checked as given."""
    if isinstance(raw, dict):
        c = Cell(*("" if raw.get(k) is None else str(raw[k]).strip()
                   for k in ("value", "quote", "location")))
    else:
        c = Cell("" if raw is None else str(raw).strip())
    if f and f.type == "pipe_list" and c.value:
        items = [x.strip() for x in c.value.split("|") if x.strip()]
        c.value = "|".join(sorted(items) if f.allowed == cb.countries else items)
    return c


def _short(ref: str) -> str:
    return ref.removeprefix(PROVISIONAL + "-")


class _Doc:
    def __init__(self, source_id: str, topic_name: str, topic: dict, llm, cb: Codebook,
                 root: Path, record: dict):
        self.source_id, self.topic_name, self.topic = source_id, topic_name, topic
        self.llm, self.cb, self.root, self.record = llm, cb, root, record
        self.text = (root / "docs" / f"{source_id}.txt").read_text("utf-8")
        self.flat = _flat(self.text)
        self.system = (ROOT / "prompts" / "structure.md").read_text("utf-8")
        self.rows: list[Row] = []
        self.log: list[dict] = []
        self.step = "study"
        self.report = ExtractReport(source_id)

    def run(self):
        for step in STEPS:
            self.step = step
            if step == "results":
                new = self._results()
            else:
                answer = self._ask(form(step, self.cb, self.topic_name, self.topic),
                                   schema(step, self.cb, self.topic_name, self._key_lists()))
                if step == "study" and self._gate(answer.get("gate")):
                    return
                new = self._rows(step, answer)
                self.rows += new
            found = self._check()
            mine = {k: v for k, v in found.items() if k[0] in new and k[1]}
            if mine:
                self._repair(mine)
            self._settle()

    def _results(self) -> list[Row]:
        """One call per kind of result, then one for the tables no result row cites."""
        body, new = form("results", self.cb, self.topic_name, self.topic), []
        for kind, what in RESULT_PARTS.items():
            answer = self._ask(body + "\n\n" + PART_FORM.format(kind=kind, what=what),
                               self._results_schema([kind]))
            got = [r for r in answer.get("results") or [] if isinstance(r, dict)
                   and str(r.get("result_kind") or kind) == kind]     # another kind: not asked
            for r in got:
                r["result_kind"] = kind
            new += self._add(self._list("results", got))
        cited = {n for r in self.rows if r.table == "results"
                 for n in TABLE_REF.findall(r.cells["location"].value)}
        missing = sorted(set(TABLE_REF.findall(self.text)) - cited, key=int)
        if missing:
            tables = ", ".join(f"Table {n}" for n in missing)
            answer = self._ask(body + "\n\n" + TABLES_FORM.format(tables=tables),
                               self._results_schema(list(RESULT_PARTS)))
            new += self._add(self._list("results", answer.get("results")))
        return new

    def _add(self, rows: list[Row]) -> list[Row]:
        self.rows += rows
        return rows

    def _results_schema(self, kinds: list[str]) -> dict:
        s = schema("results", self.cb, self.topic_name, self._key_lists())
        s["properties"]["results"]["items"]["properties"]["result_kind"]["enum"] = kinds
        return s

    # -- the model ------------------------------------------------------------------------
    def _ask(self, body: str, answer_schema: dict) -> dict:
        prompt = (f'<document source_id="{self.source_id}">\n{self.text}\n</document>\n\n'
                  f"## Keys assigned so far\n{self._keys_text()}\n\n{body}")
        self.report.calls += 1
        try:
            answer = self.llm.call("structure", prompt, answer_schema, system=self.system,
                                   doc=self.source_id)
        except json.JSONDecodeError as e:
            raise ExtractionFailed(f"the model's answer is not JSON (cut off?): {e}") from e
        return answer if isinstance(answer, dict) else {}

    def _keys_text(self) -> str:
        lines = [f"- study ID: {PROVISIONAL}"]
        for table, key, label, name in (("arms", "arm_id", "arm IDs", "arm_label"),
                                        ("arm_doses", "dose_id", "dose IDs", "drug"),
                                        ("sites", "site_no", "site numbers", "Facility")):
            rows = [r for r in self.rows if r.table == table]
            if rows:
                lines.append(f"- {label}: " + "; ".join(
                    f"{r.keys[key]} = {r.cells[name].value or '(no name)'}" for r in rows))
        return "\n".join(lines)

    def _key_lists(self) -> dict:
        return {"arms": [r.keys["arm_id"] for r in self.rows if r.table == "arms"],
                "sites": [r.keys["site_no"] for r in self.rows if r.table == "sites"]}

    def _gate(self, gate) -> bool:
        """True when the document is not a study of the topic: a "no" backed by its quote."""
        gate = gate if isinstance(gate, dict) else {}
        answer, quote = str(gate.get("answer") or ""), str(gate.get("quote") or "").strip()
        backed = bool(quote) and _flat(quote) in self.flat
        self.report.gate = "unbacked_no" if answer == "no" and not backed else answer
        if answer != "no":
            return False
        s = _screen(self.topic)
        what = f"not a study of {s['condition']} in {s['species']}"
        if backed:
            self.report.stopped = f"gate: {what}: {quote!r}"
            self._note("", "", "gate", "no", f"{what}: {quote}")
            return True
        self._note("", "", "gate", "no", f"answered no, but its quote is not in the document: "
                   f"extraction went on ({quote!r})")
        return False

    # -- answers into rows ----------------------------------------------------------------
    def _cells(self, table: str, got) -> dict:
        got = got if isinstance(got, dict) else {}
        return {f.name: _cell(got.get(f.name), f, self.cb)
                for f in _model_fields(self.cb, table, self.topic_name)}

    def _rows(self, step: str, answer: dict) -> list[Row]:
        out, base = [], {"ID": PROVISIONAL}
        for table in STEPS[step]:
            got = answer.get(table)
            if table == "study_sources":
                out.append(Row(table, table, base | {"source_id": self.source_id,
                                                     "role": "primary"}, {}))
            elif table == "study_topics":
                out.append(Row(table, table, base | {"topic": self.topic_name}, {}))
            elif table == "sources":
                out.append(self._source(got))
            elif table == "studies":
                out.append(Row(table, table, dict(base), self._cells(table, got)))
            elif table == "topic_fields":
                got = got if isinstance(got, dict) else {}
                for name, f in self.cb.topic_fields[self.topic_name].items():
                    cell = _cell(got.get(name), f, self.cb)
                    if cell.value:
                        out.append(Row(table, "topic", base | {"topic": self.topic_name,
                                                               "field": name}, {"value": cell}))
            elif table not in INSIDE:
                out += self._list(table, got)
        return out

    def _list(self, table: str, got, parent: Row | None = None) -> list[Row]:
        out, n = [], 0
        for item in got if isinstance(got, list) else []:
            item = item if isinstance(item, dict) else {}
            cells = self._cells(table, item)
            children = [c for c, p in INSIDE.items() if p == table]
            if not any(c.value for c in cells.values()) and not any(item.get(c) for c in children):
                continue
            n += 1
            row = Row(table, *self._keys_for(table, n, parent), cells)
            out.append(row)
            for child in children:
                out += self._list(child, item.get(child), row)
        return out

    def _keys_for(self, table: str, n: int, parent: Row | None) -> tuple[str, dict]:
        """The row's name and the keys code gives it; the model's own keys are never read."""
        base, doc = {"ID": PROVISIONAL}, self.source_id
        if table == "arms":
            arm = f"{PROVISIONAL}-A{n}"
            return arm, base | {"arm_id": arm}
        if table == "arm_doses":
            dose = f"{parent.keys['arm_id']}-D{n}"
            return dose, base | {"arm_id": parent.keys["arm_id"], "dose_id": dose}
        if table == "results":         # numbered on from the rows of earlier calls
            rid = f"{PROVISIONAL}-R{sum(r.table == 'results' for r in self.rows) + n}"
            return rid, base | {"result_id": rid, "source_id": doc}
        if table == "sites":
            return f"site {n}", base | {"site_no": str(n)}
        if table == "people":
            return f"person {n}", base | {"source_id": doc, "person_no": str(n)}
        if table == "definitions":
            return f"definition {n}", base | {"source_id": doc}
        if table == "conflicts":       # within one document: a person decides which holds
            return f"conflict {n}", base | {"primary_source_id": doc, "secondary_source_id": doc,
                                            "resolution": "unresolved"}
        return f"breed {n}", base

    def _source(self, got) -> Row:
        rec = self.record
        keys = {"source_id": self.source_id, "text_file": f"docs/{self.source_id}.txt"} | {
            k: str(rec.get(k) or "") for k in CODE["sources"] - {"source_id", "text_file"}}
        cells = self._cells("sources", got)
        if rec.get("doi"):                  # the index record's DOI, not the model's copy
            keys["doi"] = str(rec["doi"])
            del cells["doi"]
        return Row("sources", "sources", keys, cells)

    # -- checks ---------------------------------------------------------------------------
    def _field(self, row: Row, name: str) -> Field | None:
        if row.table == "topic_fields":
            return self.cb.topic_field(self.topic_name, row.keys["field"])
        return self.cb.field(row.table, name)

    def _tables(self) -> tuple[dict, dict]:
        """The tables as validate() reads them, and the Row (or Row and field, for evidence)
        behind each line."""
        tables, owners = {t: [] for t in TABLES}, {t: [] for t in TABLES}
        for row in self.rows:
            values = {c: "" for c in self.cb.columns(row.table)} | row.keys | {
                n: c.value for n, c in row.cells.items()}
            tables[row.table].append(values)
            owners[row.table].append(row)
            if row.table not in ROW_KEY and row.table != "topic_fields":
                continue
            row_key = (row.keys["topic"] if row.table == "topic_fields"
                       else "|".join(values[c] for c in ROW_KEY[row.table]))
            for name, cell in row.cells.items():
                f = self._field(row, name)
                if not cell.value or f.evidence == "none" or not (cell.quote or cell.location):
                    continue
                quote = cell.quote if f.evidence == "quote" else ""
                tables["evidence"].append({
                    "ID": PROVISIONAL, "source_id": self.source_id, "table": row.table,
                    "row_key": row_key, "field": f.name, "value": cell.value, "quote": quote,
                    "quote_truncated": "no" if quote else "", "location": cell.location})
                owners["evidence"].append((row, name))
        return tables, owners

    def _check(self) -> dict:
        """Every failing value: {(row, field): [problems]}; field "" for a whole row."""
        tables, owners = self._tables()
        found, fatal = {}, []
        for i in validate(tables, self.cb, source_id=self.source_id, root=self.root):
            owner = owners[i.table][i.line - 2] if i.line else None
            if i.table == "evidence":
                if i.check == "duplicate_key":
                    continue                       # follows from a duplicate row
                row, name = owner or (None, "")
            else:
                row = owner
                name = "value" if row and row.table == "topic_fields" else i.field
            if row is None or (name and name not in row.cells):
                fatal.append(f"{i.check} in {i.table} {i.key} {i.field}: {i.message}")
                continue
            message = i.message.replace("an evidence row", "its evidence")
            found.setdefault((row, name), []).append(f"{i.check}: {message}")
        if fatal:                                  # a key code made is wrong: a bug, not the model
            raise ExtractionFailed("; ".join(fatal[:3]))
        for row in self.rows:
            for table, name in MUST_FILL:
                if row.table == table and not row.cells[name].value:
                    found.setdefault((row, name), []).append(
                        "missing_value: must be filled; the row means nothing without it")
            doi = row.cells.get("doi") if row.table == "sources" else None
            if doi and doi.value and _flat(doi.value) not in self.flat:
                found.setdefault((row, "doi"), []).append(
                    "doi_not_in_document: copy the DOI from the document, never from memory")
        return found

    def _repair(self, mine: dict):
        """One call for the step: each failing value with its problems; the answers replace
        those values."""
        refs, items, props, keys = {}, [], {}, self._key_lists()
        for (row, name), problems in mine.items():
            f, c = self._field(row, name), row.cells[name]
            ref = f"{row.ref}.{f.name}"
            refs[ref] = (row, name)
            gave = ", ".join(f"{k} {json.dumps(v, ensure_ascii=False)}" for k, v in
                             (("value", c.value), ("quote", c.quote), ("location", c.location))
                             if v or k == "value")
            rest = "" if row.table in ONE_ROW else "; ".join(
                f"{n} {json.dumps(o.value, ensure_ascii=False)}" for n, o in row.cells.items()
                if o.value and n != name)
            items.append(f"\n{ref}\n" + (f"- the row: {rest}\n" if rest else "")
                         + f"- you gave: {gave}\n- problem: {'; '.join(problems)}\n"
                         + _line(self.cb, row.table, f, self.topic_name))
            props[ref] = _value(self.cb, row.table, f, keys)
        gave = {k: k[0].cells[k[1]] for k in mine}
        answer = self._ask(REPAIR + "\n".join(items), _object(props))
        for ref, raw in answer.items():
            if ref in refs:
                row, name = refs[ref]
                row.cells[name] = _cell(raw, self._field(row, name), self.cb)
        after = self._check()
        for (row, name), problems in mine.items():
            if (row, name) in after:
                continue                            # still failing: _settle empties it
            if row.cells[name].value:
                self.report.repaired += 1
            else:
                self._lost(row, name, gave[row, name].value,
                           "withdrawn in the repair round after: " + "; ".join(problems))

    def _settle(self):
        """Empty what still fails (with its evidence), or drop its row; log each."""
        for _ in range(4):
            found = self._check()
            if not found:
                return
            for (row, name), problems in found.items():
                if row not in self.rows:
                    continue                        # dropped already this round
                drop = not name or (row.table, name) in ROW_FIELDS
                value = row.cells[name].value if name else ""
                self._lost(row, name, value, "; ".join(problems) + (" (row dropped)" if drop
                                                                    else ""), drop)
                if drop:
                    self.rows.remove(row)
                else:
                    row.cells[name] = Cell()
        raise ExtractionFailed("values still fail after being emptied")

    def _lost(self, row: Row, name: str, value: str, problem: str, drop: bool = False):
        field = row.keys["field"] if row.table == "topic_fields" else name
        if drop and not name:
            value = " | ".join(f"{n}={c.value}" for n, c in row.cells.items() if c.value)
        self.report.emptied += (sum(bool(c.value) for c in row.cells.values()) if drop
                                else bool(value))
        self._note(row.table, _short(row.ref), field, value, problem)

    def _note(self, table: str, row: str, field: str, value: str, problem: str):
        self.log.append({"step": self.step, "table": table, "row": row, "field": field,
                         "value": value, "problem": problem})

    # -- into the database ----------------------------------------------------------------
    def save(self, store: Store):
        self.step = "import"
        tables, _ = self._tables()
        try:
            imp = store.import_tables(tables, where=self.source_id)
        except ValueError as e:
            raise ExtractionFailed(str(e)) from e
        if PROVISIONAL not in imp.new_studies:
            raise ExtractionFailed(f"already in the database: {imp.known_documents}")
        r = self.report
        r.study, r.stored_as, r.values = (imp.new_studies[PROVISIONAL], imp.new_documents[0],
                                          imp.values_stored)
        if imp.missing:
            self._note("", "", "", "", f"values did not arrive: {list(imp.missing)[:3]}")


def _index_record(root: Path, source_id: str) -> dict:
    """The document's line in docs/index.csv (vet fetch), if any."""
    path = root / "docs" / "index.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return next((r for r in csv.DictReader(fh) if r.get("source_id") == source_id), {})


def _stored_as(store: Store, source_id: str, record: dict) -> str:
    mine = _identifiers({"source_id": source_id, **{k: str(record.get(k) or "")
                                                    for k in ("doi", "pmid", "pmcid")}})
    return next((r["source_id"] for r in store.rows("sources") if _identifiers(r) & mine), "")


def _log_table(store: Store):
    cols = ", ".join(f'"{c}" TEXT NOT NULL DEFAULT \'\'' for c in LOG_COLUMNS)
    store.db.execute(f"CREATE TABLE IF NOT EXISTS extraction_log ({cols})")


def write_log(store: Store, report: ExtractReport, rows: list[dict]):
    """Problems and gate stops, one row each, in the database's extraction_log."""
    today = date.today().isoformat()
    names = ", ".join(f'"{c}"' for c in LOG_COLUMNS)
    with store.db:
        _log_table(store)
        store.db.executemany(
            f"INSERT INTO extraction_log ({names}) VALUES ({', '.join('?' * len(LOG_COLUMNS))})",
            [[report.stored_as or report.source_id, report.study,
              *(r.get(c, "") for c in LOG_COLUMNS[2:-1]), today] for r in rows])


def read_log(store: Store) -> list[dict]:
    with store.db:
        _log_table(store)
    names = ", ".join(f'"{c}"' for c in LOG_COLUMNS)
    return [dict(zip(LOG_COLUMNS, r)) for r in
            store.db.execute(f"SELECT {names} FROM extraction_log ORDER BY rowid")]


def write_abstract(record: dict, root: Path) -> dict:
    """A paper with no full text: its title and abstract become <root>/docs/<source_id>.txt.
    record holds title, abstract, pmid and doi (source_id too, when known). Returns the record
    for extract_document, with access = abstract_only."""
    title, abstract = (str(record.get(k) or "").strip() for k in ("title", "abstract"))
    ids = {k: str(record.get(k) or "").strip() for k in ("doi", "pmid", "pmcid", "url")}
    if not abstract:
        raise ValueError(f"{ids['pmid'] or ids['doi'] or title[:60]!r}: no abstract to extract from")
    sid = record.get("source_id") or document_key({**ids, "source_id": ""})
    if not SOURCE_ID.fullmatch(sid):
        raise ValueError(f"{sid!r} is not a source_id: the record needs a DOI or PMID")
    path, text = root / "docs" / f"{sid}.txt", f"{title}\n\n{abstract}\n"
    if path.exists() and path.read_text("utf-8") != text:
        raise ValueError(f"{path} already holds another text; not overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return {**ids, "source_id": sid, "access": "abstract_only",
            "retrieved_date": date.today().isoformat()}


def extract_document(source_id: str, topic_name: str, topic: dict, llm, store: Store,
                     root: Path | None = None, record: dict | None = None) -> ExtractReport:
    """Extract one document (<root>/docs/<source_id>.txt) into the database: four steps, one
    repair round each, then one import. record: what code knows about the document (DOI,
    PMID, access…); by default its line in docs/index.csv."""
    root = Path(root or store.path.parent)
    if not SOURCE_ID.fullmatch(source_id):
        raise ValueError(f"{source_id!r} is not a source_id")
    if topic_name not in store.cb.topics:
        raise ValueError(f"no topics/{topic_name}.toml")
    _screen(topic)
    record = _index_record(root, source_id) if record is None else record
    stored = _stored_as(store, source_id, record)
    if stored:
        return ExtractReport(source_id, stopped=f"already in the database as {stored}")
    path = root / "docs" / f"{source_id}.txt"
    if not path.is_file() or not path.read_text("utf-8").strip():
        report = ExtractReport(source_id, stopped=f"no text in {path}")
        write_log(store, report, [{"step": "study", "problem": report.stopped}])
        return report
    doc = _Doc(source_id, topic_name, topic, llm, store.cb, root, record)
    before = llm.ledger.spent
    try:
        doc.run()
        if not doc.report.stopped:
            doc.save(store)
    except ExtractionFailed as e:
        doc.report.stopped = str(e)
        doc._note("", "", "", "", f"nothing stored: {e}")
    finally:
        doc.report.usd = llm.ledger.spent - before
        write_log(store, doc.report, doc.log)
    return doc.report


def run_extract(keys: list[str], topic_name: str, llm, db_path: Path = DB,
                abstracts: dict | None = None, say=print) -> list[ExtractReport]:
    """Extract each document in turn into the database at db_path, reading its text from
    docs/ beside it. A key with no text there but a record in abstracts ({source_id: {title,
    abstract, pmid, doi}}) is extracted from its title and abstract."""
    bad = [k for k in keys if not SOURCE_ID.fullmatch(k)]
    if bad:
        raise ValueError(f"not source_ids: {bad[:3]}")
    store = Store(db_path)
    root, out = Path(db_path).parent, []
    try:
        if topic_name not in store.cb.topics:
            raise ValueError(f"no topics/{topic_name}.toml")
        topic = store.cb.topics[topic_name]
        _screen(topic)
        for key in keys:
            record = None
            if not (root / "docs" / f"{key}.txt").is_file() and key in (abstracts or {}):
                record = write_abstract({**abstracts[key], "source_id": key}, root)
            out.append(extract_document(key, topic_name, topic, llm, store, root, record))
            say(out[-1].line())
    finally:
        store.close()
    return out
