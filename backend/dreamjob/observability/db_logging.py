"""Database logging (NFR-701).

What lands in ``logs/database.log`` and why:

* **Every write transaction** - table, operation, primary key, duration.  When
  a job seeker says "my directive did not save", this is the line that says
  whether the write happened at all.
* **Every statement slower than** ``DREAMJOB_LOG_SLOW_QUERY_MS`` at WARNING,
  with the statement and the elapsed time.  This is the line that finds a
  missing index; nothing else in the system will tell you.
* **Migrations**, with version and duration.
* **Lock waits and rollbacks.**  "database is locked" is the failure mode of a
  single-writer SQLite design (NFR-102), so it is logged whether or not
  statement tracing is on - a production incident must not be invisible
  because the cheap flag was off.
* **A periodic summary** at INFO: statements, writes, average duration.  A
  healthy system should produce evidence of health, not just silence.  It rides
  on the statement trace, so it is there whenever tracing is.

Privacy (NFR-201, NFR-702).  ``sqlite3.set_trace_callback`` hands us the
*expanded* SQL - SQLite inlines every bound value, so the string it gives us
literally contains CV text, e-mail addresses and phone numbers.  That string is
never logged.  :func:`redact` reduces a statement to its shape (every literal
becomes ``?``) and reports how many values it replaced; the only value that
ever reaches the file is a primary key, which is an opaque UUID.

Cost.  A trace callback builds a Python string for every statement executed,
which is real overhead, so all statement-level work sits behind
``DREAMJOB_LOG_SQL`` (default: on outside production).

This module logs through ``dreamjob.observability.logs.get_logger``.  If that
channel has no file behind it - a migration run from the command line, a
script, a test, none of which go through the application lifespan - it attaches
its own rotating handler on ``logs/database.log``, so database evidence never
depends on who started the process.
"""

from __future__ import annotations

import atexit
import functools
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dreamjob.config import get_settings

LOGGER_NAME = "dreamjob.database"

DEFAULT_SLOW_QUERY_MS = 200.0
# A quiet installation should still say something every few minutes.
DEFAULT_SUMMARY_INTERVAL_SECONDS = 300.0
# Long enough to show the WHERE clause that needs the index, short enough that
# one pathological statement cannot fill the disk.
MAX_SQL_CHARS = 800
# Below this, waiting for the write lock is just SQLite serialising writers as
# designed; above it, somebody is holding a transaction open too long.
LOCK_WAIT_WARN_MS = 250.0

_log: logging.Logger | None = None
_local = threading.local()
_lock = threading.Lock()

_configured = False
_enabled = False
_slow_ms = DEFAULT_SLOW_QUERY_MS
_summary_interval = DEFAULT_SUMMARY_INTERVAL_SECONDS
_next_summary = 0.0
_atexit_registered = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _resolve() -> None:
    """Read the settings once.  Called on first use, not at import time."""
    global _configured, _enabled, _slow_ms, _summary_interval, _next_summary
    global _atexit_registered
    if _configured:
        return
    settings = get_settings()
    # Read defensively: a log line is never worth an AttributeError on the way
    # into a database write.
    flag = getattr(settings, "log_sql", None)
    if flag is None:
        # Default: on while somebody is watching, off where the hot path is
        # what matters.  An explicit DREAMJOB_LOG_SQL always wins.
        flag = settings.env.strip().lower() not in ("production", "prod")
    _enabled = bool(flag)
    _slow_ms = float(getattr(settings, "log_slow_query_ms", DEFAULT_SLOW_QUERY_MS))
    _summary_interval = DEFAULT_SUMMARY_INTERVAL_SECONDS
    _next_summary = time.perf_counter() + _summary_interval
    _configured = True
    if _enabled and not _atexit_registered:
        atexit.register(_final_summary)
        _atexit_registered = True


def reset() -> None:
    """Forget cached settings, counters and handler.  For tests."""
    global _configured
    _detach_logger()
    _configured = False
    _stats.reset(time.perf_counter())
    _local.__dict__.clear()


def enabled() -> bool:
    """True when statement-level tracing is switched on."""
    if not _configured:
        _resolve()
    return _enabled


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
def _log_path() -> Path:
    settings = get_settings()
    directory = getattr(settings, "abs_log_dir", None) or Path("logs")
    return Path(directory) / "database.log"


def _channel_configured(log: logging.Logger) -> bool:
    """Has something already given the database channel its own destination?

    Handlers on the logger itself, not on its ancestors: ``setup_logging()``
    attaches database.log directly to this logger, while whatever happens to be
    on the root - a test runner's capture handler, for instance - is not a home
    for database evidence.
    """
    return bool(log.handlers)


