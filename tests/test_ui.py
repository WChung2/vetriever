"""The local screens (`vet ui`), on a made-up database: what they show, and the guards that
keep the data on this Mac and the database safe. Free: no model, no network."""
import hashlib
import importlib.metadata as md
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests import fixture
from vet import chat, search, views
from vet.codebook import Codebook
from vet.store import Store
from vet.ui import app as ui
from vet.who import _connect as authors_table

TOPIC = "surgical_pain"          # a topic file that exists in topics/
HTMX_SHA384 = "HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+"  # 2.0.4, published
SCRIPT = "<script>alert(1)</script><img src=http://example.org/x.png>"


def answers(cond="yes", animals="patients", reasoning="Dogs after surgery given an analgesic."):
    return {"reasoning": reasoning, "condition": {"answer": cond, "quote": "dogs after surgery"},
            "animals": {"answer": animals, "quote": "client-owned dogs"}, "kind": "study",
            "design": "randomised_trial", "summary": "Twenty dogs given a fictional analgesic."}


RECORDS = [  # key, title, decision, rule, flags, approved
    ("1001", "FIXTURE: carprofen after ovariohysterectomy in dogs", "keep", "", [], ""),
    ("1002", "FIXTURE: robenacoxib versus meloxicam in dogs", "keep", "", ["single_case"], ""),
    ("1003", SCRIPT, "keep", "", [], ""),
    ("1004", "FIXTURE: a review of canine analgesia", "lead", "", [], ""),
    ("1005", "FIXTURE: pain after surgery in cats", "off_topic", "O1", [], ""),
    ("1006", "FIXTURE: a letter to the editor", "aside", "", [], ""),
]


def make_db(folder: Path) -> Path:
    """A made-up database: six screened records of one topic, one extracted study (the
    codebook fixture), two author rows, and a spending log."""
    db = folder / "vetriever.db"
    con = search.connect(db)
    for key, title, *_ in RECORDS:
        con.execute("INSERT INTO records (key, pmid, doi, title, abstract, year, journal) "
                    "VALUES (?, ?, ?, ?, 'An abstract.', '2020', 'Fictional Vet J')",
                    (key, key, f"10.9999/{key}", title))
        con.execute("INSERT INTO found (topic, round, key, how) VALUES (?, 1, ?, 'pubmed')",
                    (TOPIC, key))
    search.save_screening(con, TOPIC, 1, [
        {"key": k, "decision": d, "rule": r, "flags": f,
         "answers": answers("no" if d == "off_topic" else "yes") if d != "aside" else {}}
        for k, _, d, r, f, _ in RECORDS])
    # save_screening stamps today's date: pin it, so the screens show the same dates every day
    con.execute("UPDATE screening SET date = '2026-11-03'")
    con.commit()
    con.close()
    cb = Codebook()
    tables = fixture.clean(cb)
    tables["evidence"] = fixture.evidence_for(cb, tables)
    store = Store(db, cb)
    store.import_tables(tables)
    store.close()
    a = authors_table(db)
    with a:
        a.execute("INSERT INTO authors (paper, author_no, author_position, author_name, author_id, "
                  "institution_id, country_code, root_name, root_id, work_id) VALUES "
                  "('1001', 1, 'first', 'Doe J', 'A1', 'I1', 'CH', 'Fictional University', 'I1', 'W1'), "
                  "('1001', 2, 'middle', 'Helper H', 'A2', 'I2', 'CH', 'Helper Institute', 'I2', 'W1')")
    a.close()
    (folder / "spend.csv").write_text(
        "time,step,doc,model,input_tokens,output_tokens,thoughts_tokens,usd\n"
        "2026-11-03T10:00:00,screen,screen_x,gemini-3.8-flash,1000,100,0,0.25\n"
        f"2026-11-03T09:00:00,plan,plan_{TOPIC},gemini-3.8-flash,1000,100,0,0.04\n", "utf-8")
    plan = folder / "plan" / TOPIC
    plan.mkdir(parents=True)
    (plan / "state.json").write_text(json.dumps(PLAN), "utf-8")
    return db


