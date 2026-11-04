"""Search ⇄ review (step 5) and gaps (step 10): a ReAct loop with code as the hands.

Each round the model (prompts/review.md) reads code's counts and proposes changes in a fixed
form; code tests and runs them, and the next round's counts are what the model observes.

The brake, so the search grows enough but not too much:
1. a new MeSH heading must exist, and a term must be usable as written;
2. before anything is screened, code counts each new term's (or extra query's) new records;
   more than `term_max_new` goes back to the model once as too broad;
3. after screening, a new term stays only if at least `term_min_kept` of its own records
   were kept; a term is removed only if as few of its records were kept and no known paper
   depends on it.
Follow-ups (references, citing papers, similar articles) and extra queries only screen
records that name the topic's species, at most `round_max_new` new records a round.
Code stops when a round adds no new kept record, or after `review_rounds` rounds.
"""
import json
import re
import sqlite3
import tomllib
from collections import Counter
from datetime import date

from . import connectors as c
from . import search as s
from .screen import screen
from .settings import ROOT, settings

KEPT = ("keep", "lead")
SAVE_EVERY = 100


def relevant(r: dict) -> bool:
    """A kept record that reports the condition (or a related one) with its quote: what the
    stop rule and the yields count. Records kept only because a drop lacked its quote, and
    healthy animals given a treatment, are kept but don't count."""
    a = r.get("answers") or {}
    a = json.loads(a) if isinstance(a, str) else a
    flags = r["flags"] if isinstance(r["flags"], list) else r["flags"].split("|")
    return (r["decision"] in KEPT and "unbacked_drop" not in flags
            and a.get("condition", {}).get("answer") in ("yes", "related"))
SCHEMA = {"type": "object", "required": ["reasoning", "term_changes", "follow", "extra_queries",
                                         "nothing_left"],
          "properties": {
              "reasoning": {"type": "string"},
              "term_changes": {"type": "array", "items": {
                  "type": "object", "required": ["block", "action", "kind", "term", "evidence"],
                  "properties": {
                      "block": {"type": "string", "enum": ["species", "condition", "setting"]},
                      "action": {"type": "string", "enum": ["add", "remove"]},
                      "kind": {"type": "string", "enum": ["mesh", "text"]},
                      "term": {"type": "string"}, "evidence": {"type": "string"}}}},
              "follow": {"type": "array", "items": {
                  "type": "object", "required": ["key", "how"],
                  "properties": {"key": {"type": "string"},
                                 "how": {"type": "string",
                                         "enum": ["references", "cited_by", "similar"]}}}},
              "extra_queries": {"type": "array", "items": {
                  "type": "object", "required": ["query", "target"],
                  "properties": {"query": {"type": "string"}, "target": {"type": "string"}}}},
              "nothing_left": {"type": "boolean"}}}
TERMS = """CREATE TABLE IF NOT EXISTS terms (topic TEXT, block TEXT, kind TEXT, term TEXT,
  source TEXT, evidence TEXT, added_round INTEGER, removed_round INTEGER, why TEXT)"""


def load_topic(name: str) -> dict:
    return tomllib.loads((ROOT / "topics" / f"{name}.toml").read_text("utf-8"))


def current_terms(db: sqlite3.Connection, name: str, topic: dict) -> list[dict]:
    """The live search: the topic file's terms, then what review rounds added or removed."""
    db.execute(TERMS)
    if not db.execute("SELECT 1 FROM terms WHERE topic = ?", (name,)).fetchone():
        db.executemany("INSERT INTO terms VALUES (?,?,?,?,?,?,1,NULL,'')",
                       [(name, t["block"], t["kind"], t["term"], t["source"], t["evidence"])
                        for t in topic["terms"]])
        db.commit()
    return [dict(r) for r in db.execute(
        "SELECT block, kind, term, source, evidence, added_round FROM terms "
        "WHERE topic = ? AND removed_round IS NULL ORDER BY rowid", (name,))]


def log(db, name, rnd, what, detail, outcome):
    db.execute("INSERT INTO topic_log VALUES (?,?,?,?,?,?)",
               (name, rnd, what, json.dumps(detail, ensure_ascii=False) if not isinstance(
                   detail, str) else detail, outcome, date.today().isoformat()))
    db.commit()


def decisions(db, name) -> dict[str, str]:
    return {r[0]: r[1] for r in db.execute(
        "SELECT key, decision FROM screening WHERE topic = ?", (name,))}


def _record(db, key) -> dict:
    r = db.execute("SELECT * FROM records WHERE key = ?", (key,)).fetchone()
    return dict(r) if r else {}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z\-]{4,}", text.lower())


