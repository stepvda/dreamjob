"""SQLite access layer (CR-408, NFR-102).

Design points that the rest of the codebase depends on:

* WAL mode, so many readers run concurrently with one writer.
* A single global write gate.  SQLite serialises writers anyway; taking the
  gate in-process turns "database is locked" retries into an orderly queue
  (NFR-102).
* All SQL lives here and in ``db/repositories``.  Nothing else in the code
  base issues SQL, which is what keeps a later move to PostgreSQL feasible
  (CR-408).
* Rows come back as ``sqlite3.Row`` and are converted to plain dicts by the
  repositories, so callers never hold a cursor open.

The write gate, and why it is not a bare ``RLock`` any more
-----------------------------------------------------------
It used to be ``threading.RLock()``.  That serialises writers, which is the
invariant, but it says nothing about *who waits and for how long* - and that
turned out to be the half that matters.

1. **An RLock is not fair.**  CPython hands the lock to whichever waiter the
   OS wakes first, and a thread that releases and immediately re-acquires can
   keep winning.  A collection job writing 2,686 plan items does exactly that,
   thousands of times a minute.  A request that wants to write can lose the
   race indefinitely - starvation with no upper bound and nothing in the log.
2. **The wait had no ceiling.**  ``BEGIN IMMEDIATE`` runs *inside* the gate and
   can block for ``busy_timeout`` (15 s) when another *process* holds the
   database - a second uvicorn, a script, a ``sqlite3`` shell.  Each waiter
   pays that in turn, so with W writers queued the last one waits W x 15 s.
   Nothing bounded W, and nothing bounded the wait.

So the gate is explicit: a FIFO queue with two lanes.

* ``interactive`` - anything serving a person.  This is the default, so no
  caller has to know the lane exists.
* ``bulk`` - background job work.  ``jobs/runner.py`` puts every job thread in
  this lane, and interactive writers are served ahead of it.

A job therefore never queues in front of a request.  The worst an interactive
writer can wait is the one transaction already in flight (bounded by
``busy_timeout`` plus its own work), not the thousands of writes a campaign
still has to do.  Both lanes have a deadline: waiting for ever is how an
outage looks from the inside, so a writer that cannot be served raises
:class:`WriteLockTimeout` and the request fails in a way somebody can read.

Strict priority would starve the other side, so after
``_BULK_RELIEF_EVERY`` consecutive interactive grants a queued bulk writer is
let through.  A campaign under continuous API load slows down; it does not
stop.

The gate stays **re-entrant**: the same thread may take it again while it
holds it.  ``write_tx`` inside ``write_tx`` on one thread is a programming
error - SQLite refuses the second ``BEGIN IMMEDIATE`` with "cannot start a
transaction within a transaction" - and failing there is much better than
deadlocking on our own gate (tests/unit/test_runner_isolation.py pins this).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.observability import db_logging

log = logging.getLogger(__name__)

# -- lanes -------------------------------------------------------------------
#: Serving a person.  The default, so that code which knows nothing about
#: lanes is in the one that gets served first.
INTERACTIVE = "interactive"
#: Background job work (jobs/runner.py puts its threads here).
BULK = "bulk"

#: How long a writer may wait for the gate before giving up.  The interactive
#: budget matches ``PRAGMA busy_timeout`` below: a request that has been
#: waiting a whole busy_timeout for an in-process queue is already a defect,
#: and an error a person can see beats a page that never loads.  Bulk work is
#: patient - a campaign should not fail because the API was busy - but not
#: infinitely so, because an unbounded wait is indistinguishable from a hang.
#: Read through the settings rather than ``os.environ``: pydantic-settings
#: parses ``.env`` itself and does not export it, so reading the environment
#: here would have ignored the value ``.env.example`` tells the operator to
#: set and silently kept the default.
INTERACTIVE_WAIT_SECONDS = 15.0
BULK_WAIT_SECONDS = 600.0

#: Consecutive interactive grants before a queued bulk writer is let through.
_BULK_RELIEF_EVERY = 8


class WriteLockTimeout(TimeoutError):
    """A writer waited longer than its lane allows for the single writer.

    Raised instead of blocking for ever.  Seeing this in a log means writers
    were queued behind one another for longer than the budget - the thing that
    used to be invisible.
    """


class _Waiter:
    """One thread's place in the queue."""

    __slots__ = ("lane",)

    def __init__(self, lane: str) -> None:
        self.lane = lane


