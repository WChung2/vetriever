"""Read one document into the database's tables the way a careful reader does.

1. Code cuts the article into parts by its own structure (paragraphs, tables with their caption
   and notes); the reference list is left out.
2. The model sorts every part into note sections (twice; the two answers are joined).
3. With the whole article held in the model's cache, in this order: study facts and sites;
   groups and doses; results from the text, sentence by sentence; results from the tables,
   cell by cell; judgements about the whole study last.

Rules the tests settled (decisions.md 71 to 74):
- Nothing is dropped by judgement. A sentence gives rows or is listed as holding no result;
  a restatement still gives its rows; participant flow is a kind of row.
- Every sentence of a results paragraph and every cell of a results table is answered.
- Sentences that are the same text are found by code and read once.
- A row names its own sentence (or cells) and quotes it; a value taken from elsewhere is
  quoted apart, with where it stands.
- A p-value is kept only when it is printed in the row's own sentence or cells, or in a
  passage the row borrowed. Text is looked for in text; no number is computed or compared.
"""
import json
import re
import statistics
import threading
from pathlib import Path

from .codebook import Codebook, Field
from .extract import CODE
from .settings import ROOT
from .validate import QUOTE_MAX, SENTINELS, _flat

SECTIONS = ["animals", "entry", "disease", "intervention", "design", "flow", "outcomes",
            "findings", "harms", "conduct", "limitations", "background", "other"]
WANT = {"findings", "harms"}        # the sections the results steps read
FOCUS = 2500                        # characters of paragraphs in focus per request
CELLS = 60                          # table cells per request
BOUND = 1_000_000_000               # no count or measurement is larger; ends a runaway number
NOT_ASKED = {"not_applicable"}      # a field no step fills
# judgements about the whole study: answered last, in one small request, with reasoning on
JUDGED = ("Study Type", "study_kind", "study_stage", "Overall Status", "intervention_type",
          "control_type", "allocation", "randomised", "blinding", "trial_outcome",
          "outcome_basis")
NA = "not_applicable"
# the facts behind the verdict, each a small choice with its quote; the verdict follows from
# them by a fixed order (verdict()), so the same facts always give the same word
VERDICT_FACTS = {
    "tests_treatment": (("yes", "no"), "Does the study test or compare the effect or safety of a treatment?"),
    "results_reported": (("yes", "no"), "Does the article report results for its main outcome?"),
    "comparison_or_goal": (("yes", "no"), ("Is the main outcome set against a comparison group, or against a goal "
                                           "stated in advance?")),
    "low_power": (("yes", "no"), ("Do the authors say there were too few animals, or too little power, to show "
                                  "a difference?")),
    "main_outcome": (("met", "not_met", "mixed"), ("Against that comparison or goal, did the main outcome meet it? "
                                                    "mixed = main outcomes, doses or groups disagree.")),
    "called_primary": (("yes", "no"), "Does the article itself call that outcome primary?"),
}
# asked of every study, kept only for a study that assigns its treatment
ASSIGNED_ONLY = ("randomised", "n_randomised", "randomisation_as_written", "allocation", "study_stage")
EV = {"type": "array", "items": {"type": "object", "required": ["location", "quote"], "properties": {
    "location": {"type": "string"}, "quote": {"type": "string"}}}}
# a p-value field holds a p-value and nothing else; the API does not always enforce this, so
# printed() below is what protects the field
P_VALUE = (r"^((<=|>=|[<>=]) ?)?([01]([.,][0-9]+)?|[.,][0-9]+|"
           r"[0-9]([.,][0-9]+)?( ?[xX] ?10\^?-[0-9]+|[eE]-[0-9]+))$")
OWN_SENTENCE = ("sentence (a choice: the ids of the sentences to read)\n  The sentence that states the result.\n"
                "quote (text)\n  Its words.\n"
                "borrowed (a list)\n  Each value the row takes from another sentence or part (a group size, a time, a unit): "
                "the id of the sentence or part it stands in, and its words.\n")
OWN_CELLS = ("cells (a list: the ids of the cells of this request)\n  The cells the row's values stand in.\n"
             "borrowed (a list)\n  Each value the row takes from outside those cells (a group size, a statistic named in a note, "
             "a unit): the id of the part it stands in, and its words.\n")
EMPTY_CELL = {"", "-", "–", "—", "n/a", "na", "nd"}
ELIDED = re.compile(r"\[?(?:…|\.\.\.)\]?")      # words left out of a quote
LIGHT = threading.local()           # set when the API found a format too complex: choices as plain text


