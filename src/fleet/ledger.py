"""The dispatch record. SQLite, on the box, single source of truth for what is running.

⚠ **This is not the work record.** The session file in the workspace repo is that, and
it stays that. Remote agents push to that repo too, and a file in shared git is not a lock —
trying to make one file serve both is how two dispatchers claim the same session.

Claiming is atomic: `UPDATE ... WHERE state = 'queued'` and check the row count. The launch
itself happens *outside* the transaction, because starting a tmux session takes a second and
a write lock held that long turns every concurrent reader into an error. If the launch then
fails, the row goes back.

WAL mode, so `fleet status --json` reads while a tick writes. That matters: the board polls
this file and must never block the dispatcher, nor be blocked by it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    NEEDS_YOU = "needs_you"
    PARKED = "parked"
    DONE = "done"


LIVE: frozenset[State] = frozenset({State.RUNNING, State.VERIFYING})
"""States that occupy a slot. `verifying` still does: the gate may send it straight back."""

SETTLED: frozenset[State] = frozenset({State.DONE, State.NEEDS_YOU, State.PARKED})
"""States a session can be re-added from.

⚠ `needs_you` and `parked` exist precisely to wait for a human decision, and `fleet add` is
a human saying go again. Refusing to re-add them made both states a dead end: the first
real run put a session in `needs_you` for a reason that turned out to be our own bug, and
there was then no way to put it back without editing the database.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    session     TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    runner      TEXT,
    wanted      TEXT,
    model       TEXT,
    account     TEXT,
    pid         INTEGER,
    tmux        TEXT,
    log         TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    verdict     TEXT,
    note        TEXT,
    actor       TEXT,
    queued_at   REAL NOT NULL,
    started_at  REAL,
    ended_at    REAL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    session TEXT NOT NULL,
    kind    TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS items_state ON items(state);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


@dataclass(frozen=True, slots=True)
class Item:
    session: str
    state: State
    runner: str | None
    """Where it last ran. Set at launch, so it says nothing about what was asked for."""

    model: str | None
    """The model the operator named at `fleet add`, or None for the runner's default.

    ⚠ A model is not a preference, it is which quota pool the work spends. agy meters
    Gemini and the Claude/GPT models as two separate allowances per account, so naming
    `claude-sonnet-4-6` is how a session reaches a pool the Gemini one being empty says
    nothing about.
    """

    wanted: str | None
    """The runner the operator named at `fleet add`, or None for "wherever it fits".

    ⚠ Kept apart from `runner` on purpose. `launched()` overwrites `runner` with whatever
    the session landed on, so reading dispatch policy off it meant an unnamed session that
    once ran on claude could never leave claude again.
    """

    account: str | None
    pid: int | None
    tmux: str | None
    log: str | None
    attempts: int
    verdict: str | None
    note: str | None
    actor: str | None
    queued_at: float
    started_at: float | None
    ended_at: float | None

    @property
    def live(self) -> bool:
        return self.state in LIVE


