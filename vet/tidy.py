"""Repairs by rule: free, local, for any topic. `vet tidy` runs them on the whole database
and every extraction run ends with them. Each one only settles what a rule can settle from
what is already stored, logs every change in extraction_log (step = tidy), and changes
nothing more when it runs again.

- citations: a source's title, year, journal and first author from the search's own record
  of the paper (the literature service's), where the source has none;
- doses: dose_value (and dose_value_max, dose_unit) from dose_as_written where the written
  dose holds exactly one dose for that drug, and dose_statistic (stated, mean or median)
  where the written dose shows which; each value gets an evidence row quoting the written
  dose, so `vet check` confirms the number is there;
- repeats: a result, dose or person row identical to another of its study apart from its
  key is removed (with its evidence);
- unsupported: an adverse event's ae_fatal = no whose own passage says nothing about death or
  survival is emptied (empty = not stated), and a timepoint read off a label (3 from 't3') is
  emptied, the label staying in timepoint_as_written.

Nothing is guessed: a written dose with two candidate numbers, a number in words, or a
decimal written as a raised dot (0·3) is left for a person.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from .duplicates import NOT_A_DRUG
from .store import Store, norm_doi
from .validate import FATAL, timepoint_label

# source column <- the search record's column
CITATION = {"title": "title", "year": "year", "journal_or_agency": "journal",
            "first_author": "first_author"}
NUM = r"(?<![\d.,·])\d+(?:\.\d+)?(?![\d·])"
UNIT = (r"(?:mg|g|µg|μg|mcg|ug|ng|IU|U|mL|ml|mmol|mEq|mOsm)"
        r"(?:\s*/\s*(?:kg|m2|m²|dog|cat|animal|day|d|h|hr|min|dose))*")
DASH = r"\s*(?:-|‐|‑|–|—|to)\s*"
DOSE = re.compile(
    rf"(?P<median>median\D{{0,40}}?)?(?P<lo>{NUM})"
    rf"(?:\s*±\s*{NUM}|(?P<range>{DASH}(?P<hi>{NUM})))?\s*(?P<unit>{UNIT})(?![A-Za-z/])",
    re.IGNORECASE)
MEAN = re.compile(rf"{NUM}\s*±")
# a tablet's strength or a product's size, not a dose: "5 mg tablets", "a 100 mg capsule"
STRENGTH = re.compile(r"\W{0,3}(?:tablets?|tabs?\b|capsules?|caplets?|chewables?|vials?|ampoules?"
                      r"|bottles?|solution|suspension|formulation|strength)", re.IGNORECASE)
# the spread that follows a mean or median: "(range, 10-25 mg/kg)", "IQR 13.9-20.0 mg/kg"
SPREAD = re.compile(r"(?:range|IQR|interquartile|CI|confidence|SD)\W{0,6}[^\d]{0,12}$", re.IGNORECASE)
NAME_WORDS = NOT_A_DRUG | {"and", "with", "plus", "then", "dose", "high", "oral", "daily"}
RAISED_DOT = re.compile(r"\d\s*[·•]\s*\d")
STEP = "tidy"


@dataclass
class TidyReport:
    citations: int = 0
    doses: int = 0
    statistics: int = 0
    repeats: int = 0
    unsupported: int = 0
    left: list = field(default_factory=list)      # (dose_id, why) a person should look at

    def line(self) -> str:
        return (f"tidy: {self.citations} citation details from the search records, "
                f"{self.doses} dose numbers and {self.statistics} dose kinds from the written "
                f"dose, {self.repeats} repeated rows removed, {self.unsupported} values the "
                f"passage doesn't support emptied; {len(self.left)} written doses left for a person")


def tidy(store: Store, numbers: bool = False) -> TidyReport:
    """numbers: also write dose numbers read by rule from the written dose, and empty result
    values the passage does not seem to support. Off by default (decisions.md 2026-11-19):
    both compare text with numbers by rule and were wrong on fractions, number words and
    totals; the reading itself now quotes each value's passage."""
    rep = TidyReport()
    log = []
    with store.db:
        _citations(store, rep, log)
        if numbers:
            _doses(store, rep, log)
        _repeats(store, rep, log)
        if numbers:
            _unsupported(store, rep, log)
        if log:
            from .extract import LOG_COLUMNS, _log_table
            _log_table(store)
            today = date.today().isoformat()
            names = ", ".join(f'"{c}"' for c in LOG_COLUMNS)
            store.db.executemany(
                f"INSERT INTO extraction_log ({names}) VALUES ({', '.join('?' * len(LOG_COLUMNS))})",
                [[r.get(c, "") for c in LOG_COLUMNS[:-1]] + [today] for r in log])
    return rep


