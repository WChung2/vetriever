"""The chat and the search, end to end on made-up literature and a stand-in model, one test
per risk: the vet decides each round, the budget holds, a cut-off round loses nothing, and what
the vet types stays where it should. Free: no model, no network."""
import json
import re
import shutil
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_pipeline import TOPIC as IMHA
from tests.test_pipeline import draft, screen_answer
from tests.test_pipeline import world as literature
from tests.test_ui import SCRIPT, make_db
from vet import budget, chat, cli, jobs, plan, review, views
from vet import search as s
from vet.codebook import TOPIC_NAME
from vet.llm import LLM, BudgetExceeded
from vet.screen import screen, system_prompt
from vet.settings import ROOT, settings
from vet.ui import app as ui

WORDS = "Treating immune-mediated hemolytic anemia in dogs: survival"
PER_CALL = 0.01
IMHA_TOML = """question = "Treating IMHA in dogs."
known = ["3"]
[screen]
species = "dogs"
condition = "IMHA"
also_called = ["AIHA"]
related = ["Evans syndrome"]
treatments = ["azathioprine"]
[[terms]]
block = "species"
kind = "mesh"
term = "Dogs"
source = "vet"
evidence = "dogs"
[[terms]]
block = "condition"
kind = "mesh"
term = "Anemia, Hemolytic, Autoimmune"
source = "vet"
evidence = "IMHA"
"""
ADD_TERM = {"block": "condition", "action": "add", "kind": "text",
            "term": "autoimmune haemolytic anaemia", "evidence": "missed 3"}


