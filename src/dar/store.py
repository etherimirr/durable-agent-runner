"""Durable state for runs, steps and events, on SQLite in WAL mode.

Every state change and the event that describes it are written in one
transaction, so a reader of the event log never sees a state that the log
does not explain.

Workers hold a run through a lease. Each claim bumps `attempt`, which acts as
a fencing token: every write a worker makes is conditional on it still being
the owner at that attempt. A worker that was paused past its lease and then
wakes up cannot overwrite the work of the worker that replaced it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "failed", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL,
    job              TEXT NOT NULL,
    status           TEXT NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 0,
    lease_owner      TEXT,
    lease_expires    REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
-- At most one queued or running run per thread.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_thread
    ON runs(thread_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS runs_claimable ON runs(status, lease_expires, created_at);

CREATE TABLE IF NOT EXISTS steps (
    run_id      TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    output      TEXT,
    started_at  REAL,
    finished_at REAL,
    PRIMARY KEY (run_id, idx)
);

CREATE TABLE IF NOT EXISTS events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    type   TEXT NOT NULL,
    data   TEXT NOT NULL,
    ts     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, seq);
"""


class LeaseLost(Exception):
    """The caller no longer owns the run. It must stop and write nothing."""


@dataclass(frozen=True)
class Claim:
    run_id: str
    thread_id: str
    job: dict[str, Any]
    worker_id: str
    token: int          # fencing token: the run's attempt number at claim time
    recovered: bool     # True when this claim took over an expired lease


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    if "job" in d:
        d["job"] = json.loads(d["job"])
    if "data" in d:
        d["data"] = json.loads(d["data"])
    return d


