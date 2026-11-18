"""Search (step 2) and sort by type (step 3), and the tables every later step reads.

The topic's `[[terms]]` are grouped in blocks (the species, the condition): OR inside a
block, AND between blocks. PubMed gets `"heading"[Mesh]` and `word[tiab]`; Europe PMC gets the
same text words in titles and abstracts plus `NOT SRC:MED`, to reach records PubMed doesn't
have. Records are merged on PMID, then DOI. Every query asks only for papers published from
`pipeline.published_from` on. Before screening, records are set aside unscreened, each with
its reason: letters and errata; a second copy of a paper already found; a record with too little
to judge (no year, no abstract, not a paper in a journal); and papers older than that year
(follow-ups can reach them). Everything lives in the database beside the codebook tables:

- records    one row per record, whatever found it (title, abstract, MeSH, types, IDs)
- found      which round found which record, and how (pubmed, europepmc, term:…, references:…)
- queries    every query run, with its hit count and date
- screening  the screening decision per topic and record, with the model's answers
- topic_log  what each review round proposed, what code did with it, and why
"""
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict
from datetime import date
from pathlib import Path

from . import connectors as c
from .settings import DB, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, pmid TEXT, doi TEXT, pmcid TEXT,
  openalex_id TEXT, title TEXT, abstract TEXT, year TEXT, journal TEXT, first_author TEXT,
  mesh TEXT, pub_types TEXT, service TEXT, added TEXT);
CREATE TABLE IF NOT EXISTS found (topic TEXT, round INTEGER, key TEXT, how TEXT,
  PRIMARY KEY (topic, key, how));
CREATE TABLE IF NOT EXISTS queries (topic TEXT, round INTEGER, service TEXT, query TEXT,
  hits INTEGER, date TEXT);
CREATE TABLE IF NOT EXISTS screening (topic TEXT, key TEXT, decision TEXT, rule TEXT,
  flags TEXT, summary TEXT, design TEXT, answers TEXT, round INTEGER, date TEXT,
  approved TEXT DEFAULT '', rejected TEXT DEFAULT '', PRIMARY KEY (topic, key));
