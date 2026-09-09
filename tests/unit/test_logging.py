"""The logging backbone (NFR-701, NFR-702).

Runs against a throwaway log directory and a throwaway database; no network.
The redaction tests are the ones that matter most - they are the proof that a
password or a session token cannot reach any of the six files.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.observability import logs as logs_mod
from dreamjob.observability.middleware import RequestLogMiddleware
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DREAMJOB_LOG_DIR",
    "DREAMJOB_LOG_LEVEL",
    "DREAMJOB_LOG_FORMAT",
    "DREAMJOB_LOG_MAX_MB",
    "DREAMJOB_LOG_BACKUPS",
    "DREAMJOB_LOG_SLOW_REQUEST_MS",
)

_TOUCHED_LOGGERS = (
    "",
    "dreamjob.request",
    "dreamjob.database",
    "dreamjob.frontend",
    "dreamjob.audit",
    "dreamjob.security.audit",
)


def _detach() -> None:
    """Give the next test a clean logging tree.

    Handlers are process-global, so a test that left one attached would keep
    writing into a deleted tmp directory.
    """
    for name in _TOUCHED_LOGGERS:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if getattr(handler, "_dreamjob_handler", False):
                logger.removeHandler(handler)
                handler.close()
        logger.propagate = True
    logs_mod._configured_dir = None


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path) -> Iterator[Path]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["DREAMJOB_LOG_DIR"] = str(tmp_path / "logs")
    os.environ["DREAMJOB_LOG_LEVEL"] = "DEBUG"
    os.environ["DREAMJOB_LOG_FORMAT"] = "text"
    os.environ.pop("DREAMJOB_LOG_SLOW_REQUEST_MS", None)
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()
    logs_mod.setup_logging(force=True)
    yield tmp_path / "logs"

    _detach()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def read(log_dir: Path, name: str) -> str:
    path = log_dir / logs_mod.LOG_FILES[name]
    return path.read_text(encoding="utf-8") if path.exists() else ""


def reconfigure(**env: str) -> None:
    """Change a log setting mid-test and rebuild against it."""
    os.environ.update(env)
    get_settings.cache_clear()
    logs_mod.setup_logging(force=True)


# ---------------------------------------------------------------------------
# Files, format and rotation
# ---------------------------------------------------------------------------


def test_every_log_file_exists_after_setup(isolated: Path) -> None:
    for filename in logs_mod.LOG_FILES.values():
        assert (isolated / filename).exists(), filename


def test_rotation_is_configured_from_settings(isolated: Path) -> None:
    reconfigure(DREAMJOB_LOG_MAX_MB="10", DREAMJOB_LOG_BACKUPS="5")
    handlers = [h for h in logging.getLogger().handlers if getattr(h, "_dreamjob_handler", False)]
    rotating = [h for h in handlers if hasattr(h, "maxBytes")]
    assert rotating, "the application log must rotate"
    for handler in rotating:
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 5


def test_human_format_is_one_aligned_line_per_event(isolated: Path) -> None:
    logs_mod.get_logger("dreamjob.egress.client").info("fetched a page")
    line = read(isolated, "app").strip().splitlines()[-1]
    assert re.match(
        r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}  INFO   egress    \S{8}  fetched a page$", line
    ), line
    assert "\033[" not in line  # colour never reaches a file


def test_json_format_is_selectable(isolated: Path) -> None:
    reconfigure(DREAMJOB_LOG_FORMAT="json")
    with logs_mod.correlation_scope("deadbeef"):
        logs_mod.get_logger("app").warning("disk filling up", extra={"fields": {"free_mb": 12}})
    payload = json.loads(read(isolated, "app").strip().splitlines()[-1])
    assert payload["level"] == "WARNING"
    assert payload["msg"] == "disk filling up"
    assert payload["cid"] == "deadbeef"
    assert payload["free_mb"] == 12


def test_channels_write_to_their_own_file(isolated: Path) -> None:
    logs_mod.get_logger("request").info("GET /api/health  200  3ms")
    logs_mod.get_logger("database").info("SELECT job_seeker  1 row  2ms")
    logs_mod.get_logger("frontend").info("router mounted")
    logs_mod.get_logger("app").info("catalogue synchronised")

    assert "GET /api/health" in read(isolated, "requests")
    assert "SELECT job_seeker" in read(isolated, "database")
    assert "router mounted" in read(isolated, "frontend")
    assert "catalogue synchronised" in read(isolated, "app")
    # requests.log exists so that app.log stays readable.
    assert "GET /api/health" not in read(isolated, "app")


def test_errors_log_collects_warnings_from_every_channel(isolated: Path) -> None:
    for channel in ("app", "request", "database", "frontend"):
        logs_mod.get_logger(channel).warning("%s is unhappy", channel)
    logs_mod.get_logger("app").error("something broke")

    errors = read(isolated, "errors")
    for channel in ("app", "request", "database", "frontend"):
        assert f"{channel} is unhappy" in errors
    assert "something broke" in errors
    assert "WARN" in errors and "ERROR" in errors


def test_warnings_survive_a_raised_log_level(isolated: Path) -> None:
    """The level knob controls detail, never whether a problem is recorded."""
    reconfigure(DREAMJOB_LOG_LEVEL="ERROR")
    logs_mod.get_logger("app").info("routine work")
    logs_mod.get_logger("app").warning("a warning nobody asked for")

    assert "routine work" not in read(isolated, "app")
    assert "a warning nobody asked for" in read(isolated, "errors")


# ---------------------------------------------------------------------------
# Redaction (NFR-202, CR-410)
# ---------------------------------------------------------------------------

SECRETS = {
    "password": "Str0ng-Passphrase!2026",
    "api key": "sk-live-51H9zZq2example",
    "bearer token": "eyJhbGciOiJIUzI1NiJ9.payload.signature",
    "session token": "9f8e7d6c5b4a39281706",
}


def test_no_secret_of_any_kind_reaches_any_log_file(isolated: Path) -> None:
    """The proof that redaction is not optional: every file, every level."""
    log = logs_mod.get_logger("app")
    log.error("login failed password=%s", SECRETS["password"])
    log.warning('provider call {"api_key": "%s"}', SECRETS["api key"])
    log.error("Authorization: Bearer %s", SECRETS["bearer token"])
    log.info("cookie: dreamjob_session=%s", SECRETS["session token"])
    log.info("token stored", extra={"fields": {"session_token": SECRETS["session token"]}})
    logs_mod.get_logger("frontend").error(
        "xhr failed", extra={"fields": {"headers.authorization": f"Bearer {SECRETS['bearer token']}"}}
    )

    for name in logs_mod.LOG_FILES:
        body = read(isolated, name)
        for label, value in SECRETS.items():
            assert value not in body, f"{label} leaked into {name}.log"
    assert logs_mod.REDACTED in read(isolated, "app")
    assert logs_mod.REDACTED in read(isolated, "errors")


def test_redaction_leaves_ordinary_diagnostics_alone(isolated: Path) -> None:
    logs_mod.get_logger("app").info("GET /api/opportunities status_code=500 duration=1.24s")
    body = read(isolated, "app")
    assert "status_code=500" in body
    assert "duration=1.24s" in body


def test_redaction_is_idempotent() -> None:
    """A caller may redact defensively; the handler filter then runs it again."""
    for line in (
        "token=abc123",
        "Authorization: Bearer eyJhbGciOi.payload.sig",
        '{"api_key": "sk-live-1"}',
        "set-cookie: dreamjob_session=zzz",
    ):
        once = logs_mod.redact_text(line)
        assert logs_mod.redact_text(once) == once, once


def test_query_string_secrets_are_redacted_but_the_rest_is_readable() -> None:
    redacted = logs_mod.redact_query("code=4/0Abc&limit=50&access_token=zzz")
    assert "4/0Abc" not in redacted
    assert "zzz" not in redacted
    assert "limit=50" in redacted


# ---------------------------------------------------------------------------
# Correlation id
# ---------------------------------------------------------------------------


def test_one_correlation_id_ties_a_whole_request_together(isolated: Path) -> None:
    with logs_mod.correlation_scope() as cid:
        logs_mod.get_logger("app").info("started work")
        logs_mod.get_logger("database").info("wrote a row")
        logs_mod.get_logger("request").info("POST /api/x  201  40ms")
    assert len(cid) == logs_mod.CID_LENGTH
    for name in ("app", "database", "requests"):
        assert cid in read(isolated, name)


def test_a_forged_correlation_id_cannot_forge_a_line(isolated: Path) -> None:
    """A caller-supplied id ends up in a line, so it may not contain a line."""
    before = len(read(isolated, "app").splitlines())
    with logs_mod.correlation_scope("aaaa\n2101-01-01 00:00:00.000  ERROR  fake") as cid:
        logs_mod.get_logger("app").info("still one event")
    assert "\n" not in cid and " " not in cid
    lines = read(isolated, "app").splitlines()
    assert len(lines) == before + 1  # one event, one line - not two
    assert not any(line.startswith("2101-") for line in lines)


# ---------------------------------------------------------------------------
# Slow operations
# ---------------------------------------------------------------------------


def test_operation_logs_the_start_and_the_successful_outcome(isolated: Path) -> None:
    with logs_mod.operation("cv.parse", seeker="abc123") as op:
        op["pages"] = 3
    body = read(isolated, "app")
    assert "cv.parse start" in body
    assert "cv.parse ok" in body
    assert "pages=3" in body
    assert "duration=" in body


def test_operation_logs_a_failure_with_its_duration_and_re_raises(isolated: Path) -> None:
    with pytest.raises(ValueError), logs_mod.operation("campaign.run"):
        raise ValueError("no sources enabled")
    errors = read(isolated, "errors")
    assert "campaign.run failed" in errors
    assert "ValueError: no sources enabled" in errors


# ---------------------------------------------------------------------------
# Request middleware
# ---------------------------------------------------------------------------


@pytest.fixture
def http(isolated: Path) -> Iterator[TestClient]:
    app = FastAPI()

    @app.get("/ok")
    def ok() -> dict:
        return {"ok": True}

    @app.get("/slow")
    def slow() -> dict:
        time.sleep(0.05)
        return {"ok": True}

    @app.get("/missing")
    def missing() -> dict:
        raise HTTPException(404, "That opportunity is not yours")

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("the roof fell in")

    app.add_middleware(RequestLogMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_a_successful_request_is_logged_with_its_duration(isolated: Path, http: TestClient) -> None:
    assert http.get("/ok").status_code == 200
    line = read(isolated, "requests").strip().splitlines()[-1]
    assert "GET /ok" in line
    assert "  200  " in line
    assert re.search(r"\d+(ms|\.\d\ds)", line)
    assert "bytes_out=" in line and "bytes_in=" in line
    assert "INFO" in line


def test_the_correlation_id_is_returned_to_the_caller(isolated: Path, http: TestClient) -> None:
    resp = http.get("/ok", headers={"X-Correlation-ID": "browser01"})
    assert resp.headers["x-correlation-id"] == "browser01"
    assert "browser01" in read(isolated, "requests")


def test_a_client_error_is_logged_at_info_with_the_detail(isolated: Path, http: TestClient) -> None:
    assert http.get("/missing").status_code == 404
    line = read(isolated, "requests").strip().splitlines()[-1]
    assert "  404  " in line
    assert "That opportunity is not yours" in line
    assert "INFO" in line
    assert "404" not in read(isolated, "errors")


def test_a_server_error_is_logged_at_error_with_the_traceback(
    isolated: Path, http: TestClient
) -> None:
    assert http.get("/boom").status_code == 500
    errors = read(isolated, "errors")
    assert "GET /boom" in errors
    assert "ERROR" in errors
    assert "RuntimeError: the roof fell in" in errors
    assert "Traceback" in errors


def test_a_slow_request_is_logged_as_a_warning(isolated: Path, http: TestClient) -> None:
    reconfigure(DREAMJOB_LOG_SLOW_REQUEST_MS="10")
    assert http.get("/slow").status_code == 200
    line = read(isolated, "requests").strip().splitlines()[-1]
    assert "WARN" in line
    assert "slow_after=" in line


def test_a_health_poll_does_not_bury_the_log(isolated: Path) -> None:
    app = FastAPI()

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    app.add_middleware(RequestLogMiddleware)
    reconfigure(DREAMJOB_LOG_LEVEL="INFO")
    with TestClient(app) as client:
        client.get("/api/health")
    assert "/api/health" not in read(isolated, "requests")


def test_the_session_token_never_reaches_the_request_log(
    isolated: Path, http: TestClient
) -> None:
    """NFR-202: the log identifies the caller, never their credential."""
    token = "s3cr3t-session-token-value"
    http.get("/ok", headers={"Authorization": f"Bearer {token}"})
    for name in logs_mod.LOG_FILES:
        assert token not in read(isolated, name)


# ---------------------------------------------------------------------------
# Audit mirror (NFR-702)
# ---------------------------------------------------------------------------


def test_audit_writes_are_mirrored_into_audit_log(isolated: Path) -> None:
    from dreamjob.security.audit import record_audit

    event_id = record_audit(
        "application.approved",
        "application_package",
        "pkg-1",
        seeker_id="seeker-1",
        detail={"cv_version": 3},
    )
    assert event_id
    audit = read(isolated, "audit")
    assert "application.approved" in audit
    assert "application_package:pkg-1" in audit
    # The mirror is a mirror, not a second copy of the application log.
    assert "application.approved" not in read(isolated, "app")


# ---------------------------------------------------------------------------
# /api/logs
# ---------------------------------------------------------------------------


@pytest.fixture
def api(isolated: Path) -> Iterator[TestClient]:
    from dreamjob.api.routers import auth as auth_router
    from dreamjob.api.routers import logs as logs_router

    logs_router._per_client._hits.clear()
    logs_router._global._hits.clear()

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")
    app.include_router(logs_router.router, prefix="/api/logs")
    with TestClient(app) as client:
        yield client


def _become_admin(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/register",
        json={
            "email": "owner@example.com",
            "display_name": "Test Owner",
            "password": "Str0ng-Passphrase!2026",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_admin"] is True


def test_client_logs_are_accepted_without_a_session(isolated: Path, api: TestClient) -> None:
    """The error worth capturing most is the one on the sign-in screen."""
    resp = api.post(
        "/api/logs/client",
        json={
            "entries": [
                {
                    # The vocabulary the SPA's shipper actually sends.
                    "ts": "2026-09-09T10:14:22.481Z",
                    "level": "error",
                    "route": "/login",
                    "message": "sign-in failed",
                    "client_id": "b7f1a2c3",
                    "correlation_id": "abc12345",
                    "user_agent": "Mozilla/5.0 (Macintosh)",
                    "context": {"status": 500},
                },
                {"level": "info", "message": "app booted", "context": {"build": "0.1.0"}},
            ]
        },
    )
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 2

    frontend = read(isolated, "frontend")
    assert "sign-in failed" in frontend
    assert "app booted" in frontend
    assert "ctx.build=0.1.0" in frontend
    assert "route=/login" in frontend
    assert "client=b7f1a2c3" in frontend
    assert "abc12345" in frontend
    # A client-reported error is a real error: it belongs in errors.log too.
    assert "sign-in failed" in read(isolated, "errors")


def test_client_logs_accept_the_short_aliases_too(isolated: Path, api: TestClient) -> None:
    resp = api.post(
        "/api/logs/client",
        json={"entries": [{"level": "warn", "message": "hmm", "url": "/x", "cid": "zz11"}]},
    )
    assert resp.status_code == 202
    assert "route=/x" in read(isolated, "frontend")


def test_client_logs_cannot_smuggle_a_secret_in(isolated: Path, api: TestClient) -> None:
    api.post(
        "/api/logs/client",
        json={"entries": [{"level": "warn", "message": "retry with token=abcdef123456"}]},
    )
    assert "abcdef123456" not in read(isolated, "frontend")
    assert logs_mod.REDACTED in read(isolated, "frontend")


def test_client_logs_are_capped_and_rate_limited(isolated: Path, api: TestClient) -> None:
    from dreamjob.api.routers import logs as logs_router

    too_many = {"entries": [{"message": "x"}] * (logs_router.MAX_ENTRIES_PER_BATCH + 1)}
    assert api.post("/api/logs/client", json=too_many).status_code == 422

    body = {"entries": [{"message": "flood"}]}
    codes = {api.post("/api/logs/client", json=body).status_code for _ in range(40)}
    assert 429 in codes, "an open endpoint must be bounded"


def test_client_logs_refuse_an_oversized_body(isolated: Path, api: TestClient) -> None:
    from dreamjob.api.routers import logs as logs_router

    payload = json.dumps({"entries": [{"message": "a" * 1900} for _ in range(200)]})
    assert len(payload) > logs_router.MAX_BODY_BYTES
    resp = api.post(
        "/api/logs/client", content=payload, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413


def test_tail_and_summary_are_administrator_only(isolated: Path, api: TestClient) -> None:
    assert api.get("/api/logs/tail").status_code == 401
    assert api.get("/api/logs/summary").status_code == 401


def test_tail_returns_the_end_of_a_named_log(isolated: Path, api: TestClient) -> None:
    _become_admin(api)
    for i in range(10):
        logs_mod.get_logger("app").info("event number %d", i)

    resp = api.get("/api/logs/tail", params={"name": "app", "lines": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["returned"] == 3
    assert "event number 9" in body["lines"][-1]
    assert body["size_bytes"] > 0

    assert api.get("/api/logs/tail", params={"name": "../../etc/passwd"}).status_code == 404


def test_tail_reads_backwards_across_blocks(isolated: Path, api: TestClient) -> None:
    """A 200-line tail of a large file must not read the whole file."""
    from dreamjob.api.routers import logs as logs_router

    _become_admin(api)
    log = logs_mod.get_logger("app")
    for i in range(1500):
        log.info("padding line %d %s", i, "x" * 120)
    assert (isolated / "app.log").stat().st_size > logs_router._TAIL_BLOCK_BYTES

    body = api.get("/api/logs/tail", params={"name": "app", "lines": 50}).json()
    assert body["returned"] == 50
    assert "padding line 1499" in body["lines"][-1]
    assert body["truncated"] is True
    # Every line is whole - a block boundary must not produce a fragment.
    assert all(re.match(r"^\d{4}-\d\d-\d\d ", line) for line in body["lines"])


def test_tail_filters_by_level(isolated: Path, api: TestClient) -> None:
    _become_admin(api)
    logs_mod.get_logger("app").info("all is well")
    logs_mod.get_logger("app").warning("disk is filling")

    body = api.get("/api/logs/tail", params={"name": "app", "level": "WARNING"}).json()
    assert any("disk is filling" in line for line in body["lines"])
    assert not any("all is well" in line for line in body["lines"])


def test_summary_counts_by_level_over_a_window(isolated: Path, api: TestClient) -> None:
    _become_admin(api)
    for _ in range(12):
        logs_mod.get_logger("app").warning("a warning")
    logs_mod.get_logger("app").error("an error")

    body = api.get("/api/logs/summary", params={"minutes": 60}).json()
    assert body["window_minutes"] == 60
    assert body["totals"]["WARNING"] >= 12
    assert body["totals"]["ERROR"] >= 1
    assert body["files"]["errors"]["WARNING"] >= 12
    assert any("a warning" in line for line in body["recent"])
    assert {f["name"] for f in body["scanned_files"]} == set(logs_mod.LOG_FILES)
