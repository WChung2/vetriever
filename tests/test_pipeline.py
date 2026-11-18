"""Planner, search, review and the vet's list against a fake literature and a fake model
(offline, free)."""
import json
import sqlite3
import tomllib

import pytest

from tests.literature import Literature, rec
from vet import approve, budget, plan, review, views
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
    for k, v in {"term_max_new": 2, "term_min_kept": 0.5, "round_max_new": 50,
                 "screen_budget": 100, "plan_rounds": 2, "plan_papers": 3}.items():
        monkeypatch.setitem(p, k, v)


# ---- search ----------------------------------------------------------------------------

def test_query_is_or_inside_blocks_and_between_them():
    q = s.query([DOGS, IMHA_MESH, IMHA_TEXT])
    assert q == '(("Dogs"[Mesh]) AND ("Anemia, Hemolytic, Autoimmune"[Mesh] OR ' \
                '"immune-mediated hemolytic anemia"[tiab])) AND 2000:3000[dp]'
    e = s.query([DOGS, IMHA_MESH, IMHA_TEXT], "europepmc")
    assert "Mesh" not in e and "NOT SRC:MED" in e and "TITLE:" in e


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


# ---- the first year, and a paper found twice ---------------------------------------------

def test_every_query_asks_from_the_first_year_on():
    assert s.query([DOGS, IMHA_TEXT], "europepmc").endswith(") AND PUB_YEAR:[2000 TO 3000]")
    only = s.term_only([IMHA_MESH, IMHA_TEXT], IMHA_TEXT)     # one block: no others carry it
    assert only.endswith(" AND 2000:3000[dp]")


def test_no_first_year_means_no_limit(monkeypatch):
    monkeypatch.setitem(settings()["pipeline"], "published_from", 0)
    assert s.query([DOGS, IMHA_MESH]) == '("Dogs"[Mesh]) AND ("Anemia, Hemolytic, Autoimmune"[Mesh])'
    assert s.query([DOGS, IMHA_TEXT], "europepmc").endswith("NOT SRC:MED")


def old_world(monkeypatch):
    lit = world()
    lit.records["10"] = rec("10", "Azathioprine in dogs with immune-mediated hemolytic anemia, "
                            "1990-1997", "Dogs|Anemia, Hemolytic, Autoimmune", year="1998")
    return lit.install(monkeypatch)


def test_the_planner_counts_only_papers_from_the_first_year_on(monkeypatch):
    old_world(monkeypatch)
    terms = [dict(t) for t in (DOGS, IMHA_MESH)]
    assert plan.count_hits(terms, None, [])["hits"] == 1             # 1, not 10 (1998)
    assert [t["hits"] for t in terms] == [1, 1]


def test_an_older_paper_a_follow_up_finds_is_not_linked(tmp_path, monkeypatch):
    lit = old_world(monkeypatch)
    lit.records["13"] = rec("13", "Dogs with IMHA given a fictional drug", year="")
    db = s.connect(tmp_path / "v.db")
    n, left = review._new_records(None, db, "imha", 2, ["10", "4", "13"], "references:2", TOPIC["terms"], 10)
    assert (n, left) == (2, 0) and sorted(r[0] for r in db.execute("SELECT key FROM found")) == ["13", "4"]
    s.to_screen(db, "imha")                     # an undated one is linked, then shown set aside
    assert set_aside(db) == {"13": ("no_year", "No publication year given: too little to judge it by.")}


def test_a_query_the_model_writes_asks_from_the_first_year_on(tmp_path, monkeypatch, lit,
                                                               pipeline_settings):
    monkeypatch.setattr(review, "load_topic", lambda name: TOPIC)
    asked = []
    monkeypatch.setattr(c, "europepmc_search", lambda http, q, limit=100: asked.append(q) or (0, []))

    def respond(step, prompt, schema):
        if step == "screen":
            return screen_answer(prompt)
        return {"reasoning": "", "term_changes": [], "follow": [], "nothing_left": False,
                "extra_queries": [{"query": "TITLE:IMHA", "target": "x"}]}
    db = s.connect(tmp_path / "v.db")
    budget.set_budget(db, "imha", 2.0)
    widen_until_nothing_new("imha", LLM(FakeGemini(respond), corpus=tmp_path), db)
    assert "(TITLE:IMHA) AND PUB_YEAR:[2000 TO 3000]" in asked and "TITLE:IMHA" not in asked