STOP = set(re.findall(r"[a-z]+", """about after among animals based between canine dogs during effect effects
following group groups other patients study their these those using which while with within
without clinical report retrospective prospective treatment treated cases case review"""))


# ---- what the model reads --------------------------------------------------------------

def counts(name: str, topic: dict, terms: list[dict], db, http: c.Http, rnd: int,
           coverage: dict | None = None) -> dict:
    dec = decisions(db, name)
    kept = {k for k, d in dec.items() if d in KEPT}
    dropped = {k for k, d in dec.items() if d == "off_topic"}
    problems: list[str] = []
    per_term = []
    for t in terms:
        ids = set(s.pubmed_ids(http, s.term_only(terms, t), problems)[1])
        per_term.append({"block": t["block"], "kind": t["kind"], "term": t["term"],
                         "only_it_finds": len(ids), "of_those_kept": len(ids & kept)})
    in_search = {t["term"].lower() for t in terms}
    mesh_k, mesh_d, word_k, word_d = Counter(), Counter(), Counter(), Counter()
    for k in kept | dropped:
        r = _record(db, k)
        heads = {m for m in r.get("mesh", "").split("|") if m and m.lower() not in in_search}
        words = {w for w in _words(r.get("title", "")) if w not in STOP and w not in in_search}
        (mesh_k if k in kept else mesh_d).update(heads)
        (word_k if k in kept else word_d).update(words)
    cands = [{"kind": "mesh", "term": m, "kept": n, "dropped": mesh_d[m]}
             for m, n in mesh_k.most_common(40) if n >= 3][:15]
    cands += [{"kind": "title word", "term": w, "kept": n, "dropped": word_d[w]}
              for w, n in word_k.most_common(40) if n >= 3][:10]
    hit_set = set(s.pubmed_ids(http, s.query(terms), problems)[1])
    missed = []
    for row in db.execute("SELECT DISTINCT key, how FROM found WHERE topic = ? AND how NOT IN "
                          "('pubmed', 'europepmc')", (name,)):
        if row["key"] in kept and row["key"].isdigit() and row["key"] not in hit_set:
            r = _record(db, row["key"])
            missed.append({"key": row["key"], "found_by": row["how"], "title": r.get("title"),
                           "subject_terms": r.get("mesh")})
    known = {k: k in hit_set for k in topic.get("known", [])}
    if coverage is None:
        coverage = {}
        texts = {k: (_record(db, k).get("title", "") + " " + _record(db, k).get("abstract", "")).lower()
                 for k in kept}
        for tg in topic.get("targets", []):
            words = [w[:6] for w in _words(tg["item"]) if w not in STOP][:3]
            coverage[tg["item"]] = sum(any(w in t for w in words) for t in texts.values()) if words else None
    leads = [{"key": k, "title": _record(db, k).get("title")} for k, d in dec.items() if d == "lead"]
    done = {(r["what"], r["detail"]) for r in db.execute(
        "SELECT what, detail FROM topic_log WHERE topic = ?", (name,))}
    history = [{"round": r["round"], "what": r["what"], "detail": r["detail"], "outcome": r["outcome"]}
               for r in db.execute("SELECT * FROM topic_log WHERE topic = ? ORDER BY rowid", (name,))]
    return {"terms": per_term, "candidates": cands, "missed": missed[:30],
            "known_papers_found_by_search": known, "coverage": coverage,
            "leads": [ld for ld in leads if ("follow", json.dumps({"key": ld["key"], "how": "references"})) not in done][:20],
            "history": history[-60:], "problems": problems,
            "kept_so_far": len(kept), "dropped_so_far": len(dropped)}


def system_prompt(topic: dict) -> str:
    cfg = settings()["pipeline"]
    return (ROOT / "prompts" / "review.md").read_text("utf-8").format(
        question=" ".join(topic["question"].split()), species=topic["screen"]["species"],
        condition=topic["screen"]["condition"],
        targets="; ".join(t["item"] for t in topic.get("targets", [])) or "none given",
        max_terms=cfg["max_terms"], max_follow=cfg["max_follow"], max_queries=cfg["max_queries"])


# ---- what code does with the answer ----------------------------------------------------