class Billed:
    """A stand-in for the paid model: it is billed like the real one, in the corpus's
    spend.csv, at a fixed price per call. Each prompt is kept."""

    def __init__(self, gate: threading.Event | None = None):
        self.calls, self.gate = [], gate
        self.review = {"reasoning": "Term 3 is missed.", "term_changes": [ADD_TERM], "follow": [],
                       "extra_queries": [], "nothing_left": False}

    def generate(self, model, system, prompt, schema, thinking, max_output, step=""):
        if self.gate:
            self.gate.wait(5)
        self.calls.append({"step": step, "prompt": prompt})
        if step == "screen":
            answer = screen_answer(prompt)
        elif step == "review":
            answer = self.review
        else:
            answer = draft()
        usage = {"input": 0, "output": round(PER_CALL * 1e6 / 3.75), "thoughts": 0}
        return json.dumps(answer), usage, ""

    def steps(self, name: str) -> int:
        return sum(c["step"] == name for c in self.calls)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A whole install in a temporary folder: topics, prompts, corpus and a made-up literature."""
    root, corpus = tmp_path / "root", tmp_path / "corpus"
    (root / "topics").mkdir(parents=True)
    corpus.mkdir()
    shutil.copytree(ROOT / "prompts", root / "prompts")
    (root / "topics" / "imha.toml").write_text(IMHA_TOML, "utf-8")
    for module, attr, value in ((plan, "ROOT", root), (plan, "CORPUS", corpus),
                                (review, "ROOT", root), (views, "TOPICS", root / "topics")):
        monkeypatch.setattr(module, attr, value)
    for k, v in {"term_max_new": 5, "term_min_kept": 0.5, "round_max_new": 50, "screen_budget": 100,
                 "plan_rounds": 2, "plan_papers": 3}.items():
        monkeypatch.setitem(settings()["pipeline"], k, v)
    monkeypatch.setitem(settings()["prices"], "valid_until", "2999-12-31")   # any day the tests run
    literature().install(monkeypatch)
    model = Billed()
    db = corpus / "vetriever.db"
    s.connect(db).close()
    client = TestClient(ui.create_app(db, corpus, backend=model), base_url="http://127.0.0.1")
    return type("Rig", (), {"root": root, "corpus": corpus, "db": db, "model": model, "client": client})


def one_by_one(rig, monkeypatch):
    """A search that finds six records, screened one per call, so it can be cut part-way."""
    path = rig.root / "topics" / "imha.toml"
    path.write_text(path.read_text() + '[[terms]]\nblock = "condition"\nkind = "text"\n'
                    'term = "anaemia"\nsource = "vet"\nevidence = "IMHA"\n', "utf-8")
    monkeypatch.setattr(review, "SAVE_EVERY", 1)
    monkeypatch.setattr(review, "screen", lambda n, t, r, llm: screen(n, t, r, llm, batch=1, workers=1))
    monkeypatch.setattr(review.budget, "needed", lambda n: 0.0)    # past the check before a round


def post(rig, path, **data):
    data = {"csrf": rig.client.app.state.token, **data}
    r = rig.client.post(path, data=data, headers={"origin": "http://127.0.0.1:8765"},
                        follow_redirects=False)
    rig.client.app.state.runner.wait()
    return r


def stage(rig, name="imha") -> str:
    return views.panel(rig.db, rig.corpus, name)["stage"]


def rounds_run(rig, name="imha") -> list[int]:
    con = sqlite3.connect(rig.db)
    return [r["round"] for r in budget.rounds(con, name)]


def search_once(rig, usd="2.00"):
    r = post(rig, "/topics/imha/search", budget=usd, request="start")
    assert r.status_code == 303 and "note" not in r.headers["location"]
    assert stage(rig) == "decide"


def widen(rig, request, usd="2.00"):
    return post(rig, "/topics/imha/widen", budget=usd, round=str(views.panel(
        rig.db, rig.corpus, "imha")["next"]["round"]), request=request)


# the vet decides each round

def test_a_search_stops_after_one_round_and_shows_what_comes_next(rig):
    search_once(rig)
    assert rounds_run(rig) == [1]                                  # no second round
    assert rig.model.steps("review") == 1                          # only the proposal
    con = sqlite3.connect(rig.db)
    assert con.execute("SELECT MAX(round) FROM found").fetchone()[0] == 1
    html = rig.client.get("/topics/imha/chat").text
    for part in ("Round 1 finished", "Round 2 would", "autoimmune haemolytic anaemia”",
                 "Widen one more round", "Finish, go to Review", "Term 3 is missed."):
        assert part in html, part


def test_looking_at_the_pages_never_starts_a_round(rig):
    search_once(rig)
    before = len(rig.model.calls)
    for path in ("/topics/imha/chat", "/topics/imha/review", "/topics/imha/live", "/"):
        rig.client.get(path)
    rig.client.app.state.runner.wait()
    assert len(rig.model.calls) == before and rounds_run(rig) == [1]


def test_widening_runs_the_round_the_vet_read_once(rig):
    search_once(rig)
    r = widen(rig, "click")
    assert r.status_code == 303
    assert rounds_run(rig) == [1, 2]
    again = post(rig, "/topics/imha/widen", budget="2.00", round="3", request="click")
    assert rounds_run(rig) == [1, 2] and again.status_code == 303     # the same click: nothing
    assert "autoimmune haemolytic anaemia" in {
        t["term"] for t in review.current_terms(s.connect(rig.db), "imha", IMHA)}


def test_a_page_left_open_cant_run_a_round_nobody_read(rig):
    search_once(rig)
    r = post(rig, "/topics/imha/widen", budget="2.00", round="7", request="old-page")
    assert "note=stale" in r.headers["location"] and rounds_run(rig) == [1]


def test_nothing_new_is_advice_and_the_decision_stays_with_the_vet(rig):
    search_once(rig)
    widen(rig, "one")
    rig.model.review = {**rig.model.review, "term_changes": [],
                        "follow": [{"key": "2", "how": "similar"}]}
    widen(rig, "two")
    widen(rig, "three")                                     # a round that finds nothing new
    html = rig.client.get("/topics/imha/chat").text
    assert "another round is unlikely to help" in html
    assert "Widen one more round" in html


def test_with_nothing_left_to_try_there_is_no_round_to_run(rig):
    rig.model.review = {**rig.model.review, "term_changes": []}
    search_once(rig)
    html = rig.client.get("/topics/imha/chat").text
    assert "nothing left to try" in html and "Widen one more round" not in html
    r = post(rig, "/topics/imha/widen", budget="2.00", round="2", request="x")
    assert "note=stale" in r.headers["location"] and rounds_run(rig) == [1]


# the budget holds

def test_a_budget_that_cant_cover_the_step_is_refused(rig):
    r = post(rig, "/topics/imha/search", budget="0.01", request="r")
    assert "note=too_low" in r.headers["location"]
    assert rig.model.calls == [] and rounds_run(rig) == []
    assert "cover this step" in rig.client.get(r.headers["location"]).text


@pytest.mark.parametrize("usd", ["abc", "", "nan", "inf", "-1", "0"])
def test_a_budget_must_be_a_positive_number(rig, usd):
    r = post(rig, "/topics/imha/search", budget=usd, request="r")
    assert "note=budget_number" in r.headers["location"] and rig.model.calls == []


def test_a_budget_above_what_the_spending_cap_leaves_is_refused(rig):
    cap = settings()["spending"]["cap_usd"]
    r = post(rig, "/topics/imha/search", budget=str(cap + 1), request="r")
    assert "note=over_cap" in r.headers["location"] and rig.model.calls == []


def test_a_round_is_not_started_without_its_money(rig):
    search_once(rig)
    p = views.panel(rig.db, rig.corpus, "imha")
    r = widen(rig, "click", usd=f"{p['spent'] + p['needs'] - 0.05:.2f}")
    assert "note=too_low" in r.headers["location"] and rounds_run(rig) == [1]
    assert widen(rig, "again", usd=f"{p['spent'] + p['needs']:.2f}").status_code == 303
    assert rounds_run(rig) == [1, 2]


def test_screening_stops_before_its_first_paid_call_when_the_real_count_is_too_high(rig):
    con = s.connect(rig.db)
    budget.set_budget(con, "imha", 0.05)         # the form's check is passed: the true count isn't
    llm = LLM(rig.model, corpus=rig.corpus, cap=0.05)
    with pytest.raises(BudgetExceeded, match="records to screen need"):
        review.first_round("imha", llm, db=con, say=lambda m: None)
    assert rig.model.calls == []


def test_the_ledger_never_lets_a_search_past_its_budget(rig, monkeypatch):
    one_by_one(rig, monkeypatch)
    con = s.connect(rig.db)
    budget.set_budget(con, "imha", 0.08)
    jobs.carry_on(jobs.Tools(rig.db, rig.corpus, rig.model), "imha")(lambda *a, **k: None)
    spent = budget.spent(rig.corpus, "imha", "2000")
    assert 0 < spent <= 0.08
    assert stage(rig) == "paused"                     # stopped, not failed
    said = [m["body"].get("text", "") for m in chat.messages(con, "imha")]
    assert any("Stopped before passing the budget" in t for t in said)


def test_a_budget_counts_only_what_was_spent_since_it_was_set(rig):
    (rig.corpus / "spend.csv").write_text(
        "time,step,doc,model,input_tokens,output_tokens,thoughts_tokens,usd\n"
        "2020-01-01T10:00:00,screen,screen_imha,m,1,1,0,1.50\n"
        "2999-01-01T10:00:00,screen,screen_other,m,1,1,0,0.70\n", "utf-8")
    con = s.connect(rig.db)
    budget.set_budget(con, "imha", 2.0)
    assert budget.left(con, rig.corpus, "imha") == 2.0        # older, or another search's


def test_the_margin_covers_every_call_in_flight_at_its_worst(rig):
    cfg = settings()
    price = cfg["prices"][cfg["models"]["screen"]]
    one_call = cfg["spending"]["max_output_tokens"] * price["output"] / 1e6
    assert budget.needed(200) - budget.round_cost(200) >= review_workers() * one_call
    assert budget.round_cost(200) == pytest.approx(0.26)      # 200 records and two review calls


def review_workers() -> int:
    from vet.screen import WORKERS
    return WORKERS


# a round cut off part-way loses nothing and pays for nothing twice

def test_a_cut_off_round_carries_on_where_it_stopped(rig, monkeypatch):
    one_by_one(rig, monkeypatch)
    con = s.connect(rig.db)
    budget.set_budget(con, "imha", 0.08)
    tools = jobs.Tools(rig.db, rig.corpus, rig.model)
    jobs.carry_on(tools, "imha")(lambda *a, **k: None)
    done = set(review.decisions(con, "imha"))
    budget.set_budget(con, "imha", 2.0)                          # the vet raises it
    jobs.carry_on(tools, "imha")(lambda *a, **k: None)
    assert stage(rig) == "decide" and rounds_run(rig) == [1]     # the same round, finished
    sent = [json.loads(line)["key"] for c in rig.model.calls if c["step"] == "screen"
            for line in c["prompt"].splitlines()[1:]]
    assert len(sent) == len(set(sent))                           # nothing screened twice
    assert done < set(review.decisions(con, "imha"))


def test_records_answered_before_a_failure_are_saved(rig, monkeypatch):
    """Batches already answered are paid for: a later failure mustn't throw them away."""
    one_by_one(rig, monkeypatch)
    monkeypatch.setattr(review, "SAVE_EVERY", 100)               # all in one chunk
    con = s.connect(rig.db)
    s.run_search("imha", review.load_topic("imha"), con, None)
    real = rig.model.generate

    def flaky(model, system, prompt, schema, thinking, max_output, step=""):
        if '"key": "4"' in prompt:
            raise RuntimeError("the service went away")
        return real(model, system, prompt, schema, thinking, max_output, step=step)
    rig.model.generate = flaky
    with pytest.raises(RuntimeError):
        review.screen_new("imha", IMHA, con, LLM(rig.model, corpus=rig.corpus), 1, lambda m: None)
    done = review.decisions(con, "imha")
    assert {"1", "3"} <= set(done) and "4" not in done           # 4 is still to do