# -- citations ----------------------------------------------------------------------------
def search_record(db: sqlite3.Connection, row: dict) -> dict:
    """The search's record of a document (vet search keeps them in `records`), matched by
    DOI, PMID or PMCID; {} when there is none."""
    try:
        cols = [c[1] for c in db.execute("PRAGMA table_info(records)")]
    except sqlite3.Error:
        return {}
    if not cols:
        return {}
    doi, pmid, pmcid = norm_doi(row.get("doi", "")), row.get("pmid", ""), row.get("pmcid", "")
    sid = row.get("source_id", "")
    if not pmid and sid.startswith("PMID"):
        pmid = sid[4:]
    for col, value in (("pmid", pmid), ("doi", doi), ("pmcid", pmcid.upper())):
        if not value:
            continue
        expr = {"doi": "lower(doi)", "pmcid": "upper(pmcid)"}.get(col, col)
        hit = db.execute(f"SELECT * FROM records WHERE {expr} = ? LIMIT 1", (value,)).fetchone()
        if hit:
            return {c: "" if v is None else str(v).strip() for c, v in zip(cols, hit)}
    return {}


def citation(record: dict) -> dict:
    """{source column: value} the search record gives."""
    out = {mine: record.get(theirs, "") for mine, theirs in CITATION.items()}
    if not re.fullmatch(r"\d{4}", out["year"]):
        out["year"] = ""
    return {k: v for k, v in out.items() if v}


def _citations(store: Store, rep: TidyReport, log: list):
    for row in store.rows("sources"):
        found = citation(search_record(store.db, row))
        fill = {k: v for k, v in found.items() if not row.get(k)}
        for k, v in fill.items():
            store.db.execute(f'UPDATE sources SET "{k}" = ? WHERE source_id = ?',
                             (v, row["source_id"]))
            log.append({"source_id": row["source_id"], "step": STEP, "table": "sources",
                        "row": row["source_id"], "field": k, "value": v,
                        "problem": "filled from the search's record of the paper"})
            rep.citations += 1


# -- doses --------------------------------------------------------------------------------
@dataclass(frozen=True)
class Dose:
    lo: str
    hi: str
    unit: str
    statistic: str      # stated | mean | median
    span: str           # the words it was read from


def read_dose(drug: str, written: str, others: list[str] = ()) -> tuple[Dose | None, str]:
    """The one dose a written dose statement gives for this drug, or (None, why not).
    others: the other drugs named in the same study, which end this drug's part."""
    doses, why = dose_candidates(drug, written, others)
    if why:
        return None, why
    if len(doses) > 1:
        return None, f"{len(doses)} different doses in the written dose"
    return doses[0], ""


def dose_candidates(drug: str, written: str, others: list[str] = ()) -> tuple[list[Dose], str]:
    """Every distinct dose the written dose gives for this drug (a taper gives several), or
    ([], why there is none)."""
    if not written.strip():
        return [], "no written dose"
    if RAISED_DOT.search(written):
        return [], "a decimal written as a raised dot"
    parts = [p for p in re.split(r"\s*(?:…|\.\.\.)\s*", written) if p.strip()]
    found = []
    for part in parts:
        mine = _respectively(drug, part, others)
        if mine:
            found.append(mine)
            continue
        seg = _segment(drug, part, others)
        found += [(m, seg, mean) for m, mean in _candidates(seg)]
    doses = {}
    for m, seg, mean in found:
        lo, hi, unit = m["lo"], m["hi"] or "", " ".join(m["unit"].split())
        statistic = "median" if m["median"] else "mean" if mean else "stated"
        doses.setdefault((lo, hi, unit.lower().replace(" ", ""), statistic),
                         Dose(lo, hi, unit, statistic, _sentence(seg, m)))
    return (list(doses.values()), "") if doses else ([], "no number with a unit")