def _new_records(http, db, name, rnd, recs_or_pmids, how, terms, budget) -> int:
    """Store what a follow-up or query found; only records naming the species and not yet
    found for this topic are linked to the topic (and so screened)."""
    if recs_or_pmids and isinstance(recs_or_pmids[0], str):
        keys = s.pubmed_fetch(http, db, recs_or_pmids)
    else:
        keys = s.store_records(db, recs_or_pmids)
    have = {r[0] for r in db.execute("SELECT key FROM found WHERE topic = ?", (name,))}
    new = [k for k in dict.fromkeys(keys) if k not in have and s.species_match(_record(db, k), terms)]
    new = new[:max(budget, 0)]
    s.mark_found(db, name, rnd, new, how)
    return len(new)


def _openalex_id(http, db, key) -> str:
    r = _record(db, key)
    if r.get("openalex_id"):
        return r["openalex_id"]
    flt = f"pmid:{key}" if key.isdigit() else f"doi:{r.get('doi') or key.removeprefix('doi:')}"
    _, recs = c.openalex_works(http, 1, filter=flt)
    return recs[0].openalex_id if recs else ""


def follow(http, db, name, rnd, key, how, terms, budget) -> tuple[int, str]:
    if how == "similar":
        if not key.isdigit():
            return 0, "similar articles need a PMID"
        return _new_records(http, db, name, rnd, c.pubmed_similar(http, key, 20),
                            f"similar:{key}", terms, budget), ""
    wid = _openalex_id(http, db, key)
    if not wid:
        return 0, "not in OpenAlex"
    flt = f"cited_by:{wid}" if how == "references" else f"cites:{wid}"
    _, recs = c.openalex_works(http, 200, filter=flt)
    pmids = [r.pmid for r in recs if r.pmid]
    n = _new_records(http, db, name, rnd, pmids, f"{how}:{key}", terms, budget) if pmids else 0
    others = [r for r in recs if not r.pmid]
    if others:
        n += _new_records(http, db, name, rnd, others, f"{how}:{key}", terms, budget - n)
    return n, ""


def _added_records(http, terms, t, problems) -> tuple[int, list[str]]:
    trial = terms + [t]
    return s.pubmed_ids(http, s.term_only(trial, trial[-1]), problems)


def act(name, topic, terms, answer, db, http, rnd, llm, system, message) -> dict:
    """Apply one review answer under the brake; returns what was done."""
    cfg = settings()["pipeline"]
    budget = cfg["round_max_new"]
    done = {(r["what"], r["detail"]) for r in db.execute(
        "SELECT what, detail FROM topic_log WHERE topic = ?", (name,))}
    have_terms = {(t["block"], t["term"].lower()) for t in terms}
    added, broad, problems, first = [], [], [], answer
    for revising in (False, True):
        broad_now = []
        for ch in answer["term_changes"][:cfg["max_terms"]]:
            t = {k: ch[k].strip() for k in ("block", "kind", "term")} | {
                "source": "review", "evidence": ch["evidence"]}
            if ch["action"] == "remove":
                if not revising:
                    _remove(name, topic, terms, t, db, http, rnd)
                continue
            if (t["block"], t["term"].lower()) in have_terms:
                continue
            why = s.bad_term_check(t)
            if not why and t["kind"] == "mesh":
                h = c.mesh_heading(http, t["term"])
                why = "" if h else "no MeSH heading has this name"
                t["term"] = h["name"] if h else t["term"]
            if why:
                log(db, name, rnd, "add term", t, f"refused: {why}")
                continue
            n, ids = _added_records(http, terms + added, t, problems)
            if n > cfg["term_max_new"]:
                broad_now.append({"term": t["term"], "block": t["block"], "new_records": n})
                log(db, name, rnd, "add term", t, f"too broad: {n} new records")
                continue
            have_terms.add((t["block"], t["term"].lower()))
            added.append(t)
            db.execute("INSERT INTO terms VALUES (?,?,?,?,?,?,?,NULL,'')",
                       (name, t["block"], t["kind"], t["term"], t["source"], t["evidence"], rnd))
            have = {r[0] for r in db.execute("SELECT key FROM found WHERE topic = ?", (name,))}
            ids = s.pubmed_fetch(http, db, ids[:max(budget, 0)]) if ids else []
            s.mark_found(db, name, rnd, ids, f"term:{t['term']}")     # judged on all it adds
            got = len(set(ids) - have)
            budget -= got
            log(db, name, rnd, "add term", t, f"added: {n} new records, {got} to screen")
        broad += broad_now
        if revising or not broad_now:
            break
        answer = llm.call("review", message + "\n\nCode's observation: these new terms add too "
                          "many records; revise or drop them (give only term_changes that "
                          "replace them):\n" + json.dumps(broad_now), SCHEMA, system=system,
                          doc=f"review_{name}")
        answer = {"term_changes": answer["term_changes"]}
    for f in first["follow"][:cfg["max_follow"]]:
        what = json.dumps({"key": f["key"], "how": f["how"]})
        if ("follow", what) in done:
            continue
        n, why = follow(http, db, name, rnd, f["key"], f["how"], terms, budget)
        budget -= n
        log(db, name, rnd, "follow", what, why or f"{n} new records naming the species")
    for q in first["extra_queries"][:cfg["max_queries"]]:
        if ("query", q["query"]) in done:
            continue
        if "BODY:" in q["query"].upper():
            log(db, name, rnd, "query", q["query"], "refused: full-text search is not used")
            continue
        try:
            hits, recs = c.europepmc_search(http, q["query"], 1000)
        except Exception as e:  # noqa: BLE001 — a bad query is reported to the model
            log(db, name, rnd, "query", q["query"], f"refused by Europe PMC: {e}")
            continue
        s.log_query(db, name, rnd, "europepmc", q["query"], hits)
        if hits > cfg["term_max_new"]:
            log(db, name, rnd, "query", q["query"], f"too broad: {hits} records")
            continue
        n = _new_records(http, db, name, rnd, recs, f"query:{q['query'][:80]}", terms, budget)
        budget -= n
        log(db, name, rnd, "query", q["query"], f"{hits} hits, {n} new naming the species")
    return {"added": added, "too_broad": broad, "problems": problems}


