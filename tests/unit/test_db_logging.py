"""Database logging (NFR-701) and what it is forbidden to write down (NFR-702).

Two properties matter here and they pull against each other.  The log has to be
detailed enough to find a missing index - which means writing the statement
down - while SQLite's own trace callback hands us the *expanded* statement,
with every bound value inlined.  For this application those values are a job
seeker's CV text, e-mail address and phone number.  So the headline test runs
one deliberately slow query whose bind parameter is an e-mail address, and
asserts both halves at once: the WARNING is there, the address is not.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, query_one, update_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.observability import db_logging

FAKE_EMAIL = "nobody@example.invalid"
FAKE_PHONE = "+31 6 12345678"

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_LOG_DIR",
    "DREAMJOB_LOG_SQL",
    "DREAMJOB_LOG_SLOW_QUERY_MS",
    "DREAMJOB_ENV",
)

# 200k rows of a recursive CTE take tens of milliseconds on any machine that
# can run this suite, so a 5 ms threshold is slow-by-construction rather than
# slow-by-luck.  The parameter is what the test is really about.
SLOW_SQL = (
    "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 200000) "
    "SELECT count(*) AS total FROM seq WHERE :probe <> ''"
)


@pytest.fixture()
def log_file(tmp_path: Path) -> Iterator[Path]:
    """A throwaway database and a throwaway logs/ directory beside it."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_LOG_DIR"] = str(tmp_path / "logs")
    os.environ["DREAMJOB_LOG_SQL"] = "1"
    os.environ["DREAMJOB_LOG_SLOW_QUERY_MS"] = "5"
    os.environ["DREAMJOB_ENV"] = "development"
    get_settings.cache_clear()
    db_logging.reset()

    migrate()
    yield tmp_path / "logs" / "database.log"

    db_logging.reset()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {
            "email": FAKE_EMAIL,
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


# ---------------------------------------------------------------------------
# The headline: a slow query is visible, its parameters are not
# ---------------------------------------------------------------------------
def test_slow_statement_is_logged_at_warning_without_its_bind_parameters(
    log_file: Path,
) -> None:
    rows = query_all(SLOW_SQL, {"probe": FAKE_EMAIL})
    assert rows[0]["total"] == 200_000
    # A statement is timed from its start to the start of the next one, so one
    # ordinary query closes the slow one out - exactly as a running app does.
    query_one("SELECT 1 AS ok")

    text = _read(log_file)
    slow = [ln for ln in text.splitlines() if "slow statement" in ln]
    # Migrating a fresh database is itself slow enough to appear here, so pick
    # the line for the query under test rather than the first one.
    line = next((ln for ln in slow if "RECURSIVE seq" in ln), "")
    assert line, f"no slow-statement line for the query in:\n{text}"

    assert "WARN" in line
    # Five values in the statement: the CTE's 1, 1 and 200000, the probe and
    # the empty string it is compared to.  Counted, never written down.
    assert "params=5" in line
    assert "ms=" in line

    # The whole point: nothing anywhere in the file carries the address.
    assert FAKE_EMAIL not in text
    assert "nobody" not in text
    assert "?" in line                      # the value became a placeholder


def test_written_rows_do_not_leak_their_contents(log_file: Path) -> None:
    seeker_id = _seeker()
    update_row(
        "job_seeker",
        seeker_id,
        {"display_name": f"Test Seeker {FAKE_PHONE}", "updated_at": utcnow()},
    )

    text = _read(log_file)
    assert FAKE_EMAIL not in text
    assert FAKE_PHONE not in text
    assert "12345678" not in text
    # The primary key is the one value worth keeping: it is an opaque UUID and
    # it is what makes a write line answer "did my change save?".
    assert seeker_id in text


# ---------------------------------------------------------------------------
# Every write transaction
# ---------------------------------------------------------------------------
def test_writes_are_logged_with_table_operation_key_and_duration(log_file: Path) -> None:
    seeker_id = _seeker()
    update_row("job_seeker", seeker_id, {"display_name": "Renamed", "updated_at": utcnow()})

    lines = [ln for ln in _read(log_file).splitlines() if " write " in ln]
    inserts = [ln for ln in lines if "op=INSERT" in ln]
    updates = [ln for ln in lines if "op=UPDATE" in ln]

    assert inserts and updates
    assert f"table=job_seeker op=INSERT id={seeker_id}" in inserts[-1]
    assert f"table=job_seeker op=UPDATE id={seeker_id}" in updates[-1]
    assert "ms=" in inserts[-1]


def test_migrations_are_logged_with_version_and_duration(log_file: Path) -> None:
    text = _read(log_file)
    applied = [ln for ln in text.splitlines() if "migration applied" in ln]
    assert applied, f"no migration lines in:\n{text}"
    assert "version=001 name=initial" in applied[0]
    assert "ms=" in applied[0]


def test_summary_reports_statements_writes_and_average(log_file: Path) -> None:
    _seeker()
    query_one("SELECT 1 AS ok")
    db_logging.log_summary()

    summary = [ln for ln in _read(log_file).splitlines() if " summary " in ln]
    assert summary, "a healthy system should still say something"
    assert "statements=" in summary[-1]
    assert "writes=" in summary[-1]
    assert "avg_ms=" in summary[-1]
    assert "INFO" in summary[-1]


# ---------------------------------------------------------------------------
# The flag, and the one thing it must not silence
# ---------------------------------------------------------------------------
def test_tracing_is_off_in_production_by_default(log_file: Path) -> None:
    os.environ.pop("DREAMJOB_LOG_SQL")
    os.environ["DREAMJOB_ENV"] = "production"
    get_settings.cache_clear()
    db_logging.reset()

    assert db_logging.enabled() is False
    # A disabled write recorder must still be safe to drive.
    recorder = db_logging.begin_write()
    recorder.acquired()
    recorder.committed()


def test_lock_errors_are_logged_even_when_tracing_is_off(log_file: Path) -> None:
    """"database is locked" is the failure mode of a single-writer design
    (NFR-102).  Turning the hot-path flag off must not hide it."""
    os.environ["DREAMJOB_LOG_SQL"] = "0"
    get_settings.cache_clear()
    db_logging.reset()
    assert db_logging.enabled() is False

    recorder = db_logging.begin_write()
    recorder.acquired()
    recorder.failed(sqlite3.OperationalError("database is locked"))

    text = _read(log_file)
    assert "lock error" in text
    assert "database is locked" in text


def test_a_rollback_records_the_exception_type_only(log_file: Path) -> None:
    recorder = db_logging.begin_write()
    recorder.acquired()
    recorder.failed(sqlite3.IntegrityError(f"UNIQUE constraint failed on {FAKE_EMAIL}"))

    text = _read(log_file)
    assert "rollback" in text
    assert "error=IntegrityError" in text
    # Driver messages quote the row that choked; the row is the seeker's data.
    assert FAKE_EMAIL not in text


# ---------------------------------------------------------------------------
# Redaction, on its own
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO contact (id, email) VALUES ('abc', 'nobody@example.invalid')",
        "UPDATE job_seeker SET phone = '+31 6 12345678' WHERE id = 'abc'",
        "SELECT * FROM note WHERE body = 'he said ''call nobody@example.invalid'''",
        "INSERT INTO blob_store (data) VALUES (x'6e6f626f6479')",
    ],
)
def test_redaction_removes_every_literal(statement: str) -> None:
    shape, values = db_logging.redact(statement)
    assert values >= 1
    assert "nobody" not in shape
    assert "12345678" not in shape
    assert "?" in shape
    # Identifiers survive - without them the shape would be useless.
    assert shape.split()[0] in ("INSERT", "UPDATE", "SELECT")


