"""The Planner (step 1): the vet's words into topics/<topic>.toml, every item sourced and checked.

Each round the model (prompts/plan.md) drafts the search and the topic's screening parts, and
code checks the draft: a "vet" source must be copied from the vet's words, a "paper" source
must be a paper the vet marked relevant and contain the term, an "answer" source must be in
the vet's answers (an item failing its check is shown as a suggestion); every MeSH heading
must exist (unknown ones are dropped and shown), and each heading's natural other names
(MeSH entry terms) are added as text words. Code counts PubMed hits per term and for the whole
search, and picks papers for the vet to mark.

The vet reads corpus/plan/<topic>/round<n>.md, fills answers<n>.toml (answers, papers marked
relevant or not, papers they already know) and runs the next round; after the last round
(a setting) or whenever the vet is happy, `--start` writes the topic file.
"""
import json
import random
import re
from datetime import date, datetime
from pathlib import Path

from . import connectors as c
from .search import bad_term_check, connect, pubmed_term, query
from .settings import CORPUS, ROOT, settings
from .validate import _flat

SOURCES = ["vet", "paper", "answer", "suggestion"]


def _items() -> dict:
    return {"type": "array", "items": {
        "type": "object", "required": ["item", "source", "evidence"],
        "properties": {"item": {"type": "string"},
                       "source": {"type": "string", "enum": SOURCES},
                       "evidence": {"type": "string"}}}}


SCHEMA = {"type": "object", "required": ["reasoning", "restatement", "species", "condition",
                                         "blocks", "related", "treatments", "targets",
                                         "questions"],
          "properties": {
              "reasoning": {"type": "string"},
              "restatement": {"type": "string"},
              "species": {"type": "string"},
              "condition": {"type": "string"},
              "blocks": {"type": "array", "items": {
                  "type": "object", "required": ["block", "terms"],
                  "properties": {
                      "block": {"type": "string", "enum": ["species", "condition", "setting"]},
                      "terms": {"type": "array", "items": {
                          "type": "object", "required": ["kind", "term", "source", "evidence"],
                          "properties": {"kind": {"type": "string", "enum": ["mesh", "text"]},
                                         "term": {"type": "string"},
                                         "source": {"type": "string", "enum": SOURCES},
                                         "evidence": {"type": "string"}}}}}}},
              "related": _items(), "treatments": _items(), "targets": _items(),
              "questions": {"type": "array", "items": {
                  "type": "object", "required": ["question", "options"],
                  "properties": {"question": {"type": "string"},
                                 "options": {"type": "array", "items": {"type": "string"}}}}}}}
SHOWN_BEST, SHOWN_RANDOM = 5, 2        # papers shown: PubMed's best matches, then a random pick


def folder(topic: str) -> Path:
    return CORPUS / "plan" / topic


def _state(topic: str) -> dict:
    p = folder(topic) / "state.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def _save(topic: str, state: dict):
    folder(topic).mkdir(parents=True, exist_ok=True)
    (folder(topic) / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                              "utf-8")


# ---- code's checks ---------------------------------------------------------------------

def check_source(item: dict, term: str, words: str, marked: dict, answers: str) -> str:
    """Why an item's source doesn't hold ('' when it does)."""
    src, ev = item["source"], item["evidence"].strip()
    if src == "vet" and not (ev and _flat(ev) in _flat(words)):
        return "its evidence is not in the vet's words"
    if src == "paper":
        paper = marked.get(ev)
        if not paper:
            return "not a paper the vet marked relevant"
        text = " ".join([paper.get("title", ""), paper.get("abstract", ""), paper.get("mesh", "")])
        if _flat(term.rstrip("*")) not in _flat(text):
            return "not in that paper"
    if src == "answer" and not (ev and _flat(ev) in _flat(answers)):
        return "not in the vet's answers"
    return ""