class _WriteGate:
    """The single writer of NFR-102, with an explicit queue.

    Re-entrant per thread, FIFO within a lane, interactive before bulk, with a
    deadline on every wait and counters the log can quote.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._owner: int | None = None
        self._owner_lane: str = INTERACTIVE
        self._depth = 0
        self._queues: dict[str, deque[_Waiter]] = {INTERACTIVE: deque(), BULK: deque()}
        self._interactive_streak = 0

    # -- the queue ----------------------------------------------------------
    def _next(self) -> _Waiter | None:
        """Which waiter is entitled to the gate right now."""
        interactive, bulk = self._queues[INTERACTIVE], self._queues[BULK]
        if interactive and bulk:
            # Anti-starvation: continuous API load must slow a campaign down,
            # not stop it.
            if self._interactive_streak >= _BULK_RELIEF_EVERY:
                return bulk[0]
            return interactive[0]
        if interactive:
            return interactive[0]
        if bulk:
            return bulk[0]
        return None

    def acquire(self, lane: str, timeout: float) -> float:
        """Take the single writer.  Returns the wait in milliseconds.

        Re-entrant: a thread that already holds the gate is let straight back
        in, and pays nothing.
        """
        me = threading.get_ident()
        started = time.perf_counter()
        with self._cv:
            if self._owner == me:
                self._depth += 1
                return 0.0
            waiter = _Waiter(lane)
            queue = self._queues[lane]
            queue.append(waiter)
            try:
                granted = self._cv.wait_for(
                    lambda: self._owner is None and self._next() is waiter, timeout
                )
            finally:
                # Leaving the queue is the same act whether we were granted or
                # timed out, and either way the head of the queue may have
                # changed for somebody else.
                try:
                    queue.remove(waiter)
                except ValueError:  # pragma: no cover - only if granted twice
                    pass
                self._cv.notify_all()
            if not granted:
                raise WriteLockTimeout(
                    f"waited {timeout:.0f}s for the single writer in the {lane} lane "
                    f"({self.queued()} writer(s) queued); the write was not attempted"
                )
            self._owner = me
            self._owner_lane = lane
            self._depth = 1
            if lane == INTERACTIVE:
                self._interactive_streak += 1
            else:
                self._interactive_streak = 0
            return (time.perf_counter() - started) * 1000.0

    def release(self) -> None:
        with self._cv:
            if self._owner != threading.get_ident():  # pragma: no cover - a bug elsewhere
                raise RuntimeError("the write gate was released by a thread not holding it")
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._cv.notify_all()

    # -- introspection, for the log ----------------------------------------
    def queued(self) -> int:
        return len(self._queues[INTERACTIVE]) + len(self._queues[BULK])

    def state(self) -> str:
        """One short string naming who holds the gate and who is waiting."""
        return (
            f"held_by={self._owner_lane if self._owner is not None else '-'} "
            f"queued_interactive={len(self._queues[INTERACTIVE])} "
            f"queued_bulk={len(self._queues[BULK])}"
        )


_write_gate = _WriteGate()
_local = threading.local()


def new_id() -> str:
    """Opaque primary key.  Random UUID4 hex - no ordering information."""
    return uuid.uuid4().hex


def utcnow() -> str:
    """Canonical timestamp format used in every TEXT date column."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def to_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def from_json(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Which lane this thread writes in
# ---------------------------------------------------------------------------
def current_write_lane() -> str:
    """The lane this thread's writes queue in.  ``interactive`` by default."""
    return getattr(_local, "write_lane", INTERACTIVE)


