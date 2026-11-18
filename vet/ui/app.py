"""The local screens: `vet ui` serves them on this Mac only (http://127.0.0.1:8765).

A chat-first layout: the dashboard of searches on the left; on the right the chat (a new
question, or a search's conversation) or a search's history: its records to review, the
studies built (clinical_ai's essentials), and who and where. A thin layer: every page reads
through vet.views (read-only), the vet's review goes through vet.approve, and the slow or paid
steps (vet.jobs) run in the background through vet.chat.Runner. Those steps are the only things
that reach out, and only when the vet starts one: what the vet typed and the papers' abstracts
go to Gemini (paid tier), and search terms to the literature services. The database stays on
this Mac, and so do the pages:
- the server listens on 127.0.0.1 only, and answers only requests addressed to 127.0.0.1 or
  localhost (a web page elsewhere can't reach it through a renamed address);
- a change (POST) must come from these pages: same origin, and the page's token;
- every page forbids loading anything from outside (Content-Security-Policy: 'self' only);
  htmx, the one script, is kept in static/ (version 2.0.4, hash checked by the tests);
- FastAPI's built-in API pages are off (they would load scripts from a CDN), and so is its
  telemetry;
- text from papers is always escaped, so a title can't run as code.
"""
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fjkit import FjkitConfig, mount_fjkit
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import budget, chat, jobs, plan, views
from ..approve import ACTIONS, set_decision
from ..chat import Busy, Runner
from ..search import connect
from ..settings import CORPUS, DB

HOST = "127.0.0.1"
TELEMETRY_OFF = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False,
                 "auto_configure": False}
PORT = 8765
HOSTS = {"127.0.0.1", "localhost"}
HERE = Path(__file__).parent
CODE = re.compile(r"[a-z]+(?:_[a-z]+)+")          # a codebook value: prospective_trial
# img-src allows data: images: fjkit's icons are SVG inside its CSS; a data: image fetches
# nothing and runs nothing. Scripts stay this app's own files only.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'")
HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
           # same-origin, not no-referrer: under no-referrer Chrome sends "Origin: null" with a
           # plain form's POST, and the guard below refuses it; links out still send nothing
           "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
           "Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"}
DESIGNS = {"randomised_trial": "Randomised trial", "prospective": "Prospective",
           "retrospective": "Retrospective", "case_series": "Case series",
           "single_case": "Single case", "other": "Other", "unclear": "Unclear", "": "—"}
CHECKS = {"condition": "The condition", "animals": "The animals"}
LEVEL_NAMES = {"high": "High confidence", "medium": "Medium confidence", "low": "Low confidence"}
ANSWERS = {"yes": "studied", "related": "a related condition", "no": "not studied",
           "unclear": "unclear", "patients": "patients with the disease",
           "healthy_treated": "healthy animals given a treatment of interest",
           "survey": "a survey of vets or owners", "other_species": "other species only",
           "none": "no animals (laboratory or induced model)"}
STEPS = ["ask: agree the search", "search and screen", "review: your decision",
         "full text: download", "database: read in"]
SOURCES = {"vet": "your words", "answer": "your answer", "paper": "a paper you liked",
           "suggestion": "suggestion", "mesh": "MeSH"}
EXAMPLES = ["Cyclosporine for atopic dermatitis in dogs: doses, and relapse after tapering",
            "Analgesics after ovariohysterectomy in cats: pain scales and rescue rules",
            "Treatment of chronic kidney disease in cats: survival and doses",
            "Immune-mediated thrombocytopenia in dogs: treatment and survival"]
NOTES = {"busy": "Another step is running: wait for it to finish, then try again.",
         "budget_number": "Enter the budget as a number of dollars, such as 2.00.",
         "over_cap": "That budget is more than is left under the spending cap.",
         "too_low": "That budget doesn't cover this step: see what it needs below.",
         "stale": "That page was out of date. This is where the search is now.",
         "last_round": "The Planner has had its last round: agree this search, or ask a new "
                       "question.",
         "revise_first": "Your answers change the search, and only the Planner can redraw it: "
                         "press Revise with my answers, check its new draft, then agree."}
KINDS = {"prospective_trial": "Prospective trial", "retrospective_cohort": "Retrospective cohort",
         "case_series": "Case series", "other": "Other"}


