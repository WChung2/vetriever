"""Planner, search, review and the vet's list against a fake literature and a fake model
(offline, free)."""
import json
import sqlite3
import tomllib

import pytest

from tests.literature import Literature, rec
from vet import approve, plan, review
from vet import connectors as c
from vet import search as s
from vet.llm import LLM, FakeGemini
from vet.settings import settings

DOGS = {"block": "species", "kind": "mesh", "term": "Dogs", "source": "vet", "evidence": "dogs"}
IMHA_MESH = {"block": "condition", "kind": "mesh", "term": "Anemia, Hemolytic, Autoimmune",
             "source": "vet", "evidence": "IMHA"}
IMHA_TEXT = {"block": "condition", "kind": "text", "term": "immune-mediated hemolytic anemia",
             "source": "vet", "evidence": "IMHA"}
TOPIC = {"question": "Treating IMHA in dogs.", "known": ["3"],
         "screen": {"species": "dogs", "condition": "IMHA", "also_called": ["AIHA"],
                    "related": ["Evans syndrome"], "treatments": ["azathioprine"]},
         "terms": [DOGS, IMHA_MESH, IMHA_TEXT], "targets": [{"item": "azathioprine", "source": "vet"}]}


def world():
    return Literature([
        rec("1", "Azathioprine in dogs with immune-mediated hemolytic anemia",
            "Dogs|Anemia, Hemolytic, Autoimmune", abstract="Dogs with autoimmune hemolytic anemia."),
        rec("2", "A review of immune-mediated hemolytic anemia in dogs", "Dogs", types="Review"),
        rec("3", "Mycophenolate for canine autoimmune haemolytic anaemia", "Dogs"),
        rec("4", "Dogs with autoimmune haemolytic anaemia: a cohort", "Dogs"),
        rec("5", "Letter: immune-mediated hemolytic anemia in dogs", "Dogs", types="Letter"),
        rec("6", "Anaemia of kidney disease in dogs", "Dogs"),
        rec("7", "Anaemia after blood loss in dogs", "Dogs"),
        rec("8", "Anaemia in dogs with tick fever", "Dogs"),
        rec("9", "Autoimmune haemolytic anaemia in cats", "Cats"),
    ], mesh={"Dogs": ["Dog", "Canis familiaris"],
             "Anemia, Hemolytic, Autoimmune": ["Autoimmune Hemolytic Anemia", "Anemia, Autoimmune Hemolytic"]},
       references={"2": ["1", "3", "9"]})


@pytest.fixture
def lit(monkeypatch):
    return world().install(monkeypatch)


@pytest.fixture
def pipeline_settings(monkeypatch):
    p = settings()["pipeline"]
    for k, v in {"term_max_new": 2, "term_min_kept": 0.5, "review_rounds": 3, "round_max_new": 50,
                 "screen_budget": 100, "plan_rounds": 2, "plan_papers": 3}.items():
        monkeypatch.setitem(p, k, v)


# ---- search ----------------------------------------------------------------------------

def test_query_is_or_inside_blocks_and_between_them():
    q = s.query([DOGS, IMHA_MESH, IMHA_TEXT])
    assert q == '("Dogs"[Mesh]) AND ("Anemia, Hemolytic, Autoimmune"[Mesh] OR ' \
                '"immune-mediated hemolytic anemia"[tiab])'
    e = s.query([DOGS, IMHA_MESH, IMHA_TEXT], "europepmc")
    assert "Mesh" not in e and e.endswith("NOT SRC:MED") and "TITLE:" in e


def test_term_only_finds_what_the_rest_of_its_block_misses(lit):
    _, ids = lit.pubmed_search(None, s.term_only([DOGS, IMHA_MESH, IMHA_TEXT], IMHA_TEXT))
    assert ids == ["2", "5"]           # 1 also has the heading


@pytest.mark.parametrize("term, ok", [("ana*", False), ("anaem*", True), ("anaem*ia", False),
                                      ("immune mediat*", False), ('"IMHA"', False), ("IMHA", True)])
def test_unusable_terms_are_refused(term, ok):
    assert (s.bad_term_check({"term": term}) == "") == ok


@pytest.mark.parametrize("types, kind", [("Journal Article|Review", "lead"), ("Letter", "aside"),
                                         ("Case Reports", "screen"), ("Comment|Review", "aside")])
def test_sort_by_type(types, kind):
    assert s.kind_of(types) == kind


def test_a_doi_already_stored_under_its_pmid_is_the_same_record(tmp_path):
    db = s.connect(tmp_path / "v.db")
    from vet.connectors import Record
    s.store_records(db, [Record("pubmed", pmid="1", doi="10.1/X", title="t")])
    assert s.store_records(db, [Record("europepmc", doi="10.1/x", title="t")]) == ["1"]