def set_write_lane(lane: str) -> str:
    """Put this thread in a lane for good.  Returns the lane it was in.

    Used by ``jobs/runner.py`` for its pool threads, which exist only to run
    job work; everything else stays interactive without having to know.
    """
    previous = current_write_lane()
    _local.write_lane = lane
    return previous


@contextmanager
def write_lane(lane: str) -> Iterator[None]:
    """Run a block of work in one lane, then restore the previous one."""
    previous = set_write_lane(lane)
    try:
        yield
    finally:
        set_write_lane(previous)


def _wait_budget(lane: str) -> float:
    settings = get_settings()
    if lane == BULK:
        return float(getattr(settings, "bulk_write_wait_seconds", BULK_WAIT_SECONDS))
    return float(getattr(settings, "write_wait_seconds", INTERACTIVE_WAIT_SECONDS))


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # How long SQLite itself waits for a database held by another *process*.
    # The in-process gate above is what stops threads of this process from
    # queueing on it; this is for the second uvicorn, the script, the shell.
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    # 64 MB page cache; the knowledge base is read-heavy (NFR-101).
    conn.execute("PRAGMA cache_size=-64000")


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """One connection per thread.  Safe to call from anywhere."""
    path = db_path or get_settings().abs_db_path
    key = f"conn_{path}"
    conn = getattr(_local, key, None)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15.0)
        _configure(conn)
        # NFR-701: statement visibility.  No-op unless DREAMJOB_LOG_SQL is on.
        db_logging.attach(conn)
        setattr(_local, key, conn)
    return conn


def checkpoint(mode: str = "PASSIVE") -> dict[str, int]:
    """Collapse the write-ahead log back into the database.

    A WAL that never checkpoints grows without bound - one installation reached
    3.3 GB of WAL against a 1.85 GB database, which slows every reader and
    inflates every backup.  SQLite checkpoints automatically, but only when no
    other connection holds a read transaction open; a long-lived reader can pin
    the WAL indefinitely.  ``PASSIVE`` never blocks and is safe while the API is
    serving; ``TRUNCATE`` is for boot, when nothing else is attached.

    Returns ``{busy, log, checkpointed}`` in frames; ``busy`` non-zero means a
    reader pinned the log and the next call should try again.
    """
    mode = (mode or "PASSIVE").upper()
    if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        mode = "PASSIVE"
    try:
        row = get_connection().execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    except Exception:  # noqa: BLE001 - storage hygiene must never crash a caller
        log.exception("WAL checkpoint (%s) failed", mode)
        return {"busy": -1, "log": -1, "checkpointed": -1}
    if row is None:
        return {"busy": -1, "log": -1, "checkpointed": -1}
    result = {"busy": int(row[0]), "log": int(row[1]), "checkpointed": int(row[2])}
    if result["busy"]:
        log.info(
            "WAL checkpoint (%s) was busy: %d frame(s) still in the log", mode, result["log"]
        )
    return result


def close_thread_connections() -> None:
    """Drop every connection this thread has opened.

    Pool threads outlive the job that borrowed them (``jobs/runner.py``), and a
    connection is a file handle onto a database that may since have been
    replaced - which in the test suite means writing into a deleted inode.  A
    thread that has finished its job hands its connections back.
    """
    for key in [k for k in vars(_local) if k.startswith("conn_")]:
        conn = getattr(_local, key, None)
        delattr(_local, key)
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - a connection still in use is not ours to break
            pass