def _remove(name, topic, terms, t, db, http, rnd):
    cfg = settings()["pipeline"]
    match = [o for o in terms if o["block"] == t["block"] and o["term"].lower() == t["term"].lower()]
    if not match:
        return log(db, name, rnd, "remove term", t, "refused: not in the search")
    rest = [o for o in terms if o is not match[0]]
    _, ids = s.pubmed_ids(http, s.term_only(terms, match[0]), [])
    dec = decisions(db, name)
    kept = sum(dec.get(i) in KEPT for i in ids)
    if ids and kept / len(ids) >= cfg["term_min_kept"]:
        return log(db, name, rnd, "remove term", t, f"refused: {kept} of its {len(ids)} records kept")
    if not rest or not {o["block"] for o in rest} >= {o["block"] for o in terms}:
        return log(db, name, rnd, "remove term", t, "refused: it is its block's last term")
    hits = set(s.pubmed_ids(http, s.query(rest), [])[1])
    lost = [k for k in topic.get("known", []) if k.isdigit() and k not in hits]
    if lost:
        return log(db, name, rnd, "remove term", t, f"refused: the search would lose known papers {lost}")
    db.execute("UPDATE terms SET removed_round = ?, why = ? WHERE topic = ? AND block = ? AND "
               "lower(term) = lower(?) AND removed_round IS NULL",
               (rnd, f"{kept} of its {len(ids)} records kept", name, t["block"], t["term"]))
    terms.remove(match[0])
    log(db, name, rnd, "remove term", t, f"removed: {kept} of its {len(ids)} records kept")


def judge_yields(name, db, rnd):
    """After screening: each follow-up's and query's yield, for the model's history."""
    rows = {r["key"]: dict(r) for r in db.execute(
        "SELECT * FROM screening WHERE topic = ?", (name,))}
    for (how,) in db.execute("SELECT DISTINCT how FROM found WHERE topic = ? AND round = ? AND "
                             "how NOT IN ('pubmed', 'europepmc', 'known')", (name, rnd)).fetchall():
        keys = [k for (k,) in db.execute("SELECT key FROM found WHERE topic = ? AND how = ?",
                                         (name, how)) if k in rows]
        log(db, name, rnd, "yield", how, f"{sum(relevant(rows[k]) for k in keys)} relevant of "
                                         f"{len(keys)} screened")


def judge_added(name, added, db, rnd):
    """After screening: a new term stays only if enough of its own records were kept."""
    cfg = settings()["pipeline"]
    for t in added:
        keys = [r[0] for r in db.execute("SELECT key FROM found WHERE topic = ? AND how = ?",
                                         (name, f"term:{t['term']}"))]
        rows = {r["key"]: dict(r) for r in db.execute(
            "SELECT * FROM screening WHERE topic = ?", (name,))}
        kept = sum(relevant(rows[k]) for k in keys if k in rows)
        if keys and kept / len(keys) < cfg["term_min_kept"]:
            db.execute("UPDATE terms SET removed_round = ?, why = ? WHERE topic = ? AND term = ? "
                       "AND removed_round IS NULL",
                       (rnd, f"only {kept} of its {len(keys)} records kept", name, t["term"]))
            log(db, name, rnd, "judge term", t["term"], f"removed: only {kept} of {len(keys)} kept")
        else:
            log(db, name, rnd, "judge term", t["term"], f"stays: {kept} of {len(keys)} kept")
    db.commit()