PLAN = {"words": "Pain relief after spaying in dogs; doses and rescue analgesia", "started": "2026-11-03",
        "rounds": [{"n": 1, "time": "2026-11-03T09:00:00",
                    "draft": {"questions": [{"question": "Include cats?",
                                             "options": ["Dogs only", "Dogs and cats"]}]},
                    "checked": {"restatement": "Which analgesics and doses relieve pain after "
                                               "ovariohysterectomy in dogs?",
                                "species": "dogs", "condition": "pain after surgery",
                                "treatments": [{"item": "carprofen", "source": "vet"},
                                               {"item": "robenacoxib", "source": "suggestion"}],
                                "terms": [{"block": "species", "term": "Dogs", "hits": 402},
                                          {"block": "condition", "term": "Pain, Postoperative",
                                           "hits": 77}],
                                "hits": 61, "problems": ["'anaesth*' dropped: too short"]},
                    "papers": [{"key": "1001", "title": "FIXTURE: carprofen paper", "relevant": "yes"},
                               {"key": "1005", "title": "FIXTURE: cats paper", "relevant": "no"}],
                    "answers": [{"question": "Include cats?", "answer": "Dogs only"}]}]}


@pytest.fixture
def db(tmp_path) -> Path:
    return make_db(tmp_path)


@pytest.fixture
def client(db) -> TestClient:
    return TestClient(ui.create_app(db, db.parent), base_url="http://127.0.0.1")


def post(client, path, data, token=True, origin="http://127.0.0.1:8765", **headers):
    if token:
        data = {**data, "csrf": client.app.state.token}
    if origin:
        headers["origin"] = origin
    return client.post(path, data=data, headers=headers, follow_redirects=False)


def pages(db) -> list[str]:
    t = f"/topics/{TOPIC}"
    return ["/", "/?q=cats", t, f"{t}/chat", f"{t}/review",
            *(f"{t}/review?view={v}" for v in views.VIEWS), f"{t}/review?q=carprofen",
            f"{t}/review?flag=single_case", f"{t}/studies", f"{t}/who",
            "/studies", "/studies/VET-1", "/who", "/spending"]


# what the screens show

def test_every_page_shows_its_data(client, db):
    for path in pages(db):
        r = client.get(path)
        assert r.status_code == 200, path
    records = f"/topics/{TOPIC}/review"
    assert "carprofen after ovariohysterectomy" in client.get(records).text
    assert "pain after surgery in cats" in client.get(f"{records}?view=dropped").text
    assert "only other species" in client.get(f"{records}?view=dropped").text
    study = client.get("/studies/VET-1").text
    assert "fictional analgesic trial" in study and "fixture quote for" in study
    assert "Fictional University" in client.get("/who").text
    assert "$0.25" in client.get("/spending").text


def test_the_numbers_are_the_databases(client, db):
    c = views.counts(db, TOPIC)
    con = sqlite3.connect(db)
    assert c["keep"] == con.execute("SELECT COUNT(*) FROM screening WHERE topic = ? AND "
                                    "decision = 'keep'", (TOPIC,)).fetchone()[0] == 3
    assert c["found"] == 6 and c["studies"] == 1
    tabs = views.records(db, TOPIC)["tabs"]
    assert tabs == {"to_review": 3, "approved": 0, "rejected": 0, "dropped": 1, "overviews": 1,
                    "aside": 1}
    filtered = views.records(db, TOPIC, q="robenacoxib")
    assert [r["key"] for r in filtered["rows"]] == ["1002"]


def decide(client, data, **headers):
    return post(client, f"/topics/{TOPIC}/decide", data, **headers)


def state(db, key):
    return sqlite3.connect(db).execute("SELECT approved != '', rejected != '' FROM screening "
                                       "WHERE key = ?", (key,)).fetchone()


