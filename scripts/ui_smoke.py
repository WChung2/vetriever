"""The screens for real: start `vet ui` on a made-up database, as a separate process, and check
what the unit tests can't (they call the app in-process, with the server and browser left out).

  1. it starts, and every page answers, with its security headers and no error page
  2. it listens on 127.0.0.1 only: the machine's network address is refused
  3. htmx, the one script, is served and is the published release
  4. in a real browser (headless Chrome, when installed) no page has a blocked style or script
     or a script error: the Planner's form, the budget form, the widen panel, and a running
     step's live progress (which polls the server every two seconds)
  5. clicking Next from the bottom of a long list lands at the top of the next page, with the
     header still on screen (a real click: only a browser can show where a page ends up)
  6. a second `vet ui` on the same port stops, before opening any browser
  7. a plain form's POST from a page is accepted (the Send box and the Planner's forms are plain
     forms): Chrome sends their Origin by the page's referrer policy, and the guard checks it

Only the process this script starts is stopped. Made-up data only, in a temporary folder; nothing
leaves the machine (Chrome runs with its background services off and a throwaway profile).
Run by scripts/check.sh (step 6), so locally and in CI alike:  uv run python scripts/ui_smoke.py
"""
import asyncio
import base64
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import websockets

from tests.test_ui import HTMX_SHA384, PLAN, TOPIC, answers, make_db
from vet import chat

PLANNING, READY = "imha", "cie"       # topic files that exist; made-up states in the temp folder
LONG_LIST = 60
WHERE = """(function(){const r=document.scrollingElement,bar=document.querySelector('#topbar');
return {page:document.querySelector('#records p').innerText,
list:Math.round(document.querySelector('#records').getBoundingClientRect().top),
head:Math.round(bar.getBoundingClientRect().top),
headEnd:Math.round(bar.getBoundingClientRect().bottom),
root:Math.round(r.scrollTop),window:innerHeight}})()"""
PROPOSAL = {"answer": {"reasoning": "FIXTURE", "nothing_left": False, "extra_queries": [],
                       "follow": [{"key": "1001", "how": "references"}],
                       "term_changes": [{"action": "add", "term": "FIXTURE term"}]}, "message": ""}
PAGES = ["/", "/?q=cats", f"/topics/{TOPIC}", f"/topics/{TOPIC}/chat", f"/topics/{PLANNING}/chat",
         f"/topics/{READY}/chat", f"/topics/{TOPIC}/review",
         f"/topics/{TOPIC}/review?view=dropped", f"/topics/{TOPIC}/studies",
         f"/topics/{TOPIC}/who", "/studies", "/studies/VET-1", "/who", "/spending"]
CHROME = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "google-chrome",
          "google-chrome-stable", "chromium", "chromium-browser"]


def stages(db: Path):
    """The chat's stages, on made-up data: a Planner waiting, a search ready, a proposal waiting."""
    state = db.parent / "plan" / PLANNING / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(PLAN | {"started": ""}), "utf-8")
    con = sqlite3.connect(db)
    with con:
        for i in range(LONG_LIST):            # more than one page of records to review
            key = f"long{i}"
            con.execute("INSERT INTO records (key, title, abstract, year) VALUES (?, ?, ?, '2020')",
                        (key, f"FIXTURE record {i}", "FIXTURE dogs after surgery, client-owned dogs"))
            con.execute("INSERT INTO screening (topic, key, decision, rule, flags, summary, design, "
                        "answers, round, date) VALUES (?, ?, 'keep', '', '', 's', 'retrospective', "
                        "?, 1, '2026-11-03')", (TOPIC, key, json.dumps(answers())))
        con.execute("INSERT INTO search_state VALUES (?, 2.0, '2026-11-03T00:00:00', 2, ?)",
                    (TOPIC, json.dumps(PROPOSAL)))
        con.execute("INSERT INTO search_rounds VALUES (?, 1, '2026-11-03T09:00:00', "
                    "'2026-11-03T10:00:00', 3)", (TOPIC,))
    con.close()