def prompt_text(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text("utf-8")


# -- 1. the article as parts ---------------------------------------------------------------
PARAGRAPH_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
FOOT = re.compile(r"^(?:[a-z]\s?[A-Z(]|[*†‡§¶#]|[A-Za-z][\w /-]{0,30}:\s|Abbreviations|Note|Data are|Values are|To convert)"
                  r"|Abbreviations?:")
REFS = re.compile(r"(?:\d+\.?\s*)?(references|reference list|literature cited|bibliography|works cited)\s*:?", re.IGNORECASE)
AFTER_REFS = re.compile(r"^(Table|TABLE|Fig|FIG|Figure|FIGURE|Appendix|APPENDIX|Supplement|SUPPLEMENT)")


def is_table(line: str) -> bool:
    return line.rstrip().endswith("|")


def pdf_title(line: str) -> bool:
    s = line.strip()
    # a heading in capitals (DISCUSSION, CASE HISTORIES), not a sentence's end ("IMHA.")
    caps = s.isupper() and not s.endswith(".") and (" " in s or len(s) >= 6)
    return bool(s) and len(s) < 60 and (caps or bool(re.fullmatch(r"Case \d+", s)) or bool(REFS.fullmatch(s)))


def parts(text: str, pdf: bool | None = None) -> list[dict]:
    """The article as parts, in order: paragraphs (lines joined for PDF text), tables with
    their 'Table N' line, caption and footnotes, each with the title it stands under."""
    lines = text.split("\n")
    body = [ln for ln in lines if ln.strip() and not ln.startswith("## ") and not is_table(ln)]
    pdf = statistics.median(len(ln) for ln in body) < 80 if pdf is None else pdf
    out, buf, title, i = [], [], "", 0

    def flush():
        if buf:
            out.append({"title": title, "kind": "text", "text": " ".join(buf).strip()})
            buf.clear()

    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("## "):
            flush()
            title = line[3:].strip()
            i += 1
            continue
        if is_table(line):
            flush()
            rows = []
            while i < len(lines) and (is_table(lines[i]) or not lines[i].strip()):
                if lines[i].strip():
                    rows.append(" ".join(lines[i].split()))
                i += 1
            head = []                                   # "Table N" and its caption, just before
            while out and out[-1]["kind"] == "text" and len(out[-1]["text"]) < 300 and len(head) < 2 and (
                    re.match(r"^(Table|TABLE)\s*\S+", out[-1]["text"]) or (head == [] and len(out) > 1
                    and re.match(r"^(Table|TABLE)\s*\S+", out[-2]["text"]))):
                head.insert(0, out.pop()["text"])
            notes = []                                  # footnotes, just after
            while i < len(lines) and len(notes) < 4 and lines[i].strip() and not lines[i].startswith("## ") \
                    and len(lines[i]) < 500 and FOOT.search(lines[i].strip()):
                notes.append(" ".join(lines[i].split()))
                i += 1
            out.append({"title": title, "kind": "table", "text": "\n".join(head + rows + notes)})
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        if pdf:
            if pdf_title(line):
                flush()
                title = line.strip()
                i += 1
                continue
            s = " ".join(line.split())
            if buf and re.search(r"\s?-$", buf[-1]) and s[:1].islower():
                buf[-1] = re.sub(r"\s?-$", "", buf[-1]) + s           # a word broken at the line end
            else:
                buf.append(s)
        else:
            flush()
            buf.append(" ".join(line.split()))
            flush()
        i += 1
    flush()
    sized = []                                          # long paragraphs split at sentence ends
    for p in out:
        if p["kind"] == "text" and len(p["text"]) > 1500:
            piece = ""
            for s in PARAGRAPH_END.split(p["text"]):
                if piece and len(piece) + len(s) > 1000:
                    sized.append({**p, "text": piece})
                    piece = ""
                piece = (piece + " " + s).strip()
            if piece:
                sized.append({**p, "text": piece})
        else:
            sized.append(p)
    merged = []                                         # a scrap joins the next part
    for p in sized:
        if merged and merged[-1]["kind"] == "text" and len(merged[-1]["text"]) < 60 and p["kind"] == "text" \
                and merged[-1]["title"] == p["title"]:
            p = {**p, "text": merged.pop()["text"] + " " + p["text"]}
        merged.append(p)
    # the reference list costs tokens and holds no fact of this study: everything under the last
    # references title goes, except a table, figure, appendix or supplement placed after it
    last = max((n for n, p in enumerate(merged) if REFS.fullmatch(p["title"].strip())), default=None)
    while last and REFS.fullmatch(merged[last - 1]["title"].strip()):
        last -= 1                                       # the first part of that reference list
    if last is not None:
        merged = merged[:last] + [p for p in merged[last:] if not REFS.fullmatch(p["title"].strip())
                                  or AFTER_REFS.match(p["text"])]
    return [{"id": f"P{n}", **p} for n, p in enumerate(merged, 1)]


# -- sentences ------------------------------------------------------------------------------
ABBREVIATIONS = {"e.g", "i.e", "vs", "fig", "figs", "al", "no", "dr", "approx", "ca", "cf", "sp", "spp", "st"}
SENTENCE_END = re.compile(r"(?:(?<=[A-Za-z\)\]%\"”’])[.!?](?:\d{1,3}(?:[,–\-]\d{1,3})*)?|(?<=\d)[.!?])\s+(?=[A-Z“\"\[(])")


def sentences(text: str) -> list[str]:
    """A paragraph cut into sentences. Nothing is dropped: the pieces joined give the paragraph back."""
    out, start = [], 0
    for m in SENTENCE_END.finditer(text):
        word = re.split(r"[\s(]", text[start:m.start()])[-1].lower()
        if word in ABBREVIATIONS:
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    out.append(text[start:].strip())
    return [s for s in out if s]


def same_sentences(groups: list) -> dict:
    """Sentences of the paragraphs to read that are the same text as an earlier one: {later id: first id}.
    The same = equal once case and spacing are set aside, or one holds the other whole and the shorter is at
    least 40 characters and four fifths of the longer (a lead-in such as "In summary,")."""
    seen, same = [], {}
    for g in groups:
        for q in g:
            for n, s in enumerate(sentences(q["text"]), 1):
                sid, flat = f"{q['id']}.{n}", _flat(s)
                first = next((i for i, f in seen if f == flat or (min(len(f), len(flat)) >= 40 and (f in flat or flat in f)
                              and min(len(f), len(flat)) >= 0.8 * max(len(f), len(flat)))), None)
                if first:
                    same[sid] = first
                else:
                    seen.append((sid, flat))
    return same


def printed(p_value: str, texts: list) -> bool:
    """Is this p-value, as text, printed in the row's own sentence or in what the row borrowed?
    Text looked for in text, spaces ignored; no number is computed or compared."""
    lit = re.sub(r"\s", "", p_value).lower().replace(",", ".")
    bare = re.sub(r"^(<=|>=|[<>=])", "", lit)
    forms = {lit, bare, bare.replace("0.", ".", 1) if bare.startswith("0.") else "0" + bare if bare.startswith(".") else bare}
    hay = re.sub(r"\s", "", " ".join(texts)).lower().replace(",", ".").replace("≤", "<=").replace("≥", ">=")
    return any(f and f in hay for f in forms)


def in_text(quote: str, text: str) -> bool:
    """A quote, with or without words left out, stands in this text."""
    return all(_flat(x) in _flat(text) for x in ELIDED.split(quote) if _flat(x))


def criteria_layout(text: str) -> str:
    """Entry criteria in the database's layout: the headers 'Inclusion Criteria:' and 'Exclusion
    Criteria:' as the codebook writes them, every criterion on a line that starts with '* '.
    Only the layout is touched, never the words of a criterion."""
    out = []
    for ln in (x.strip() for x in text.split("\n")):
        head = re.fullmatch(r"(inclusion|exclusion) criteria:?", ln, re.IGNORECASE)
        if head:
            out.append(f"{head.group(1).capitalize()} Criteria:")
        elif ln:
            out.append("* " + re.sub(r"^[*•\-–]\s*", "", ln))
    if out and out[0] not in ("Inclusion Criteria:", "Exclusion Criteria:"):
        out.insert(0, "Inclusion Criteria:")
    return "\n".join(out)


def verdict(facts: dict) -> tuple[str, str]:
    """trial_outcome and outcome_basis from the facts, first line that applies."""
    if facts.get("tests_treatment") != "yes":
        return "", ""
    basis = "primary_endpoint" if facts.get("called_primary") == "yes" else "authors_conclusion_only"
    if facts.get("results_reported") == "no":
        return "unknown", basis
    if facts.get("comparison_or_goal") != "yes" or facts.get("low_power") == "yes":
        return "inconclusive", basis
    word = {"met": "positive", "not_met": "negative", "mixed": "mixed"}.get(facts.get("main_outcome"), "")
    return word, basis if word else ""


def too_complex(e: Exception) -> bool:
    return "too many states" in str(e) or "INVALID_ARGUMENT" in str(e)


def text_of(v) -> str:
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.10g}"
    if isinstance(v, list):
        return "|".join(text_of(x) for x in v if text_of(x))
    return str(v).strip()


