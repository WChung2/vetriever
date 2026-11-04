"""The chat behind the screens: a topic's messages and its background jobs, in the database.

- **Messages** (table `chat`) are one topic's conversation, in order: the vet's words, the
  Planner's rounds, and events such as a step starting or ending, with what it cost.
- **Jobs** (table `jobs`) are the slow or paid steps, run one at a time in a background
  thread so the screens stay usable. A job carries the request ID of the click that started
  it, so a double click runs it once. A job still marked running when the app starts again
  was cut off, and is marked interrupted.

Nothing here calls a model: a job's work is a function handed in by the caller.
"""
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (topic TEXT NOT NULL, n INTEGER NOT NULL, role TEXT NOT NULL,
  kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '{}', cost REAL NOT NULL DEFAULT 0,
  time TEXT NOT NULL, PRIMARY KEY (topic, n));
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
  kind TEXT NOT NULL, state TEXT NOT NULL, request TEXT NOT NULL UNIQUE, started TEXT NOT NULL,
  ended TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
"""
ROLES = {"vet", "planner", "event"}


class Busy(RuntimeError):
    """Another job is running: one at a time."""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=10)
    con.executescript(SCHEMA)
    return con


def add(db: Path, topic: str, role: str, kind: str, body: dict, cost: float = 0.0) -> int:
    """Append a message to a topic's conversation; returns its number."""
    if role not in ROLES:
        raise ValueError(f"role {role!r}: one of {sorted(ROLES)}")
    con = connect(db)
    try:
        with con:
            n = con.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM chat WHERE topic = ?",
                            (topic,)).fetchone()[0]
            con.execute("INSERT INTO chat (topic, n, role, kind, body, cost, time) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (topic, n, role, kind, json.dumps(body, ensure_ascii=False), cost, now()))
    finally:
        con.close()
    return n


def messages(con: sqlite3.Connection, topic: str) -> list[dict]:
    """A topic's messages in order (on any connection, a read-only one too)."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'chat'").fetchone():
        return []
    return [{"n": n, "role": role, "kind": kind, "body": json.loads(body), "cost": cost,
             "time": time} for n, role, kind, body, cost, time in con.execute(
        "SELECT n, role, kind, body, cost, time FROM chat WHERE topic = ? ORDER BY n", (topic,))]


def jobs(con: sqlite3.Connection, topic: str = "", state: str = "") -> list[dict]:
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'jobs'").fetchone():
        return []
    where, args = [], []
    for col, value in (("topic", topic), ("state", state)):
        if value:
            where.append(f"{col} = ?")
            args.append(value)
    sql = "SELECT id, topic, kind, state, request, started, ended, detail FROM jobs"
    sql += f" WHERE {' AND '.join(where)}" if where else ""
    cols = ("id", "topic", "kind", "state", "request", "started", "ended", "detail")
    return [dict(zip(cols, r)) for r in con.execute(sql + " ORDER BY id", args)]


class Runner:
    """Runs one job at a time in a background thread."""

    def __init__(self, db: Path):
        self.db = Path(db)
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def recover(self) -> int:
        """Mark jobs left running by a stopped app as interrupted; returns how many. Writes
        nothing when there are none (opening the screens must not change the database)."""
        if not self.db.exists():
            return 0
        con = sqlite3.connect(self.db, timeout=10)
        try:
            left = jobs(con, state="running")
            if not left:
                return 0
            with con:
                con.execute("UPDATE jobs SET state = 'interrupted', ended = ? WHERE state = "
                            "'running'", (now(),))
        finally:
            con.close()
        for j in left:
            add(self.db, j["topic"], "event", "interrupted",
                {"text": f"{j['kind']} was cut off when the app stopped", "job": j["id"]})
        return len(left)

    def running(self) -> dict | None:
        if not self.db.exists():
            return None
        con = sqlite3.connect(self.db, timeout=10)
        try:
            left = jobs(con, state="running")
        finally:
            con.close()
        return left[0] if left else None

    def start(self, topic: str, kind: str, work: Callable[[Callable], object],
              request: str) -> int:
        """Start `work(say)` in the background, where say(text, **more) adds a progress message.
        The same request (a double click) returns the job it already started."""
        with self.lock:
            con = connect(self.db)
            try:
                same = con.execute("SELECT id FROM jobs WHERE request = ?", (request,)).fetchone()
                if same:
                    return same[0]
                if jobs(con, state="running"):
                    raise Busy("another step is running: wait for it to finish")
                with con:
                    job = con.execute("INSERT INTO jobs (topic, kind, state, request, started) "
                                      "VALUES (?, ?, 'running', ?, ?)",
                                      (topic, kind, request, now())).lastrowid
            finally:
                con.close()
            add(self.db, topic, "event", "started", {"text": f"{kind} started", "job": job})
            self.thread = threading.Thread(target=self._run, args=(job, topic, kind, work),
                                           name=f"vet-job-{job}", daemon=True)
            self.thread.start()
            return job

    def _run(self, job: int, topic: str, kind: str, work: Callable):
        def say(text: str, **more):
            add(self.db, topic, "event", "progress", {"text": text, "job": job, **more})
        state, detail = "done", ""
        try:
            out = work(say)
            detail = out if isinstance(out, str) else ""
        except Exception as e:  # noqa: BLE001 (any error goes into the chat, not a crash)
            state, detail = "failed", f"{type(e).__name__}: {str(e)[:300]}"
        con = connect(self.db)
        try:
            with con:
                con.execute("UPDATE jobs SET state = ?, ended = ?, detail = ? WHERE id = ?",
                            (state, now(), detail, job))
        finally:
            con.close()
        add(self.db, topic, "event", state,
            {"text": f"{kind} {'finished' if state == 'done' else 'failed'}"
                     + (f": {detail}" if detail else ""), "job": job})

    def wait(self, timeout: float = 10.0):
        if self.thread:
            self.thread.join(timeout)