def test_review_approve_reject_and_undo(client, db):
    r = decide(client, {"key": "1001", "action": "approve"}, **{"hx-request": "true"})
    assert r.status_code == 200 and "✓ approved" in r.text and ">undo<" in r.text
    assert state(db, "1001") == (1, 0)
    r = decide(client, {"key": "1002", "action": "reject"}, **{"hx-request": "true"})
    assert "✗ rejected" in r.text and state(db, "1002") == (0, 1)
    decide(client, {"key": "1002", "action": "approve"})            # a change of mind
    assert state(db, "1002") == (1, 0)
    decide(client, {"key": "1001", "action": "undo"})
    assert state(db, "1001") == (0, 0)
    log = sqlite3.connect(db).execute("SELECT what, detail FROM topic_log ORDER BY rowid").fetchall()
    assert log == [("approve", "1001"), ("reject", "1002"), ("approve", "1002"), ("undo", "1001")]
    assert decide(client, {"key": "1001", "action": "delete"}).status_code == 400


def test_a_dropped_article_can_be_kept_anyway_but_not_an_overview(client, db):
    html = client.get(f"/topics/{TOPIC}/review?view=dropped").text
    assert "Keep anyway" in html
    decide(client, {"key": "1005", "action": "approve"})            # dropped by the screening
    assert state(db, "1005") == (1, 0)
    assert "kept anyway" in client.get(f"/topics/{TOPIC}/review?view=approved").text
    decide(client, {"key": "1004", "action": "approve"})            # an overview: no decision
    decide(client, {"key": "1005", "action": "reject"})             # dropped: approve or undo only
    assert state(db, "1004") == (0, 0) and state(db, "1005") == (1, 0)


def test_the_lists_follow_your_decisions(client, db):
    decide(client, {"key": ["1001", "1003"], "action": "approve"})
    tabs = views.records(db, TOPIC)["tabs"]
    assert (tabs["to_review"], tabs["approved"]) == (1, 2)
    html = client.get(f"/topics/{TOPIC}/review").text
    assert "<b>1</b> article waits for your decision" in html and "Approve all 1" in html
    decide(client, {"key": "1002", "action": "reject"})
    html = client.get(f"/topics/{TOPIC}/review").text               # nothing left: opens Approved
    assert 'aria-current="page">Approved by you' in html
    assert "Nothing waits for your review" in client.get(
        f"/topics/{TOPIC}/review?view=to_review").text


def test_approve_all_waiting_and_undo_all(client, db):
    decide(client, {"key": "1002", "action": "reject"})
    r = decide(client, {"all": "1", "action": "approve", "view": "to_review"})
    assert r.status_code == 303
    assert views.counts(db, TOPIC)["approved"] == 2                 # the rejected one stays out
    decide(client, {"all": "1", "action": "undo", "view": "approved"})
    assert views.counts(db, TOPIC)["approved"] == 0
    assert decide(client, {"all": "1", "action": "reject"}).status_code == 400   # never in bulk


def test_a_database_from_before_the_rejected_column(client, db):
    con = sqlite3.connect(db)
    with con:
        con.execute("ALTER TABLE screening DROP COLUMN rejected")
    assert client.get(f"/topics/{TOPIC}/review").status_code == 200   # read-only: no column, fine
    decide(client, {"key": "1001", "action": "reject"})                 # a decision adds it
    assert state(db, "1001") == (0, 1)


def test_the_command_line_approve_all_leaves_your_rejections(db):
    from vet import approve
    con = search.connect(db)
    approve.set_decision(TOPIC, con, ["1002"], "reject")
    assert approve.approve(TOPIC, con) == 2
    assert state(db, "1002") == (0, 1)


# the data stays on this Mac

def test_the_server_listens_on_this_mac_only(monkeypatch, tmp_path):
    from vet import cli
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)
    cli.main(["--db", str(make_db(tmp_path)), "ui", "--no-browser"])
    assert seen["host"] == "127.0.0.1"
    with pytest.raises(SystemExit):                  # there is no option to listen elsewhere
        cli.main(["ui", "--host", "0.0.0.0"])


def test_a_taken_port_stops_before_the_browser_opens(monkeypatch, tmp_path):
    """Else the browser would open whatever other program holds the port."""
    from vet import cli
    started = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: started.append("server"))
    monkeypatch.setattr("webbrowser.open", lambda url: started.append("browser"))
    monkeypatch.setattr(cli, "_port_free", lambda host, port: False)
    with pytest.raises(SystemExit, match="used by another program"):
        cli.main(["--db", str(make_db(tmp_path)), "ui"])
    assert started == []


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.nip.io", "192.168.1.20", ""])
def test_requests_for_other_addresses_are_refused(client, host):
    """A web page elsewhere can't reach the app by pointing its own name at 127.0.0.1."""
    assert client.get("/", headers={"host": host}).status_code == 400