def check(draft: dict, words: str, marked: dict, answers: str, http: c.Http) -> dict:
    """The draft as code accepts it: terms with sources checked, MeSH verified and expanded,
    hit counts; every change is noted."""
    terms, problems = [], []
    for b in draft["blocks"]:
        for t in b["terms"]:
            t = {"block": b["block"], **{k: t[k].strip() for k in ("kind", "term", "source",
                                                                   "evidence")}}
            why = bad_term_check(t)
            if why:
                problems.append(f"{t['term']!r} dropped: {why}")
                continue
            if t["kind"] == "mesh":
                heading = c.mesh_heading(http, t["term"])
                if heading is None:
                    problems.append(f"MeSH {t['term']!r} dropped: no heading has this name")
                    continue
                t["term"] = heading["name"]
                t["entry_terms"] = [e for e in heading["entry_terms"] if "," not in e][:10]
            note = check_source(t, t["term"], words, marked, answers)
            terms.append(t | ({"source": "suggestion", "note": note} if note else {}))
    have = {(t["block"], t["term"].lower()) for t in terms}
    for t in [t for t in terms if t.get("entry_terms")]:
        for e in t.pop("entry_terms"):
            if (t["block"], e.lower()) not in have:
                have.add((t["block"], e.lower()))
                terms.append({"block": t["block"], "kind": "text", "term": e, "source": "mesh",
                              "evidence": f"other name of MeSH {t['term']}"})
    out = {k: draft[k] for k in ("reasoning", "restatement", "species", "condition", "questions")}
    for part in ("related", "treatments", "targets"):
        out[part] = []
        for it in draft[part]:
            note = check_source(it, it["item"], words, marked, answers)
            out[part].append(it | ({"source": "suggestion", "note": note} if note else {}))
    blocks = {t["block"] for t in terms}
    for need in ("species", "condition"):
        if need not in blocks:
            problems.append(f"no usable {need} block")
    out["terms"], out["problems"] = terms, problems
    if blocks >= {"species", "condition"}:
        out |= count_hits(terms, http, problems)
        for t in [t for t in terms if t["source"] == "mesh" and t.get("hits") == 0]:
            t["hits"] = None                  # an index synonym no record uses: clutter
            problems.append(f"{t['term']!r} left out: it adds no record")
        out["terms"] = [t for t in terms if t.get("hits") is not None]
    return out


def count_hits(terms: list[dict], http: c.Http, problems: list[str]) -> dict:
    """PubMed hits for each term with the other blocks, and for the whole search. A term
    PubMed can't find at all is dropped and shown (its hits stay None)."""
    for t in terms:
        try:
            c.pubmed_search(http, pubmed_term(t), limit=0)
            t["hits"] = 0
        except c.QueryProblem:
            t["hits"] = None
            problems.append(f"{t['term']!r} dropped: PubMed has no record with it")
    good = [t for t in terms if t["hits"] is not None]
    for t in good:
        others = [o for o in good if o["block"] != t["block"]]
        t["hits"] = c.pubmed_search(http, pubmed_term(t) + (" AND " + query(others) if others
                                                           else ""), limit=0)[0]
    return {"hits": c.pubmed_search(http, query(good), limit=0)[0]}


def pick_papers(topic: str, terms: list[dict], http: c.Http, shown: set[str]) -> list[str]:
    """PubMed's best matches, then a random pick from the first 200, none shown before."""
    _, ids = c.pubmed_search(http, query(terms), limit=200, sort="relevance")
    fresh = [i for i in ids if i not in shown]
    best = fresh[:SHOWN_BEST]
    rest = fresh[SHOWN_BEST:]
    n = settings()["pipeline"]["plan_papers"] - len(best)
    return best + random.Random(f"{topic}{len(shown)}").sample(rest, min(n, len(rest)))