def test_a_round_that_fails_while_widening_can_be_carried_on(rig, monkeypatch):
    search_once(rig)
    real = review.act
    monkeypatch.setattr(review, "act", lambda *a: (_ for _ in ()).throw(RuntimeError("no network")))
    widen(rig, "w1")
    assert stage(rig) == "paused"
    assert "no network" in rig.client.get("/topics/imha/chat").text
    monkeypatch.setattr(review, "act", real)
    post(rig, "/topics/imha/search", budget="2.00", request="w2")
    assert stage(rig) == "decide" and rounds_run(rig) == [1, 2]
    html = rig.client.get("/topics/imha/chat").text
    assert "no network" not in html                  # a problem since solved leaves the chat


def test_an_app_stopped_mid_round_offers_to_carry_on(rig):
    con = s.connect(rig.db)
    budget.set_budget(con, "imha", 2.0)
    budget.begin_round(con, "imha", 1)
    s.mark_found(con, "imha", 1, ["1"], "pubmed")
    assert stage(rig) == "paused"
    html = rig.client.get("/topics/imha/chat").text
    assert "This round was cut off" in html and "Carry on" in html


def test_a_job_keeps_no_lock_on_the_database(rig):
    """A job waits minutes on the network: the vet's clicks must still be able to write."""
    con = jobs.Tools(rig.db, rig.corpus).connect()
    con.execute("INSERT INTO found VALUES ('imha', 1, 'k', 'pubmed')")     # no commit
    other = sqlite3.connect(rig.db, timeout=0.2)
    with other:
        other.execute("INSERT INTO found VALUES ('imha', 1, 'k2', 'pubmed')")


