"""SQLite access layer (CR-408, NFR-102).

Design points that the rest of the codebase depends on:

* WAL mode, so many readers run concurrently with one writer.
* A single global write lock.  SQLite serialises writers anyway; taking the
  lock in-process turns "database is locked" retries into an orderly queue
  (NFR-102).
* All SQL lives here and in ``db/repositories``.  Nothing else in the code
  base issues SQL, which is what keeps a later move to PostgreSQL feasible
  (CR-408).
* Rows come back as ``sqlite3.Row`` and are converted to plain dicts by the
  repositories, so callers never hold a cursor open.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.observability import db_logging

_write_lock = threading.RLock()
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


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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


@contextmanager
def read_tx(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Read-only usage.  Concurrent with other readers and with the writer."""
    yield get_connection(db_path)


@contextmanager
def write_tx(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Serialised write transaction.  Commits on success, rolls back on error."""
    conn = get_connection(db_path)
    # NFR-701: duration, lock waits and rollbacks.  ``tx`` is a shared no-op
    # object when logging is off, except on failure - a write that could not
    # commit is always worth a line.
    tx = db_logging.begin_write()
    with _write_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            tx.acquired()
            yield conn
            conn.commit()
        except Exception as exc:
            conn.rollback()
            tx.failed(exc)
            raise
        tx.committed()


def query_all(sql: str, params: tuple | dict = (), db_path: Path | None = None) -> list[dict]:
    cur = get_connection(db_path).execute(sql, params)
    try:
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()


def query_one(sql: str, params: tuple | dict = (), db_path: Path | None = None) -> dict | None:
    cur = get_connection(db_path).execute(sql, params)
    try:
        row = cur.fetchone()
        return dict(row) if row else None
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