def resolve_known(entries: list[str], http: c.Http) -> tuple[list[str], list[str]]:
    """PMIDs for the papers the vet pasted (PMIDs, DOIs or titles); what can't be found."""
    keys, missing = [], []
    for e in (x.strip() for x in entries if x.strip()):
        if e.isdigit():
            keys.append(e)
            continue
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", e, flags=re.IGNORECASE)
        q = f'"{doi}"[aid]' if doi.startswith("10.") else f'"{e}"[ti]'
        try:
            n, ids = c.pubmed_search(http, q, limit=2)
        except c.QueryProblem:
            n, ids = 0, []
        if n == 1:
            keys.append(ids[0])
        elif doi.startswith("10."):
            keys.append("doi:" + doi.lower())
        else:
            missing.append(e)
    return keys, missing


def known_found(known: list[str], terms: list[dict], http: c.Http) -> dict[str, bool]:
    q = query(terms)
    out = {}
    for k in known:
        if k.isdigit():
            out[k] = c.pubmed_search(http, f"({q}) AND {k}[pmid]", limit=0)[0] > 0
        else:
            q = query(terms, "europepmc", outside_pubmed=False)
            out[k] = c.europepmc_search(http, f'({q}) AND DOI:"{k[4:]}"', 1)[0] > 0
    return out


# ---- a round ---------------------------------------------------------------------------

def _message(state: dict, last: int) -> str:
    words, rounds = state["words"], state["rounds"]
    lines = ["The vet's words:", words, "", f"Round {len(rounds) + 1} of {last}."]
    if len(rounds) + 1 == last:
        lines.append("This is the last round: give no questions.")
    if rounds:
        prev = rounds[-1]
        lines += ["", ("Your last draft as code accepted it (notes say what code changed; "
                       "hits = PubMed records with the other blocks):"),
                  json.dumps({k: prev["checked"][k] for k in ("restatement", "species",
                              "condition", "terms", "related", "treatments", "targets",
                              "problems", "hits") if k in prev["checked"]},
                             ensure_ascii=False, indent=0),
                  "", "The vet's answers:",
                  json.dumps(prev.get("answers", []), ensure_ascii=False)]
        lines += ["", ("Papers the vet marked (relevant: yes or no), with title, abstract and "
                       "subject terms; a paper's key is its PMID:")]
        for p in prev.get("papers", []):
            if p.get("relevant") in ("yes", "no"):
                lines.append(json.dumps({k: p[k] for k in ("key", "relevant", "title",
                                                            "abstract", "mesh")},
                                        ensure_ascii=False))
        if state.get("known"):
            lines += ["", "Papers the vet knows, and whether this draft's search finds them:",
                      json.dumps(state.get("known_found", {}))]
    return "\n".join(lines)


def run_round(topic: str, llm, http: c.Http, words: str = "", answers_file: Path | None = None) -> dict:
    """One Planner round: the first from the vet's words, the next from an answers file."""
    cfg = settings()["pipeline"]
    state = _state(topic)
    if words:
        if state:
            raise ValueError(f"{topic} already has a plan in {folder(topic)}: continue it with "
                             "--answers, or move that folder away to start again")
        state = {"words": words, "rounds": [], "known": [], "started": ""}
    elif not state:
        raise ValueError(f"no plan for {topic} yet: give the vet's words")
    if state.get("started"):
        raise ValueError(f"{topic} was started on {state['started']}")
    if len(state["rounds"]) >= cfg["plan_rounds"]:
        raise ValueError(f"{topic}: {cfg['plan_rounds']} rounds done; run --start")
    if state["rounds"]:
        if answers_file is None:
            raise ValueError("give the answers file of the last round (--answers)")
        read_answers(state, Path(answers_file), http)
        prev = state["rounds"][-1]["checked"]
        if state["known"] and "hits" in prev:
            state["known_found"] = known_found(state["known"], prev["terms"], http)
    draft = llm.call("plan", _message(state, cfg["plan_rounds"]), SCHEMA,
                     system=(ROOT / "prompts" / "plan.md").read_text("utf-8"), doc=f"plan_{topic}")
    marked = {p["key"]: p for r in state["rounds"] for p in r.get("papers", [])
              if p.get("relevant") == "yes"}
    given = " ".join(f"{a['question']} {a['answer']}" for r in state["rounds"]
                     for a in r.get("answers", []))
    checked = check(draft, state["words"], marked, given, http)
    shown = {p["key"] for r in state["rounds"] for p in r.get("papers", [])}
    papers = []
    if "hits" in checked:
        keys = pick_papers(topic, checked["terms"], http, shown)
        papers = [{"key": r.pmid, "title": r.title, "abstract": r.abstract, "mesh": r.mesh,
                   "relevant": ""} for r in c.pubmed_records(http, keys)]
        if state["known"]:
            state["known_found"] = known_found(state["known"], checked["terms"], http)
    state["rounds"].append({"n": len(state["rounds"]) + 1, "draft": draft, "checked": checked,
                            "papers": papers, "answers": [], "time": datetime.now().isoformat()})
    _save(topic, state)
    write_for_vet(topic, state, cfg["plan_rounds"])
    return state


