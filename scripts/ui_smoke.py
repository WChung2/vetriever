"""The screens for real: start `vet ui` on a made-up database, as a separate process, and check
what the unit tests can't (they call the app in-process, with the server and browser left out).

  1. it starts, and every page answers, with its security headers and no error page
  2. it listens on 127.0.0.1 only: the machine's network address is refused
  3. htmx, the one script, is served and is the published release
  4. in a real browser (headless Chrome, when installed) no page has a blocked style or script
     or a script error
  5. a second `vet ui` on the same port stops, before opening any browser

Only the process this script starts is stopped. Made-up data only, in a temporary folder; nothing
leaves the machine (Chrome runs with its background services off and a throwaway profile).
Run by scripts/check.sh (step 6), so locally and in CI alike:  uv run python scripts/ui_smoke.py
"""
import base64
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_ui import HTMX_SHA384, TOPIC, make_db

PAGES = ["/", "/?q=cats", f"/topics/{TOPIC}", f"/topics/{TOPIC}/chat", f"/topics/{TOPIC}/review",
         f"/topics/{TOPIC}/review?view=dropped", f"/topics/{TOPIC}/studies",
         f"/topics/{TOPIC}/who", "/studies", "/studies/VET-1", "/who", "/spending"]
CHROME = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "google-chrome",
          "google-chrome-stable", "chromium", "chromium-browser"]


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


def main() -> int:
    failures = []

    def check(ok: bool, what: str):
        print(("  ok    " if ok else "  FAIL  ") + what)
        if not ok:
            failures.append(what)

    work = Path(tempfile.mkdtemp(prefix="vet-ui-smoke-"))
    db = make_db(work)
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
        status, _, js = get(base + "/static/htmx.min.js")
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
        else:
            print("  skip  no Chrome or Chromium installed")

        print("5. a taken port")
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
