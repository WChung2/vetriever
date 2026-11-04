"""What the screens show: read-only queries on the database that return plain dicts and lists.

The web pages (vet/ui) call only these functions and `approve.set_approved`, so another front
end (the clinical_ai app, or a React page) can reuse them unchanged. Every query here opens the
database read-only, so no screen can change data by being looked at.
"""
import csv
import json
import re
import sqlite3
import tomllib
from pathlib import Path

from . import chat as chatdb
from .approve import DESIGN_ORDER
from .settings import CORPUS, ROOT, settings
from .who import rank

TOPICS = ROOT / "topics"
TOPIC_NAME = re.compile(r"[a-z0-9_]{1,40}")
STUDY_ID = re.compile(r"VET-\d{1,9}")
# the lists of the Review step ({rej}: the rejected column, or '' in a database made before it)
VIEWS = {"to_review": "s.decision = 'keep' AND s.approved = '' AND {rej} = ''",
         "approved": "s.approved != ''",
         "rejected": "{rej} != ''",
         "dropped": "s.decision = 'off_topic' AND s.approved = ''",
         "overviews": "s.decision = 'lead'",
         "aside": "s.decision = 'aside'"}
RULES = {"O1": "it studies only other species, or people",
         "O2": "the condition is only background, not studied",
         "O3": "no animals were studied (laboratory work or an induced model)"}
FLAGS = {"related_condition": "related condition", "research_animals": "healthy dogs treated",
         "survey": "survey", "single_case": "single case", "title_only": "no abstract",
         "unbacked_drop": "kept: no quote for its drop", "not_answered": "kept: not answered"}
PER_PAGE = 50


def read_only(db: Path) -> sqlite3.Connection:
    """The database opened read-only: a write through it fails."""
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def topic_names() -> list[str]:
    return sorted(p.stem for p in TOPICS.glob("*.toml") if TOPIC_NAME.fullmatch(p.stem))


def topic_file(name: str) -> dict:
    """topics/<name>.toml; only names in topics/ are read (KeyError otherwise)."""
    if name not in topic_names():
        raise KeyError(name)
    return tomllib.loads((TOPICS / f"{name}.toml").read_text("utf-8"))


def _fetched(con) -> set[str]:
    """PMIDs and lowercased DOIs that have a full text."""
    if "fetches" not in _tables(con):
        return set()
    got = set()
    for pmid, doi in con.execute("SELECT pmid, doi FROM fetches WHERE route != ''"):
        got |= {pmid or "", (doi or "").lower()}
    return got - {""}


def _rej(con) -> str:
    cols = [r[1] for r in con.execute("PRAGMA table_info(screening)")]
    return "COALESCE(s.rejected, '')" if "rejected" in cols else "''"


def _view(con, view: str) -> str:
    return VIEWS[view].format(rej=_rej(con))


def counts(db: Path, name: str) -> dict:
    """How far a topic has come: found; screened (keep, lead = overviews, off_topic, aside);
    the vet's review (to_review, approved, rejected); with full text; studies; result rows."""
    out = {"found": 0, "keep": 0, "lead": 0, "off_topic": 0, "aside": 0, "to_review": 0,
           "approved": 0, "rejected": 0, "full_text": 0, "studies": 0, "results": 0}
    if not Path(db).exists():
        return out
    con = read_only(db)
    try:
        have = _tables(con)
        if "found" in have:
            out["found"] = con.execute("SELECT COUNT(DISTINCT key) FROM found WHERE topic = ?",
                                       (name,)).fetchone()[0]
        if "screening" in have:
            for d, n in con.execute("SELECT decision, COUNT(*) FROM screening WHERE topic = ? "
                                    "GROUP BY decision", (name,)):
                out[d] = n
            for v in ("to_review", "rejected"):
                out[v] = con.execute(f"SELECT COUNT(*) FROM screening s WHERE s.topic = ? AND "
                                     f"{_view(con, v)}", (name,)).fetchone()[0]
            got = _fetched(con)
            approved = con.execute(
                "SELECT r.pmid, r.doi FROM screening s JOIN records r ON r.key = s.key "
                "WHERE s.topic = ? AND s.approved != ''", (name,)).fetchall()
            out["approved"] = len(approved)
            out["full_text"] = sum(1 for p, d in approved
                                   if (p and p in got) or (d and d.lower() in got))
        if "study_topics" in have:
            out["studies"] = con.execute("SELECT COUNT(DISTINCT ID) FROM study_topics "
                                         "WHERE topic = ?", (name,)).fetchone()[0]
            out["results"] = con.execute(
                "SELECT COUNT(*) FROM results WHERE ID IN "
                "(SELECT ID FROM study_topics WHERE topic = ?)", (name,)).fetchone()[0]
    finally:
        con.close()
    return out