def _item(row: sqlite3.Row) -> Item:
    return Item(
        session=row["session"],
        state=State(row["state"]),
        runner=row["runner"],
        wanted=row["wanted"],
        model=row["model"],
        account=row["account"],
        pid=row["pid"],
        tmux=row["tmux"],
        log=row["log"],
        attempts=row["attempts"],
        verdict=row["verdict"],
        note=row["note"],
        actor=row["actor"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
    )


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=15, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=15000")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """⚠ `wanted` was added on 2026-09-19 and the ledger on the box predates it.

        Existing rows get `wanted = NULL`, which is the right answer: it means nobody asked
        for a particular runner. Copying `runner` into it would pin every session that had
        ever run on claude to claude for good, which is the exact bug the column exists to
        undo.
        """
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(items)")}
        if "wanted" not in have:
            self._db.execute("ALTER TABLE items ADD COLUMN wanted TEXT")
        if "model" not in have:
            # Existing rows get NULL, meaning "whatever the runner runs by default", which
            # is exactly what they were dispatched with.
            self._db.execute("ALTER TABLE items ADD COLUMN model TEXT")

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE takes the write lock up front rather than upgrading mid-way,
        which is what turns two concurrent ticks into `database is locked` instead of a
        clean wait."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # --- events ---------------------------------------------------------------------

    def record(self, session: str, kind: str, detail: str = "") -> None:
        self._db.execute(
            "INSERT INTO events (ts, session, kind, detail) VALUES (?, ?, ?, ?)",
            (time.time(), session, kind, detail),
        )

    def events(self, since_id: int = 0, limit: int = 200) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, ts, session, kind, detail FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (since_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- reading --------------------------------------------------------------------

    def get(self, session: str) -> Item | None:
        row = self._db.execute("SELECT * FROM items WHERE session = ?", (session,)).fetchone()
        return _item(row) if row else None

    def all(self) -> list[Item]:
        rows = self._db.execute(
            "SELECT * FROM items ORDER BY "
            "CASE state WHEN 'running' THEN 0 WHEN 'verifying' THEN 1 WHEN 'needs_you' "
            "THEN 2 WHEN 'queued' THEN 3 WHEN 'parked' THEN 4 ELSE 5 END, queued_at"
        ).fetchall()
        return [_item(r) for r in rows]

    def in_state(self, *states: State) -> list[Item]:
        names = [s.value for s in states]
        marks = ",".join("?" * len(names))
        rows = self._db.execute(
            f"SELECT * FROM items WHERE state IN ({marks}) ORDER BY queued_at", names
        ).fetchall()
        return [_item(r) for r in rows]

    def running_by_account(self) -> dict[tuple[str, str | None], int]:
        """How many live sessions each (runner, account) pair is carrying."""
        counts: dict[tuple[str, str | None], int] = {}
        for it in self.in_state(*LIVE):
            if it.runner:
                key = (it.runner, it.account)
                counts[key] = counts.get(key, 0) + 1
        return counts

    # --- writing --------------------------------------------------------------------

    def add(
        self,
        session: str,
        actor: str | None = None,
        runner: str | None = None,
        model: str | None = None,
    ) -> bool:
        """Queue a session. Returns False if it is already queued or in flight.

        Re-adding something already queued, running or being verified is a mistake repeated
        rather than an instruction, so it is refused. Anything settled — done, needs_you or
        parked — can be sent round again, because that is what a human deciding looks like.
        """
        with self._write() as db:
            existing = db.execute(
                "SELECT state FROM items WHERE session = ?", (session,)
            ).fetchone()
            if existing and State(existing["state"]) not in SETTLED:
                return False
            db.execute(
                "INSERT INTO items "
                "(session, state, wanted, model, runner, actor, queued_at, attempts) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, 0) "
                "ON CONFLICT(session) DO UPDATE SET state=excluded.state, "
                "wanted=excluded.wanted, model=excluded.model, runner=NULL, "
                "actor=excluded.actor, queued_at=excluded.queued_at, "
                "attempts=0, verdict=NULL, note=NULL, pid=NULL, tmux=NULL, log=NULL, "
                "started_at=NULL, ended_at=NULL",
                (session, State.QUEUED.value, runner, model, actor, time.time()),
            )
        asked = {"actor": actor, "runner": runner, "model": model}
        self.record(session, "queued", json.dumps(asked))
        return True

    def claim(self, session: str) -> bool:
        """Take a queued session for dispatch. Atomic; False means someone else won.

        The launch happens after this returns, never inside it — holding the write lock
        across a tmux spawn is how a second reader gets `database is locked`.
        """
        with self._write() as db:
            cur = db.execute(
                "UPDATE items SET state = ?, started_at = ? WHERE session = ? AND state = ?",
                (State.RUNNING.value, time.time(), session, State.QUEUED.value),
            )
            return cur.rowcount == 1

    def launched(
        self,
        session: str,
        runner: str,
        account: str | None,
        pid: int,
        tmux: str | None,
        log: str | None,
    ) -> None:
        with self._write() as db:
            db.execute(
                "UPDATE items SET runner=?, account=?, pid=?, tmux=?, log=?, "
                "attempts = attempts + 1 WHERE session = ?",
                (runner, account, pid, tmux, log, session),
            )
        self.record(session, "launched", f"{runner}/{account or '-'} pid={pid}")

    def transition(
        self,
        session: str,
        state: State,
        *,
        verdict: str | None = None,
        note: str | None = None,
        kind: str | None = None,
    ) -> None:
        ended = time.time() if state in (State.DONE, State.NEEDS_YOU, State.PARKED) else None
        with self._write() as db:
            db.execute(
                "UPDATE items SET state=?, verdict=COALESCE(?, verdict), "
                "note=COALESCE(?, note), ended_at=COALESCE(?, ended_at) WHERE session=?",
                (state.value, verdict, note, ended, session),
            )
        self.record(session, kind or state.value, note or verdict or "")

    def requeue(self, session: str, note: str) -> None:
        """Back to the queue, keeping the attempt count. Clears the handle: whatever ran
        is gone, and a stale pid in the ledger is worse than none."""
        with self._write() as db:
            db.execute(
                "UPDATE items SET state=?, pid=NULL, tmux=NULL, started_at=NULL, "
                "note=? WHERE session=?",
                (State.QUEUED.value, note, session),
            )
        self.record(session, "requeued", note)

    def forget(self, session: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM items WHERE session = ?", (session,))
        self.record(session, "removed", "")
