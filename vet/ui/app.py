"""The local screens: `vet ui` serves them on this Mac only (http://127.0.0.1:8765).

A chat-first layout: the dashboard of searches on the left; on the right the chat (a new
question, or a search's conversation) or a search's history: its records to approve, the
studies built (clinical_ai's essentials), and who and where. A thin layer: every page calls
vet.views (read-only), the one action so far, approving records, calls
vet.approve.set_approved, and slow or paid steps will run through vet.chat.Runner. Nothing here talks to a model or the internet, and nothing
leaves the Mac:
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
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import views
from ..approve import ACTIONS, set_decision
from ..chat import Runner
from ..settings import CORPUS, DB

HOST = "127.0.0.1"
TELEMETRY_OFF = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False,
                 "auto_configure": False}
PORT = 8765
HOSTS = {"127.0.0.1", "localhost"}
HERE = Path(__file__).parent
CODE = re.compile(r"[a-z]+(?:_[a-z]+)+")          # a codebook value: prospective_trial
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'")
HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
           "Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"}
DESIGNS = {"randomised_trial": "Randomised trial", "prospective": "Prospective",
           "retrospective": "Retrospective", "case_series": "Case series",
           "single_case": "Single case", "other": "Other", "unclear": "Unclear", "": "—"}
CHECKS = {"condition": "The condition", "animals": "The animals"}
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
            "Mycophenolate in dogs with immune-mediated disease: adverse effects"]
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


def create_app(db: Path = DB, corpus: Path = CORPUS) -> FastAPI:
    # no API pages (they load scripts from a CDN) and no telemetry: FastAPI's OpenTelemetry
    # support is switched off whole, so no environment variable can turn on an exporter
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, telemetry=TELEMETRY_OFF)
    app.state.token = secrets.token_urlsafe(32)       # new every start; only the pages know it
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["thousands"] = lambda n: f"{int(n or 0):,}"
    env.filters["usd"] = lambda x: f"${x:,.2f}"
    env.filters["label"] = lambda s: s.replace("_", " ")
    env.filters["plain"] = lambda s: s.replace("_", " ") if CODE.fullmatch(s or "") else s
    env.filters["day"] = _day
    env.globals |= {"designs": DESIGNS, "kinds": KINDS, "flag_names": views.FLAGS,
                    "token": app.state.token, "check_names": CHECKS, "answer_names": ANSWERS,
                    "step_names": STEPS}
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

    def page(request: Request, template: str, **ctx):
        return pages.TemplateResponse(request, template, {
            "spend": views.spending(corpus), "path": request.url.path,
            "searches": views.searches(db, corpus), "current": "", "chat_enabled": False,
            "sources": SOURCES} | ctx)

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

    @app.get("/", response_class=HTMLResponse)
    def new_question(request: Request, q: str = ""):
        return page(request, "new.html", examples=EXAMPLES, q=q[:2000])

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
    def topic_chat(request: Request, name: str):
        ctx = topic_ctx(name)
        return page(request, "chat.html", items=views.history(db, corpus, name), **ctx)

    @app.get("/topics/{name}/records")
    def records_moved(request: Request, name: str):
        topic_or_404(name)
        q = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/topics/{name}/review{q}", 307)

    @app.get("/topics/{name}/review", response_class=HTMLResponse)
    def review(request: Request, name: str, view: str = "", q: str = "", flag: str = "",
               design: str = "", page_no: int = 1):
        """Step 3: the vet reviews what the search found. Opens on what waits for a decision,
        or on the approved list when nothing does."""
        ctx = topic_ctx(name)
        if view not in views.VIEWS:
            view = "to_review" if ctx["counts"]["to_review"] else "approved"
        ctx |= {"q": q, "flag": flag, "design": design, "rules": views.RULES,
                "data": views.records(db, name, view, q, flag, design, page_no),
                "b": views.built(db, corpus, name)}
        if request.headers.get("hx-request") and request.headers.get("hx-target") == "records":
            return page(request, "_records.html", **ctx)
        return page(request, "review.html", **ctx)

    @app.get("/topics/{name}/studies", response_class=HTMLResponse)
    def topic_studies(request: Request, name: str):
        ctx = topic_ctx(name)
        return page(request, "topic_studies.html", rows=views.studies(db, name), **ctx)

    @app.get("/topics/{name}/who", response_class=HTMLResponse)
    def topic_who(request: Request, name: str):
        ctx = topic_ctx(name)
        return page(request, "topic_who.html", rows=views.institutions(db, name), **ctx)

    @app.post("/topics/{name}/decide", response_class=HTMLResponse)
    async def decide(request: Request, name: str):
        """The vet's review: approve (or keep anyway), reject, or undo; one record, or a whole
        list (all=1: approve every record waiting for review, or undo every approval)."""
        topic_or_404(name)
        form = await check_token(request)
        action = form.get("action", "")
        if action not in ACTIONS:
            raise HTTPException(400, "Unknown decision.")
        if form.get("all") == "1":
            whole = {"approve": "to_review", "undo": "approved"}.get(action)
            if whole is None:
                raise HTTPException(400, "Only approving or undoing can apply to a whole list.")
            keys = views.keys_in(db, name, whole)
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
            return page(request, "_record.html", name=name, r=r, rules=views.RULES)
        view = form.get("view") if form.get("view") in views.VIEWS else ""
        back = f"/topics/{name}/review" + (f"?view={view}" if view else "")
        if request.headers.get("hx-request"):          # htmx reloads the page itself
            return HTMLResponse("", headers={"HX-Redirect": back})
        return RedirectResponse(back, 303)

    @app.get("/studies", response_class=HTMLResponse)
    def studies(request: Request):
        return page(request, "studies.html", rows=views.studies(db))

    @app.get("/studies/{sid}", response_class=HTMLResponse)
    def study(request: Request, sid: str):
        s = views.study(db, sid)
        if s is None:
            raise HTTPException(404, "No such study.")
        return page(request, "study.html", s=s, current=(s["topics"] or [""])[0])

    @app.get("/who", response_class=HTMLResponse)
    def who(request: Request):
        return page(request, "who.html", rows=views.institutions(db))

    @app.get("/spending", response_class=HTMLResponse)
    def spending(request: Request):
        return page(request, "spending.html")

    @app.exception_handler(StarletteHTTPException)     # also unknown addresses (404)
    async def problem(request: Request, exc: StarletteHTTPException):
        r = page(request, "problem.html", status=exc.status_code, detail=exc.detail)
        r.status_code = exc.status_code
        return r

    return app