def job(db: Path, state: str):
    """A step running on the READY topic (only its row: nothing runs), or no step."""
    con = chat.connect(db)
    with con:
        if state == "running":
            con.execute("INSERT INTO jobs (topic, kind, state, request, started) VALUES "
                        "(?, 'search', 'running', 'smoke', '2026-11-03T09:00:00')", (READY,))
            con.execute("INSERT INTO chat (topic, n, role, kind, body, time) VALUES (?, 1, "
                        "'event', 'progress', ?, '2026-11-03T09:00:00')",
                        (READY, json.dumps({"text": "screening 3 records", "job": 1})))
        else:
            con.execute("UPDATE jobs SET state = 'done'")
    con.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def network_address() -> str | None:
    """This machine's address on its network (no packet is sent), or None without one."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))           # a documentation address: nothing is sent
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


def get(url: str, host: str | None = None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def chrome() -> str | None:
    for c in CHROME:
        if Path(c).exists() or shutil.which(c):
            return c if Path(c).exists() else shutil.which(c)
    return None


def browser_problems(exe: str, url: str, profile: Path) -> list[str]:
    """Console lines of a blocked style or script, a script error or a failed load."""
    r = subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", "--disable-background-networking",
                        "--disable-component-update", "--disable-sync", "--disable-extensions",
                        "--disable-default-apps", "--no-sandbox", f"--user-data-dir={profile}",
                        "--enable-logging=stderr", "--v=0", "--virtual-time-budget=3000",
                        "--dump-dom", url], capture_output=True, text=True, timeout=60,
                       check=False)
    return [line for line in r.stderr.splitlines() if "CONSOLE" in line and any(
        w in line for w in ("violates", "Refused", "Uncaught", "Failed to load", "Error"))]


def in_chrome(exe: str, profile: Path, steps):
    """Run `steps(run)` in a real Chrome driven through its debugging port, on this machine
    only; `run(code)` evaluates JavaScript in the page, `run.call(method, **params)` sends a
    DevTools command. Returns what `steps` returns."""
    port = free_port()
    browser = subprocess.Popen([exe, "--headless=new", "--disable-gpu", "--no-first-run",
                                "--no-default-browser-check", "--disable-background-networking",
                                "--disable-component-update", "--disable-sync", "--no-sandbox",
                                "--disable-extensions", f"--user-data-dir={profile}",
                                f"--remote-debugging-port={port}", "--window-size=1280,900",
                                "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def drive() -> dict:
        for _ in range(80):
            try:
                tabs = json.loads(get(f"http://127.0.0.1:{port}/json")[2])
                break
            except OSError:
                await asyncio.sleep(0.25)
        target = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
        async with websockets.connect(target, max_size=None) as ws:
            sent = 0

            async def call(method: str, **params) -> dict:
                nonlocal sent
                sent += 1
                await ws.send(json.dumps({"id": sent, "method": method, "params": params}))
                while True:
                    answer = json.loads(await ws.recv())
                    if answer.get("id") == sent:
                        return answer.get("result", {})

            async def run(code: str):
                got = await call("Runtime.evaluate", expression=code, returnByValue=True)
                return got["result"].get("value")
            run.call = call
            await call("Page.enable")
            return await steps(run)
    try:
        return asyncio.run(asyncio.wait_for(drive(), 40))
    finally:
        browser.terminate()                       # only the browser this script started
        try:
            browser.wait(10)
        except subprocess.TimeoutExpired:
            browser.kill()


def next_from_the_bottom(exe: str, url: str, profile: Path) -> dict:
    """Open a long list, scroll to its bottom, click Next, and say where the page ended up."""
    async def steps(run):
        await run.call("Page.navigate", url=url)
        await asyncio.sleep(1.5)
        # instantly, as a person scrolling does: the page's CSS would animate a scripted scroll
        await run("window.scrollTo({top: document.scrollingElement.scrollHeight, "
                  "behavior: 'instant'})")
        await asyncio.sleep(0.2)
        await run("[...document.querySelectorAll('nav[aria-label=\"Pages\"] a')]"
                  ".find(a=>a.innerText.includes('Next')).click()")
        await asyncio.sleep(1.5)
        return await run(WHERE)
    return in_chrome(exe, profile, steps)


def plain_form_sent(exe: str, url: str, profile: Path, key: str) -> str:
    """On a page, send a plain form (not htmx: a navigation POST, as the Send box and the
    Planner's forms are) approving one made-up record; the text of the page it lands on."""
    async def steps(run):
        await run.call("Page.navigate", url=url)
        await asyncio.sleep(1.5)
        await run("""(function(){const f=document.createElement('form');f.method='post';
          f.action=location.pathname.replace(/review$/,'decide');
          for (const [n,v] of [['csrf',document.querySelector('input[name=csrf]').value],
              ['action','approve'],['key','""" + key + """']]){
            const i=document.createElement('input');i.type='hidden';i.name=n;i.value=v;f.append(i);}
          document.body.append(f);f.submit();})()""")
        await asyncio.sleep(1.5)
        return await run("document.body.innerText")
    return in_chrome(exe, profile, steps)


def main() -> int:
    failures = []

    def check(ok: bool, what: str):
        print(("  ok    " if ok else "  FAIL  ") + what)
        if not ok:
            failures.append(what)

    work = Path(tempfile.mkdtemp(prefix="vet-ui-smoke-"))
    db = make_db(work)
    stages(db)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = open(work / "server.log", "w+")  # noqa: SIM115 (closed in the finally below)
    server = subprocess.Popen([sys.executable, "-m", "vet.cli", "--db", str(db), "ui", "--port",
                               str(port), "--no-browser"], cwd=ROOT, stdout=log, stderr=log,
                              env=os.environ | {"PYTHONUNBUFFERED": "1"})
    try:
        for _ in range(60):
            if server.poll() is not None:
                break
            try:
                get(base + "/")
                break
            except OSError:
                time.sleep(0.25)
        log.seek(0)
        started = server.poll() is None
        check(started, f"vet ui started (PID {server.pid}, port {port})")
        if not started:
            print(log.read())
            return 1

        print("1. every page")
        for path in PAGES:
            status, headers, body = get(base + path)
            csp = headers.get("content-security-policy", "")
            check(status == 200 and "default-src 'none'" in csp and b"Traceback" not in body,
                  f"{status} {path}")

        print("2. this machine only")
        check(get(base + "/", host="evil.example")[0] == 400, "another host name is refused")
        ip = network_address()
        if ip:
            try:
                socket.create_connection((ip, port), timeout=2).close()
                reached = True
            except OSError:
                reached = False
            check(not reached, f"the network address {ip} is refused")
        else:
            print("  skip  no network address on this machine")

        print("3. htmx")
        status, _, js = get(base + "/_fjkit/vendor/htmx/htmx.min.js")    # fjkit's copy, every page
        check(status == 200 and base64.b64encode(hashlib.sha384(js).digest()).decode()
              == HTMX_SHA384, "served, and the published release")

        print("4. in a real browser")
        exe = chrome()
        if exe:
            for path in PAGES:
                problems = browser_problems(exe, base + path, work / "chrome")
                check(not problems, f"{path} loads with nothing blocked")
                for p in problems[:3]:
                    print("        " + p[-240:])
            job(db, "running")
            problems = browser_problems(exe, f"{base}/topics/{READY}/chat", work / "chrome")
            status, _, live = get(f"{base}/topics/{READY}/live")
            check(not problems and status == 200 and b"screening 3 records" in live,
                  "a running step's progress is polled with nothing blocked")
            for p in problems[:3]:
                print("        " + p[-240:])
            job(db, "done")
            check("hx-refresh" in {k.lower() for k in get(f"{base}/topics/{READY}/live")[1]},
                  "the page is told to reload when the step ends")
            print("5. Next, from the bottom of a long list")
            end = next_from_the_bottom(exe, f"{base}/topics/{TOPIC}/review?view=to_review",
                                       work / "chrome-next")
            check("page 2" in end["page"], "the next page opens")
            check(end["head"] == 0 and end["root"] > 0, "the header stays on screen")
            check(end["headEnd"] <= end["list"] <= end["window"] // 3,
                  f"the list starts at the top of the window ({end['list']}px down)")
            print("7. a plain form sent from a page")
            landed = plain_form_sent(exe, f"{base}/topics/{TOPIC}/review?view=to_review",
                                     work / "chrome-form", "long0")
            con = sqlite3.connect(db)
            approved = con.execute("SELECT approved FROM screening WHERE key = 'long0'").fetchone()[0]
            con.close()
            check("only accepted from these pages" not in landed and bool(approved),
                  "its POST is accepted (the browser sends the page's Origin)")
        else:
            print("  skip  no Chrome or Chromium installed")

        print("6. a taken port")
        second = subprocess.run([sys.executable, "-m", "vet.cli", "--db", str(db), "ui",
                                 "--port", str(port)], cwd=ROOT, capture_output=True, text=True,
                                timeout=30, check=False)
        check(second.returncode != 0 and "used by another program" in second.stderr,
              "a second vet ui on the same port stops with a message")
    finally:
        server.terminate()                        # only the process this script started
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)
    print("ok" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