def read_answers(state: dict, path: Path, http: c.Http):
    import tomllib
    a = tomllib.loads(path.read_text("utf-8"))
    last = state["rounds"][-1]
    last["answers"] = [{"question": q["question"], "answer": q.get("answer", "").strip()}
                       for q in a.get("question", []) if q.get("answer", "").strip()]
    marks = {p["key"]: p.get("relevant", "").strip().lower() for p in a.get("paper", [])}
    for p in last["papers"]:
        p["relevant"] = marks.get(p["key"], "")
    keys, missing = resolve_known(a.get("known", []), http)
    state["known"] = list(dict.fromkeys(state["known"] + keys))
    state.setdefault("known_missing", []).extend(missing)


def _toml(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def write_for_vet(topic: str, state: dict, last: int):
    r = state["rounds"][-1]
    ch, n = r["checked"], r["n"]
    out = [f"# Planner round {n} of {last}: {topic}", "", f"**The question:** {ch['restatement']}",
           f"**Species:** {ch['species']}. **Condition:** {ch['condition']}.", "",
           (f"**The search** finds {ch.get('hits', '?')} PubMed records (OR inside a block, AND "
            "between blocks):"), "", "| block | term | kind | source | hits | note |",
           "|---|---|---|---|---|---|"]
    out += [f"| {t['block']} | {t['term']} | {t['kind']} | {t['source']}: {t['evidence']} | "
            f"{t.get('hits', '')} | {t.get('note', '')} |" for t in ch["terms"]]
    if ch["problems"]:
        out += ["", "**Code dropped:**"] + [f"- {p}" for p in ch["problems"]]
    for part in ("related", "treatments", "targets"):
        out += ["", f"**{part.capitalize()}:** " + "; ".join(
            f"{i['item']} ({i['source']}{': ' + i['note'] if i.get('note') else ''})" for i in ch[part])]
    if state.get("known"):
        out += ["", "**Papers you know:** " + ", ".join(
            f"{k} {'found' if v else 'NOT FOUND'}" for k, v in state.get("known_found", {}).items())]
    out += ["", "## Questions"] + [f"{i}. {q['question']} ({' / '.join(q['options'])})"
                                   for i, q in enumerate(ch["questions"], 1)]
    out += ["", "## Papers from this search: relevant or not?"]
    for p in r["papers"]:
        out += [f"- **{p['key']}** {p['title']}", f"  {p['abstract'][:250]}…"]
    out += ["", "## The model's reasoning", ch["reasoning"]]
    (folder(topic) / f"round{n}.md").write_text("\n".join(out) + "\n", "utf-8")
    form = [f"# Planner round {n}: answer, mark the papers, list papers you know, then run",
            f"#   uv run vet plan {topic} --answers {folder(topic) / f'answers{n}.toml'}",
            "# or, when the search looks right:", f"#   uv run vet plan {topic} --start", "",
            "known = []     # PMIDs, DOIs or titles of papers you know are relevant", ""]
    for q in ch["questions"]:
        form += ["[[question]]", f"question = {_toml(q['question'])}",
                 f"options = {_toml(q['options'])}", 'answer = ""     # one option, or your own words', ""]
    for p in r["papers"]:
        form += ["[[paper]]", f"key = {_toml(p['key'])}", f"title = {_toml(p['title'])}",
                 'relevant = ""   # yes or no', ""]
    (folder(topic) / f"answers{n}.toml").write_text("\n".join(form), "utf-8")


# ---- start: write the topic file -------------------------------------------------------

def start(topic: str) -> Path:
    """Write topics/<topic>.toml from the last round; the topic's own [[fields]] are kept and
    the previous file is saved in corpus/plan/<topic>/."""
    state = _state(topic)
    if not state.get("rounds"):
        raise ValueError(f"no plan for {topic} yet")
    ch = state["rounds"][-1]["checked"]
    if ch["problems"] and "hits" not in ch:
        raise ValueError(f"the last round has no usable search: {ch['problems']}")
    related = " ".join(i["item"] for i in ch["related"])
    cond = [t["term"] for t in ch["terms"] if t["block"] == "condition" and t["kind"] == "text"
            and t["term"].lower() != ch["condition"].lower()
            and _flat(t["term"]) not in _flat(related)]        # a related condition, not a name
    also = list({w.lower(): w for w in reversed(cond)}.values())[::-1][:8]
    path = ROOT / "topics" / f"{topic}.toml"
    keep = ""
    if path.exists():
        old = path.read_text("utf-8")
        (folder(topic) / f"previous_{datetime.now():%Y%m%dT%H%M%S}.toml").write_text(old, "utf-8")
        i = old.find("[[fields]]")
        if i >= 0:
            j = old.rfind("\n\n", 0, i)          # with the comment just above the fields
            keep = old[j + 2 if j >= 0 else i:]
    lines = [f"# Topic {topic}: written by `vet plan` on {date.today()} from the vet's words",
             f"# ({_toml(state['words'])}) and {len(state['rounds'])} Planner round(s); the",
             f"# rounds are in corpus/plan/{topic}/. Sources: vet, paper, answer, mesh, suggestion.",
             "", f"question = {_toml(ch['restatement'])}",
             f"known = {_toml(state.get('known', []))}", "", "[screen]",
             f"species = {_toml(ch['species'])}", f"condition = {_toml(ch['condition'])}",
             f"also_called = {_toml(also)}",
             f"related = {_toml([i['item'] for i in ch['related']])}",
             f"treatments = {_toml([i['item'] for i in ch['treatments']])}", ""]
    for t in ch["terms"]:
        lines += ["[[terms]]"] + [f"{k} = {_toml(t[k])}" for k in ("block", "kind", "term",
                                                                   "source", "evidence")] + [""]
    for i in ch["targets"]:
        lines += ["[[targets]]", f"item = {_toml(i['item'])}", f"source = {_toml(i['source'])}", ""]
    path.write_text("\n".join(lines) + ("\n" + keep if keep else ""), "utf-8")
    state["started"] = date.today().isoformat()
    _save(topic, state)
    record_input(topic)
    return path


def record_input(topic: str, db: Path | None = None):
    """Keep the vet's own words and the agreed search in the database, beside the records they
    found (table topic_inputs): the words as typed, the question as agreed, and the expanded
    terms with their PubMed hits. The queries actually run are in the queries table."""
    state = _state(topic)
    if not state.get("rounds"):
        raise ValueError(f"no plan for {topic} yet")
    ch = state["rounds"][-1]["checked"]
    con = connect(db or CORPUS / "vetriever.db")
    try:
        with con:
            con.execute("INSERT OR REPLACE INTO topic_inputs (topic, words, question, terms, "
                        "rounds, started) VALUES (?, ?, ?, ?, ?, ?)",
                        (topic, state["words"], ch["restatement"],
                         json.dumps(ch["terms"], ensure_ascii=False), len(state["rounds"]),
                         state.get("started") or date.today().isoformat()))
    finally:
        con.close()