class Store:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._conn().executescript(SCHEMA)

    # -- connections -------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    @staticmethod
    def _emit(c: sqlite3.Connection, run_id: str, type_: str, data: dict[str, Any]) -> None:
        c.execute(
            "INSERT INTO events(run_id, type, data, ts) VALUES (?, ?, ?, ?)",
            (run_id, type_, json.dumps(data), time.time()),
        )

    @staticmethod
    def _guard(c: sqlite3.Connection, run_id: str, worker_id: str, token: int) -> None:
        ok = c.execute(
            "SELECT 1 FROM runs WHERE id=? AND status='running' AND lease_owner=? AND attempt=?",
            (run_id, worker_id, token),
        ).fetchone()
        if ok is None:
            raise LeaseLost(run_id)

    # -- client side -------------------------------------------------------

    def submit(self, thread_id: str, job: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Queue a run. If the thread already has an active run, return that
        one instead, so a retried or double-clicked request is harmless."""
        now = time.time()
        with self._tx() as c:
            existing = c.execute(
                "SELECT * FROM runs WHERE thread_id=? AND status IN ('queued','running')",
                (thread_id,),
            ).fetchone()
            if existing is not None:
                return _row(existing), False
            run_id = uuid.uuid4().hex
            c.execute(
                "INSERT INTO runs(id, thread_id, job, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (run_id, thread_id, json.dumps(job), now, now),
            )
            self._emit(c, run_id, "run.queued", {"thread_id": thread_id, "steps": len(job["steps"])})
            return _row(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return _row(self._conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def latest_for_thread(self, thread_id: str) -> dict[str, Any] | None:
        return _row(
            self._conn()
            .execute("SELECT * FROM runs WHERE thread_id=? ORDER BY created_at DESC LIMIT 1", (thread_id,))
            .fetchone()
        )

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM steps WHERE run_id=? ORDER BY idx", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def events(self, run_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows = (
            self._conn()
            .execute(
                "SELECT seq, type, data, ts FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, limit),
            )
            .fetchall()
        )
        return [_row(r) for r in rows]

    def request_cancel(self, run_id: str) -> dict[str, Any] | None:
        """A queued run is cancelled on the spot. A running run is flagged and
        its worker kills the step on its next heartbeat."""
        now = time.time()
        with self._tx() as c:
            r = c.execute("SELECT status, cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
            if r is None:
                return None
            if r["status"] == "queued":
                c.execute("UPDATE runs SET status='cancelled', updated_at=? WHERE id=?", (now, run_id))
                self._emit(c, run_id, "run.cancelled", {"while": "queued"})
            elif r["status"] == "running" and not r["cancel_requested"]:
                c.execute("UPDATE runs SET cancel_requested=1, updated_at=? WHERE id=?", (now, run_id))
                self._emit(c, run_id, "run.cancel_requested", {})
        return self.get_run(run_id)

    # -- worker side -------------------------------------------------------

    def claim(self, worker_id: str, lease_ttl: float) -> Claim | None:
        """Take the oldest run that is queued, or running under an expired
        lease. The check and the update share one write transaction, so two
        workers can never claim the same run."""
        now = time.time()
        with self._tx() as c:
            while True:
                r = c.execute(
                    "SELECT * FROM runs WHERE status='queued' "
                    "OR (status='running' AND lease_expires < ?) "
                    "ORDER BY created_at LIMIT 1",
                    (now,),
                ).fetchone()
                if r is None:
                    return None
                recovered = r["status"] == "running"
                if recovered and r["cancel_requested"]:
                    # Its worker died after cancel was requested. Nothing to resume.
                    c.execute("UPDATE runs SET status='cancelled', updated_at=? WHERE id=?", (now, r["id"]))
                    self._emit(c, r["id"], "run.cancelled", {"while": "orphaned"})
                    continue
                token = r["attempt"] + 1
                c.execute(
                    "UPDATE runs SET status='running', attempt=?, lease_owner=?, lease_expires=?, updated_at=? "
                    "WHERE id=?",
                    (token, worker_id, now + lease_ttl, now, r["id"]),
                )
                self._emit(
                    c,
                    r["id"],
                    "run.recovered" if recovered else "run.claimed",
                    {"worker": worker_id, "attempt": token, "previous_owner": r["lease_owner"]},
                )
                return Claim(r["id"], r["thread_id"], json.loads(r["job"]), worker_id, token, recovered)

    def heartbeat(self, claim: Claim, lease_ttl: float) -> bool:
        """Extend the lease. Returns True when cancellation was requested."""
        now = time.time()
        with self._tx() as c:
            self._guard(c, claim.run_id, claim.worker_id, claim.token)
            c.execute(
                "UPDATE runs SET lease_expires=?, updated_at=? WHERE id=?",
                (now + lease_ttl, now, claim.run_id),
            )
            r = c.execute("SELECT cancel_requested FROM runs WHERE id=?", (claim.run_id,)).fetchone()
            return bool(r["cancel_requested"])

    def step_started(self, claim: Claim, idx: int, name: str) -> None:
        now = time.time()
        with self._tx() as c:
            self._guard(c, claim.run_id, claim.worker_id, claim.token)
            c.execute(
                "INSERT INTO steps(run_id, idx, name, status, attempts, started_at) VALUES (?, ?, ?, 'running', 1, ?) "
                "ON CONFLICT(run_id, idx) DO UPDATE SET status='running', attempts=attempts+1, started_at=excluded.started_at",
                (claim.run_id, idx, name, now),
            )
            self._emit(c, claim.run_id, "step.started", {"idx": idx, "name": name, "attempt": claim.token})

    def step_finished(self, claim: Claim, idx: int, ok: bool, output: str) -> None:
        now = time.time()
        with self._tx() as c:
            self._guard(c, claim.run_id, claim.worker_id, claim.token)
            c.execute(
                "UPDATE steps SET status=?, output=?, finished_at=? WHERE run_id=? AND idx=?",
                ("done" if ok else "failed", output, now, claim.run_id, idx),
            )
            self._emit(c, claim.run_id, "step.finished", {"idx": idx, "ok": ok, "output": output[-2000:]})

    def finish(self, claim: Claim, status: str, error: str | None = None) -> None:
        assert status in TERMINAL
        now = time.time()
        with self._tx() as c:
            self._guard(c, claim.run_id, claim.worker_id, claim.token)
            c.execute(
                "UPDATE runs SET status=?, error=?, lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE id=?",
                (status, error, now, claim.run_id),
            )
            self._emit(c, claim.run_id, f"run.{status}", {"error": error} if error else {})