def test_letters_are_set_aside_not_screened(tmp_path, lit):
    db = s.connect(tmp_path / "v.db")
    s.run_search("imha", TOPIC, db, None)
    todo = [r["key"] for r in s.to_screen(db, "imha")]
    assert todo == ["1", "2"] and db.execute(
        "SELECT decision FROM screening WHERE key = '5'").fetchone()[0] == "aside"


# ---- the Planner -----------------------------------------------------------------------

def draft(**over):
    d = {"reasoning": "r", "restatement": "Treating IMHA in dogs.", "species": "dogs",
         "condition": "IMHA", "questions": [{"question": "Include Evans syndrome?", "options": ["yes", "no"]}],
         "blocks": [{"block": "species", "terms": [
             {"kind": "mesh", "term": "dogs", "source": "vet", "evidence": "in dogs"}]},
             {"block": "condition", "terms": [
                 {"kind": "mesh", "term": "Anemia, Hemolytic, Autoimmune", "source": "vet", "evidence": "IMHA"},
                 {"kind": "mesh", "term": "Immune Anaemia", "source": "suggestion", "evidence": ""},
                 {"kind": "text", "term": "IMHA", "source": "vet", "evidence": "immune mediated"},
                 {"kind": "text", "term": "Evans syndrome", "source": "suggestion", "evidence": ""}]}],
         "related": [{"item": "Evans syndrome", "source": "suggestion", "evidence": ""}],
         "treatments": [{"item": "azathioprine", "source": "vet", "evidence": "azathioprine"}],
         "targets": [{"item": "survival", "source": "vet", "evidence": "survival"}]}
    return d | over


WORDS = "IMHA in dogs: azathioprine or not, survival"


def test_planner_checks_sources_and_mesh(lit):
    out = plan.check(draft(), WORDS, {}, "", None)
    terms = {t["term"]: t for t in out["terms"]}
    assert "Dogs" in terms                                    # the heading's exact name
    assert "Immune Anaemia" not in terms and any("Immune Anaemia" in p for p in out["problems"])
    assert terms["IMHA"]["source"] == "suggestion"            # "immune mediated" isn't the vet's
    assert terms["Autoimmune Hemolytic Anemia"]["source"] == "mesh"   # natural entry term added
    assert "Anemia, Autoimmune Hemolytic" not in terms                # index form left out
    assert "Canis familiaris" not in terms                    # a synonym no record uses
    assert out["hits"] == 1 and terms["Dogs"]["hits"] == 1


def test_a_term_pubmed_cannot_find_is_dropped_not_fatal(lit, monkeypatch):
    real = lit.pubmed_search

    def picky(http, q, limit=100, sort=""):
        if "Canis familiaris" in q:
            raise c.QueryProblem("PubMed ignored ['\"Canis familiaris\"[tiab]']")
        return real(http, q, limit, sort)
    monkeypatch.setattr(c, "pubmed_search", picky)
    out = plan.check(draft(), WORDS, {}, "", None)
    assert "Canis familiaris" not in {t["term"] for t in out["terms"]}
    assert out["hits"] == 1 and {t["term"]: t["hits"] for t in out["terms"]}["Dogs"] == 1


def test_a_paper_source_must_be_a_marked_paper_containing_the_term():
    marked = {"3": {"title": "canine autoimmune haemolytic anaemia", "abstract": "", "mesh": ""}}
    item = {"source": "paper", "evidence": "3"}
    assert plan.check_source(item, "autoimmune haemolytic anaemia", "", marked, "") == ""
    assert plan.check_source(item, "Evans syndrome", "", marked, "") == "not in that paper"
    assert plan.check_source(item | {"evidence": "4"}, "x", "", marked, "")


