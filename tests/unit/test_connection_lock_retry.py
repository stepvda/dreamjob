"""Writes survive a transient cross-process lock (NFR-102, NFR-701).

The audit trail lost 296 rows to ``database is locked`` because a write gave
up in under a second while ``PRAGMA busy_timeout`` was configured for 15 s -
something had shortened the timeout on the connection, and nothing retried.
These tests hold a real write lock on a second connection, start a write, and
release the lock while the writer is still waiting; the row must land and no
exception may escape.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import (
    BUSY_TIMEOUT_MS,
    insert_row,
    query_one,
    utcnow,
)
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import seekers as repo
from dreamjob.security import auth_service as auth
from dreamjob.security.audit import (
    flush_pending_audits,
    pending_audit_count,
    record_audit,
)


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    migrate()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def fast_lock(monkeypatch):
    """A millisecond-scale lock budget so each test finishes quickly.

    The shipped values are 15,000 ms and 15 s; only the scale changes here.
    """
    from dreamjob.db import connection as connection_module

    monkeypatch.setattr(connection_module, "BUSY_TIMEOUT_MS", 60)
    monkeypatch.setattr(connection_module, "LOCK_RETRY_INITIAL_MS", 10.0)
    monkeypatch.setattr(connection_module, "LOCK_RETRY_MAX_MS", 40.0)
    monkeypatch.setattr(connection_module, "_wait_budget", lambda lane: 5.0)
    return connection_module


def _hold_write_lock(seconds: float):
    """Hold a real write transaction for ``seconds`` on a separate connection.

    Returns the releasing thread; the lock is released in the background so
    the writer under test has to wait for it.
    """
    settings = get_settings()
    outsider = sqlite3.connect(
        str(settings.abs_db_path), timeout=30.0, check_same_thread=False
    )
    outsider.execute("PRAGMA journal_mode=WAL")
    outsider.execute("BEGIN IMMEDIATE")
    outsider.execute(
        "INSERT INTO audit_event (id, action, created_at) VALUES ('holder', 'x', ?)",
        (utcnow(),),
    )
    released = threading.Event()

    def release() -> None:
        try:
            time.sleep(seconds)
            outsider.rollback()
        finally:
            outsider.close()
            released.set()

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    return thread, released


def test_insert_row_retries_and_lands_through_a_transient_lock(db: None, fast_lock) -> None:
    thread, released = _hold_write_lock(0.4)
    try:
        event_id = insert_row(
            "audit_event", {"action": "retry.ok", "created_at": utcnow()}
        )
    finally:
        thread.join(timeout=10)

    assert released.is_set()
    assert event_id
    landed = query_one("SELECT COUNT(*) AS n FROM audit_event WHERE action = 'retry.ok'")
    assert landed["n"] == 1


def test_record_audit_lands_through_a_transient_lock(db: None, fast_lock) -> None:
    thread, _ = _hold_write_lock(0.4)
    try:
        event_id = record_audit("lock.audit.ok", "audit_event", None)
    finally:
        thread.join(timeout=10)

    assert event_id is not None
    assert pending_audit_count() == 0
    row = query_one("SELECT action FROM audit_event WHERE id = ?", (event_id,))
    assert row is not None and row["action"] == "lock.audit.ok"


def test_record_audit_buffers_then_flushes_when_the_lock_outlives_the_budget(
    db: None, monkeypatch
) -> None:
    """A lock longer than the budget must not drop the event on the floor."""
    from dreamjob.db import connection as connection_module

    monkeypatch.setattr(connection_module, "BUSY_TIMEOUT_MS", 40)
    monkeypatch.setattr(connection_module, "LOCK_RETRY_INITIAL_MS", 10.0)
    monkeypatch.setattr(connection_module, "LOCK_RETRY_MAX_MS", 40.0)
    monkeypatch.setattr(connection_module, "_wait_budget", lambda lane: 0.2)

    thread, released = _hold_write_lock(1.2)
    try:
        event_id = record_audit("lock.buffered", "audit_event", None)
    finally:
        thread.join(timeout=10)

    assert released.is_set()
    assert event_id is None, "the write cannot have landed while the lock was held"
    assert pending_audit_count() >= 1

    # The database is reachable again; the next successful audit drains it.
    flushed = record_audit("lock.flushed", "audit_event", None)
    assert flushed is not None
    flush_pending_audits()
    assert pending_audit_count() == 0
    buffered = query_one("SELECT COUNT(*) AS n FROM audit_event WHERE action = 'lock.buffered'")
    assert buffered["n"] == 1


def test_start_session_survives_a_transient_lock(db: None, fast_lock) -> None:
    seeker_id = repo.create_seeker("lock-session@example.invalid", "Lock Session")
    thread, released = _hold_write_lock(0.4)
    try:
        token, expires_at = auth.start_session(seeker_id, "test-agent", "127.0.0.1")
    finally:
        thread.join(timeout=10)

    assert released.is_set()
    assert token and expires_at
    sessions = query_one(
        "SELECT COUNT(*) AS n FROM session WHERE job_seeker_id = ?", (seeker_id,)
    )
    assert sessions["n"] == 1


def test_configured_busy_timeout_is_the_shipped_value() -> None:
    """The constant the whole retry budget is built on stays 15 s."""
    assert BUSY_TIMEOUT_MS == 15_000