@contextmanager
def read_tx(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Read-only usage.  Concurrent with other readers and with the writer.

    Deliberately does not touch the write gate.  Under WAL a reader never
    waits for a writer, and that is what makes the API's own reads survive a
    campaign (NFR-102).
    """
    yield get_connection(db_path)


@contextmanager
def write_tx(
    db_path: Path | None = None,
    *,
    lane: str | None = None,
    timeout: float | None = None,
) -> Iterator[sqlite3.Connection]:
    """Serialised write transaction.  Commits on success, rolls back on error.

    ``lane`` defaults to this thread's lane (:func:`set_write_lane`), so job
    work queues behind requests without any call site knowing about it.
    """
    conn = get_connection(db_path)
    lane = lane or current_write_lane()
    budget = _wait_budget(lane) if timeout is None else timeout
    # NFR-701: duration, lock waits and rollbacks.  ``tx`` is a shared no-op
    # object when logging is off, except on failure - a write that could not
    # commit is always worth a line.
    tx = db_logging.begin_write()
    try:
        waited_ms = _write_gate.acquire(lane, budget)
    except WriteLockTimeout as exc:
        # Never reached the database, but it is a write that did not happen,
        # which is exactly what NFR-701 says has to be on the record.
        tx.failed(exc)
        raise
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Both halves of the wait, separately: ``gate`` is this process
            # queueing behind its own writers, ``begin`` is SQLite waiting for
            # somebody else's.  Reporting one number for the two is how a
            # queue gets mistaken for a slow database.
            tx.acquired(gate_wait_ms=waited_ms, lane=lane)
            yield conn
            conn.commit()
        except Exception as exc:
            conn.rollback()
            tx.failed(exc)
            raise
        tx.committed()
    finally:
        _write_gate.release()


def _unseal(row: dict) -> dict:
    """Return the row with NFR-201 sealed columns opened for its job seeker.

    The import is local so the connection layer stays importable without the
    crypto dependency, and so a test that swaps the connection cache does not
    drag the security package in with it.
    """
    try:
        from dreamjob.security import at_rest  # noqa: PLC0415

        return at_rest.unseal_row(row) or row
    except Exception:  # noqa: BLE001 - reading a row is never worth a crash here
        log.debug("Could not unseal a row; returning it as stored", exc_info=True)
        return row


def query_all(sql: str, params: tuple | dict = (), db_path: Path | None = None) -> list[dict]:
    cur = get_connection(db_path).execute(sql, params)
    try:
        return [_unseal(dict(r)) for r in cur.fetchall()]
    finally:
        cur.close()


def query_one(sql: str, params: tuple | dict = (), db_path: Path | None = None) -> dict | None:
    cur = get_connection(db_path).execute(sql, params)
    try:
        row = cur.fetchone()
        return _unseal(dict(row)) if row else None
    finally:
        cur.close()


def execute(sql: str, params: tuple | dict = (), db_path: Path | None = None) -> int:
    with db_logging.writing_sql(sql), write_tx(db_path) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount


def insert_row(table: str, values: dict, db_path: Path | None = None) -> str:
    """Insert a dict, JSON-encoding any nested structures.  Returns the id."""
    values = dict(values)
    values.setdefault("id", new_id())
    payload = {k: (to_json(v) if isinstance(v, (dict, list)) else v) for k, v in values.items()}
    cols = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    with db_logging.writing(table, "INSERT", values["id"]), write_tx(db_path) as conn:
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", payload)
    return values["id"]


def upsert_row(
    table: str, values: dict, conflict_cols: list[str], db_path: Path | None = None
) -> None:
    values = dict(values)
    payload = {k: (to_json(v) if isinstance(v, (dict, list)) else v) for k, v in values.items()}
    cols = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k not in conflict_cols)
    conflict = ", ".join(conflict_cols)
    sql = f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({conflict}) DO UPDATE SET {updates}"
    with db_logging.writing(table, "UPSERT", values.get("id")), write_tx(db_path) as conn:
        conn.execute(sql, payload)


def update_row(table: str, row_id: str, values: dict, db_path: Path | None = None) -> None:
    if not values:
        return
    payload = {k: (to_json(v) if isinstance(v, (dict, list)) else v) for k, v in values.items()}
    sets = ", ".join(f"{k}=:{k}" for k in payload)
    payload["__id"] = row_id
    with db_logging.writing(table, "UPDATE", row_id), write_tx(db_path) as conn:
        conn.execute(f"UPDATE {table} SET {sets} WHERE id=:__id", payload)