CREATE TABLE IF NOT EXISTS topic_log (topic TEXT, round INTEGER, what TEXT, detail TEXT,
  outcome TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS topic_inputs (topic TEXT PRIMARY KEY, words TEXT NOT NULL,
  question TEXT NOT NULL, terms TEXT NOT NULL, rounds INTEGER NOT NULL, started TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_state (topic TEXT PRIMARY KEY, budget REAL NOT NULL,
  since TEXT NOT NULL, next_round INTEGER NOT NULL DEFAULT 0, proposal TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS search_rounds (topic TEXT NOT NULL, round INTEGER NOT NULL,
  started TEXT NOT NULL, ended TEXT NOT NULL DEFAULT '', relevant INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (topic, round));
"""
LEADS = {"review", "systematic review", "meta-analysis", "practice guideline", "guideline",
         "consensus development conference", "review-article"}
ASIDE = {"comment", "letter", "editorial", "published erratum", "erratum", "retraction notice",
         "retraction of publication", "news", "letter to the editor"}
PUBMED_MAX = 9999
# not a paper in a journal, as PubMed, Europe PMC and OpenAlex name them: preprints, theses,
# books and chapters, congress abstract books, meeting calendars ("other": textbook chapters)
NOT_JOURNAL = {"preprint", "dissertation", "thesis", "book", "book-chapter", "chapter-article",
               "book-review", "congress", "conference proceedings", "conference-paper",
               "conference-abstract", "meeting-report", "meeting report", "abstract", "calendar",
               "overall", "other", "news", "paratext", "dataset", "report", "reference-entry"}
SAME_TITLE_WORDS = 6     # a shorter title ("Immune-mediated hemolytic anemia") is too generic to match on


def connect(path: Path = DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    add_rejected(db)
    return db


def add_rejected(db: sqlite3.Connection):
    """The vet's review stores two dates per record: approved, or rejected. Databases made
    before 2026-11-03 have only the first; the column is added (empty) when missing."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(screening)")]
    if cols and "rejected" not in cols:
        with db:
            db.execute("ALTER TABLE screening ADD COLUMN rejected TEXT DEFAULT ''")


# ---- queries ---------------------------------------------------------------------------

def blocks(terms: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in terms:
        out.setdefault(t["block"], []).append(t)
    return out


def pubmed_term(t: dict) -> str:
    if t["kind"] == "mesh":
        return f'"{t["term"]}"[Mesh]'
    w = t["term"]
    return f'"{w}"[tiab]' if re.search(r"[\s\-/]", w) else f"{w}[tiab]"


def europepmc_term(t: dict) -> str:
    w = t["term"]
    w = f'"{w}"' if re.search(r"[\s\-/]", w) else w
    return f"TITLE:{w} OR ABSTRACT:{w}"


def first_year() -> int:
    """The first publication year searched and screened; 0 means no limit."""
    return int(settings()["pipeline"].get("published_from") or 0)


def dated(q: str, service: str = "pubmed") -> str:
    """The query limited to papers published from first_year() on."""
    y = first_year()
    if not (y and q):
        return q
    return f"({q}) AND {y}:3000[dp]" if service == "pubmed" else f"({q}) AND PUB_YEAR:[{y} TO 3000]"


def query(terms: list[dict], service: str = "pubmed", outside_pubmed: bool = True) -> str:
    """OR inside a block, AND between blocks, from first_year() on. Europe PMC uses text words
    only, and by default only reaches what PubMed doesn't have."""
    return dated(_query(terms, service, outside_pubmed), service)


def _query(terms: list[dict], service: str = "pubmed", outside_pubmed: bool = True) -> str:
    one = pubmed_term if service == "pubmed" else europepmc_term
    parts = []
    for ts in blocks(terms).values():
        ts = [t for t in ts if service == "pubmed" or t["kind"] == "text"]
        if ts:
            parts.append("(" + " OR ".join(one(t) for t in ts) + ")")
    q = " AND ".join(parts)
    return q + " NOT SRC:MED" if service == "europepmc" and q and outside_pubmed else q


def term_only(terms: list[dict], t: dict) -> str:
    """PubMed: what this term finds that the rest of its block doesn't."""
    others = [o for o in terms if o["block"] != t["block"]]
    rest = [o for o in terms if o["block"] == t["block"] and o is not t]
    q = f"({pubmed_term(t)})"
    if others:
        q += " AND " + _query(others)
    if rest:
        q += " NOT (" + " OR ".join(pubmed_term(o) for o in rest) + ")"
    return dated(q)


def bad_term_check(t: dict) -> str:
    """Why a term can't be used ('' when it can)."""
    w = t["term"].strip()
    if not w:
        return "empty term"
    if "*" in w and (" " in w or not w.endswith("*") or w.count("*") > 1
                     or len(w.split("*")[0]) < 4):
        return "* only at the end of a single word, after at least four letters"
    if '"' in w or "[" in w:
        return "write the bare term; code adds quotes and fields"
    return ""


def pubmed_ids(http: c.Http, q: str, problems: list[str]) -> tuple[int, list[str]]:
    """Search PubMed; a term PubMed ignores is reported, never silently dropped."""
    try:
        return c.pubmed_search(http, q, limit=PUBMED_MAX)
    except c.QueryProblem as e:
        problems.append(str(e))
        return 0, []


# ---- records ---------------------------------------------------------------------------

def record_key(r: c.Record) -> str:
    if r.pmid:
        return r.pmid
    if r.doi:
        return "doi:" + r.doi.lower()
    return "title:" + re.sub(r"[^a-z0-9]+", " ", r.title.lower()).strip()[:80]


def store_records(db: sqlite3.Connection, recs: list[c.Record]) -> list[str]:
    """Add records not yet stored (a DOI already stored under its PMID is the same record);
    returns every record's key."""
    keys = []
    by_doi = {row["doi"].lower(): row["key"] for row in db.execute(
        "SELECT key, doi FROM records WHERE doi != ''")}
    for r in recs:
        key = record_key(r)
        if not r.pmid and r.doi and r.doi.lower() in by_doi:
            key = by_doi[r.doi.lower()]
        d = asdict(r)
        db.execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (key, r.pmid, r.doi, r.pmcid, r.openalex_id, r.title, r.abstract, r.year,
                    r.journal, r.first_author, r.mesh, r.pub_types, r.service,
                    date.today().isoformat()))
        if r.doi:
            by_doi.setdefault(r.doi.lower(), key)
        if d["mesh"] or d["abstract"]:      # a richer copy (PubMed's) fills what was missing
            db.execute("UPDATE records SET mesh = CASE WHEN mesh = '' THEN ? ELSE mesh END, "
                       "abstract = CASE WHEN abstract = '' THEN ? ELSE abstract END "
                       "WHERE key = ?", (r.mesh, r.abstract, key))
        keys.append(key)
    db.commit()
    return keys


def pubmed_fetch(http: c.Http, db: sqlite3.Connection, pmids: list[str]) -> list[str]:
    """PubMed records for these PMIDs, stored; only those not stored yet are downloaded."""
    have = {r[0] for r in db.execute("SELECT key FROM records WHERE mesh != '' OR service = 'pubmed'")}
    new = [p for p in dict.fromkeys(pmids) if p not in have]
    if new:
        store_records(db, c.pubmed_records(http, new))
    return list(dict.fromkeys(pmids))


def mark_found(db: sqlite3.Connection, topic: str, rnd: int, keys: list[str], how: str):
    db.executemany("INSERT OR IGNORE INTO found VALUES (?,?,?,?)",
                   [(topic, rnd, k, how) for k in keys])
    db.commit()


def log_query(db: sqlite3.Connection, topic: str, rnd: int, service: str, q: str, hits: int):
    db.execute("INSERT INTO queries VALUES (?,?,?,?,?,?)",
               (topic, rnd, service, q, hits, date.today().isoformat()))
    db.commit()
    c.log_search(service, q, hits, hits)


def kind_of(pub_types: str) -> str:
    """lead (reviews, guidelines), aside (letters, comments, errata) or screen."""
    types = _types(pub_types)
    if types & ASIDE:
        return "aside"
    if types & LEADS:
        return "lead"
    return "screen"


# ---- one search ------------------------------------------------------------------------

def run_search(topic_name: str, topic: dict, db: sqlite3.Connection, http: c.Http,
               rnd: int = 1) -> dict:
    """Step 2: the topic's search in PubMed and Europe PMC; every hit stored and marked."""
    terms, problems = topic["terms"], []
    q = query(terms)
    n, pmids = pubmed_ids(http, q, problems)
    log_query(db, topic_name, rnd, "pubmed", q, n)
    mark_found(db, topic_name, rnd, pubmed_fetch(http, db, pmids), "pubmed")
    eq = query(terms, "europepmc")
    en, erecs = c.europepmc_search(http, eq, limit=1000) if eq else (0, [])
    log_query(db, topic_name, rnd, "europepmc", eq, en)
    mark_found(db, topic_name, rnd, store_records(db, erecs), "europepmc")
    return {"pubmed": n, "europepmc": en, "capped": n > PUBMED_MAX or en > 1000,
            "problems": problems, "pubmed_ids": set(pmids)}


def known_check(topic: dict, found: set[str]) -> dict[str, bool]:
    """Each known paper (a PMID or doi:…) and whether the search finds it."""
    return {k: k in found for k in topic.get("known", [])}


def _year(r) -> int:
    try:
        y = str(r["year"] or "")[:4]
    except (KeyError, IndexError):      # a record not stored
        return 0
    return int(y) if y.isdigit() else 0


def too_old(r) -> bool:
    """Published before first_year(). A record with no year is screened."""
    return 0 < _year(r) < first_year()


def _plain(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def same_paper(db: sqlite3.Connection, topic_name: str) -> dict[str, str]:
    """Records found for this topic that are a second copy of another: key -> the copy kept.
    Two records are one paper when they share a PMCID, or carry the same title (at least
    SAME_TITLE_WORDS words) in the same journal, at most a year apart; PubMed and Europe PMC
    often hold one paper under two keys when only one copy has the DOI. The copy kept is the one
    already screened, then one with an abstract, then one with a PMID, then the longer abstract."""
    rows = db.execute(
        "SELECT r.*, s.key IS NOT NULL AS screened FROM records r LEFT JOIN screening s "
        "ON s.topic = ? AND s.key = r.key WHERE r.key IN (SELECT key FROM found WHERE topic = ?)",
        (topic_name, topic_name)).fetchall()
    rows.sort(key=lambda r: (not r["screened"], not (r["abstract"] or "").strip(), not r["pmid"],
                             -len(r["abstract"] or ""), r["key"]))
    by_pmc, by_title, out = {}, {}, {}
    for r in rows:
        title, y = (_plain(r["title"]), _plain(r["journal"])), _year(r)
        twin = by_pmc.get(r["pmcid"]) if r["pmcid"] else None
        long = len(title[0].split()) >= SAME_TITLE_WORDS
        if twin is None and long and y:
            twin = next((k for ky, k in by_title.get(title, []) if abs(ky - y) <= 1), None)
        if twin is not None:
            out[r["key"]] = twin
            continue
        if r["pmcid"]:
            by_pmc[r["pmcid"]] = r["key"]
        if long and y:
            by_title.setdefault(title, []).append((y, r["key"]))
    return out


def _named(key: str) -> str:
    return f"PMID {key}" if key.isdigit() else key


def _types(pub_types: str) -> set[str]:
    return {t.strip().lower() for t in (pub_types or "").split("|") if t.strip()}


def aside_why(r, twins: dict[str, str]) -> tuple[str, str]:
    """Why a record is set aside unscreened, as (rule, the reason in words); ("", "") when it
    is screened. Too little to judge: no year, no abstract, or not a paper in a journal."""
    if kind_of(r["pub_types"]) == "aside":
        return "letter", ""
    if r["key"] in twins:
        return "same_paper", f"The same paper as {_named(twins[r['key']])}, screened once."
    if not _year(r):
        return "no_year", "No publication year given: too little to judge it by."
    if too_old(r):
        return "too_old", f"Published {_year(r)}; the search starts in {first_year()}."
    if not (r["abstract"] or "").strip():
        return "no_abstract", "No abstract: too little to judge it by."
    kind = sorted(_types(r["pub_types"]) & NOT_JOURNAL)
    if kind:
        return "not_journal", f"Not a paper in a journal ({kind[0]})."
    if not (r["journal"] or "").strip():
        return "not_journal", "No journal named."
    return "", ""


def to_screen(db: sqlite3.Connection, topic_name: str) -> list[dict]:
    """Records found for this topic and not screened yet; the rest are set aside unscreened,
    each with its reason (`aside_why`)."""
    rows = db.execute(
        "SELECT r.* FROM records r WHERE r.key IN (SELECT key FROM found WHERE topic = ?) "
        "AND r.key NOT IN (SELECT key FROM screening WHERE topic = ?) ORDER BY r.key",
        (topic_name, topic_name)).fetchall()
    twins = same_paper(db, topic_name) if rows else {}
    out = []
    for r in rows:
        rule, why = aside_why(r, twins)
        if rule:
            save_screening(db, topic_name, 0, [{"key": r["key"], "decision": "aside",
                                                "rule": "" if rule == "letter" else rule,
                                                "flags": [], "answers": {"summary": why} if why else {}}])
            continue
        out.append({"key": r["key"], "year": r["year"], "title": r["title"],
                    "abstract": r["abstract"], "subject_terms": r["mesh"],
                    "publication_types": r["pub_types"]})
    return out


def save_screening(db: sqlite3.Connection, topic_name: str, rnd: int, results: list[dict]):
    """Store screening results. A record screened again keeps its first round and the vet's
    decision: only the model's answers change."""
    rows = []
    for r in results:
        a = r.get("answers") or {}
        rows.append((topic_name, r["key"], r["decision"], r["rule"], "|".join(r["flags"]),
                     a.get("summary", ""), a.get("design", ""), json.dumps(a, ensure_ascii=False),
                     rnd, date.today().isoformat()))
    db.executemany("INSERT INTO screening (topic, key, decision, rule, flags, summary, design, "
                   "answers, round, date) VALUES (?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT (topic, key) DO UPDATE SET decision = excluded.decision, "
                   "rule = excluded.rule, flags = excluded.flags, summary = excluded.summary, "
                   "design = excluded.design, answers = excluded.answers", rows)
    db.commit()


def species_match(rec: dict, terms: list[dict]) -> bool:
    """Does a record name the topic's species (a species-block word or heading)? Used to
    choose which follow-up records are worth screening."""
    words = [t["term"].lower().rstrip("*") for t in blocks(terms).get("species", [])]
    text = " ".join([rec.get("title", ""), rec.get("abstract", ""), rec.get("mesh", "")]).lower()
    return any(re.search(r"\b" + re.escape(w), text) for w in words if w)