# round counts

def test_screening_again_keeps_the_first_round_and_the_vets_decision(tmp_path):
    con = s.connect(tmp_path / "v.db")
    one = [{"key": "1", "decision": "keep", "rule": "", "flags": [], "answers": {}}]
    s.save_screening(con, "t", 2, one)
    con.execute("UPDATE screening SET approved = '2026-11-03'")
    s.save_screening(con, "t", 5, [one[0] | {"decision": "lead"}])
    row = con.execute("SELECT decision, round, approved FROM screening").fetchone()
    assert tuple(row) == ("lead", 2, "2026-11-03")


def test_each_round_shows_what_it_screened_kept_and_cost(rig):
    search_once(rig)
    widen(rig, "w")
    p = views.panel(rig.db, rig.corpus, "imha")
    assert [r["round"] for r in p["rounds"]] == [1, 2]
    assert p["rounds"][0]["screened"] == 2 and p["rounds"][0]["kept"] == 2
    assert all(r["cost"] > 0 for r in p["rounds"])
    assert p["spent"] == pytest.approx(PER_CALL * len(rig.model.calls), rel=1e-3)
    html = rig.client.get("/topics/imha/chat").text
    assert "Search round 2 (widening)" in html and "Search finished" not in html


def test_old_rounds_are_counted_by_when_a_record_was_first_found(tmp_path):
    """Records re-screened in a later round keep their place (IMHA's rounds were re-stamped)."""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE screening SET round = 5")
    con.close()
    html = TestClient(ui.create_app(db, db.parent), base_url="http://127.0.0.1").get(
        "/topics/surgical_pain/chat").text
    assert "Search: 5 records screened" in html and "Search round 5" not in html


def test_an_old_search_shows_no_invented_costs(tmp_path, monkeypatch):
    """Rounds run before this table existed: asked about, not costed."""
    db = make_db(tmp_path)
    p = views.panel(db, db.parent, "surgical_pain")
    assert p["stage"] == "ask" and p["rounds"] == []
    html = TestClient(ui.create_app(db, db.parent), base_url="http://127.0.0.1").get(
        "/topics/surgical_pain/chat").text
    assert "ran before each round was your decision" in html
    assert ">Screened</th>" not in html and "Search finished" in html        # no rounds table