def found(db, *recs):
    s.mark_found(db, "imha", 1, s.store_records(db, list(recs)), "test")


def set_aside(db) -> dict:
    return {k: (rule, why) for k, rule, why in db.execute(
        "SELECT key, rule, summary FROM screening WHERE decision = 'aside'")}


def test_older_and_undated_papers_are_set_aside_with_their_reason(tmp_path):
    db = s.connect(tmp_path / "v.db")
    found(db, rec("10", "Azathioprine in dogs with IMHA, 1990-1997", year="1999"),
          rec("11", "A dog with immune-mediated hemolytic anemia", year=""),
          rec("12", "Mycophenolate in dogs with IMHA", year="2000"))
    assert [r["key"] for r in s.to_screen(db, "imha")] == ["12"]
    assert set_aside(db) == {"10": ("too_old", "Published 1999; the search starts in 2000."),
                             "11": ("no_year", "No publication year given: too little to judge it by.")}


def paper(service="europepmc", **over):
    return c.Record(service, **({"doi": "10.1/p", "title": "Mycophenolate in dogs with IMHA",
                                 "abstract": "Dogs were treated.", "journal": "J Vet Intern Med",
                                 "year": "2019", "pub_types": "research-article"} | over))


@pytest.mark.parametrize("record, rule, why", [
    (paper(abstract=""), "no_abstract", "No abstract: too little to judge it by."),
    (paper(pub_types="Preprint"), "not_journal", "Not a paper in a journal (preprint)."),
    (paper("openalex", pub_types="dissertation"), "not_journal", "Not a paper in a journal (dissertation)."),
    (paper("openalex", pub_types="book-chapter"), "not_journal", "Not a paper in a journal (book-chapter)."),
    (paper("openalex", pub_types="other"), "not_journal", "Not a paper in a journal (other)."),  # textbooks
    (paper(pub_types="abstract|Journal Article"), "not_journal",                         # congress book
     "Not a paper in a journal (abstract)."),
    (paper(journal=""), "not_journal", "No journal named."),
])
def test_a_record_with_too_little_to_judge_is_set_aside(tmp_path, record, rule, why):
    db = s.connect(tmp_path / "v.db")
    found(db, record, paper(doi="10.1/q", title="Another study of prednisolone in dogs with IMHA"))
    assert [r["key"] for r in s.to_screen(db, "imha")] == ["doi:10.1/q"]
    assert set_aside(db) == {"doi:10.1/p": (rule, why)}


TITLE = "Prednisolone and azathioprine in dogs with immune-mediated hemolytic anemia"


def pubmed_copy(**over):
    return c.Record("pubmed", **({"pmid": "20", "title": TITLE, "journal": "J Vet Intern Med",
                                  "year": "2016", "abstract": "The longer abstract PubMed has."} | over))


def other_copy(**over):
    return c.Record("europepmc", **({"title": TITLE, "journal": "J Vet Intern Med", "year": "2016",
                                     "doi": "10.1/x", "abstract": "Short."} | over))