def topics(db: Path) -> list[dict]:
    return [{"name": n, "question": " ".join(topic_file(n).get("question", "").split()),
             "condition": topic_file(n).get("screen", {}).get("condition", ""),
             "counts": counts(db, n)} for n in topic_names()]


SELECT = ("SELECT s.key, s.decision, s.rule, s.flags, s.summary, s.design, s.approved, "
          "{rej} AS rejected, s.answers, r.title, r.year, r.journal, r.pmid, r.doi "
          "FROM screening s JOIN records r ON r.key = s.key")


def records(db: Path, name: str, view: str = "to_review", q: str = "", flag: str = "",
            design: str = "", page: int = 1) -> dict:
    """One page of a list of the Review step, with the filters' choices and every list's count."""
    topic_file(name)
    view = view if view in VIEWS else "to_review"
    page = max(1, page)
    out = {"rows": [], "total": 0, "page": page, "pages": 1, "view": view,
           "tabs": {v: 0 for v in VIEWS}, "flags": {}, "designs": {}}
    if not Path(db).exists():
        return out
    con = read_only(db)
    try:
        if "screening" not in _tables(con):
            return out
        for v in VIEWS:
            out["tabs"][v] = con.execute(f"SELECT COUNT(*) FROM screening s WHERE s.topic = ? "
                                         f"AND {_view(con, v)}", (name,)).fetchone()[0]
        where, args = [_view(con, view)], [name]
        for f, d in con.execute(f"SELECT s.flags, s.design FROM screening s WHERE s.topic = ? "
                                f"AND {_view(con, view)}", (name,)):
            for one in filter(None, (f or "").split("|")):
                out["flags"][one] = out["flags"].get(one, 0) + 1
            out["designs"][d or ""] = out["designs"].get(d or "", 0) + 1
        if q.strip():
            where.append("(r.title LIKE ? OR s.summary LIKE ? OR r.key = ?)")
            args += [f"%{q.strip()}%", f"%{q.strip()}%", q.strip()]
        if flag:
            where.append("('|' || s.flags || '|') LIKE ?")
            args.append(f"%|{flag}|%")
        if design:
            where.append("s.design = ?")
            args.append(design)
        sql = " AND ".join(where)
        out["total"] = con.execute(f"SELECT COUNT(*) FROM screening s JOIN records r "
                                   f"ON r.key = s.key WHERE s.topic = ? AND {sql}",
                                   args).fetchone()[0]
        out["pages"] = max(1, -(-out["total"] // PER_PAGE))
        order = " ".join(f"WHEN '{d}' THEN {i}" for i, d in enumerate(DESIGN_ORDER))
        rows = con.execute(
            SELECT.format(rej=_rej(con)) + f" WHERE s.topic = ? AND {sql} "
            "ORDER BY instr(s.flags, 'research_animals') > 0, instr(s.flags, 'unbacked_drop') > 0, "
            f"CASE s.design {order} ELSE 9 END, r.year DESC, s.key LIMIT ? OFFSET ?",
            [*args, PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
        out["rows"] = [_record(r) for r in rows]
        hows = _found_by(con, name, [r["key"] for r in out["rows"]])
        for r in out["rows"]:
            r["found_by"] = hows.get(r["key"], [])
    finally:
        con.close()
    return out


def found_by(how: str) -> str:
    """How a record was found, in words: 'the PubMed search', 'references of PMID 123'."""
    kind, _, what = how.partition(":")
    return {"pubmed": "the PubMed search", "europepmc": "the Europe PMC search",
            "known": "a paper you named", "term": f'the term "{what}", added in a review round',
            "references": f"the references of PMID {what}",
            "cited_by": f"papers citing PMID {what}",
            "query": "a full-text query in a review round"}.get(kind, how)


def _found_by(con, name: str, keys: list[str]) -> dict[str, list[str]]:
    if not keys or "found" not in _tables(con):
        return {}
    out = {}
    marks = ", ".join("?" for _ in keys)
    for key, how in con.execute(f"SELECT key, how FROM found WHERE topic = ? AND key IN "
                                f"({marks}) ORDER BY round, how", (name, *keys)):
        out.setdefault(key, []).append(found_by(how))
    return out


def record(db: Path, name: str, key: str) -> dict | None:
    """One screened record of a topic (the card a list shows)."""
    con = read_only(db)
    try:
        r = con.execute(SELECT.format(rej=_rej(con)) + " WHERE s.topic = ? AND s.key = ?",
                        (name, key)).fetchone()
        return _record(r) | {"found_by": _found_by(con, name, [key]).get(key, [])} if r else None
    finally:
        con.close()


def keys_in(db: Path, name: str, view: str) -> list[str]:
    """Every record of one list of the Review step (to approve or undo them all at once)."""
    topic_file(name)
    con = read_only(db)
    try:
        return [r[0] for r in con.execute(f"SELECT s.key FROM screening s WHERE s.topic = ? AND "
                                          f"{_view(con, view)}", (name,))]
    finally:
        con.close()


def _record(r) -> dict:
    a = json.loads(r["answers"] or "{}")
    return {"key": r["key"], "decision": r["decision"], "rule": r["rule"],
            "rule_text": RULES.get(r["rule"], ""),
            "flags": [f for f in (r["flags"] or "").split("|") if f],
            "summary": r["summary"] or "", "design": r["design"] or "",
            "approved": r["approved"] or "", "rejected": r["rejected"] or "",
            "title": r["title"] or "(no title)",
            "year": r["year"] or "", "journal": r["journal"] or "", "pmid": r["pmid"] or "",
            "doi": r["doi"] or "", "reasoning": a.get("reasoning", ""),
            "checks": [(c, a[c]["answer"], a[c]["quote"]) for c in ("condition", "animals")
                       if isinstance(a.get(c), dict)]}


def studies(db: Path, topic: str = "") -> list[dict]:
    """Every extracted study (or one topic's), newest first, with the essentials clinical_ai's
    vet table shows: title, status, species, condition, intervention, enrolled, sites, KOLs
    by name (first and last authors, or a trial's director), sponsor, year."""
    if topic:
        topic_file(topic)
    if not Path(db).exists():
        return []
    con = read_only(db)
    try:
        if "studies" not in _tables(con):
            return []
        ids = [r[0] for r in (con.execute("SELECT DISTINCT ID FROM study_topics WHERE topic = ?",
                                          (topic,)) if topic else con.execute("SELECT ID FROM studies"))]
        out = []
        for sid in ids:
            s = con.execute("SELECT * FROM studies WHERE ID = ?", (sid,)).fetchone()
            if s is None:
                continue
            src = _primary(con, sid)
            out.append({"ID": sid, "title": src.get("title", ""), "year": src.get("year", ""),
                        "first_author": src.get("first_author", ""), "access": src.get("access", ""),
                        "status": s["Overall Status"], "species": species_words(s["species"]),
                        "species_raw": s["species"],
                        "condition": s["disease_name"], "study_kind": s["study_kind"],
                        "intervention": _drugs(con, sid), "enrolled": s["n_enrolled"],
                        "sites": con.execute("SELECT COUNT(*) FROM sites WHERE ID = ?",
                                             (sid,)).fetchone()[0],
                        "kols": [p["Name"] for p in kols([dict(p) for p in con.execute(
                            "SELECT * FROM people WHERE ID = ? ORDER BY CAST(person_no AS "
                            "INTEGER)", (sid,))])[0]],
                        "sponsor": s["Sponsor"],
                        "topics": [r[0] for r in con.execute(
                            "SELECT topic FROM study_topics WHERE ID = ?", (sid,))]})
    finally:
        con.close()
    return sorted(out, key=lambda s: (s["year"], s["ID"]), reverse=True)


def _drugs(con, sid: str) -> str:
    """The drugs given, once each whatever their spelling: 'prednisolone, azathioprine'."""
    seen = {}
    for (d,) in con.execute("SELECT drug FROM arm_doses WHERE ID = ? ORDER BY dose_id", (sid,)):
        if d and d.strip():
            seen.setdefault(d.strip().lower(), d.strip())
    return ", ".join(seen.values())


ROLE_NAMES = {"first": "first author", "last": "last author",
              "study_director": "study director", "investigator": "lead investigator"}


def kols(people: list[dict]) -> tuple[list[dict], list[dict]]:
    """The key people of a study, and the others. For a publication the first and last authors
    usually lead it; middle authors often help with one part. For a trial record (a regulatory
    dossier or a registry entry) it is the study director who ran it, or else the first named
    investigator."""
    key = [p | {"why": ROLE_NAMES[p["author_position"]]} for p in people
           if p.get("role") == "author" and p.get("author_position") in ("first", "last")]
    lead = [p for p in people if p.get("role") == "study_director"] or \
        [p for p in people if p.get("role") == "investigator"][:1]
    key += [p | {"why": ROLE_NAMES[p["role"]]} for p in lead]
    names = set()
    key = [p for p in key if not (p["Name"] in names or names.add(p["Name"]))]
    return key, [p for p in people if p["Name"] not in names]


SAME_ANIMAL = {"dog": "dogs", "dogs": "dogs", "canine": "dogs", "canines": "dogs",
               "canis familiaris": "dogs", "canis lupus familiaris": "dogs", "puppies": "dogs",
               "cat": "cats", "cats": "cats", "feline": "cats", "felines": "cats",
               "felis catus": "cats", "kittens": "cats", "horse": "horses", "horses": "horses",
               "equine": "horses", "cattle": "cattle", "bovine": "cattle", "cow": "cattle",
               "cows": "cattle"}


def species_words(value: str) -> str:
    """'canine|dogs' as 'dogs': the species once each, in plain words."""
    seen = []
    for part in re.split(r"[|,;/]", value or ""):
        word = SAME_ANIMAL.get(part.strip().lower(), part.strip())
        if word and word not in seen:
            seen.append(word)
    return ", ".join(seen)


def _criteria(text: str) -> tuple[str, str]:
    """CT.gov-style eligibility text split into inclusion and exclusion, as clinical_ai shows it."""
    head, _, tail = (text or "").partition("Exclusion Criteria:")
    return head.replace("Inclusion Criteria:", "").strip(), tail.strip()


def _primary(con, sid: str) -> dict:
    r = con.execute("SELECT so.* FROM study_sources ss JOIN sources so ON so.source_id = "
                    "ss.source_id WHERE ss.ID = ? ORDER BY ss.role != 'primary' LIMIT 1",
                    (sid,)).fetchone()
    return dict(r) if r else {}


def study(db: Path, sid: str) -> dict | None:
    """One study with everything stored for it: its fields with their quotes, arms and doses,
    results by kind (each with the text it was read from), sources and people."""
    if not STUDY_ID.fullmatch(sid) or not Path(db).exists():
        return None
    con = read_only(db)
    try:
        s = con.execute("SELECT * FROM studies WHERE ID = ?", (sid,)).fetchone()
        if s is None:
            return None
        quotes = {}
        for e in con.execute("SELECT * FROM evidence WHERE ID = ?", (sid,)):
            quotes.setdefault((e["table"], e["row_key"], e["field"]), []).append(
                {"quote": e["quote"], "location": e["location"], "source_id": e["source_id"]})
        fields = [{"name": k, "value": s[k], "quotes": quotes.get(("studies", "", k), [])}
                  for k in s.keys() if k != "ID" and s[k]]  # noqa: SIM118 (a Row yields values)
        doses = {}
        for d in con.execute("SELECT * FROM arm_doses WHERE ID = ? ORDER BY dose_id", (sid,)):
            doses.setdefault(d["arm_id"], []).append(dict(d))
        arms = [dict(a) | {"doses": doses.get(a["arm_id"], []),
                           "quotes": quotes.get(("arms", a["arm_id"], "arm_as_written"), [])}
                for a in con.execute("SELECT * FROM arms WHERE ID = ? ORDER BY arm_id", (sid,))]
        labels = {a["arm_id"]: a["arm_label"] for a in arms}
        results = {"outcome": [], "adverse_event": [], "baseline": []}
        for r in con.execute("SELECT * FROM results WHERE ID = ? ORDER BY result_id", (sid,)):
            row = dict(r) | {"arm": labels.get(r["arm_id"], "all dogs" if not r["arm_id"]
                                               else r["arm_id"])}
            results.setdefault(r["result_kind"] or "outcome", []).append(row)
        sources = [dict(r) | {"role": r["role"]} for r in con.execute(
            "SELECT so.*, ss.role FROM study_sources ss JOIN sources so ON so.source_id = "
            "ss.source_id WHERE ss.ID = ?", (sid,))]
        people = [dict(p) for p in con.execute(
            "SELECT * FROM people WHERE ID = ? ORDER BY CAST(person_no AS INTEGER)", (sid,))]
        topics_ = [r[0] for r in con.execute("SELECT topic FROM study_topics WHERE ID = ?", (sid,))]
        sites = [dict(r) for r in con.execute(
            "SELECT * FROM sites WHERE ID = ? ORDER BY CAST(site_no AS INTEGER)", (sid,))]
        drugs = _drugs(con, sid)
    finally:
        con.close()
    inclusion, exclusion = _criteria(s["Eligibility Criteria"])
    key_people, others = kols(people)
    main = next((x for x in sources if x["role"] == "primary"), sources[0] if sources else {})
    return {"ID": sid, "title": main.get("title", ""), "main": main, "fields": fields,
            "arms": arms, "results": results, "sources": sources, "people": people,
            "topics": topics_, "study_kind": s["study_kind"], "n_enrolled": s["n_enrolled"],
            "status": s["Overall Status"], "species": species_words(s["species"]),
            "condition": s["disease_name"],
            "study_type": s["Study Type"], "country": s["Country"], "sponsor": s["Sponsor"],
            "summary": s["Summary"], "intervention": drugs, "sites": sites,
            "kols": key_people, "others": others,
            "inclusion": inclusion, "exclusion": exclusion,
            "primary_outcome": s["Primary Outcome Measures"]}


def institutions(db: Path, topic: str = "", limit: int = 50) -> list[dict]:
    """Root institutions by the papers they led: a first or last author there (one topic's
    approved papers, or all), with those authors' names."""
    if not Path(db).exists():
        return []
    con = read_only(db)
    try:
        if "authors" not in _tables(con):
            return []
        ids = None
        if topic:
            topic_file(topic)
            ids = [p or d for p, d in con.execute(
                "SELECT r.pmid, r.doi FROM screening s JOIN records r ON r.key = s.key "
                "WHERE s.topic = ? AND s.approved != ''", (topic,)) if p or d]
        return rank(con, ids, lead_only=True)[:limit]
    finally:
        con.close()


def spending(corpus: Path = CORPUS) -> dict:
    """Money spent on model calls (corpus/spend.csv) against the cap, by step and by model."""
    path = Path(corpus) / "spend.csv"
    rows = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    by_step, by_model, by_day = {}, {}, {}
    for r in rows:
        usd = float(r["usd"] or 0)
        for d, k in ((by_step, r["step"]), (by_model, r["model"]), (by_day, r["time"][:10])):
            d[k] = d.get(k, 0.0) + usd
    total = sum(by_step.values())
    cap = float(settings()["spending"]["cap_usd"])
    return {"total": total, "cap": cap, "share": min(1.0, total / cap) if cap else 0.0,
            "calls": len(rows),
            "by_step": sorted(by_step.items(), key=lambda x: -x[1]),
            "by_model": sorted(by_model.items(), key=lambda x: -x[1]),
            "by_day": sorted(by_day.items(), reverse=True)[:14]}


# the dashboard and the chat history

def _n(n: int, one: str, many: str = "") -> str:
    """'1 study', '2 studies', '1,295 records'."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def plan_state(corpus: Path, name: str) -> dict:
    """The Planner's saved rounds for a topic (corpus/plan/<topic>/state.json), if any."""
    p = Path(corpus) / "plan" / name / "state.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def title(name: str) -> str:
    t = topic_file(name)
    c = t.get("screen", {}).get("condition") or name.replace("_", " ")
    return c[:1].upper() + c[1:]


def searches(db: Path, corpus: Path) -> list[dict]:
    """The dashboard: every topic, most recent first, with its five steps (plan, search,
    approve, full text, extract), a status and its last activity."""
    running, last = {}, {}
    if Path(db).exists():
        con = read_only(db)
        try:
            running = {j["topic"]: j["kind"] for j in chatdb.jobs(con, state="running")}
            have = _tables(con)
            for table, col in (("screening", "date"), ("topic_log", "date"), ("chat", "time")):
                if table in have:
                    for topic, d in con.execute(f"SELECT topic, MAX({col}) FROM {table} "
                                                "GROUP BY topic"):
                        last[topic] = max(last.get(topic, ""), (d or "")[:10])
        finally:
            con.close()
    out = []
    for name in topic_names():
        c, state = counts(db, name), plan_state(corpus, name)
        rounds = state.get("rounds", [])
        for r in rounds:
            last[name] = max(last.get(name, ""), r.get("time", "")[:10])
        # planned: its topic file has search terms (an older topic that has searched, too)
        planned = bool(topic_file(name).get("terms")) or c["found"] > 0
        done = [planned, c["found"] > 0, c["approved"] > 0, c["full_text"] > 0, c["studies"] > 0]
        now_at = next((i for i, d in enumerate(done) if not d), None)
        if name in running:
            status, tone = f"{running[name]} running…", "wait"
        elif c["studies"]:
            status, tone = f"{_n(c['studies'], 'study', 'studies')} in the database", "built"
        elif c["full_text"]:
            status, tone = "full texts ready to read in", "wait"
        elif c["approved"]:
            status, tone = "approved: full texts next", "wait"
        elif c["to_review"]:
            status, tone = f"{c['to_review']:,} waiting for your review", "wait"
        elif planned:
            status, tone = "ready to search", "plan"
        else:
            status, tone = ("planning the search" if rounds else "new"), "plan"
        line = (f"{c['found']:,} found · {c['approved']:,} approved" if c["found"] else
                f"{len(rounds)} planning round{'' if len(rounds) == 1 else 's'}" if rounds
                else "not searched yet")
        out.append({"name": name, "title": title(name), "status": status, "tone": tone,
                    "steps": ["done" if d else ("now" if i == now_at else "")
                              for i, d in enumerate(done)],
                    "line": line, "date": last.get(name, ""), "counts": c})
    return sorted(out, key=lambda s: (s["date"], s["name"]), reverse=True)


def _costs(corpus: Path, name: str) -> dict:
    """What planning, screening and review cost a topic, from corpus/spend.csv (their calls are
    logged under plan_<topic>, screen_<topic> and review_<topic>). Extraction is logged per
    document, which tests and benchmarks share, so earlier runs show no extraction cost; a job
    started from the chat stores its own cost."""
    path, out = Path(corpus) / "spend.csv", {"plan": 0.0, "screen": 0.0, "review": 0.0}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            step = r["doc"].removesuffix(f"_{name}")
            if step in out and r["doc"] == f"{step}_{name}":
                out[step] += float(r["usd"] or 0)
    return out


def history(db: Path, corpus: Path, name: str) -> list[dict]:
    """A topic's conversation, as the chat shows it: the vet's words and the Planner's rounds
    (from its saved state), then each step the pipeline took (from the database), then any
    messages the chat stored. Earlier runs, made from the command line, read the same way."""
    topic_file(name)
    items, state = [], plan_state(corpus, name)
    if state.get("words"):
        items.append({"who": "vet", "kind": "question", "text": state["words"],
                      "time": (state.get("rounds") or [{}])[0].get("time", "")})
    for r in state.get("rounds", []):
        ch, asked = r.get("checked", {}), r.get("draft", {}).get("questions", [])
        answers = {a["question"]: a["answer"] for a in r.get("answers", [])}
        chips = [(ch.get("species", ""), "species"), (ch.get("condition", ""), "condition")]
        chips = [{"item": i, "source": "", "group": g} for i, g in chips if i]
        for group in ("related", "treatments", "targets"):
            chips += [{"item": x["item"], "source": x.get("source", ""), "group": group}
                      for x in ch.get(group, [])]
        items.append({"who": "planner", "kind": "round", "n": r["n"], "time": r.get("time", ""),
                      "restatement": ch.get("restatement", ""), "chips": chips,
                      "terms": ch.get("terms", []), "hits": ch.get("hits"),
                      "problems": ch.get("problems", []),
                      "questions": [{"question": q.get("question", ""),
                                     "options": q.get("options", []),
                                     "answer": answers.get(q.get("question", ""), "")}
                                    for q in asked],
                      "papers": [{"key": p["key"], "title": p.get("title", ""),
                                  "mark": p.get("relevant", "")} for p in r.get("papers", [])]})
    if not Path(db).exists():
        return items
    con = read_only(db)
    try:
        have, cost = _tables(con), _costs(corpus, name)
        if (state.get("started") or "found" in have) and state.get("rounds"):
            items.append({"who": "event", "kind": "done", "time": state.get("started", ""),
                          "text": f"Plan agreed after {len(state['rounds'])} round"
                                  f"{'' if len(state['rounds']) == 1 else 's'}",
                          "cost": cost["plan"]})
        if "screening" in have:
            rounds = {}
            for rnd, decision, n in con.execute(
                    "SELECT round, decision, COUNT(*) FROM screening WHERE topic = ? AND "
                    "decision != 'aside' GROUP BY round, decision ORDER BY round", (name,)):
                rounds.setdefault(rnd, {})[decision] = n
            aside = con.execute("SELECT COUNT(*) FROM screening WHERE topic = ? AND "
                                "decision = 'aside'", (name,)).fetchone()[0]
            log = {}
            if "topic_log" in have:
                for rnd, what, n in con.execute(
                        "SELECT round, what, COUNT(*) FROM topic_log WHERE topic = ? "
                        "GROUP BY round, what", (name,)):
                    log.setdefault(rnd, {})[what] = n
            for rnd, d in rounds.items():
                screened = sum(d.values())
                more = log.get(rnd, {})
                review = ", ".join(_n(more[k], one, many) for k, one, many in (
                    ("follow", "reference list or citation list followed",
                     "reference lists or citation lists followed"),
                    ("query", "extra query run", "extra queries run"),
                    ("add term", "term added", "terms added"),
                    ("remove term", "term removed", "terms removed")) if more.get(k))
                items.append({"who": "event", "kind": "progress", "time": "",
                              "text": f"{'Search' if rnd <= 1 else f'Search round {rnd} (widening)'}: "
                                      f"{_n(screened, 'record')} screened, "
                                      f"{d.get('keep', 0):,} kept, "
                                      f"{_n(d.get('lead', 0), 'overview')}, "
                                      f"{d.get('off_topic', 0):,} dropped"
                                      + (f" (widening: {review})" if review else "")})
            if aside:
                items.append({"who": "event", "kind": "progress", "time": "",
                              "text": f"{_n(aside, 'record')} set aside without screening "
                                      "(letters, comments, errata)"})
            if rounds:
                items.append({"who": "event", "kind": "done", "time": "",
                              "text": "Search finished: a round found no new relevant records",
                              "cost": cost["screen"] + cost["review"]})
        c = counts(db, name)
        if c["approved"]:
            items.append({"who": "event", "kind": "done", "time": "",
                          "text": f"You reviewed the list: {_n(c['approved'], 'record')} "
                                  f"approved" + (f", {c['rejected']:,} rejected"
                                                 if c["rejected"] else ""),
                          "link": "review"})
        if c["full_text"]:
            items.append({"who": "event", "kind": "done", "time": "",
                          "text": f"{_n(c['full_text'], 'full text')} fetched (free)"})
        if c["studies"]:
            items.append({"who": "event", "kind": "done", "time": "",
                          "text": f"{_n(c['studies'], 'study', 'studies')} read into the "
                                  f"database, of {_n(c['full_text'], 'full text')} downloaded",
                          "link": "studies"})
        for m in chatdb.messages(con, name):
            items.append({"who": m["role"], "kind": m["kind"], "time": m["time"],
                          "cost": m["cost"], **m["body"]})
    finally:
        con.close()
    return items


def _term_list(terms) -> list[dict]:
    """A topic file's terms as a list of {block, kind, term}: today's [[terms]] list, or the
    older layout of blocks ({block: {mesh: [...], text: [...]}})."""
    if isinstance(terms, dict):
        return [{"block": block, "kind": kind, "term": term}
                for block, kinds in terms.items() for kind, words in kinds.items()
                for term in words]
    return list(terms)


def built(db: Path, corpus: Path, name: str) -> dict:
    """How a search was built, for the records page: the vet's words as typed, the question as
    agreed, the expanded terms by block (with their PubMed hits), the first queries run, and
    what the review rounds added. From the database (topic_inputs, queries, topic_log); the
    Planner's saved state or the topic file when a topic predates topic_inputs."""
    t, state = topic_file(name), plan_state(corpus, name)
    out = {"words": state.get("words", ""), "question": " ".join(t.get("question", "").split()),
           "terms": _term_list(t.get("terms", [])), "started": state.get("started", ""), "rounds": 0,
           "first": [], "later": [], "added": {}}
    if not Path(db).exists():
        return out
    con = read_only(db)
    try:
        have = _tables(con)
        if "topic_inputs" in have:
            r = con.execute("SELECT * FROM topic_inputs WHERE topic = ?", (name,)).fetchone()
            if r:
                out |= {"words": r["words"], "question": r["question"],
                        "terms": json.loads(r["terms"]), "started": r["started"],
                        "rounds": r["rounds"]}
        if "queries" in have:
            for q in con.execute("SELECT round, service, query, hits, date FROM queries WHERE "
                                 "topic = ? ORDER BY round, rowid", (name,)):
                (out["first"] if q["round"] <= 1 else out["later"]).append(dict(q))
        if "topic_log" in have:
            for what, n in con.execute("SELECT what, COUNT(*) FROM topic_log WHERE topic = ? AND "
                                       "round > 1 GROUP BY what", (name,)):
                out["added"][what] = n
    finally:
        con.close()
    blocks = {}
    for term in out["terms"]:
        blocks.setdefault(term.get("block", ""), []).append(term)
    out["blocks"] = blocks
    a = out["added"]
    out["added_text"] = ", ".join(_n(a[k], one, many) for k, one, many in (
        ("add term", "search term", ""), ("follow", "follow-up of references or citations",
                                          "follow-ups of references or citations"),
        ("query", "full-text query", "full-text queries"),
        ("remove term", "term taken out", "terms taken out")) if a.get(k))
    return out