def _attach_fallback(log: logging.Logger) -> None:
    """Give the database channel a file of its own when nothing else has.

    ``logs.setup_logging()`` is called from the application lifespan, so in a
    running Dream Job this never fires.  A migration run from the command line,
    a script or a test has no lifespan, and database evidence should not depend
    on who started the process (NFR-701).
    """
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    handler = RotatingFileHandler(
        path,
        maxBytes=int(getattr(settings, "log_max_mb", 10)) * 1024 * 1024,
        backupCount=int(getattr(settings, "log_backups", 5)),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    handler._dreamjob_db_handler = True  # type: ignore[attr-defined]
    # Wear the shared module's marker too, so that if setup_logging() runs
    # later it takes the file over instead of writing every line twice.
    setattr(handler, _shared_marker(), True)
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    # Per-statement chatter belongs in its own file; propagating it would drown
    # the console the developer is actually reading.
    log.propagate = False


def _shared_marker() -> str:
    try:
        from dreamjob.observability import logs

        return str(getattr(logs, "_MARK", "_dreamjob_handler"))
    except Exception:  # noqa: BLE001
        return "_dreamjob_handler"


def get_db_logger() -> logging.Logger:
    """The ``dreamjob.database`` channel, wired to a file one way or another."""
    global _log
    if _log is not None:
        return _log
    try:
        from dreamjob.observability.logs import get_logger

        log = get_logger(LOGGER_NAME)
    except Exception:  # noqa: BLE001 - a missing log factory must not break writes
        log = logging.getLogger(LOGGER_NAME)
    if not _channel_configured(log):
        _attach_fallback(log)
    _log = log
    return _log


def _detach_logger() -> None:
    global _log
    log = logging.getLogger(LOGGER_NAME)
    for handler in list(log.handlers):
        if getattr(handler, "_dreamjob_db_handler", False):
            log.removeHandler(handler)
            handler.close()
    _log = None


def _never_raises(method: Callable[..., None]) -> Callable[..., None]:
    """Swallow anything this module gets wrong.

    These functions run inside somebody else's transaction.  A job seeker
    losing a saved profile because a log line could not be formatted is not a
    trade this system makes (NFR-701 is about evidence, not about behaviour).
    """

    @functools.wraps(method)
    def guarded(*args: object, **kwargs: object) -> None:
        try:
            method(*args, **kwargs)
        except Exception:  # noqa: BLE001
            pass

    return guarded


# ---------------------------------------------------------------------------
# Redaction - the only thing standing between the trace callback and a leak
# ---------------------------------------------------------------------------
_BLOB_RE = re.compile(r"\b[xX]'[0-9a-fA-F]*'")
_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_NUMBER_RE = re.compile(r"(?<![\w:$.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_WS_RE = re.compile(r"\s+")


def redact(sql: str) -> tuple[str, int]:
    """Reduce a statement to its shape and count the values removed.

    Blobs first, then quoted strings, then bare numbers: every literal SQLite
    can inline becomes ``?``.  The count is what the brief calls the parameter
    count - values that were bound, plus any literal written into the SQL,
    which from the outside are the same thing.
    """
    count = 0

    def _mask(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "?"

    shape = _BLOB_RE.sub(_mask, sql)
    shape = _STRING_RE.sub(_mask, shape)
    shape = _NUMBER_RE.sub(_mask, shape)
    shape = _WS_RE.sub(" ", shape).strip()
    # Truncate only after masking.  Cutting first would leave an unterminated
    # literal that the string pattern no longer recognises, and the tail of a
    # CV would go straight into the file.
    if len(shape) > MAX_SQL_CHARS:
        shape = shape[:MAX_SQL_CHARS] + " ..."
    return shape, count


# ---------------------------------------------------------------------------
# Counters for the periodic summary
# ---------------------------------------------------------------------------
class _Stats:
    """Advisory counters.

    Incremented without a lock: a lost increment under concurrency costs a
    rounding error in a summary line, while a mutex on every statement would
    cost more than the number is worth.
    """

    __slots__ = ("locked", "since", "slow", "statements", "total_ms", "writes")

    def __init__(self) -> None:
        self.reset(time.perf_counter())

    def reset(self, now: float) -> None:
        self.statements = 0
        self.total_ms = 0.0
        self.writes = 0
        self.slow = 0
        self.locked = 0
        self.since = now


_stats = _Stats()


def _summarise(now: float, *, final: bool = False) -> None:
    global _next_summary
    with _lock:
        if not final and now < _next_summary:
            return  # another thread got here first
        _next_summary = now + _summary_interval
        statements, writes = _stats.statements, _stats.writes
        if not statements and not writes:
            return
        window = max(now - _stats.since, 0.001)
        average = _stats.total_ms / statements if statements else 0.0
        slow, locked = _stats.slow, _stats.locked
        _stats.reset(now)
    get_db_logger().info(
        "summary window_s=%.0f statements=%d writes=%d avg_ms=%.2f slow=%d locked=%d",
        window, statements, writes, average, slow, locked,
    )


def log_summary() -> None:
    """Emit the summary now, whatever the interval says."""
    _summarise(time.perf_counter(), final=True)


def _final_summary() -> None:
    # At interpreter shutdown the handlers' streams may already be closed.
    # logging would then print its own "--- Logging error ---" traceback, which
    # is a worse last impression than a missing summary line.
    previous = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        log_summary()
    except Exception:  # noqa: BLE001
        pass
    finally:
        logging.raiseExceptions = previous


# ---------------------------------------------------------------------------
# Statement tracing
# ---------------------------------------------------------------------------
def attach(conn: sqlite3.Connection) -> None:
    """Give one connection statement visibility (NFR-701).

    Called once per connection from ``db.connection.get_connection``.  A no-op
    when tracing is off, which is the whole point of the flag: no callback, no
    expanded-SQL string, no cost.
    """
    if not enabled():
        return
    conn.set_trace_callback(_trace)


@_never_raises
def _trace(statement: str) -> None:
    """Fires as each statement starts.

    SQLite gives no completion event, so a statement is timed from its own
    start to the start of the next one on this thread - which for the workload
    here (one statement at a time per connection) is its duration.  The tail of
    a batch is closed out by :func:`flush` when the transaction commits.
    """
    now = time.perf_counter()
    pending = getattr(_local, "pending", None)
    if pending is not None:
        _local.pending = None
        _finish(pending[0], pending[1], now)
    _local.pending = (statement, now)

    # Only pay for the migration sniff inside a write transaction.
    write = getattr(_local, "write", None)
    if write is not None and write.migration is None and "schema_migration" in statement:
        write.migration = _migration_identity(statement)

    if now >= _next_summary:
        _summarise(now)


def _finish(statement: str, start: float, end: float) -> None:
    elapsed_ms = (end - start) * 1000.0
    _stats.statements += 1
    _stats.total_ms += elapsed_ms
    if elapsed_ms >= _slow_ms:
        _stats.slow += 1
        shape, values = redact(statement)
        get_db_logger().warning(
            "slow statement ms=%.1f params=%d sql=%s", elapsed_ms, values, shape
        )


def flush() -> None:
    """Close out this thread's in-flight statement."""
    pending = getattr(_local, "pending", None)
    if pending is not None:
        _local.pending = None
        _finish(pending[0], pending[1], time.perf_counter())


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
# The migrator records each applied migration with an INSERT into
# schema_migration inside the same transaction that ran the script, so the
# transaction's own duration is the migration's duration.  Version and name are
# pulled out of the expanded statement under a strict pattern - three digits and
# a lower-case identifier, i.e. the filename, never free text.
_MIGRATION_RE = re.compile(
    r"INSERT\s+INTO\s+schema_migration\b.*?VALUES\s*\(\s*'(\d{3})'\s*,\s*'([a-z0-9_]{1,80})'",
    re.IGNORECASE | re.DOTALL,
)


def _migration_identity(statement: str) -> tuple[str, str] | None:
    match = _MIGRATION_RE.search(statement)
    return (match.group(1), match.group(2)) if match else None


def record_migration(version: str, name: str, duration_ms: float) -> None:
    """Log one applied migration.  Public so ``db.migrator`` can call it
    directly; with tracing off that is the only way this line appears."""
    get_db_logger().info(
        "migration applied version=%s name=%s ms=%.1f", version, name, duration_ms
    )


# ---------------------------------------------------------------------------
# Write transactions
# ---------------------------------------------------------------------------
def _is_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class _Intent:
    """What the caller knows about a write that the SQL alone would not say."""

    __slots__ = ("op", "row_id", "table")

    def __init__(self, table: str | None, op: str | None, row_id: str | None) -> None:
        self.table = table
        self.op = op
        self.row_id = row_id


class _NullCtx:
    __slots__ = ()

    def __enter__(self) -> _NullCtx:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


_NULL_CTX = _NullCtx()


class _Writing:
    """Publishes the caller's intent for the duration of one write."""

    __slots__ = ("_intent", "_previous")

    def __init__(self, table: str | None, op: str | None, row_id: str | None) -> None:
        self._intent = _Intent(table, op, row_id)
        self._previous: _Intent | None = None

    def __enter__(self) -> _Writing:
        self._previous = getattr(_local, "intent", None)
        _local.intent = self._intent
        return self

    def __exit__(self, *exc: object) -> bool:
        _local.intent = self._previous
        return False


def writing(table: str, op: str, row_id: str | None = None) -> _Writing | _NullCtx:
    """Name the table, operation and primary key of the write about to happen."""
    return _Writing(table, op, row_id) if enabled() else _NULL_CTX


_SHAPE_RE = re.compile(
    r"^\s*(INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+[\"`\[]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def describe(sql: str) -> tuple[str | None, str | None]:
    """Operation and table from a statement - identifiers only, no values."""
    match = _SHAPE_RE.match(sql)
    if not match:
        return None, None
    return match.group(2), match.group(1).split()[0].upper()


def writing_sql(sql: str) -> _Writing | _NullCtx:
    """Same as :func:`writing` where only the SQL is known."""
    if not enabled():
        return _NULL_CTX
    table, op = describe(sql)
    return _Writing(table, op, None)


class _NullWrite:
    """The recorder used when tracing is off.

    Still reports lock failures: a transaction that could not commit is not a
    hot path, and "database is locked" is exactly what production needs to see
    (NFR-102, NFR-701).
    """

    __slots__ = ()

    def acquired(self) -> None:
        return None

    def committed(self) -> None:
        return None

    @_never_raises
    def failed(self, exc: BaseException) -> None:
        if _is_lock_error(exc):
            get_db_logger().warning("lock error: %s", exc)


_NULL_WRITE = _NullWrite()


class _WriteRecord:
    __slots__ = ("intent", "migration", "previous", "started", "waited_ms")

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.waited_ms = 0.0
        self.intent: _Intent | None = getattr(_local, "intent", None)
        self.migration: tuple[str, str] | None = None
        self.previous = getattr(_local, "write", None)
        _local.write = self

    # -- caller callbacks ---------------------------------------------------
    @_never_raises
    def acquired(self) -> None:
        """The write lock is held and BEGIN IMMEDIATE has returned."""
        self.waited_ms = (time.perf_counter() - self.started) * 1000.0
        if self.waited_ms >= LOCK_WAIT_WARN_MS:
            get_db_logger().warning("lock wait ms=%.0f %s", self.waited_ms, self._subject())

    @_never_raises
    def committed(self) -> None:
        # Released first: whatever goes wrong below, the thread must not be
        # left believing it is still inside a write.
        self._release()
        flush()
        elapsed_ms = (time.perf_counter() - self.started) * 1000.0
        _stats.writes += 1
        if self.migration is not None:
            record_migration(self.migration[0], self.migration[1], elapsed_ms)
            return
        get_db_logger().info(
            "write %s ms=%.1f wait_ms=%.1f", self._subject(), elapsed_ms, self.waited_ms
        )

    @_never_raises
    def failed(self, exc: BaseException) -> None:
        self._release()
        flush()
        elapsed_ms = (time.perf_counter() - self.started) * 1000.0
        log = get_db_logger()
        if _is_lock_error(exc):
            _stats.locked += 1
            # SQLite's busy handler already retried for busy_timeout ms; by the
            # time this fires the queue never cleared.
            log.warning(
                "lock error %s ms=%.1f wait_ms=%.1f error=%s",
                self._subject(), elapsed_ms, self.waited_ms, exc,
            )
        else:
            # Only the exception type: an adapter's error text can quote the
            # row it choked on, and that row is the job seeker's data.
            log.warning(
                "rollback %s ms=%.1f error=%s",
                self._subject(), elapsed_ms, type(exc).__name__,
            )

    # -- helpers ------------------------------------------------------------
    def _release(self) -> None:
        _local.write = self.previous

    def _subject(self) -> str:
        intent = self.intent
        if intent is None:
            return "table=- op=TX"
        parts = [f"table={intent.table or '-'}", f"op={intent.op or 'TX'}"]
        if intent.row_id:
            parts.append(f"id={intent.row_id}")
        return " ".join(parts)


def begin_write() -> _WriteRecord | _NullWrite:
    """Start recording one write transaction.  Returns a recorder whose
    ``acquired``/``committed``/``failed`` hooks ``db.connection.write_tx``
    calls; the disabled version costs one comparison and an attribute load."""
    if not enabled():
        return _NULL_WRITE
    return _WriteRecord()