# the Planner in the chat

def ask(rig, text=WORDS, request="ask-1"):
    r = post(rig, "/chat", text=text, request=request)
    assert r.status_code == 303
    return re.search(r"/topics/(\w+)/chat", r.headers["location"]).group(1)


def test_a_question_starts_a_search_with_the_planner(rig):
    name = ask(rig)
    assert name == "treating_immune_mediated_hemolytic" and rig.model.steps("plan") == 1
    assert (rig.root / "topics" / f"{name}.toml").exists()
    assert stage(rig, name) == "planning"
    html = rig.client.get(f"/topics/{name}/chat").text
    for part in (WORDS, "Agree this search", "Revise with my answers", "Papers you already know"):
        assert part in html, part
    assert any(s_["name"] == name for s_ in views.searches(rig.db, rig.corpus))


def test_a_jobs_own_steps_stay_out_of_the_conversation(rig):
    name = ask(rig)
    html = rig.client.get(f"/topics/{name}/chat").text
    assert "Planner started" not in html and "Planner finished" not in html


def test_a_double_click_asks_once(rig):
    name = ask(rig)
    assert ask(rig, request="ask-1") == name and rig.model.steps("plan") == 1
    assert len(list((rig.root / "topics").glob("*.toml"))) == 2       # imha, and this one


def test_a_new_question_waits_while_a_step_runs(rig):
    rig.model.gate = threading.Event()
    name = ask_without_waiting(rig)
    r = rig.client.post("/chat", data={"csrf": rig.client.app.state.token, "text": "Cats",
                                       "request": "second"},
                        headers={"origin": "http://127.0.0.1:8765"}, follow_redirects=False)
    assert "note=busy" in r.headers["location"] and "q=Cats" in r.headers["location"]
    assert stage(rig, name) == "running"
    html = rig.client.get(f"/topics/{name}/chat").text
    assert f'hx-get="/topics/{name}/live"' in html
    assert 'hx-trigger="every 2s"' in html                     # it keeps asking while the step runs
    assert "<p" in rig.client.get(f"/topics/{name}/live").text
    rig.model.gate.set()
    rig.client.app.state.runner.wait()
    assert rig.client.get(f"/topics/{name}/live").headers["hx-refresh"] == "true"


def ask_without_waiting(rig) -> str:
    r = rig.client.post("/chat", data={"csrf": rig.client.app.state.token, "text": WORDS,
                                       "request": "ask-1"},
                        headers={"origin": "http://127.0.0.1:8765"}, follow_redirects=False)
    return re.search(r"/topics/(\w+)/chat", r.headers["location"]).group(1)


def test_a_form_can_only_pick_what_was_offered(rig):
    name = ask(rig)
    state = views.plan_state(rig.corpus, name)
    paper = state["rounds"][0]["papers"][0]["key"]
    form = {"q0": "a made-up option", f"p{paper}": "yes", "pNOTSHOWN": "yes",
            "note": "Leave out cats", "known": "123\n\n"}
    got = plan.answers_from(state, lambda f: form.get(f, ""))
    assert got["question"] == [{"question": plan.NOTE, "answer": "Leave out cats"}]
    assert got["paper"] == [{"key": paper, "relevant": "yes"}] and got["known"] == ["123"]


@pytest.mark.parametrize("form, answer", [
    ({"q0": "yes", "q0_own": "  only with \n IMTP "}, "yes: only with IMTP"),   # a detail, kept with it
    ({"q0_own": "mycophenolate"}, "mycophenolate"),                             # no option fits
    ({"q0": "a made-up option", "q0_own": "x" * 400}, "x" * 300),               # own words, capped
    ({"q0": "a made-up option"}, None),                                         # nothing offered
])
def test_a_question_takes_the_vets_own_words_too(rig, form, answer):
    name = ask(rig)
    state = views.plan_state(rig.corpus, name)
    got = plan.answers_from(state, lambda f: form.get(f, ""))["question"]
    assert got == ([{"question": "Include Evans syndrome?", "answer": answer}] if answer else [])


def test_the_round_shows_whether_healthy_dogs_are_kept(rig):
    name = ask(rig)
    html = rig.client.get(f"/topics/{name}/chat").text
    assert "healthy or research dogs given a treatment: included" in html       # not given: as before