class Reading:
    """One document read for one study. labels: the note sections of each part when the
    document was sorted before ({part id: [sections]}); without them it is sorted here."""

    def __init__(self, study_id: str, source_id: str, text: str, llm, cb: Codebook,
                 pdf: bool | None = None, labels: dict | None = None):
        self.ID, self.sid, self.llm, self.cb = study_id, source_id, llm, cb
        self.parts = parts(text, pdf)
        self.by_id = {p["id"]: p for p in self.parts}
        self.tags = labels
        self.cache = ""
        self.groups_by_name = {}
        self.evidence = []
        self.calc = []
        self.raw = {}
        self.stats = {"calls": 0, "quotes": 0, "quote_not_found": 0, "bad_location": 0, "no_evidence_dropped": 0,
                      "gated_emptied": 0}

    # -- 2. the notes -----------------------------------------------------------------------
    def _sort_once(self, only=None) -> dict:
        ids = [p["id"] for p in self.parts]
        ask = only or ids
        article = json.dumps([{"id": p["id"], "title": p["title"], "text": p["text"]} for p in self.parts],
                             ensure_ascii=False, indent=0)
        prompt = (f"<article source_id=\"{self.sid}\">\n{article}\n</article>\n\n"
                  + (f"Sort only these parts: {', '.join(only)}." if only else "Sort every part."))
        schema = {"type": "object", "required": ["parts"], "properties": {"parts": {"type": "array", "items": {
            "type": "object", "required": ["id", "sections"], "properties": {
                "id": {"type": "string", "enum": ask},
                "sections": {"type": "array", "items": {"type": "string", "enum": SECTIONS}},
                "about": {"type": "string"}}}}}}
        got = self.llm.call("structure", prompt, schema, system=prompt_text("sort"), doc=f"sort_{self.sid}",
                            thinking="low")
        return {x["id"]: x for x in got.get("parts") or [] if isinstance(x, dict) and x.get("id") in ask}

    def _labelled(self) -> dict:
        got = self._sort_once()
        missing = [p["id"] for p in self.parts if not got.get(p["id"], {}).get("sections")]
        if missing:
            got |= self._sort_once(only=missing)
        return got

    def sort(self):
        """Each part's note sections: sorted twice, the two answers joined."""
        runs = [self._labelled() for _ in range(2)]
        self.tags = {p["id"]: sorted(set().union(*(set(r.get(p["id"], {}).get("sections") or []) for r in runs)),
                                     key=SECTIONS.index) for p in self.parts}

    @property
    def notes(self) -> str:
        return (f'<notes source_id="{self.sid}">\n' + json.dumps(
            [{"id": p["id"], "title": p["title"], "sections": self.tags.get(p["id"], []), "text": p["text"]}
             for p in self.parts], ensure_ascii=False, indent=0) + "\n</notes>\n\n")

    # -- the form of an answer ----------------------------------------------------------------
    def typed(self, f: Field) -> dict:
        if getattr(LIGHT, "on", False) and f.type in ("enum", "pipe_list"):
            return {"type": "array", "items": {"type": "string"}} if f.type == "pipe_list" else {"type": "string"}
        if f.type == "integer":
            return {"type": "integer", "minimum": 0, "maximum": BOUND}
        if f.type == "number":
            return {"type": "number", "minimum": -BOUND, "maximum": BOUND}
        listed = f.allowed and f.allowed != self.cb.countries
        if f.type == "enum" and listed:
            return {"type": "string", "enum": list(f.allowed)}
        if f.type == "pipe_list":
            return {"type": "array", "items": {"type": "string", "enum": list(f.allowed)} if listed else {"type": "string"}}
        return {"type": "string"}

    def show(self, f: Field) -> str:
        listed = f.allowed and f.allowed != self.cb.countries
        kind = {"integer": "a whole number", "number": "a number", "date": "a date", "year": "a year",
                "enum": "a choice: " + " | ".join(f.allowed) if listed else "a country name",
                "pipe_list": ("a list of: " + " | ".join(f.allowed)) if listed else "a list"}.get(f.type, "text")
        return f"{f.name} ({kind})\n  {f.description + (' ' + f.rule if f.rule else '')}"

    def fields(self, table: str, skip=()) -> list[Field]:
        return [f for f in self.cb.tables[table] if f.name not in CODE.get(table, set()) and f.name not in NOT_ASKED
                and f.name not in skip]

    def one(self, f: Field) -> dict:
        return {"type": "object", "required": ["evidence", "value"], "properties": {
            "evidence": EV, "value": self.typed(f), "calculation": {"type": "string"}}}

    def row(self, fs: list, required: list, extra: dict | None = None) -> dict:
        """The answer format of one row. Each number field must be answered, with the number or
        with null when the passage gives none: a number field that could only hold a number came
        back as 0 where nothing was stated (a dose of 0, a group of 0 animals)."""
        props = {"evidence": EV} | {f.name: self.typed(f) for f in fs} | (extra or {})
        numbers = [f.name for f in fs if f.type in ("integer", "number")]
        for name in numbers:
            props[name] = {**props[name], "nullable": True}
        return {"type": "object", "required": ["evidence", *required, *[n for n in numbers if n not in required]],
                "properties": props}

    # -- asking -------------------------------------------------------------------------------
    def ask(self, prompt: str, schema: dict, thinking: str = "") -> dict:
        self.stats["calls"] += 1
        for attempt in (1, 2):
            try:
                return self.llm.call("structure", ("" if self.cache else self.notes) + prompt, schema,
                                     system=prompt_text("fill"), doc=f"fill_{self.sid}", cache=self.cache,
                                     thinking=thinking)
            except ValueError:                      # an answer that cannot be read: asked once more
                self.stats["unreadable"] = self.stats.get("unreadable", 0) + 1
                if attempt == 2:
                    raise

    def ask_rows(self, prompt: str, build) -> dict:
        """A request for rows: build() gives its format; rebuilt lighter when the API refuses it."""
        try:
            return self.ask(prompt, build())
        except Exception as e:
            if not too_complex(e) or getattr(LIGHT, "on", False):
                raise
            self.stats["calls"] -= 1
            self.stats["light_format"] = True
            LIGHT.on = True
            return self.ask(prompt, build())

    def allowed(self, f: Field, value: str) -> str:
        """A choice outside its list is emptied (only possible when the format was lightened), and
        so is a cell that only holds a word for "no value" (none, not stated, n/a): the codebook
        asks for an empty cell there. What a result measured (endpoint) is left as written: Na is
        also sodium."""
        if value.strip().lower() in SENTINELS and value not in f.allowed and (f.table, f.name) != ("results", "endpoint"):
            self.stats["no_value_words_emptied"] = self.stats.get("no_value_words_emptied", 0) + 1
            return ""
        if not value or not f.allowed or f.allowed == self.cb.countries or f.type not in ("enum", "pipe_list"):
            return value
        kept = [x for x in value.split("|") if x in f.allowed]
        self.stats["bad_choice"] = self.stats.get("bad_choice", 0) + (len(kept) != len(value.split("|")))
        return "|".join(kept)

    def check(self, evidence) -> str:
        """The quotes of one answer, checked against the parts they name; returns the passage."""
        good = []
        for e in evidence or []:
            if not isinstance(e, dict) or not str(e.get("quote") or "").strip():
                continue
            self.stats["quotes"] += 1
            part = self.by_id.get(str(e.get("location") or "").strip())
            if part is None:
                self.stats["bad_location"] += 1
            elif not all(_flat(x) in _flat(part["text"]) for x in re.split(r"…|\.\.\.", e["quote"]) if _flat(x)):
                self.stats["quote_not_found"] += 1
            good.append(str(e["quote"]).strip())
        return " … ".join(good)

    def where(self, evidence) -> str:
        """Where an answer's first passage stands: the title its part is under, else the part's number."""
        for e in evidence or []:
            if isinstance(e, dict) and str(e.get("quote") or "").strip():
                loc = str(e.get("location") or "").strip()
                return (self.by_id.get(loc.split(".")[0]) or {}).get("title") or loc
        return ""

    def support(self, table: str, key: str, row: dict, passage: str, location: str):
        """The evidence rows of one stored row: one for each filled field the codebook asks evidence for."""
        for f in self.cb.tables[table]:
            value = row.get(f.name, "")
            if not value or f.evidence == "none" or not (passage or location):
                continue
            quote = passage[:QUOTE_MAX] if f.evidence == "quote" else ""
            self.evidence.append({"ID": self.ID, "source_id": self.sid, "table": table, "row_key": key,
                                  "field": f.name, "value": value, "quote": quote,
                                  "quote_truncated": ("yes" if len(passage) > QUOTE_MAX else "no") if quote else "",
                                  "location": location or "document"})

    # -- 3a. the study ------------------------------------------------------------------------
    def study(self) -> tuple[dict, list]:
        fs = self.fields("studies", skip=JUDGED)
        sites = self.fields("sites")

        def ask_fields(group, with_sites):
            text = ("## This request: the study\nFill these fields; leave out a field no passage answers.\n\n"
                    "### studies: one record for the study\n" + "\n".join(self.show(f) for f in group)
                    + ("\n\n### sites: one row per site where animals were enrolled\n"
                       + "\n".join(self.show(f) for f in sites) if with_sites else ""))
            props = {"studies": {"type": "object", "properties": {f.name: self.one(f) for f in group}}}
            if with_sites:
                props["sites"] = {"type": "array", "items": self.row(sites, [])}
            try:
                return [self.ask(text, {"type": "object", "properties": props})]
            except Exception as e:
                if not too_complex(e) or len(group) < 4:
                    raise
                self.stats["calls"] -= 1
                half = len(group) // 2
                return ask_fields(group[:half], False) + ask_fields(group[half:], with_sites)

        answers = ask_fields(fs, True)
        self.stats["study_requests"] = len(answers)
        a = {"studies": {}, "sites": []}
        for x in answers:
            a["studies"] |= x.get("studies") or {}
            a["sites"] += x.get("sites") or []
        self.raw["study"] = a
        rec = {"ID": self.ID}
        self.study_evidence = {}
        for f in fs:
            got = (a.get("studies") or {}).get(f.name)
            if not isinstance(got, dict) or text_of(got.get("value")) == "":
                continue
            passage = self.check(got.get("evidence"))
            if not passage:
                self.stats["no_evidence_dropped"] += 1
                continue
            rec[f.name] = self.allowed(f, text_of(got.get("value")))
            if f.type == "criteria":
                rec[f.name] = criteria_layout(rec[f.name])
            self.study_evidence[f.name] = (passage, self.where(got.get("evidence")))
            if got.get("calculation"):
                self.calc.append(f"{f.name}: {got['calculation']}")
        site_rows = []
        for n, s in enumerate([s for s in a.get("sites") or [] if isinstance(s, dict)], 1):
            passage = self.check(s.get("evidence"))
            if passage:
                row = {"ID": self.ID, "site_no": str(n)} | {f.name: self.allowed(f, text_of(s.get(f.name))) for f in sites}
                site_rows.append(row)
                self.support("sites", str(n), row, passage, self.where(s.get("evidence")))
        return rec, site_rows

    # -- 3e. judgements, last -----------------------------------------------------------------
    def judge(self, study: dict, arms: list, results: list):
        fs = [f for f in self.cb.tables["studies"] if f.name in JUDGED and f.name not in ("trial_outcome", "outcome_basis")]
        kinds = {}
        for r in results:
            kinds[r["result_kind"]] = kinds.get(r["result_kind"], 0) + 1
        recorded = ("Recorded so far:\n- groups: " + ("; ".join(
            f"{a['arm_label']}" + (f" (n = {a['n_assigned']})" if a["n_assigned"] else "") for a in arms) or "none")
            + f"\n- main outcome (Primary Outcome Measures): {study.get('Primary Outcome Measures') or 'none recorded'}"
            + f"\n- results recorded: {', '.join(f'{n} {k}' for k, n in kinds.items()) or 'none'}")
        described = []
        props = {}
        for f in fs:
            listed = list(f.allowed) + [NA]
            several = f.type == "pipe_list"             # a field that holds every value that applies
            described.append(f"{f.name} ({'a list of' if several else 'a choice'}: {' | '.join(listed)})\n  "
                             f"{f.description}" + (" " + f.rule if f.rule else ""))
            choice = {"type": "string", "enum": listed}
            props[f.name] = {"type": "object", "required": ["evidence", "value"], "properties": {
                "evidence": EV, "value": {"type": "array", "items": choice} if several else choice}}
        asked = []
        for name, (choices, question) in VERDICT_FACTS.items():
            asked.append(f"{name} (a choice: {' | '.join(choices)})\n  {question}")
            props[name] = {"type": "object", "required": ["evidence", "value"], "properties": {
                "evidence": EV, "value": {"type": "string", "enum": list(choices)}}}
        prompt = ("## This request: judgements about the whole study\nThe facts are recorded. Now judge the study "
                  f"as a whole. For each field give the evidence first, then the value. Choose {NA} when the "
                  "question does not apply to this kind of study, and leave a field out when the notes do not say.\n\n"
                  + recorded + "\n\n### studies\n" + "\n".join(described)
                  + "\n\n### the main outcome: answer each of these six (evidence may be empty for a no)\n"
                  + "\n".join(asked))
        a = self.ask(prompt, {"type": "object", "required": list(VERDICT_FACTS), "properties": props}, thinking="medium")
        self.raw["judgement"] = a
        facts = {k: text_of((a.get(k) or {}).get("value")) for k in VERDICT_FACTS}
        for k in VERDICT_FACTS:
            self.check((a.get(k) or {}).get("evidence"))
        self.facts = facts
        study.pop("trial_outcome", None)
        study.pop("outcome_basis", None)
        word, basis = verdict(facts)
        if word:
            study["trial_outcome"], study["outcome_basis"] = word, basis
            main = (a.get("main_outcome") or {}).get("evidence") or (a.get("results_reported") or {}).get("evidence")
            passage = self.check(main)
            for name in ("trial_outcome", "outcome_basis"):
                self.study_evidence[name] = (passage, self.where(main))
        for f in fs:
            got = a.get(f.name)
            value = text_of(got.get("value")) if isinstance(got, dict) else ""
            if "|" in value:                            # a list: "not applicable" beside a real value is dropped
                value = "|".join(dict.fromkeys(x for x in value.split("|") if x != NA))
            if value == NA:
                self.stats["not_applicable"] = self.stats.get("not_applicable", 0) + 1
            elif value and self.check(got.get("evidence")):
                study[f.name] = value
                self.study_evidence[f.name] = (self.check(got.get("evidence")), self.where(got.get("evidence")))
            elif value:
                self.stats["no_evidence_dropped"] += 1
        if study.get("Study Type") != "INTERVENTIONAL":
            for name in ASSIGNED_ONLY:
                self.stats["gated_emptied"] += bool(study.pop(name, ""))

    # -- 3b. groups and doses -----------------------------------------------------------------
    def groups(self) -> tuple[list, list]:
        arms = self.fields("arms", skip=("arm_as_written",))
        doses = self.fields("arm_doses", skip=("dose_as_written",))
        prompt = ("## This request: groups and doses\n"
                  "### arms: one row per group the study set up or selected at the start (treatment groups; "
                  "cases and comparison animals). A group formed afterwards by what happened to the animals "
                  "(survivors, responders) is not a group here.\n" + "\n".join(self.show(f) for f in arms)
                  + "\n\n### arm_doses: inside each group, one row per drug and dose; a dose that changes over "
                    "time (a taper), in the text or in a table, is one row per step\n" + "\n".join(self.show(f) for f in doses))
        a = self.ask_rows(prompt, lambda: {"type": "object", "required": ["arms"], "properties": {"arms": {
            "type": "array", "items": self.row(arms, ["arm_label"], {"arm_doses": {"type": "array",
                                                                                "items": self.row(doses, ["drug"])}})}}})
        self.raw["groups"] = a
        arm_rows, dose_rows, self.groups_by_name = [], [], {}
        for n, arm in enumerate([x for x in a.get("arms") or [] if isinstance(x, dict)], 1):
            arm_id = f"{self.ID}-A{n}"
            label = text_of(arm.get("arm_label")) or f"group {n}"
            while label in self.groups_by_name:
                label += " (2)"
            self.groups_by_name[label] = arm_id
            passage = self.check(arm.get("evidence"))
            row = ({"ID": self.ID, "arm_id": arm_id, "arm_as_written": passage}
                   | {f.name: self.allowed(f, text_of(arm.get(f.name))) for f in arms} | {"arm_label": label})
            arm_rows.append(row)
            self.support("arms", arm_id, row, passage, self.where(arm.get("evidence")))
            for k, d in enumerate([x for x in arm.get("arm_doses") or [] if isinstance(x, dict)], 1):
                passage = self.check(d.get("evidence"))
                row = ({"ID": self.ID, "arm_id": arm_id, "dose_id": f"{arm_id}-D{k}", "dose_as_written": passage}
                       | {f.name: self.allowed(f, text_of(d.get(f.name))) for f in doses})
                if not row.get("dose_value"):       # the codebook: what a dose is, only when there is a dose
                    row["dose_statistic"] = ""
                dose_rows.append(row)
                self.support("arm_doses", row["dose_id"], row, passage, self.where(d.get("evidence")))
        return arm_rows, dose_rows

    # -- 3c and 3d. results ---------------------------------------------------------------------
    def result_format(self) -> tuple[list, str, object]:
        """The fields of a result row: what the model is shown (form) and the answer format of one row (item)."""
        names = [*self.groups_by_name, "all animals"]
        fs = self.fields("results", skip=("arm_id", "reference_arm_id", "as_written", "location"))
        described = []
        for f in fs:
            extra = " Empty for a finding stated in words; what was found then goes in notes." if f.name == "value" else ""
            described.append(self.show(f) + extra)
        form = ("### results: one row per result\nA result is anything the study found about its animals: a count, a "
                "measurement, a comparison, a test result, or a finding stated in words. Every number field "
                "is answered: the number, or null when the passage gives none.\n"
                f"group (a choice: {' | '.join(names)})\n  The group the result is about; all animals when it is "
                "for every animal together.\n"
                f"reference_group (a choice: {' | '.join(names)})\n  The group it is compared against, when stated.\n"
                + "\n".join(described))
        numbers = [f.name for f in fs if f.type in ("integer", "number")]

        def item():
            group = {"type": "string"} if getattr(LIGHT, "on", False) else {"type": "string", "enum": names}
            # each number is answered, with a value or with null when not stated (see row())
            return self.row(fs, ["result_kind", "endpoint", *numbers], {"group": group, "reference_group": group})
        return fs, form, item

    def _answer_format(self, item, own: dict, required: list, ids: list, strict: bool) -> dict:
        it = item()
        rest = {k: v for k, v in it["properties"].items() if k != "evidence"}
        one = {"type": "string", "enum": ids} if strict or "sentence" in own else {"type": "string"}
        if strict:
            rest["p_value"] = {"type": "string", "pattern": P_VALUE}
        props = {k: (one if v == "one" else {"type": "array", "items": one} if v == "many" else v) for k, v in own.items()}
        row = {"type": "object", "required": [*required, *[r for r in it["required"] if r != "evidence"]],
               "properties": props | {"borrowed": EV} | rest}
        return {"type": "object", "required": ["results", "no_result"], "properties": {
            "results": {"type": "array", "items": row}, "no_result": {"type": "array", "items": one}}}

    def _request(self, prompt: str, item, own: dict, required: list, ids: list, system: str, doc: str,
                 cache: str, st: dict) -> dict:
        st["requests"] += 1
        try:
            return self.llm.call("structure", prompt, self._answer_format(item, own, required, ids, True),
                                 system=system, doc=doc, cache=cache or "")
        except Exception as e:
            if not too_complex(e):
                raise
            st["format_refused"] += 1               # the API refused the format: once more, lighter
            return self.llm.call("structure", prompt, self._answer_format(item, own, required, ids, False),
                                 system=system, doc=doc, cache=cache or "")

    def _borrowed(self, r: dict, st: dict) -> list:
        also = []
        for e in [e for e in r.get("borrowed") or [] if isinstance(e, dict) and str(e.get("quote") or "").strip()]:
            loc = str(e.get("location") or "").strip()
            part = self.by_id.get(loc.split(".")[0])
            ok = part is not None and in_text(str(e["quote"]), part["text"])
            st["second_quotes"] += 1
            st["second_quote_not_found"] += not ok
            also.append({"location": loc, "quote": str(e["quote"]), "found": ok})
        return also

    def packs(self) -> list:
        focus = [q for q in self.parts if q["kind"] == "text" and WANT & set(self.tags.get(q["id"], []))]
        groups = [[]]
        for q in focus:
            if groups[-1] and sum(len(x["text"]) for x in groups[-1]) + len(q["text"]) > FOCUS:
                groups.append([])
            groups[-1].append(q)
        return [g for g in groups if g]

    def read_text(self, cache: str) -> tuple[list, dict]:
        """Results from the text: every sentence of the results paragraphs gets rows or is listed as no result."""
        fs, form, item = self.result_format()
        form = form.replace("\ngroup (a choice", "\n" + OWN_SENTENCE + "group (a choice", 1)
        system, doc = prompt_text("read"), f"read_{self.sid}"
        own = {"sentence": "one", "quote": {"type": "string"}}
        rows, groups = [], self.packs()
        same = same_sentences(groups)
        st = {"requests": 0, "sentences": 0, "reasked": 0, "unanswered": 0, "rows_out_of_focus": 0,
              "quote_not_in_sentence": 0, "second_quotes": 0, "second_quote_not_found": 0, "format_refused": 0,
              "p_value_not_printed": 0, "same_sentences": len(same), "no_result": 0}
        for g in groups:
            cut = {q["id"]: sentences(q["text"]) for q in g}
            text_at = {f"{pid}.{n}": s for pid, ss in cut.items() for n, s in enumerate(ss, 1)}
            st["sentences"] += len(text_at)
            todo = [s for s in text_at if s not in same]        # a sentence read before is not read again
            for attempt in (1, 2):
                if not todo:
                    break
                narrow = attempt == 2
                shown = [{"id": q["id"], "title": q["title"], "sentences": [
                             {"id": f"{q['id']}.{n}", "text": s} | ({"read_before_as": same[f"{q['id']}.{n}"]} if f"{q['id']}.{n}" in same else {})
                             for n, s in enumerate(cut[q["id"]], 1)]} for q in g if not narrow or any(t.startswith(q["id"] + ".") for t in todo)]
                prompt = (("" if cache else self.notes) + "## Paragraphs to read\n" + json.dumps(shown, ensure_ascii=False, indent=0)
                          + "\n\n## This request\nGive every result in the paragraphs to read, one row per result. List under "
                            "no_result the sentences that state no result. A sentence marked read_before_as is already done."
                          + ("\nStill open, to be answered now: " + ", ".join(todo) + "." if narrow else "")
                          + "\n\n" + form)
                ids = [s["id"] for q in shown for s in q["sentences"] if s["id"] not in same]
                a = self._request(prompt, item, own, ["sentence", "quote"], ids, system, doc, cache, st)
                done = set()
                for x in a.get("no_result") or []:
                    if str(x) in text_at:
                        done.add(str(x))
                        st["no_result"] += 1
                for r in [r for r in a.get("results") or [] if isinstance(r, dict)]:
                    home, quote = str(r.get("sentence") or "").strip(), str(r.get("quote") or "").strip()
                    if home not in text_at or not quote:
                        st["rows_out_of_focus"] += 1        # no quote from a sentence in focus: the row is not kept
                        continue
                    done.add(home)
                    st["quote_not_in_sentence"] += not in_text(quote, text_at[home])
                    also = self._borrowed(r, st)
                    rec = {f.name: text_of(r.get(f.name)) for f in fs}
                    if rec.get("p_value") and not printed(rec["p_value"], [text_at[home]] + [x["quote"] for x in also]):
                        st["p_value_not_printed"] += 1      # no evidence, no value
                        rec["p_value"] = ""
                    rows.append(rec | {"_part": home.split(".")[0], "_quote": quote, "_also": also,
                                       "group": r.get("group"), "reference_group": r.get("reference_group")})
                todo = [t for t in todo if t not in done]
                if attempt == 1:
                    st["reasked"] += len(todo)
            st["unanswered"] += len(todo)
        return rows, st

    def tables(self) -> list:
        """Each table the results steps read: its title and notes, its heading, and its rows cut into cells."""
        out = []
        for q in self.parts:
            if q["kind"] != "table" or not WANT & set(self.tags.get(q["id"], [])):
                continue
            lines = q["text"].split("\n")
            data = [ln for ln in lines if ln.rstrip().endswith("|")]
            if len(data) < 2:
                continue
            rows = []
            for n, line in enumerate(data[1:], 1):
                cells = [c.strip() for c in line.strip().rstrip("|").split("|")]
                rows.append({"row": f"R{n}", "label": cells[0], "line": line,
                             "cells": [{"id": f"R{n}C{k}", "text": c} for k, c in enumerate(cells[1:], 1)
                                       if c.lower() not in EMPTY_CELL]})
            out.append({"id": q["id"], "notes": [ln for ln in lines if not ln.rstrip().endswith("|")],
                        "heading": [c.strip() for c in data[0].strip().rstrip("|").split("|")], "head_line": data[0],
                        "rows": rows})
        return out

    @staticmethod
    def blocks(t: dict) -> list:
        out = [[]]
        for r in t["rows"]:
            if out[-1] and sum(len(x["cells"]) for x in out[-1]) + len(r["cells"]) > CELLS:
                out.append([])
            out[-1].append(r)
        return [b for b in out if any(r["cells"] for r in b)]

    def read_tables(self, cache: str) -> tuple[list, dict]:
        """Results from the tables: every cell is used by a row or listed as no result; a row names all its cells."""
        fs, form, item = self.result_format()
        form = form.replace("\ngroup (a choice", "\n" + OWN_CELLS + "group (a choice", 1)
        system, doc = prompt_text("table"), f"table_{self.sid}"
        own = {"cells": "many"}
        rows = []
        st = {"tables": 0, "requests": 0, "cells": 0, "reasked": 0, "cells_left_open": 0, "rows_naming_no_cell": 0,
              "second_quotes": 0, "second_quote_not_found": 0, "p_value_not_printed": 0, "format_refused": 0,
              "no_result": 0}
        for t in self.tables():
            st["tables"] += 1
            for b in self.blocks(t):
                text = {c["id"]: c["text"] for r in b for c in r["cells"]}
                line = {c["id"]: r["line"] for r in b for c in r["cells"]}
                st["cells"] += len(text)
                todo = list(text)
                for attempt in (1, 2):
                    if not todo:
                        break
                    shown = {"table": t["id"], "title and notes": t["notes"], "heading": t["heading"],
                             "rows": [{"row": r["row"], "label": r["label"],
                                       "cells": [{"id": c["id"], "text": c["text"]} for c in r["cells"]]} for r in b]}
                    prompt = (("" if cache else self.notes) + "## Table rows to read\n" + json.dumps(shown, ensure_ascii=False, indent=0)
                              + "\n\n## This request\nGive every result in these cells, one row per result. List under no_result the "
                                "cells that hold no result."
                              + ("\nStill open, to be answered now: " + ", ".join(todo) + "." if attempt == 2 else "") + "\n\n" + form)
                    a = self._request(prompt, item, own, ["cells"], list(text), system, doc, cache, st)
                    done = set()
                    for x in a.get("no_result") or []:
                        if str(x) in text:
                            done.add(str(x))
                            st["no_result"] += 1
                    for r in [r for r in a.get("results") or [] if isinstance(r, dict)]:
                        mine = [c for c in dict.fromkeys(str(x) for x in r.get("cells") or []) if c in text]
                        if not mine:
                            st["rows_naming_no_cell"] += 1  # not kept
                            continue
                        done |= set(mine)
                        also = self._borrowed(r, st)
                        rec = {f.name: text_of(r.get(f.name)) for f in fs}
                        if rec.get("p_value") and not printed(rec["p_value"], [text[c] for c in mine] + [x["quote"] for x in also]):
                            st["p_value_not_printed"] += 1  # no evidence, no value
                            rec["p_value"] = ""
                        lines = list(dict.fromkeys(line[c] for c in mine))
                        rows.append(rec | {"_part": t["id"], "_quote": t["head_line"] + " … " + " … ".join(lines), "_also": also,
                                           "group": r.get("group"), "reference_group": r.get("reference_group")})
                    todo = [c for c in todo if c not in done]
                    if attempt == 1:
                        st["reasked"] += len(todo)
                st["cells_left_open"] += len(todo)
        return rows, st

    def result_rows(self, found: list) -> list:
        """Rows as the database holds them: the group's id, the passage, where it stands; a field kept
        only for the kind of result the codebook gives it to."""
        fs = self.fields("results", skip=("arm_id", "reference_arm_id", "as_written", "location"))
        out = []
        for r in found:
            rec = {f.name: self.allowed(f, r.get(f.name, "")) if r.get(f.name) else "" for f in fs}
            for f in fs:
                if rec[f.name] and f.applies and not f.applies_to(rec):
                    rec[f.name] = ""
            part = self.by_id.get(r["_part"]) or {}
            out.append(rec | {"ID": self.ID, "source_id": self.sid, "result_id": f"{self.ID}-R{len(out) + 1}",
                              "arm_id": self.groups_by_name.get(r.get("group"), ""),
                              "reference_arm_id": self.groups_by_name.get(r.get("reference_group"), ""),
                              "as_written": " … ".join([r["_quote"]] + [a["quote"] for a in r.get("_also") or []]),
                              "location": part.get("title") or r["_part"]})
        return out

    # -- the whole reading ----------------------------------------------------------------------
    def _held(self, system: str, doc: str) -> str:
        return self.llm.open_cache("structure", system, self.notes, doc)

    def _release(self, cache: str, doc: str):
        if cache:
            self.llm.close_cache(cache, "structure", doc)

    def run(self) -> tuple[dict, dict]:
        """The tables of this reading ({table: [rows]}, evidence included) and what happened on the way."""
        stats = {}
        LIGHT.on = False                    # a format lightened for one paper is not kept for the next
        if self.tags is None:
            self.sort()
        self.cache = self._held(prompt_text("fill"), f"fill_{self.sid}")
        try:
            study, sites = self.study()
            arms, doses = self.groups()
            cache = self._held(prompt_text("read"), f"read_{self.sid}")
            try:
                text_rows, stats["text"] = self.read_text(cache)
            finally:
                self._release(cache, f"read_{self.sid}")
            table_rows, stats["tables"] = [], {}
            if self.tables():
                cache = self._held(prompt_text("table"), f"table_{self.sid}")
                try:
                    table_rows, stats["tables"] = self.read_tables(cache)
                finally:
                    self._release(cache, f"table_{self.sid}")
            results = self.result_rows(text_rows + table_rows)
            self.judge(study, arms, results)
        finally:
            self._release(self.cache, f"fill_{self.sid}")
        if self.calc:
            study["notes"] = (study.get("notes", "") + " calculations: " + "; ".join(self.calc)).strip()
        for name, (passage, location) in self.study_evidence.items():
            if study.get(name):
                self.support_field("studies", "", name, study[name], passage, location)
        stats["fill"] = self.stats
        return ({"studies": [study], "sites": sites, "arms": arms, "arm_doses": doses, "results": results,
                 "evidence": self.evidence}, stats)

    def support_field(self, table: str, key: str, name: str, value: str, passage: str, location: str):
        f = self.cb.field(table, name)
        if f is None or f.evidence == "none" or not (passage or location):
            return
        quote = passage[:QUOTE_MAX] if f.evidence == "quote" else ""
        self.evidence.append({"ID": self.ID, "source_id": self.sid, "table": table, "row_key": key, "field": name,
                              "value": value, "quote": quote,
                              "quote_truncated": ("yes" if len(passage) > QUOTE_MAX else "no") if quote else "",
                              "location": location or "document"})