def _candidates(seg: str) -> list[tuple[re.Match, bool]]:
    """The doses written in seg, each with whether it is a mean (± SD); a tablet's strength
    and the spread after a mean or median are not doses."""
    out, summary = [], False
    for m in DOSE.finditer(seg):
        if STRENGTH.match(seg, m.end()) or (summary and SPREAD.search(seg[:m.start()])):
            continue
        mean = bool(MEAN.match(seg, m.start("lo")))
        summary = summary or mean or bool(m["median"])
        out.append((m, mean))
    return out


def _respectively(drug: str, part: str, others) -> tuple | None:
    """'aspirin and cyclosporine (1.0 mg/kg and 9.9 mg/kg, respectively)': the drugs named
    before the first dose take the doses in order, when there are as many of each."""
    low = part.lower()
    if "respectively" not in low:
        return None
    doses = _candidates(part)
    if not doses:
        return None
    first = doses[0][0].start()
    named = sorted((low.find(n.lower()), n.lower()) for n in {drug, *others}
                   if n and 0 <= low.find(n.lower()) < first)
    order = [n for _, n in named]
    if len(order) != len(doses) or drug.lower() not in order:
        return None
    m, mean = doses[order.index(drug.lower())]
    return m, part, mean


def _segment(drug: str, part: str, others) -> str:
    """The words about this drug: from its name to the next other drug's name; the whole
    part when the name isn't in it and no other drug is."""
    low = part.lower()
    names = [w for w in re.findall(r"[a-z0-9]+", drug.lower())
             if len(w) >= 3 and w not in NAME_WORDS] or [drug.lower()]
    at = min((low.find(w) for w in names if w in low), default=-1)
    rivals = [o.lower() for o in others if o and o.lower() != drug.lower()
              and not set(re.findall(r"[a-z0-9]+", o.lower())) & set(names)]
    if at < 0:
        return "" if any(o in low for o in rivals) else part
    # from the start of the part, or the end of an earlier drug's name ("The median dose of
    # MMF was"), to the next other drug's name
    before = [low.rfind(o, 0, at) + len(o) for o in rivals if low.rfind(o, 0, at) >= 0]
    ends = [low.find(o, at + 1) for o in rivals if low.find(o, at + 1) > at]
    return part[max(before, default=0):min(ends) if ends else len(part)]


def _sentence(seg: str, m: re.Match) -> str:
    """The quote for a value: the written dose around it, at most 300 characters."""
    start = max(0, m.start() - 120)
    return seg[start:m.end() + 60].strip()