def test_localhost_is_answered(client):
    assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200


def test_pages_load_nothing_from_outside(client, db):
    for path in pages(db):
        r = client.get(path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp
        assert "connect-src 'self'" in csp and "'unsafe" not in csp
        assert r.headers["referrer-policy"] == "no-referrer"
        html = r.text
        assert re.findall(r"<script[^>]*>", html) == ['<script src="/static/htmx.min.js" defer>']
        for tag, attr, url in re.findall(r"<(\w+)[^>]*?\s(src|href|action)=\"(https?:[^\"]*)\"", html):
            assert tag == "a" and attr == "href", (path, tag, url)   # only links you click
    for t in (Path(ui.__file__).parent / "templates").glob("*.html"):
        text = t.read_text()
        assert not re.search(r"\son\w+=|style=|<style|javascript:", text), t.name
        assert "| safe" not in text and "|safe" not in text, t.name
    assert "@import" not in (Path(ui.__file__).parent / "static" / "style.css").read_text()


def test_links_out_send_no_referrer(client, db):
    for path in pages(db):
        for a in re.findall(r"<a [^>]*href=\"https?:[^>]*>", client.get(path).text):
            assert 'rel="noopener noreferrer"' in a, a


def test_htmx_is_the_published_release():
    js = (Path(ui.__file__).parent / "static" / "htmx.min.js").read_bytes()
    import base64
    assert base64.b64encode(hashlib.sha384(js).digest()).decode() == HTMX_SHA384


def test_htmx_runs_no_code_from_pages(client):
    head = client.get("/").text
    assert '"allowEval": false' in head and '"allowScriptTags": false' in head
    assert '"selfRequestsOnly": true' in head and '"historyCacheSize": 0' in head


def test_no_telemetry_and_no_api_pages(client):
    assert client.app._telemetry["auto_configure"] is False
    assert not any(client.app._telemetry[k] for k in ("tracing", "metrics", "logs",
                                                      "operation_spans"))
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404
    for pkg in ("opentelemetry-sdk", "opentelemetry-exporter-otlp",
                "opentelemetry-exporter-otlp-proto-http", "opentelemetry-exporter-otlp-proto-grpc"):
        with pytest.raises(md.PackageNotFoundError):
            md.version(pkg)


def test_no_model_and_no_internet(monkeypatch, client, db):
    def refuse(*a, **k):
        raise AssertionError("the screens called a model or the internet")
    monkeypatch.setattr("vet.llm.Gemini.__init__", refuse)
    monkeypatch.setattr("vet.connectors.Http.get", refuse)
    for path in pages(db):
        assert client.get(path).status_code == 200
    assert decide(client, {"key": "1001", "action": "approve"}).status_code == 303


def test_secrets_never_reach_a_page(monkeypatch, client, db):
    for name in ("GEMINI_API_KEY", "ELSEVIER_API_KEY", "WILEY_TDM_TOKEN", "NCBI_API_KEY",
                 "CONTACT_EMAIL"):
        monkeypatch.setenv(name, f"SECRET-{name}")
    for path in pages(db):
        assert "SECRET-" not in client.get(path).text


# the database stays safe

def test_looking_changes_nothing(client, db):
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    for path in pages(db):
        client.get(path)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        views.read_only(db).execute("DELETE FROM screening")


def test_a_change_must_come_from_these_pages(client, db):
    path, data = f"/topics/{TOPIC}/decide", {"key": "1001", "action": "approve"}
    assert post(client, path, data, origin="").status_code == 403              # no origin
    assert post(client, path, data, origin="https://evil.example").status_code == 403
    assert post(client, path, data, token=False).status_code == 403            # no token
    bad = post(client, path, {**data, "csrf": "guess"}, token=False)
    assert bad.status_code == 403
    assert views.counts(db, TOPIC)["approved"] == 0
    assert post(client, path, data).status_code == 303


def test_every_change_is_guarded(client):
    for route in client.app.routes:
        if "POST" in getattr(route, "methods", ()):
            path = route.path.replace("{name}", TOPIC)
            assert post(client, path, {}, token=False).status_code == 403, path


def test_text_from_papers_is_shown_never_run(client):
    html = client.get(f"/topics/{TOPIC}/review").text
    assert SCRIPT not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


@pytest.mark.parametrize("path", ["/topics/..%2Fsettings", "/topics/nope", "/topics/nope/chat",
                                  "/topics/..%2F..%2Fsettings/review", "/studies/VET-1%27--",
                                  "/studies/1", "/static/..%2F..%2Fsettings.toml",
                                  "/static/../app.py"])
def test_unknown_names_and_paths_are_not_found(client, path):
    r = client.get(path)
    assert r.status_code == 404, path
    assert "cap_usd" not in r.text and "import" not in r.text


def test_errors_show_no_internals(monkeypatch, db):
    monkeypatch.setattr(views, "searches", lambda db, corpus: 1 / 0)
    c = TestClient(ui.create_app(db, db.parent), base_url="http://127.0.0.1",
                   raise_server_exceptions=False)
    r = c.get("/")
    assert r.status_code == 500
    assert "ZeroDivisionError" not in r.text and "Traceback" not in r.text


def test_an_empty_database_still_opens(tmp_path):
    c = TestClient(ui.create_app(tmp_path / "none.db", tmp_path), base_url="http://127.0.0.1")
    for path in ("/", f"/topics/{TOPIC}", f"/topics/{TOPIC}/chat", "/studies", "/who",
                 "/spending"):
        assert c.get(path).status_code == 200, path
    assert not (tmp_path / "none.db").exists()       # looking didn't create it


# the dashboard, the chat and the study, clinical_ai's way

def test_the_dashboard_lists_every_search_with_its_steps(client, db):
    s = next(x for x in views.searches(db, db.parent) if x["name"] == TOPIC)
    assert s["title"] == "Pain after surgery"
    assert s["status"] == "1 study in the database" and s["tone"] == "built"   # the fixture's
    assert s["steps"] == ["done", "done", "now", "", "done"]   # nothing reviewed yet
    assert s["line"] == "6 found · 0 approved" and s["date"] == "2026-11-03"
    side = client.get("/").text
    assert "Pain after surgery" in side and "1 study in the database" in side
    assert "search and screen: done" in side and "review: your decision: next" in side
    assert {x["name"] for x in views.searches(db, db.parent)} == set(views.topic_names())


def test_a_search_opens_on_what_it_built(client):
    """Clicking a search shows its database: studies when there are some."""
    html = client.get(f"/topics/{TOPIC}").text
    assert 'aria-current="page">Studies' in html and "fictional analgesic trial" in html


def test_the_chat_rebuilds_an_earlier_run(client):
    html = client.get(f"/topics/{TOPIC}/chat").text
    assert "Pain relief after spaying in dogs" in html                 # the vet's words
    assert "Which analgesics and doses relieve pain" in html            # the Planner's reading
    assert "your words" in html and "suggestion" in html                # where each item came from
    assert 'class="opt on">Dogs only' in html                           # the answer given
    assert "👍" in html and "👎" in html                                 # the papers marked
    assert "Plan agreed after 1 round" in html and "$0.04" in html      # its cost
    assert "Search: 5 records screened, 3 kept, 1 overview, 1 dropped" in html
    assert "1 record set aside without screening" in html
    assert "1 study read into the database" in html


def test_chat_messages_are_kept_in_order_and_shown_as_text(client, db):
    chat.add(db, TOPIC, "vet", "question", {"text": SCRIPT})
    chat.add(db, TOPIC, "event", "done", {"text": "Search finished"}, cost=0.5)
    con = sqlite3.connect(db)
    assert [m["n"] for m in chat.messages(con, TOPIC)] == [1, 2]
    html = client.get(f"/topics/{TOPIC}/chat").text
    assert SCRIPT not in html and "&lt;script&gt;" in html and "$0.50" in html
    with pytest.raises(ValueError):
        chat.add(db, TOPIC, "model", "x", {})                          # only known roles


def test_the_chat_box_waits_for_the_planner(client):
    """B1: the box is shown but switched off; nothing can be sent yet."""
    html = client.get("/?q=Cats+after+spaying").text
    assert re.search(r"<textarea[^>]*\sdisabled[\s>]", html) and "Cats after spaying" in html
    assert re.search(r'<button class="send"[^>]*\sdisabled[\s>]', html)
    assert not [r for r in client.app.routes if getattr(r, "path", "") == "/chat"]


def test_studies_show_clinical_ais_essentials(client, db):
    row = views.studies(db)[0]
    assert {k: row[k] for k in ("status", "species", "intervention", "enrolled", "sites", "kols",
                                "sponsor")} == {
        "status": "COMPLETED", "species": "dogs", "intervention": "analgesic X", "enrolled": "60",
        "sites": 1, "kols": ["Doe J", "Roe R"], "sponsor": "Fictional Pharma"}
    head = client.get("/studies").text
    for col in ("Title", "Status", "Species", "Condition", "Intervention", "Enrolled", "Sites",
                "KOLs", "Sponsor"):
        assert f">{col}</th>" in head, col
    study = client.get("/studies/VET-1").text
    for part in ("Classification", "Sites", "KOLs", "Inclusion criteria", "Exclusion criteria",
                 "Fictional Veterinary Hospital", "Doe J", "first author", "60 enrolled"):
        assert part in study, part
    assert '<td class="nowrap">completed</td>' in head          # never broken mid-word
    assert "<span>Doe J</span><span>Roe R</span>" in head          # KOLs by name
    assert "Doe J · 2020 · Prospective trial" in head              # the year, under the title
    assert "<dt>Sponsor</dt><dd>Fictional Pharma</dd>" in study
    inc, exc = views._criteria("Inclusion Criteria:\n* dogs\nExclusion Criteria:\n* cats")
    assert (inc, exc) == ("* dogs", "* cats")


# the job runner

def test_one_job_at_a_time_and_a_double_click_runs_once(tmp_path):
    db, gate = tmp_path / "v.db", threading.Event()
    runner = chat.Runner(db)

    def work(say):
        say("halfway")
        gate.wait(5)
        return "all done"
    job = runner.start(TOPIC, "search", work, request="click-1")
    assert runner.start(TOPIC, "search", work, request="click-1") == job     # the same click
    with pytest.raises(chat.Busy):
        runner.start(TOPIC, "search", work, request="click-2")
    gate.set()
    runner.wait()
    con = sqlite3.connect(db)
    assert [j["state"] for j in chat.jobs(con)] == ["done"]
    assert [m["kind"] for m in chat.messages(con, TOPIC)] == ["started", "progress", "done"]


def test_a_failing_job_is_recorded_not_raised(tmp_path):
    db = tmp_path / "v.db"
    runner = chat.Runner(db)
    runner.start(TOPIC, "search", lambda say: 1 / 0, request="r")
    runner.wait()
    con = sqlite3.connect(db)
    job = chat.jobs(con)[0]
    assert job["state"] == "failed" and "ZeroDivisionError" in job["detail"]
    assert chat.messages(con, TOPIC)[-1]["kind"] == "failed"
    assert runner.start(TOPIC, "search", lambda say: None, request="r2")    # the next can start


def test_a_job_cut_off_by_a_stop_is_marked_interrupted(tmp_path):
    db = tmp_path / "v.db"
    con = chat.connect(db)
    with con:
        con.execute("INSERT INTO jobs (topic, kind, state, request, started) "
                    "VALUES (?, 'search', 'running', 'r', '2026-11-03')", (TOPIC,))
    assert chat.Runner(db).recover() == 1
    assert chat.jobs(con)[0]["state"] == "interrupted"
    assert chat.messages(con, TOPIC)[-1]["kind"] == "interrupted"
    assert chat.Runner(db).recover() == 0                      # nothing left: nothing written


# reading: every number explained; the vet's words and the expanded search kept

def test_every_count_says_what_it_means(client):
    html = client.get(f"/topics/{TOPIC}/review").text
    for word, meaning in (("found", "articles the search turned up"),
                          ("kept by screening", "on topic, judged from the abstract"),
                          ("approved by you", "in your review; 3 still to review"),
                          ("full text", "approved articles downloaded"),
                          ("in the database", "studies read in, every value cited")):
        assert f"<span>{word}</span><small>{meaning}</small>" in html, word
    assert "What do these numbers mean?" in html
    assert ">results<" not in html                       # a count of numbers means nothing
    assert ">reviews<" not in html and "Review · lead" not in html    # review is your step only
    assert "Overviews &amp; guidelines" in html
    assert "waiting for your decision" in html                         # what this list is


def test_the_vets_words_and_the_expanded_search_are_kept(client, db, tmp_path, monkeypatch):
    from vet import plan
    monkeypatch.setattr(plan, "CORPUS", db.parent)          # the Planner's folders: the test's
    plan.record_input(TOPIC)
    row = sqlite3.connect(db).execute("SELECT words, question, terms FROM topic_inputs").fetchone()
    assert row[0] == PLAN["words"] and "ovariohysterectomy" in row[1]
    assert json.loads(row[2])[0]["term"] == "Dogs"
    con = sqlite3.connect(db)
    with con:
        con.execute("INSERT INTO queries (topic, round, service, query, hits, date) VALUES "
                    "(?, 1, 'pubmed', '(\"Dogs\"[Mesh]) AND (pain[tiab])', 61, '2026-11-03')",
                    (TOPIC,))
    html = client.get(f"/topics/{TOPIC}/review").text
    assert "How this search was built" in html
    assert "Pain relief after spaying in dogs" in html                   # as typed
    assert "Which analgesics and doses relieve pain" in html             # as agreed
    assert "Pain, Postoperative" in html and "this found <b>61</b> records" in html  # expanded, run
    assert "Understood as" in html and "it is not what PubMed was sent" in html
    assert '<p class="and">AND</p>' in html                     # the blocks, as one search
    assert "[Mesh]) AND (pain[tiab])" in html                            # the exact query


def test_older_topic_files_show_their_terms():
    old = {"species": {"mesh": ["Dogs"], "text": ["dog"]}}
    assert views._term_list(old) == [{"block": "species", "kind": "mesh", "term": "Dogs"},
                                     {"block": "species", "kind": "text", "term": "dog"}]


def test_each_record_says_how_it_was_found(client):
    html = client.get(f"/topics/{TOPIC}/review").text
    assert "Found by</span> the PubMed search" in html
    assert views.found_by("references:123") == "the references of PMID 123"
    assert views.found_by("term:PRCA") == 'the term "PRCA", added in a review round'


def test_kols_are_a_papers_first_and_last_authors_or_a_trials_director():
    def person(name, role="author", position=""):
        return {"Name": name, "role": role, "author_position": position}
    paper = [person("First F", position="first"), person("Mid M", position="middle"),
             person("Last L", position="last")]
    key, others = views.kols(paper)
    assert [(p["Name"], p["why"]) for p in key] == [("First F", "first author"),
                                                     ("Last L", "last author")]
    assert [p["Name"] for p in others] == ["Mid M"]
    trial = [person("Helper H", "investigator"), person("Boss B", "study_director"),
             person("Other O", "investigator")]
    assert [(p["Name"], p["why"]) for p in views.kols(trial)[0]] == [("Boss B", "study director")]
    no_director = [person("Lead L", "investigator"), person("Other O", "investigator")]
    assert [p["Name"] for p in views.kols(no_director)[0]] == ["Lead L"]


def test_who_and_where_counts_only_lead_authors(client):
    html = client.get("/who").text
    assert "Fictional University" in html and "Doe J" in html          # a first author
    assert "Helper Institute" not in html                             # a middle author only


def test_the_old_records_address_leads_to_review(client):
    r = client.get(f"/topics/{TOPIC}/records?view=dropped", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"/topics/{TOPIC}/review?view=dropped"


def test_species_read_as_plain_words():
    assert views.species_words("canine|dogs") == "dogs"
    assert views.species_words("dog|feline|cats") == "dogs, cats"
    assert views.species_words("rabbits") == "rabbits"