@pytest.mark.parametrize("first, second, same", [
    (pubmed_copy(), other_copy(), True),                    # Europe PMC's copy, with the DOI
    (pubmed_copy(), other_copy(title=TITLE.upper() + ".", journal="J. Vet. Intern. Med."), True),
    (pubmed_copy(), other_copy(year="2017"), True),         # online one year, in print the next
    (pubmed_copy(), other_copy(year="2018"), False),        # two years apart: another paper
    (pubmed_copy(), other_copy(journal="Vet Rec"), False),  # another journal
    (pubmed_copy(pmcid="PMC1"), other_copy(title="Other words, other journal", journal="X",
                                           pmcid="PMC1"), True),
    (pubmed_copy(title="Immune-mediated hemolytic anemia"),  # too generic to match on
     other_copy(title="Immune-mediated hemolytic anemia"), False),
])
def test_a_paper_found_twice_is_screened_once(tmp_path, first, second, same):
    db = s.connect(tmp_path / "v.db")
    found(db, first, second)
    todo = [r["key"] for r in s.to_screen(db, "imha")]
    if same:
        assert todo == ["20"]
        assert set_aside(db) == {"doi:10.1/x": ("same_paper", "The same paper as PMID 20, screened once.")}
    else:
        assert sorted(todo) == ["20", "doi:10.1/x"] and set_aside(db) == {}


def test_the_copy_with_an_abstract_is_the_one_kept(tmp_path):
    db = s.connect(tmp_path / "v.db")
    found(db, pubmed_copy(abstract=""), other_copy())
    assert [r["key"] for r in s.to_screen(db, "imha")] == ["doi:10.1/x"]
    assert set_aside(db) == {"20": ("same_paper", "The same paper as doi:10.1/x, screened once.")}


def test_the_copy_already_screened_is_the_one_kept(tmp_path):
    db = s.connect(tmp_path / "v.db")
    found(db, other_copy())
    s.save_screening(db, "imha", 1, [{"key": "doi:10.1/x", "decision": "keep", "rule": "",
                                      "flags": [], "answers": {}}])
    found(db, pubmed_copy())
    assert s.to_screen(db, "imha") == []
    assert set_aside(db) == {"20": ("same_paper", "The same paper as doi:10.1/x, screened once.")}


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


@pytest.mark.parametrize("given, kept", [
    ({"value": "include", "source": "vet", "evidence": "azathioprine or not"},
     {"value": "include", "source": "vet", "evidence": "azathioprine or not"}),
    ({"value": "exclude", "source": "vet", "evidence": "phase 2"},           # not in the words
     {"value": "exclude", "source": "suggestion", "evidence": "phase 2",
      "note": "its evidence is not in the vet's words"}),
    ({"value": "exclude", "source": "answer", "evidence": "only dogs with IMHA"},
     {"value": "exclude", "source": "answer", "evidence": "only dogs with IMHA"}),
    ({"value": "maybe", "source": "vet", "evidence": "x"},                   # not a listed value
     {"value": "include", "source": "suggestion", "evidence": "x", "note": "not given: kept, as before"}),
    (None, {"value": "include", "source": "suggestion", "evidence": "", "note": "not given: kept, as before"}),
])
def test_the_planners_healthy_treated_is_checked_like_any_item(given, kept):
    answers = "Include healthy dogs? No, only dogs with IMHA"
    assert plan.check_healthy(given, WORDS, {}, answers) == kept


def test_the_planners_healthy_treated_reaches_the_search_file_and_the_round(tmp_path, monkeypatch, lit):
    monkeypatch.setattr(plan, "CORPUS", tmp_path)
    monkeypatch.setattr(plan, "ROOT", tmp_path)
    (tmp_path / "topics").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "plan.md").write_text("system")
    h = {"value": "exclude", "source": "vet", "evidence": "survival"}
    plan.run_round("imha", LLM(FakeGemini(lambda *a: draft(healthy_treated=h)), corpus=tmp_path), None,
                   words=WORDS)
    md = (tmp_path / "plan" / "imha" / "round1.md").read_text()
    assert "**Healthy or research animals given a treatment:** exclude (vet: survival)" in md
    assert tomllib.loads(plan.start("imha").read_text())["screen"]["healthy_treated"] == "exclude"