def _day(iso: str) -> str:
    """'2026-11-02…' as 'Nov 2'; anything else as it is."""
    try:
        d = datetime.strptime((iso or "")[:10], "%Y-%m-%d")
    except ValueError:
        return iso or ""
    return f"{d:%b} {d.day}"


def _host(value: str) -> str:
    return urlsplit(f"//{value}").hostname or ""


def create_app(db: Path = DB, corpus: Path = CORPUS, backend=None) -> FastAPI:
    # no API pages (they load scripts from a CDN) and no telemetry: FastAPI's OpenTelemetry
    # support is switched off whole, so no environment variable can turn on an exporter
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, telemetry=TELEMETRY_OFF)
    app.state.token = secrets.token_urlsafe(32)       # new every start; only the pages know it
    # fjkit (UI kit: Jinja macros, Basecoat CSS, htmx) serves its own files from /_fjkit, all
    # local; the same kit and outline as clinical_ai's poc_app. Our templates come first, then the
    # kit's; `fjkit check` keeps ours to its vocabulary.
    kit = mount_fjkit(app, FjkitConfig(template_dir=HERE / "templates", strict_undefined=False,
                                       color_scheme="system"))
    env = kit.env
    env.filters["thousands"] = lambda n: f"{int(n or 0):,}"
    env.filters["usd"] = lambda x: f"${x:,.2f}"
    env.filters["label"] = lambda s: s.replace("_", " ")
    env.filters["plain"] = lambda s: s.replace("_", " ") if CODE.fullmatch(s or "") else s
    env.filters["day"] = _day
    env.globals |= {"designs": DESIGNS, "kinds": KINDS, "flag_names": views.FLAGS,
                    "token": app.state.token, "check_names": CHECKS, "answer_names": ANSWERS,
                    "step_names": STEPS, "level_names": LEVEL_NAMES}
    pages = Jinja2Templates(env=env)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if _host(request.headers.get("host", "")) not in HOSTS:
            return PlainTextResponse("This app only answers on 127.0.0.1.", 400)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if _host(urlsplit(origin).netloc) not in HOSTS:
                return PlainTextResponse("Changes are only accepted from these pages.", 403)
        response = await call_next(request)
        response.headers.update(HEADERS)
        return response

    async def check_token(request: Request):
        form = await request.form()
        if not secrets.compare_digest(request.headers.get("x-csrf") or form.get("csrf") or "",
                                      app.state.token):
            raise HTTPException(403, "The page's token is missing: reload the page.")
        return form

    runner = Runner(db)
    runner.recover()                                   # jobs cut off by a stop: interrupted
    app.state.runner = runner
    tools = jobs.Tools(db, corpus, backend)

    def page(request: Request, template: str, **ctx):
        return pages.TemplateResponse(request, template, {
            "spend": views.spending(corpus), "path": request.url.path,
            "searches": views.searches(db, corpus), "current": "", "sources": SOURCES,
            "request_id": secrets.token_urlsafe(16)} | ctx)

    def topic_or_404(name: str):
        try:
            return views.topic_file(name)
        except KeyError:
            raise HTTPException(404, "No such search.")

    def topic_ctx(name: str) -> dict:
        t = topic_or_404(name)
        s = next(x for x in views.searches(db, corpus) if x["name"] == name)
        return {"name": name, "topic": t, "s": s, "counts": views.counts(db, name),
                "current": name}

    def back(name: str, note: str = "") -> RedirectResponse:
        return RedirectResponse(f"/topics/{name}/chat" + (f"?note={note}" if note else "")
                                + "#now", 303)

    def run(name: str, kind: str, work, form) -> RedirectResponse:
        """Start a step in the background; the click's request ID makes a double click one."""
        try:
            runner.start(name, kind, work, form.get("request", "")[:64] or secrets.token_urlsafe(16))
        except Busy:
            return back(name, "busy")
        return back(name)

    def set_budget(name: str, p: dict, form) -> str:
        """Save the budget the form sent; the note's name when it can't be used."""
        why = views.budget_problem(form.get("budget"), p)
        if not why:
            con = connect(db)
            try:
                budget.set_budget(con, name, float(form.get("budget")))
            finally:
                con.close()
        return why

    @app.get("/", response_class=HTMLResponse)
    def new_question(request: Request, q: str = "", note: str = ""):
        return page(request, "home/page.html", examples=EXAMPLES, q=q[:2000], note=NOTES.get(note, ""))

    @app.post("/chat")
    async def ask(request: Request):
        """A new question: a new search, and the Planner's first round in the background."""
        form = await check_token(request)
        words = " ".join(form.get("text", "").split())[:2000]
        if not words:
            return RedirectResponse("/", 303)
        same = chat.topic_of(db, form.get("request", ""))
        if same:                                       # a double click: the search it started
            return back(same)
        if runner.running():
            return RedirectResponse(f"/?note=busy&q={quote(words)}", 303)
        name = plan.open_topic(words)
        return run(name, "Planner", jobs.plan_first(tools, name, words), form)

    @app.get("/topics/{name}", response_class=HTMLResponse)
    def topic(request: Request, name: str):
        """A search opens on what it has built: studies, else its records, else the chat."""
        c = topic_ctx(name)["counts"]
        if c["studies"]:
            return topic_studies(request, name)
        if c["keep"] or c["lead"] or c["off_topic"]:
            return review(request, name)
        return topic_chat(request, name)

    @app.get("/topics/{name}/chat", response_class=HTMLResponse)
    def topic_chat(request: Request, name: str, note: str = ""):
        ctx = topic_ctx(name)
        return page(request, "chat/page.html", items=views.history(db, corpus, name),
                    p=views.panel(db, corpus, name), note=NOTES.get(note, ""), **ctx)

    @app.get("/topics/{name}/live", response_class=HTMLResponse)
    def live(request: Request, name: str):
        """Polled while a step runs: its latest progress, or a reload when it has ended."""
        topic_or_404(name)
        p = views.panel(db, corpus, name)
        if not p["job"]:
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        return pages.TemplateResponse(request, "chat/_live.html", {
            "name": name, "p": p, "lines": views.progress(db, p["job"])})

    @app.post("/topics/{name}/plan")
    async def planner(request: Request, name: str):
        """The Planner's form: try the first round again, revise, or agree the search."""
        t = topic_or_404(name)
        form = await check_token(request)
        p = views.panel(db, corpus, name)
        go = form.get("go", "")
        if go == "retry" and p["stage"] == "retry":
            return run(name, "Planner", jobs.plan_first(tools, name, t["question"]), form)
        state = views.plan_state(corpus, name)
        if p["stage"] != "planning" or form.get("round") != str(len(state.get("rounds", []))):
            return back(name, "stale")
        answers = plan.answers_from(state, lambda field: form.get(field, ""))
        if go == "agree":
            # agreeing keeps the draft as shown: answers to its questions (or a note) would be
            # lost, so they go through Revise, unless no round is left
            if answers["question"] and not p["last_round"]:
                return back(name, "revise_first")
            return run(name, "Agreeing the search", jobs.agree(tools, name, answers), form)
        if go == "revise":
            if p["last_round"]:
                return back(name, "last_round")
            return run(name, "Planner", jobs.plan_next(tools, name, answers), form)
        raise HTTPException(400, "Unknown step.")

    @app.post("/topics/{name}/search")
    async def search(request: Request, name: str):
        """Start the search with its budget, or carry on a round that was cut off."""
        topic_or_404(name)
        form = await check_token(request)
        p = views.panel(db, corpus, name)
        if p["stage"] not in ("ready", "paused"):
            return back(name, "stale")
        why = set_budget(name, p, form)
        return back(name, why) if why else run(name, "search", jobs.carry_on(tools, name), form)

    @app.post("/topics/{name}/widen")
    async def widen(request: Request, name: str):
        """The vet's decision: one more round, the one they read. Nothing else starts a round."""
        topic_or_404(name)
        form = await check_token(request)
        p = views.panel(db, corpus, name)
        if p["stage"] != "decide" or not p["next"]["steps"] \
                or form.get("round") != str(p["next"]["round"]):
            return back(name, "stale")
        why = set_budget(name, p, form)
        return back(name, why) if why else run(name, "widening", jobs.widen(tools, name), form)

    @app.post("/topics/{name}/propose")
    async def propose(request: Request, name: str):
        """Ask what the next round would do (one review call); nothing is run yet."""
        topic_or_404(name)
        form = await check_token(request)
        p = views.panel(db, corpus, name)
        if p["stage"] != "ask":
            return back(name, "stale")
        why = set_budget(name, p, form)
        return back(name, why) if why else run(name, "proposal", jobs.ask_next(tools, name), form)

    @app.get("/topics/{name}/records")
    def records_moved(request: Request, name: str):
        topic_or_404(name)
        q = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/topics/{name}/review{q}", 307)

    @app.get("/topics/{name}/review", response_class=HTMLResponse)
    def review(request: Request, name: str, view: str = "", q: str = "", flag: str = "",
               design: str = "", page_no: int = 1, level: str = ""):
        """Step 3: the vet reviews what the search found. Opens on what waits for a decision,
        or on the approved list when nothing does."""
        ctx = topic_ctx(name)
        if view not in views.VIEWS:
            view = "to_review" if ctx["counts"]["to_review"] else "approved"
        ctx |= {"q": q, "flag": flag, "design": design, "level": level, "rules": views.RULES,
                "data": views.records(db, name, view, q, flag, design, page_no, level),
                "b": views.built(db, corpus, name)}
        ctx["considered_waiting"] = len(views.keys_in(db, name, "to_review", views.CONSIDERED)) \
            if ctx["data"]["tabs"]["to_review"] else 0
        if request.headers.get("hx-request") and request.headers.get("hx-target") == "records":
            return page(request, "review/_records.html", **ctx)
        return page(request, "review/page.html", **ctx)

    @app.get("/topics/{name}/studies", response_class=HTMLResponse)
    def topic_studies(request: Request, name: str):
        ctx = topic_ctx(name)
        return page(request, "studies/topic.html", rows=views.studies(db, name), **ctx)

    @app.get("/topics/{name}/who", response_class=HTMLResponse)
    def topic_who(request: Request, name: str):
        ctx = topic_ctx(name)
        return page(request, "who/topic.html", rows=views.institutions(db, name), **ctx)

    @app.post("/topics/{name}/decide", response_class=HTMLResponse)
    async def decide(request: Request, name: str):
        """The vet's review: approve (or keep anyway), reject, or undo; one record, or a whole
        list (all=1: approve every record waiting for review, or those of one confidence level,
        or undo every approval)."""
        topic_or_404(name)
        form = await check_token(request)
        action = form.get("action", "")
        if action not in ACTIONS:
            raise HTTPException(400, "Unknown decision.")
        if form.get("all") == "1":
            whole = {"approve": "to_review", "undo": "approved"}.get(action)
            if whole is None:
                raise HTTPException(400, "Only approving or undoing can apply to a whole list.")
            level = form.get("level", "")
            if level and level not in (*views.LEVELS, views.CONSIDERED):
                raise HTTPException(400, "Unknown confidence level.")
            keys = views.keys_in(db, name, whole, level)
        else:
            keys = form.getlist("key")
        con = sqlite3.connect(db, timeout=10)
        try:
            set_decision(name, con, list(keys), action)
        finally:
            con.close()
        if request.headers.get("hx-request") and form.get("all") != "1" and len(keys) == 1:
            r = views.record(db, name, keys[0])
            if r is None:
                raise HTTPException(404, "No such record.")
            return page(request, "review/_record.html", name=name, r=r, rules=views.RULES)
        view = form.get("view") if form.get("view") in views.VIEWS else ""
        back = f"/topics/{name}/review" + (f"?view={view}" if view else "")
        if request.headers.get("hx-request"):          # htmx reloads the page itself
            return HTMLResponse("", headers={"HX-Redirect": back})
        return RedirectResponse(back, 303)

    @app.get("/studies", response_class=HTMLResponse)
    def studies(request: Request):
        return page(request, "studies/page.html", rows=views.studies(db))

    @app.get("/studies/{sid}", response_class=HTMLResponse)
    def study(request: Request, sid: str):
        s = views.study(db, sid)
        if s is None:
            raise HTTPException(404, "No such study.")
        return page(request, "studies/study.html", s=s, current=(s["topics"] or [""])[0])

    @app.get("/who", response_class=HTMLResponse)
    def who(request: Request):
        return page(request, "who/page.html", rows=views.institutions(db))

    @app.get("/spending", response_class=HTMLResponse)
    def spending(request: Request):
        return page(request, "spending/page.html")

    @app.exception_handler(StarletteHTTPException)     # also unknown addresses (404)
    async def problem(request: Request, exc: StarletteHTTPException):
        r = page(request, "problem/page.html", status=exc.status_code, detail=exc.detail)
        r.status_code = exc.status_code
        return r

    return app
