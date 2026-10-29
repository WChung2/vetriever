"""The codebook: one row per field in codebook.csv drives the empty tables, the allowed
values, the checks (validate.py) and, later, the extraction prompts. Changing a field means
editing one row.

`vet templates` writes templates/<table>.csv (header row only) and codebook.md from it.
"""
import csv
from dataclasses import dataclass
from pathlib import Path

from .settings import ROOT

TABLES = ["sources", "study_sources", "studies", "arms", "arm_doses", "results", "sites",
          "people", "breeds", "definitions", "evidence", "ext_pain", "ext_imha", "ext_cie"]
EXT = {"surgical_pain": "ext_pain", "imha": "ext_imha", "cie": "ext_cie"}
KINDS = {"K": "key", "T": "transcribed", "C": "coded", "N": "note", "D": "derived"}
TYPES = {"text", "integer", "number", "date", "year", "enum", "pipe_list", "criteria"}
EVIDENCE = {"quote", "location", "none"}
COLUMNS = ["table", "field", "kind", "type", "allowed_values", "rule", "evidence",
           "applies_to", "max_len", "description"]


@dataclass(frozen=True)
class Field:
    table: str
    name: str
    kind: str
    type: str
    allowed: tuple        # () = free value
    rule: str
    evidence: str
    applies: tuple        # () = all; else ((column, (values…)), …), all must hold
    max_len: int | None
    description: str

    def applies_to(self, study: dict) -> bool:
        return all(study.get(col, "") in vals for col, vals in self.applies)


def _applies(text: str) -> tuple:
    if text in ("", "all"):
        return ()
    out = []
    for part in text.split(";"):
        col, _, vals = part.partition("=")
        out.append((col.strip(), tuple(v.strip() for v in vals.split("|"))))
    return tuple(out)


class Codebook:
    def __init__(self, path: Path = ROOT / "codebook.csv"):
        path = Path(path)
        countries = tuple(line.strip() for line in
                          (path.parent / "vocab" / "countries.txt").read_text("utf-8").splitlines()
                          if line.strip())
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != COLUMNS:
                raise ValueError(f"codebook.csv columns must be {COLUMNS}")
            rows = list(reader)
        self.fields: list[Field] = []
        for i, r in enumerate(rows, start=2):
            self._check_row(i, r)
            allowed = r["allowed_values"]
            allowed = (countries if allowed == "@countries" else ("@fields",) if allowed == "@fields"
                       else tuple(allowed.split("|")) if allowed else ())
            self.fields.append(Field(r["table"], r["field"], r["kind"], r["type"], allowed,
                                     r["rule"], r["evidence"], _applies(r["applies_to"]),
                                     int(r["max_len"]) if r["max_len"] else None,
                                     r["description"]))
        # @fields = the names a study's not_applicable may list (studies + extension tables)
        names = tuple(f.name for f in self.fields
                      if f.table == "studies" or f.table in EXT.values())
        self.fields = [Field(**{**f.__dict__, "allowed": names}) if f.allowed == ("@fields",)
                       else f for f in self.fields]
        self.tables = {t: [f for f in self.fields if f.table == t] for t in TABLES}
        self.countries = countries

    @staticmethod
    def _check_row(line: int, r: dict):
        problems = []
        if r["table"] not in TABLES and r["table"] != "derived":
            problems.append(f"unknown table {r['table']!r}")
        if r["kind"] not in KINDS:
            problems.append(f"kind must be one of {sorted(KINDS)}")
        if r["type"] not in TYPES:
            problems.append(f"type must be one of {sorted(TYPES)}")
        if r["evidence"] not in EVIDENCE:
            problems.append(f"evidence must be one of {sorted(EVIDENCE)}")
        if r["type"] == "enum" and not r["allowed_values"]:
            problems.append("enum needs allowed_values")
        if r["kind"] == "C" and not r["rule"]:
            problems.append("coded field needs a rule")
        if problems:
            raise ValueError(f"codebook.csv line {line} ({r['table']}.{r['field']}): "
                             + "; ".join(problems))

    def columns(self, table: str) -> list[str]:
        return [f.name for f in self.tables[table]]

    def field(self, table: str, name: str) -> Field | None:
        return next((f for f in self.tables.get(table, []) if f.name == name), None)