def labels_file(root: Path, source_id: str) -> Path:
    return Path(root) / "notes" / f"{source_id}.json"


def stored_labels(root: Path, source_id: str, text: str, pdf: bool | None) -> dict | None:
    """The note sections kept from an earlier sorting of this very text, or None."""
    f = labels_file(root, source_id)
    if not f.is_file():
        return None
    saved = json.loads(f.read_text("utf-8"))
    return saved["sections"] if saved.get("parts") == [p["text"] for p in parts(text, pdf)] else None


def keep_labels(root: Path, reading: Reading):
    f = labels_file(root, reading.sid)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"parts": [p["text"] for p in reading.parts], "sections": reading.tags},
                            ensure_ascii=False, indent=1), "utf-8")


# -- stored studies read again --------------------------------------------------------------
def reading_file(root: Path, study_id: str) -> Path:
    return Path(root) / "readings" / f"{study_id}.json"


def reread(ids: list[str], llm, db_path: Path, say=print, workers: int = 3) -> dict:
    """Read stored studies again, each from the text its stored reading used (sources.text_file:
    the full text when it may be sent, else the abstract), and put each new reading in place of
    the old one (Store.replace_reading).

    Paid work is never thrown away: a finished reading is written to readings/<ID>.json beside
    the database before anything else, and stored from there; a reading found there is stored
    without asking the model again. Work is handed out only as fast as it is stored, so a run
    that stops leaves nothing half-paid. A study is replaced only when its whole reading came
    through; one that failed, in the reading or in the storing, keeps its old reading and is
    tried again by the next run. Studies done are noted in reread_done.json."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from datetime import date

    from .extract import LOG_COLUMNS, _log_table
    from .store import Store

    root = Path(db_path).parent
    store = Store(db_path)
    note = root / "reread_done.json"
    done = json.loads(note.read_text("utf-8")) if note.is_file() else {}
    out = {"read": 0, "failed": 0, "skipped": 0, "already done": 0, "stored from a kept reading": 0}

    def put(study_id: str, sid: str) -> bool:
        """Store the reading kept on disk for this study; False when the database refuses it."""
        kept = json.loads(reading_file(root, study_id).read_text("utf-8"))
        try:
            counts = store.replace_reading(study_id, kept["tables"])
        except Exception as e:  # noqa: BLE001 - one study that cannot be stored must not stop the others
            out["failed"] += 1
            say(f"{study_id}: read, but not stored; its reading is kept in {reading_file(root, study_id).name} "
                f"({type(e).__name__}: {str(e)[:160]})")
            return False
        text_st, table_st = kept["stats"]["text"], kept["stats"].get("tables") or {}
        line = (f"{counts['results']} results, {counts['arms']} groups, {counts['arm_doses']} doses; "
                f"sentences {text_st['sentences']} (left open {text_st['unanswered']}), table cells "
                f"{table_st.get('cells', 0)} (left open {table_st.get('cells_left_open', 0)}), p-values "
                f"emptied as not printed {text_st['p_value_not_printed'] + table_st.get('p_value_not_printed', 0)}, "
                f"quotes not found {kept['stats']['fill']['quote_not_found'] + text_st['quote_not_in_sentence']}, "
                f"breeds now for the whole study {counts['breeds_set_to_the_whole_study']}, breeds rows dropped as "
                f"already there {counts['breeds_already_there_for_the_whole_study']}")
        with store.db:
            names = ", ".join(f'"{c}"' for c in LOG_COLUMNS)
            store.db.execute(
                f"INSERT INTO extraction_log ({names}) VALUES ({', '.join('?' * len(LOG_COLUMNS))})",
                [sid, study_id, "reading", "", "", "", "", line, date.today().isoformat()])
        done[study_id] = {"date": date.today().isoformat(), **counts}
        note.write_text(json.dumps(done, indent=1), "utf-8")
        say(f"{study_id}: {line}")
        return True

    def one(job):
        study_id, sid, text, pdf, labels = job
        reading = Reading(study_id, sid, text, llm, store.cb, pdf=pdf, labels=labels)
        tables, stats = reading.run()
        f = reading_file(root, study_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"source_id": sid, "tables": tables, "stats": stats}, ensure_ascii=False), "utf-8")
        keep_labels(root, reading)
        return study_id

    jobs = []
    try:
        _log_table(store)
        for study_id in ids:
            if study_id in done:
                out["already done"] += 1
                continue
            row = store.db.execute(
                "SELECT so.source_id, so.file_name, so.text_file FROM study_sources ss JOIN sources so "
                "USING (source_id) WHERE ss.ID = ? AND ss.role = 'primary'", (study_id,)).fetchone()
            path = root / (row[2] or f"docs/{row[0]}.txt") if row else None
            if path is None or not path.is_file() or not path.read_text("utf-8").strip():
                say(f"{study_id}: no primary document with a text; left as it is")
                out["skipped"] += 1
                continue
            if reading_file(root, study_id).is_file():       # read before, not stored yet: no model call
                out["stored from a kept reading"] += put(study_id, row[0])
                continue
            text, pdf = path.read_text("utf-8"), str(row[1]).endswith(".pdf") or None
            jobs.append((study_id, row[0], text, pdf, stored_labels(root, row[0], text, pdf)))

        waiting = iter(jobs)
        with ThreadPoolExecutor(workers) as pool:
            running = {}
            try:
                while True:
                    for job in waiting:                      # hand out work only as fast as it is stored
                        running[pool.submit(one, job)] = job
                        if len(running) >= workers:
                            break
                    if not running:
                        break
                    finished, _ = wait(running, return_when=FIRST_COMPLETED)
                    for f in finished:
                        study_id, sid = running.pop(f)[:2]
                        try:
                            f.result()
                        except Exception as e:  # noqa: BLE001 - one study that fails must not stop the others
                            out["failed"] += 1
                            say(f"{study_id}: not read, its stored reading stays ({type(e).__name__}: {str(e)[:160]})")
                            continue
                        out["read"] += put(study_id, sid)
            finally:
                for f in running:                            # stopping: nothing more is started
                    f.cancel()
    finally:
        store.close()
    return out