@pytest.mark.parametrize("source, kept", [("vet", 1), ("suggestion", 2)])
def test_a_healthy_treated_the_vet_settled_is_not_asked_again(lit, source, kept):
    asked = [{"question": "Include Evans syndrome?", "options": ["yes", "no"]},
             {"question": "Should the database include studies of healthy or research dogs given "
                          "azathioprine?", "options": ["Yes, include them", "No, only dogs with IMHA"]}]
    h = {"value": "include", "source": source, "evidence": "azathioprine or not"}
    got = plan.check(draft(questions=asked, healthy_treated=h), WORDS, {}, "", None)["questions"]
    assert len(got) == kept and got[0]["question"] == "Include Evans syndrome?"


def test_the_planner_is_told_how_to_decide_healthy_treated():
    text = (plan.ROOT / "prompts" / "plan.md").read_text()
    assert "## Healthy or research animals" in text and "- healthy_treated: see" in text
    assert "the earliest stage decides" in text and "It never changes the search blocks" in text
    assert plan.HEALTHY_Q in " ".join(text.split()) and "Ask only when the source is suggestion" in text
    assert "healthy_treated" in plan.SCHEMA["required"]
    assert plan.SCHEMA["properties"]["healthy_treated"]["properties"]["value"]["enum"] == [
        "include", "exclude", "not_applicable"]


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

def widen_until_nothing_new(name, llm, db) -> list[dict]:
    """A vet who widens for as long as a round finds something."""
    def quiet(_):
        return None
    rounds = [review.first_round(name, llm, db=db, say=quiet)]
    while rounds[-1]["new_kept"] and budget.state(db, name)["proposal"]:
        rounds.append(review.widen(name, llm, db=db, say=quiet))
    return rounds


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


def outcomes(db, rnd) -> dict:
    return {(w, d): o for w, d, o in db.execute(
        "SELECT what, detail, outcome FROM topic_log WHERE round = ?", (rnd,))}


def test_a_step_the_round_limit_stops_is_not_run_and_can_come_back(tmp_path, monkeypatch, lit):
    """As in a real round: the first follow-up used the round's new records, so the next ones
    found nothing. They must say "not run" and stay open, not read "0 new" and count as done."""
    cap = settings()["pipeline"]
    db = s.connect(tmp_path / "v.db")
    first = json.dumps({"key": "2", "how": "references"})        # cites 1, 3 (dogs) and 9 (cats)
    later = json.dumps({"key": "4", "how": "references"})
    answer = {"term_changes": [], "follow": [json.loads(first), json.loads(later)],
              "extra_queries": [{"query": "TITLE:IMHA", "target": "x"}]}
    monkeypatch.setitem(cap, "round_max_new", 1)
    review.act("imha", TOPIC, TOPIC["terms"], answer, db, None, 2, None, "", "")
    got = outcomes(db, 2)
    assert got[("follow", first)] == ("1 new records naming the species; 1 more cut at this "
                                      "round's limit of 1 new records")
    assert got[("follow", later)] == "not run: this round's limit of 1 new records was reached"
    assert got[("query", "TITLE:IMHA")] == "not run: this round's limit of 1 new records was reached"
    assert review.ran(db, "imha") == set()                       # none of the three is done
    monkeypatch.setitem(cap, "round_max_new", 10)                # a later round runs them
    review.act("imha", TOPIC, TOPIC["terms"], answer, db, None, 3, None, "", "")
    got = outcomes(db, 3)
    assert got[("follow", first)] == "1 new records naming the species"   # 3, the one cut before
    assert got[("follow", later)] == "0 new records naming the species"
    assert sorted(r[0] for r in db.execute("SELECT key FROM found")) == ["1", "3"]
    assert ("follow", first) in review.ran(db, "imha")