def write_templates(cb: Codebook, folder: Path = ROOT / "templates"):
    folder.mkdir(parents=True, exist_ok=True)
    for t in TABLES:
        with open(folder / f"{t}.csv", "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh, lineterminator="\n").writerow(cb.columns(t))


INTRO = """# Codebook (schema v2)

Generated from `codebook.csv` by `vet templates`. Edit the CSV, not this file.

## Field kinds
- **K key**: identifiers and links.
- **T transcribed**: copied from the document as stated; empty if the document does not state it.
- **C coded**: one of the listed values, assigned by the written rule. A filled C field always
  has an evidence row with its supporting quote.
- **N note**: the extractor's own words (only `studies.notes`); never checked against the text.
- **D derived**: computed downstream by our code. Never extracted.

## Missing values
An empty cell always means "not stated". "Not applicable" is recorded only by the
`applies_to` column below (which fields apply to which indication and study kind) and by
`studies.not_applicable` for one-off exceptions. Never write NA, N/A, NULL, none, -,
"not reported" or "not stated" in a cell; only a listed value may say `unknown` or `none`.

## Keys (letters A-Z, digits and hyphens, uppercase)
| Document | source_id |
|---|---|
| has a PMID | `PMID31234567` |
| has a DOI | `DOI-10-1111-VAA-12345` (every non-alphanumeric character becomes `-`) |
| FDA CVM FOI summary | `FDA-NADA141455` / `FDA-ANADA200123` |
| EMA EPAR | `EMA-EMEAVC000123` |
| registry entry | `REG-AVMA-1234` |
| conference abstract | `ABS-ACVIM2023-GI12` |
| agency or web page | `WEB-WSAVA-20240115-1` |

Use the first rule that applies; add `-2`, `-3`… if two documents would collide.
Study `ID` = `VET-<source_id of the primary document>-<n>`; `arm_id` = `<ID>-A<n>`;
`dose_id` = `<arm_id>-D<n>`; `result_id` = `<ID>-R<n>`; `site_no` and `person_no` are
integers unique within a study.

## Formats
- CSV, UTF-8, standard quoting, header row in template order, no index column.
- Lists: pipe-separated, no spaces around the pipe. Country lists sorted alphabetically,
  names from `vocab/countries.txt` (the ClinicalTrials.gov names the app already uses).
- Numbers: digits and a dot only; no units, thousands separators, `~` or `≥`. If only an
  approximate value or a range is given where one number is expected, leave it empty and
  explain in notes.
- Dates: ISO at the precision stated (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`).
- Verbatim fields: original language and spelling, symbols kept (≥, µg, °C).
- Eligibility Criteria: an `Inclusion Criteria:` header, a blank line, one `* ` line per
  criterion, then the same for `Exclusion Criteria:`.

## Evidence
`evidence = quote`: needs an evidence row with a verbatim quote (≤500 characters, or
`quote_truncated = yes`). `evidence = location`: needs an evidence row with its location
only. `results` and `definitions` rows carry their own text and location.

## Working rules
Each document is extracted alone, from its saved text `docs/<source_id>.txt`, into
`out/<source_id>/`. Copy, don't paraphrase. Never fill a gap from another source or from
memory, never read values off figures, never compute, convert, round, pool or total.
When unsure, leave the field empty and say why in notes.
"""


def write_markdown(cb: Codebook, path: Path = ROOT / "codebook.md"):
    out = [INTRO]
    for t in TABLES + ["derived"]:
        fields = cb.tables.get(t) or [f for f in cb.fields if f.table == t]
        out.append(f"\n## {t}\n\n| field | kind | type | allowed values | meaning and rule | "
                   f"evidence | applies to |\n|---|---|---|---|---|---|---|")
        for f in fields:
            allowed = ("see vocab/countries.txt" if f.allowed == cb.countries
                       else "field names" if f.name == "not_applicable"
                       else ", ".join(f.allowed))
            text = (" ".join(x for x in (f.description, f.rule) if x)
                    .replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;"))
            applies = "; ".join(f"{c} = {' or '.join(v)}" for c, v in f.applies) or "all"
            out.append(f"| {f.name} | {f.kind} | {f.type} | {allowed} | {text} | "
                       f"{f.evidence} | {applies} |")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