def _doses(store: Store, rep: TidyReport, log: list):
    doses = store.rows("arm_doses")
    drugs = {}
    for d in doses:
        drugs.setdefault(d["ID"], set()).add(d["drug"])
    written_ev = {(e["ID"], e["row_key"]): e for e in store.rows("evidence")
                  if e["table"] == "arm_doses" and e["field"] == "dose_as_written"}
    primary = {r["ID"]: r["source_id"] for r in store.rows("study_sources") if r["role"] == "primary"}
    for d in doses:
        if not d["drug"] or d["dose_statistic"] or not d["dose_as_written"]:
            continue
        dose, why = read_dose(d["drug"], d["dose_as_written"], sorted(drugs[d["ID"]]))
        if dose is None and d["dose_value"]:
            # several doses written (a taper): the extractor picked one; say what kind it is
            # when exactly the doses with its number agree
            same = [c for c in dose_candidates(d["drug"], d["dose_as_written"],
                                               sorted(drugs[d["ID"]]))[0]
                    if c.lo == d["dose_value"] and c.hi == d["dose_value_max"]
                    and (not d["dose_unit"] or _unit(c.unit) == _unit(d["dose_unit"]))]
            if len({c.statistic for c in same}) == 1:
                dose = same[0]
        if dose is None:
            if not d["dose_value"]:
                rep.left.append((d["dose_id"], why))
            continue
        if d["dose_value"] and d["dose_value"] != dose.lo:
            continue                     # the extractor read another number: a person looks
        if d["dose_unit"] and _unit(d["dose_unit"]) != _unit(dose.unit):
            if not d["dose_value"]:
                rep.left.append((d["dose_id"], f"unit {dose.unit!r} is not {d['dose_unit']!r}"))
            continue
        ev = written_ev.get((d["ID"], d["dose_id"])) or {"source_id": primary.get(d["ID"], "")}
        fill = {"dose_statistic": dose.statistic}
        if not d["dose_value"]:
            fill |= {"dose_value": dose.lo, "dose_value_max": dose.hi,
                     "dose_unit": d["dose_unit"] or dose.unit}
        fill = {k: v for k, v in fill.items() if v and not d[k]}
        for k, v in fill.items():
            store.db.execute(f'UPDATE arm_doses SET "{k}" = ? WHERE dose_id = ?', (v, d["dose_id"]))
            if k in ("dose_value", "dose_value_max", "dose_statistic"):
                store.db.execute(
                    "INSERT OR IGNORE INTO evidence (ID, source_id, \"table\", row_key, field, "
                    "value, quote, quote_truncated, location) VALUES (?, ?, 'arm_doses', ?, ?, ?, "
                    "?, '', ?)", (d["ID"], ev.get("source_id", ""), d["dose_id"], k, v,
                                  dose.span, ev.get("location", "")))
            log.append({"source_id": ev.get("source_id", ""), "ID": d["ID"], "step": STEP,
                        "table": "arm_doses", "row": d["dose_id"], "field": k, "value": v,
                        "problem": "read from the written dose"})
        rep.doses += "dose_value" in fill
        rep.statistics += "dose_statistic" in fill


def _unit(u: str) -> str:
    return re.sub(r"\s+", "", u.lower().replace("μ", "µ").replace("mcg", "µg").replace("ug", "µg"))


# -- repeats ------------------------------------------------------------------------------
REPEATABLE = {"results": "result_id", "arm_doses": "dose_id", "people": "person_no"}


def _repeats(store: Store, rep: TidyReport, log: list):
    for table, number in REPEATABLE.items():
        seen = {}
        for r in store.rows(table):
            body = (r["ID"],) + tuple((c, v) for c, v in r.items() if c not in (number, "ID"))
            if body not in seen:
                seen[body] = r[number]
                continue
            store.db.execute(f'DELETE FROM "{table}" WHERE "ID" = ? AND "{number}" = ?',
                             (r["ID"], r[number]))
            store.db.execute('DELETE FROM evidence WHERE "ID" = ? AND "table" = ? AND row_key = ?',
                             (r["ID"], table, r[number]))
            log.append({"source_id": r.get("source_id", ""), "ID": r["ID"], "step": STEP,
                        "table": table, "row": r[number], "field": "",
                        "problem": f"removed: same as {seen[body]} apart from its key"})
            rep.repeats += 1


# -- values the row's own passage doesn't support --------------------------------------------
def _unsupported(store: Store, rep: TidyReport, log: list):
    for r in store.rows("results"):
        empty = []
        if r["ae_fatal"] == "no" and r["as_written"] and not FATAL.search(r["as_written"]):
            empty.append(("ae_fatal", "its passage says nothing about death or survival"))
        if timepoint_label(r["timepoint_value"], r["timepoint_as_written"]):
            why = f"{r['timepoint_as_written']!r} is a label, not a time"
            empty += [("timepoint_value", why)] + ([("timepoint_unit", why)] if r["timepoint_unit"] else [])
        for name, why in empty:
            store.db.execute(f'UPDATE results SET "{name}" = \'\' WHERE result_id = ?', (r["result_id"],))
            log.append({"source_id": r["source_id"], "ID": r["ID"], "step": STEP, "table": "results",
                        "row": r["result_id"], "field": name, "value": r[name],
                        "problem": f"emptied: {why}"})
            rep.unsupported += 1