def test_the_chat_counts_only_the_follow_ups_that_ran(tmp_path):
    db = s.connect(tmp_path / "v.db")
    for detail, outcome in (("a", "5 new records naming the species"),
                            ("b", "not run: this round's limit of 200 new records was reached")):
        review.log(db, "imha", 2, "follow", detail, outcome)
    db.execute("INSERT INTO search_rounds VALUES ('imha', 2, '2026-11-17T10:00', '2026-11-17T11:00', 1)")
    db.execute("INSERT INTO records (key, title) VALUES ('1', 't')")
    db.execute("INSERT INTO screening (topic, key, decision, round) VALUES ('imha', '1', 'keep', 2)")
    db.commit()
    texts = " ".join(i.get("text", "") for i in views.history(tmp_path / "v.db", tmp_path, "imha"))
    assert "1 reference list or citation list followed" in texts


def test_a_term_the_round_limit_stops_is_not_run(tmp_path, monkeypatch, lit):
    monkeypatch.setitem(settings()["pipeline"], "round_max_new", 1)
    db = s.connect(tmp_path / "v.db")
    first = {"block": "condition", "action": "add", "kind": "text",
             "term": "autoimmune haemolytic anaemia", "evidence": "x"}      # finds 3 and 4
    later = first | {"term": "mycophenolate"}
    terms = review.current_terms(db, "imha", TOPIC)              # as a round does, first
    review.act("imha", TOPIC, terms, {"term_changes": [first, later], "follow": [],
                                      "extra_queries": []}, db, None, 2, None, "", "")
    got = [r[0] for r in db.execute("SELECT outcome FROM topic_log")]
    assert got == [("added: 2 new records, 1 to screen; the rest cut at this round's limit of 1 "
                    "new records"), "not run: this round's limit of 1 new records was reached"]
    assert [t[0] for t in db.execute("SELECT term FROM terms WHERE source = 'review'")] == [
        "autoimmune haemolytic anaemia"]


def test_a_review_whose_references_were_not_followed_is_still_offered(tmp_path, lit):
    db = s.connect(tmp_path / "v.db")
    s.run_search("imha", TOPIC, db, None)
    s.save_screening(db, "imha", 1, [{"key": "2", "decision": "lead", "rule": "", "flags": [],
                                      "answers": {}}])
    refs = json.dumps({"key": "2", "how": "references"})
    review.log(db, "imha", 2, "follow", refs, "not run: this round's limit of 200 new records was reached")
    assert [ld["key"] for ld in review.counts("imha", TOPIC, TOPIC["terms"], db, None, 3)["leads"]] == ["2"]
    review.log(db, "imha", 3, "follow", refs, "2 new records naming the species")
    assert review.counts("imha", TOPIC, TOPIC["terms"], db, None, 4)["leads"] == []


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
    budget.set_budget(db, "imha", 2.0)
    out = widen_until_nothing_new("imha", LLM(FakeGemini(respond), corpus=tmp_path), db)
    dec = review.decisions(db, "imha")
    assert dec == {"1": "keep", "2": "lead", "3": "keep", "4": "keep", "5": "aside"}
    assert "9" not in dec            # a cat paper from the review's references isn't screened
    active = {t["term"] for t in review.current_terms(db, "imha", TOPIC)}
    assert "autoimmune haemolytic anaemia" in active and "anaemia" not in active
    assert "too many records" in calls[1] and '"new_records": 3' in calls[1]   # 6, 7, 8: 3 and 4 came with the term added first
    assert '"3": false' in calls[0]  # the known paper the first search misses is reported
    assert [r["new_kept"] for r in out] == [3, 1, 0]   # the known paper 3 from round 1
    assert len(calls) == 4           # a proposal after each of three rounds, and the revision


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
    budget.set_budget(db, "imha", 2.0)
    widen_until_nothing_new("imha", LLM(FakeGemini(respond), corpus=tmp_path), db)
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
    budget.set_budget(db, "imha", 2.0)
    out = widen_until_nothing_new("imha", LLM(FakeGemini(respond), corpus=tmp_path), db)
    assert [r["new_kept"] for r in out] == [3, 0] and len(calls) == 2
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