def test_planner_rounds_then_start_writes_the_topic_file(tmp_path, monkeypatch, lit, pipeline_settings):
    monkeypatch.setattr(plan, "CORPUS", tmp_path)
    (tmp_path / "topics").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "plan.md").write_text("system")
    (tmp_path / "topics" / "imha.toml").write_text(
        'question = "old"\n\n# Extra fields\n[[fields]]\nfield = "entry_pcv"\nkind = "T"\n')
    monkeypatch.setattr(plan, "ROOT", tmp_path)
    seen = []

    def respond(step, prompt, schema):
        seen.append(prompt)
        return draft(questions=[]) if "last round" in prompt else draft()
    llm = LLM(FakeGemini(respond), corpus=tmp_path)
    state = plan.run_round("imha", llm, None, words=WORDS)
    assert len(state["rounds"][0]["papers"]) == 1             # the search finds one paper
    answers = tmp_path / "plan" / "imha" / "answers1.toml"
    form = answers.read_text().replace('answer = ""', 'answer = "yes"').replace(
        'relevant = ""', 'relevant = "yes"').replace("known = []", 'known = ["3"]')
    answers.write_text(form)
    plan.run_round("imha", llm, None, answers_file=answers)
    assert "last round" in seen[1] and "Azathioprine in dogs" in seen[1]   # the marked paper, whole
    assert '"3": false' in seen[1]                             # the known paper the search misses
    with pytest.raises(ValueError, match="rounds done"):
        plan.run_round("imha", llm, None, answers_file=answers)
    t = tomllib.loads(plan.start("imha").read_text())
    assert t["screen"]["condition"] == "IMHA" and t["known"] == ["3"]
    assert "Autoimmune Hemolytic Anemia" in t["screen"]["also_called"]
    assert "Evans syndrome" not in t["screen"]["also_called"]   # related, not another name
    assert {x["term"] for x in t["terms"]} >= {"Dogs", "Anemia, Hemolytic, Autoimmune"}
    assert t["fields"][0]["field"] == "entry_pcv"              # the topic's own fields kept
    # the vet's words, kept in the database
    words, terms = sqlite3.connect(tmp_path / "vetriever.db").execute(
        "SELECT words, terms FROM topic_inputs WHERE topic = 'imha'").fetchone()
    assert words == WORDS and "Anemia, Hemolytic, Autoimmune" in terms
    assert list((tmp_path / "plan" / "imha").glob("previous_*.toml"))   # nothing lost


# ---- search ⇄ review -------------------------------------------------------------------

def screen_answer(prompt):
    keys = [json.loads(line)["key"] for line in prompt.splitlines()[1:]]
    out = []
    for k in keys:
        cats = k == "9"
        out.append({"key": k, "reasoning": "", "summary": f"summary {k}", "design": "retrospective",
                    "kind": "review" if k == "2" else "study",
                    "condition": {"answer": "no" if k in "678" else "yes",
                                  "quote": "Anaemia" if k in "678" else ""},
                    "animals": {"answer": "other_species" if cats else "patients",
                                "quote": "cats" if cats else ""}})
    return {"records": out}


def test_review_loop_follows_adds_brakes_and_stops(tmp_path, monkeypatch, lit, pipeline_settings):
    monkeypatch.setattr(review, "load_topic", lambda name: TOPIC)
    calls = []

    def respond(step, prompt, schema):
        if step == "screen":
            return screen_answer(prompt)
        calls.append(prompt)
        none = {"reasoning": "", "term_changes": [], "follow": [], "extra_queries": [], "nothing_left": True}
        if len(calls) == 1:          # round 2: one good term, one too broad, follow a review
            return none | {"term_changes": [
                {"block": "condition", "action": "add", "kind": "text",
                 "term": "autoimmune haemolytic anaemia", "evidence": "missed 3"},
                {"block": "condition", "action": "add", "kind": "text", "term": "anaemia", "evidence": "x"}],
                "follow": [{"key": "2", "how": "references"}]}
        return none                  # the revision, and round 3: nothing more
    db = s.connect(tmp_path / "v.db")
    out = review.run("imha", LLM(FakeGemini(respond), corpus=tmp_path), db=db, say=lambda m: None)
    dec = review.decisions(db, "imha")
    assert dec == {"1": "keep", "2": "lead", "3": "keep", "4": "keep", "5": "aside"}
    assert "9" not in dec            # a cat paper from the review's references isn't screened
    active = {t["term"] for t in review.current_terms(db, "imha", TOPIC)}
    assert "autoimmune haemolytic anaemia" in active and "anaemia" not in active
    assert "too many records" in calls[1] and '"new_records": 3' in calls[1]   # 6, 7, 8: 3 and 4 came with the term added first
    assert '"3": false' in calls[0]  # the known paper the first search misses is reported
    assert [r["new_kept"] for r in out["rounds"]] == [3, 1, 0]   # the known paper 3 from round 1 and len(calls) == 3


def test_a_new_term_whose_records_are_dropped_is_taken_out(tmp_path, monkeypatch, lit, pipeline_settings):
    monkeypatch.setitem(settings()["pipeline"], "term_max_new", 5)
    monkeypatch.setattr(review, "load_topic", lambda name: TOPIC)
    calls = []

    def respond(step, prompt, schema):
        if step == "screen":
            return screen_answer(prompt)
        calls.append(prompt)
        add = [{"block": "condition", "action": "add", "kind": "text", "term": "anaemia", "evidence": "x"}]
        return {"reasoning": "", "term_changes": add if len(calls) == 1 else [], "follow": [],
                "extra_queries": [], "nothing_left": False}
    db = s.connect(tmp_path / "v.db")
    review.run("imha", LLM(FakeGemini(respond), corpus=tmp_path), db=db, say=lambda m: None)
    gone = db.execute("SELECT why FROM terms WHERE term = 'anaemia'").fetchone()[0]
    assert gone == "only 2 of its 5 records kept"