def test_redaction_keeps_identifiers_that_contain_digits() -> None:
    shape, values = db_logging.redact("SELECT sha256, col2 FROM t WHERE n = 42 LIMIT 10")
    assert "sha256" in shape and "col2" in shape
    assert values == 2


def test_long_statements_are_truncated() -> None:
    shape, _ = db_logging.redact("SELECT " + ", ".join(f"c{i}" for i in range(500)) + " FROM t")
    assert len(shape) <= db_logging.MAX_SQL_CHARS + 4
    assert shape.endswith("...")


def test_describe_reads_operation_and_table_not_values() -> None:
    assert db_logging.describe("INSERT INTO company (id) VALUES ('x')") == ("company", "INSERT")
    assert db_logging.describe("UPDATE application SET x=1") == ("application", "UPDATE")
    assert db_logging.describe("DELETE FROM contact WHERE id='x'") == ("contact", "DELETE")
    assert db_logging.describe("SELECT 1") == (None, None)


def test_the_database_log_does_not_reach_the_console(log_file: Path) -> None:
    """Per-statement chatter has its own file; propagating it would drown the
    console the developer is actually reading."""
    _seeker()
    assert logging.getLogger(db_logging.LOGGER_NAME).propagate is False


def test_a_configured_channel_is_used_as_is(log_file: Path, tmp_path: Path) -> None:
    """When ``logs.setup_logging()`` has given the channel a file, this module
    must use it rather than open a second handle on the same path."""
    db_logging.reset()
    before = len(_read(log_file))
    channel = logging.getLogger(db_logging.LOGGER_NAME)
    shared = logging.FileHandler(tmp_path / "shared.log", encoding="utf-8")
    shared.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    channel.addHandler(shared)
    channel.setLevel(logging.INFO)
    try:
        files = [
            handler
            for handler in db_logging.get_db_logger().handlers
            if isinstance(handler, logging.FileHandler)
        ]
        assert files == [shared]
        db_logging.record_migration("099", "example", 12.5)
        assert "migration applied version=099" in (tmp_path / "shared.log").read_text()
        assert len(_read(log_file)) == before  # nothing opened database.log again
    finally:
        channel.removeHandler(shared)
        shared.close()