# ---- the loop --------------------------------------------------------------------------

def screen_new(name, topic, db, llm, rnd, say) -> int:
    """Screen everything found and not yet screened; returns how many are relevant."""
    todo = s.to_screen(db, name)
    if not todo:
        return 0
    say(f"  screening {len(todo)} records")
    n = 0
    for i in range(0, len(todo), SAVE_EVERY):      # saved as it goes: a crash loses one chunk
        results = screen(name, topic, todo[i:i + SAVE_EVERY], llm)
        s.save_screening(db, name, rnd, results)
        n += sum(relevant(r) for r in results)
    return n


def run(name: str, llm, http: c.Http | None = None, db: sqlite3.Connection | None = None,
        say=print) -> dict:
    """Search, screen and review until a round adds no new kept record."""
    cfg = settings()["pipeline"]
    http, db = http or c.Http(), db or s.connect()
    topic = load_topic(name)
    terms = current_terms(db, name, topic)
    first = s.run_search(name, topic | {"terms": terms}, db, http, rnd=1)
    say(f"round 1: PubMed {first['pubmed']}, Europe PMC (outside PubMed) {first['europepmc']}")
    for p in first["problems"]:
        say(f"  problem: {p}")
    if first["pubmed"] + first["europepmc"] > cfg["screen_budget"]:
        raise ValueError(f"{first['pubmed'] + first['europepmc']} records is more than the "
                         f"screening budget ({cfg['screen_budget']}): narrow the search with "
                         "the Planner first")
    s.mark_found(db, name, 1, [k for k in topic.get("known", [])], "known")
    if any(k.isdigit() for k in topic.get("known", [])):
        s.pubmed_fetch(http, db, [k for k in topic.get("known", []) if k.isdigit()])
    kept = screen_new(name, topic, db, llm, 1, say)
    say(f"  kept {kept}")
    system = system_prompt(topic)
    rounds = [{"round": 1, "new_kept": kept}]
    for rnd in range(2, cfg["review_rounds"] + 2):
        message = "Counts after round {}:\n{}".format(rnd - 1, json.dumps(
            counts(name, topic, terms, db, http, rnd), ensure_ascii=False, indent=0))
        answer = llm.call("review", message, SCHEMA, system=system, doc=f"review_{name}")
        log(db, name, rnd, "reasoning", answer["reasoning"], "")
        did = act(name, topic, terms, answer, db, http, rnd, llm, system, message)
        terms = current_terms(db, name, topic)
        new_kept = screen_new(name, topic, db, llm, rnd, say)
        judge_added(name, did["added"], db, rnd)
        judge_yields(name, db, rnd)
        terms = current_terms(db, name, topic)
        say(f"round {rnd}: added {[t['term'] for t in did['added']]}, too broad "
            f"{[b['term'] for b in did['too_broad']]}, new kept {new_kept}")
        rounds.append({"round": rnd, "new_kept": new_kept})
        if new_kept == 0:
            say("  stop: this round added no new relevant record")
            break
    return {"rounds": rounds, "kept": sum(d in KEPT for d in decisions(db, name).values())}


def gaps(name: str, coverage: dict, llm, http: c.Http | None = None,
         db: sqlite3.Connection | None = None, say=print) -> int:
    """Step 10: one more targeted round, with coverage taken from the database."""
    http, db = http or c.Http(), db or s.connect()
    topic = load_topic(name)
    terms = current_terms(db, name, topic)
    rnd = 1 + (db.execute("SELECT max(round) FROM topic_log WHERE topic = ?", (name,)).fetchone()[0] or 1)
    message = "Gaps after extraction (coverage = studies in the database per target):\n" + json.dumps(
        counts(name, topic, terms, db, http, rnd, coverage=coverage), ensure_ascii=False, indent=0)
    system = system_prompt(topic)
    answer = llm.call("review", message, SCHEMA, system=system, doc=f"review_{name}")
    log(db, name, rnd, "reasoning (gaps)", answer["reasoning"], "")
    did = act(name, topic, terms, answer, db, http, rnd, llm, system, message)
    new_kept = screen_new(name, topic, db, llm, rnd, say)
    judge_added(name, did["added"], db, rnd)
    judge_yields(name, db, rnd)
    return new_kept