def test_a_term_is_not_removed_when_a_known_paper_needs_it(tmp_path, monkeypatch, lit, pipeline_settings):
    db = s.connect(tmp_path / "v.db")
    topic = TOPIC | {"known": ["2"]}
    terms = review.current_terms(db, "imha", topic)
    review._remove("imha", topic, terms, IMHA_TEXT, db, None, 2)
    why = db.execute("SELECT outcome FROM topic_log").fetchone()[0]
    assert why == "refused: the search would lose known papers ['2']"
    assert len(review.current_terms(db, "imha", topic)) == 3


# ---- the vet's list --------------------------------------------------------------------

def test_list_approval_and_hand_download_ranking(tmp_path, lit):
    db = s.connect(tmp_path / "v.db")
    s.run_search("imha", TOPIC, db, None)
    s.to_screen(db, "imha")                     # sets the letter aside
    s.save_screening(db, "imha", 1, [
        {"key": "1", "decision": "keep", "rule": "", "flags": [],
         "answers": {"summary": "12 dogs given azathioprine", "design": "retrospective"}},
        {"key": "2", "decision": "keep", "rule": "", "flags": [],
         "answers": {"summary": "40 dogs randomised", "design": "randomised_trial"}}])
    kept, details = approve.write_list("imha", db, tmp_path)
    assert "12 dogs given azathioprine" in kept.read_text() and "Letter" in details.read_text()
    assert approve.approve("imha", db, leave_out=["1"]) == 1
    db.execute("CREATE TABLE fetches (pmid TEXT, doi TEXT, route TEXT)")
    assert [r["key"] for r in approve.without_text("imha", db)] == ["2"]
    approve.approve("imha", db)
    assert [r["key"] for r in approve.without_text("imha", db)] == ["2", "1"]   # trial first
    db.execute("INSERT INTO fetches VALUES ('2', '', 'pmc')")
    assert [r["key"] for r in approve.without_text("imha", db)] == ["1"]


def test_unbacked_keeps_dont_keep_the_search_going(tmp_path, monkeypatch, lit, pipeline_settings):
    monkeypatch.setitem(settings()["pipeline"], "term_max_new", 5)
    monkeypatch.setattr(review, "load_topic", lambda name: TOPIC)
    calls = []

    def respond(step, prompt, schema):
        if step == "screen":
            out = screen_answer(prompt)
            for r in out["records"]:         # kept, but none of these is relevant:
                if r["key"] in "48":             # "no" with nothing quoted
                    r["condition"] = {"answer": "no", "quote": ""}
                if r["key"] == "6":              # healthy animals given a treatment
                    r["condition"] = {"answer": "no", "quote": "Anaemia"}
                    r["animals"] = {"answer": "healthy_treated", "quote": ""}
                if r["key"] == "7":              # other species, with nothing quoted
                    r["condition"] = {"answer": "yes", "quote": ""}
                    r["animals"] = {"answer": "other_species", "quote": ""}
            return out
        calls.append(prompt)
        return {"reasoning": "", "follow": [], "nothing_left": False, "term_changes": [
            {"block": "condition", "action": "add", "kind": "text", "term": "anaemia", "evidence": "x"}],
            "extra_queries": [{"query": 'BODY:"IMHA"', "target": "x"}]}
    db = s.connect(tmp_path / "v.db")
    out = review.run("imha", LLM(FakeGemini(respond), corpus=tmp_path), db=db, say=lambda m: None)
    assert [r["new_kept"] for r in out["rounds"]] == [3, 0] and len(calls) == 1
    log = {r[0]: r[1] for r in db.execute("SELECT what, outcome FROM topic_log")}
    assert log["query"] == "refused: full-text search is not used"
    assert log["yield"] == "1 relevant of 5 screened"        # 3 (the known paper) only
    assert "only 1 of its 5 records kept" in db.execute(
        "SELECT why FROM terms WHERE term = 'anaemia'").fetchone()[0]


def test_screening_is_saved_as_it_goes(tmp_path, monkeypatch, lit):
    monkeypatch.setattr(review, "SAVE_EVERY", 1)
    db = s.connect(tmp_path / "v.db")
    s.run_search("imha", TOPIC, db, None)

    def respond(step, prompt, schema):
        if '"key": "2"' in prompt:
            raise RuntimeError("the service went away")
        return screen_answer(prompt)
    with pytest.raises(RuntimeError):
        review.screen_new("imha", TOPIC, db, LLM(FakeGemini(respond), corpus=tmp_path), 1, print)
    assert review.decisions(db, "imha") == {"1": "keep", "5": "aside"}   # 1 was saved before