def test_the_own_words_box_is_under_each_question_and_reaches_the_planner(rig):
    name = ask(rig)
    html = rig.client.get(f"/topics/{name}/chat").text
    assert 'name="q0_own"' in html and "Your own answer, or a detail" in html
    post(rig, f"/topics/{name}/plan", go="revise", round="1", q0="yes", q0_own="mycophenolate",
         request="r2")
    assert "yes: mycophenolate" in [c for c in rig.model.calls if c["step"] == "plan"][1]["prompt"]


def test_answers_reach_the_planner_and_the_chat(rig):
    name = ask(rig)
    r = post(rig, f"/topics/{name}/plan", go="revise", round="1", note="Leave out cats",
             request="r2")
    assert r.status_code == 303 and rig.model.steps("plan") == 2
    assert "Leave out cats" in [c for c in rig.model.calls if c["step"] == "plan"][1]["prompt"]
    html = rig.client.get(f"/topics/{name}/chat").text
    assert re.search(r"<h3>You</h3>.*?<p>Leave out cats</p>", html, re.DOTALL)  # the vet's words


def test_the_planners_last_round_can_only_be_agreed(rig):
    name = ask(rig)
    post(rig, f"/topics/{name}/plan", go="revise", round="1", request="r2")
    html = rig.client.get(f"/topics/{name}/chat").text
    assert "Agree this search" in html and "Revise with my answers" not in html
    assert 'name="go" value="agree"' in html                   # the button sends the choice
    r = post(rig, f"/topics/{name}/plan", go="revise", round="2", request="r3")
    assert "note=last_round" in r.headers["location"] and rig.model.steps("plan") == 2


@pytest.mark.parametrize("answer", [{"q0": "yes"}, {"q0_own": "mycophenolate"}, {"note": "Leave out cats"}])
def test_answers_go_through_revise_not_agree(rig, answer):
    name = ask(rig)
    r = post(rig, f"/topics/{name}/plan", go="agree", round="1", request="r2", **answer)
    assert "note=revise_first" in r.headers["location"] and stage(rig, name) == "planning"
    assert "press Revise with my answers" in rig.client.get(r.headers["location"]).text


def test_papers_marked_or_known_can_still_be_agreed(rig):
    name = ask(rig)
    paper = views.plan_state(rig.corpus, name)["rounds"][0]["papers"][0]["key"]
    post(rig, f"/topics/{name}/plan", go="agree", round="1", request="r2", known="123",
         **{f"p{paper}": "yes"})
    assert stage(rig, name) == "ready"


def test_on_the_last_round_answers_can_be_agreed(rig):
    name = ask(rig)
    post(rig, f"/topics/{name}/plan", go="revise", round="1", request="r2")
    assert "can no longer change the search" in rig.client.get(f"/topics/{name}/chat").text
    post(rig, f"/topics/{name}/plan", go="agree", round="2", request="r3", q0="yes")
    assert stage(rig, name) == "ready"


def test_an_old_planner_form_is_refused(rig):
    name = ask(rig)
    r = post(rig, f"/topics/{name}/plan", go="revise", round="9", request="r2")
    assert "note=stale" in r.headers["location"] and rig.model.steps("plan") == 1


def test_agreeing_writes_the_search_and_quotes_its_cost(rig):
    name = ask(rig)
    calls = len(rig.model.calls)
    post(rig, f"/topics/{name}/plan", go="agree", round="1", request="r2")
    assert len(rig.model.calls) == calls                       # agreeing is free
    assert stage(rig, name) == "ready"
    p = views.panel(rig.db, rig.corpus, name)
    assert p["records"] == p["pubmed"] and p["records"] > 0
    html = rig.client.get(f"/topics/{name}/chat").text
    assert "Ready to search" in html and "Start the search" in html
    assert 'name="budget"' in html and 'value="2.00"' in html
    con = sqlite3.connect(rig.db)
    assert con.execute("SELECT words FROM topic_inputs WHERE topic = ?", (name,)).fetchone()[0] == WORDS


def test_a_search_too_large_to_screen_can_still_be_narrowed(rig, monkeypatch):
    name = ask(rig)
    monkeypatch.setitem(settings()["pipeline"], "screen_budget", 0)
    post(rig, f"/topics/{name}/plan", go="agree", round="1", request="r2")
    assert stage(rig, name) == "planning"                       # not agreed
    html = rig.client.get(f"/topics/{name}/chat").text
    assert html.count("more than the 0 one search may screen") == 1
    assert html.index("may screen") < html.index("Agree this search")    # seen, not scrolled past
    post(rig, f"/topics/{name}/plan", go="revise", round="1", request="r3")
    assert "at most 0 can be screened" in [c for c in rig.model.calls if c["step"] == "plan"][-1]["prompt"]
    monkeypatch.setitem(settings()["pipeline"], "plan_rounds", 2)
    post(rig, f"/topics/{name}/plan", go="agree", round="2", request="r4")
    assert "ask a new, narrower question" in rig.client.get(f"/topics/{name}/chat").text


def test_a_failed_planner_can_be_asked_again(rig):
    rig.model.generate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network"))
    name = ask(rig)
    assert stage(rig, name) == "retry"
    html = rig.client.get(f"/topics/{name}/chat").text
    assert "Ask the Planner again" in html and "no network" in html
    rig.model.generate = Billed().generate
    post(rig, f"/topics/{name}/plan", go="retry", request="again")
    assert stage(rig, name) == "planning"


def test_a_question_with_no_related_conditions_can_still_be_screened(rig):
    """A screening slot the vet's question leaves empty must not stop the search."""
    name = ask(rig)
    p = rig.corpus / "plan" / name / "state.json"
    state = json.loads(p.read_text())
    state["rounds"][-1]["checked"] |= {"related": [], "treatments": []}
    p.write_text(json.dumps(state))
    post(rig, f"/topics/{name}/plan", go="agree", round="1", request="r2")
    assert "none named" in system_prompt(name, review.load_topic(name))


def test_what_the_model_writes_is_shown_as_text(rig):
    rig.model.review = {**rig.model.review, "reasoning": SCRIPT}
    search_once(rig)
    html = rig.client.get("/topics/imha/chat").text
    assert SCRIPT not in html and "&lt;script&gt;" in html


def test_a_new_searchs_name_is_safe_and_new(rig):
    taken = {"cats"}
    assert plan.topic_name("Cats", taken) == "cats_2"
    assert plan.topic_name("../../etc/passwd", set()) == "etc_passwd"
    assert plan.topic_name("犬の貧血", set()) == "search"
    for words in ("A" * 200 + " dogs", "5-fluorouracil in cats", "5" * 200, "犬の貧血"):
        name = plan.topic_name(words, set())
        assert views.TOPIC_NAME.fullmatch(name) and TOPIC_NAME.fullmatch(name), name


# the command line follows the same rule

def test_the_command_line_runs_one_round_at_a_time(rig, monkeypatch, capsys):
    monkeypatch.setattr("vet.llm.LLM", lambda: LLM(rig.model, corpus=rig.corpus))
    cli.main(["--db", str(rig.db), "search", "imha", "--budget", "2"])
    out = capsys.readouterr().out
    assert "round 2 would:" in out and "--widen" in out and rounds_run(rig) == [1]
    cli.main(["--db", str(rig.db), "search", "imha", "--widen"])
    assert rounds_run(rig) == [1, 2]
    with pytest.raises(SystemExit, match="the vet's decision"):
        cli.main(["--db", str(rig.db), "search", "imha"])     # widening has to be asked for


# the guards hold on the new pages

@pytest.mark.parametrize("path", ["/chat", "/topics/imha/plan", "/topics/imha/search",
                                  "/topics/imha/widen", "/topics/imha/propose"])
def test_every_new_action_needs_the_page_token_and_origin(rig, path):
    no_token = rig.client.post(path, data={"text": WORDS, "budget": "2"},
                               headers={"origin": "http://127.0.0.1:8765"}, follow_redirects=False)
    other_site = rig.client.post(path, data={"csrf": rig.client.app.state.token, "text": WORDS,
                                             "budget": "2"},
                                 headers={"origin": "http://evil.example"}, follow_redirects=False)
    assert no_token.status_code == 403 and other_site.status_code == 403
    assert rig.model.calls == []


def test_a_search_asked_in_the_chat_stays_out_of_git():
    ignored = (ROOT / ".gitignore").read_text()
    assert "topics/*.toml" in ignored
    for kept in ("cie", "imha", "surgical_pain"):
        assert f"!topics/{kept}.toml" in ignored
    assert "^topics/" in (ROOT / "scripts" / "check.sh").read_text()
