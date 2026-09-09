"""One line per HTTP request (NFR-701).

    2026-09-09 10:14:22.481  INFO   request   a1b2c3d4  POST /api/profile/uploads/cv  201  1.24s  seeker=7995a3

Written as raw ASGI rather than ``BaseHTTPMiddleware`` for three reasons: the
byte counts are only visible at the message level, an exception raised by a
route reaches us unwrapped so the traceback is the real one, and nothing is
buffered on the way through.

The level is chosen so that reading ``errors.log`` is enough to know something
is wrong and reading ``requests.log`` is enough to know things are working:

* **5xx** - ERROR with the traceback.
* **slower than ``DREAMJOB_LOG_SLOW_REQUEST_MS``** - WARNING.  A request that
  succeeded but took four seconds is a problem in waiting.
* **4xx** - INFO with the ``detail`` the client was given, so a support
  question can be answered from the log.
* **anything else** - INFO.  Successful work is logged, not just failure.
* **health polls and CORS preflights** - DEBUG.  A monitor hitting
  ``/api/health`` every five seconds would bury everything else; a *failing*
  health check still goes through the rules above.

The job seeker id is logged, never the session token (NFR-202).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dreamjob.config import get_settings
from dreamjob.observability.logs import (
    correlation_scope,
    format_duration,
    get_logger,
    get_seeker_id,
    redact_query,
    short_seeker,
)

log = get_logger("request")

CORRELATION_HEADER = "x-correlation-id"
_INBOUND_HEADERS = (b"x-correlation-id", b"x-request-id")
_SESSION_COOKIE = "dreamjob_session"

# Paths whose success is not worth a line at INFO.
_QUIET_PATHS = frozenset({"/api/health"})

# Enough of an error body to carry the ``detail`` FastAPI puts in it.
_MAX_DETAIL_CAPTURE = 2048
_MAX_DETAIL_CHARS = 300

# token hash -> (job seeker id, cached at).  A log line should not cost a
# database read per request; a session's owner does not change, so a short TTL
# is plenty and the cache stays small.
_SEEKER_CACHE: dict[str, tuple[str, float]] = {}
_SEEKER_CACHE_TTL = 60.0
_SEEKER_CACHE_MAX = 512


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return ""


def _inbound_correlation_id(scope: Scope) -> str:
    """Adopt the client's id when it sends one, so a browser bug report and a
    server log line can be joined up."""
    for name in _INBOUND_HEADERS:
        value = _header(scope, name)
        if value:
            return value
    return ""


def _token_from(scope: Scope) -> str:
    auth = _header(scope, b"authorization")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    cookies = _header(scope, b"cookie")
    for part in cookies.split(";"):
        name, _, value = part.strip().partition("=")
        if name == _SESSION_COOKIE:
            return value
    return ""


def _seeker_id(scope: Scope) -> str:
    """Who is making this request, for the log line only.

    A dependency that has already resolved the session wins (it bound the id
    into the context); otherwise the session is looked up by token *hash*, so
    the token itself is never held anywhere by this module.  Any failure here
    is swallowed: a missing annotation on a log line is not worth a 500.
    """
    bound = get_seeker_id()
    if bound:
        return bound
    token = _token_from(scope)
    if not token:
        return ""
    try:
        from dreamjob.db.repositories import seekers as seeker_repo  # noqa: PLC0415
        from dreamjob.security.crypto import hash_token  # noqa: PLC0415

        key = hash_token(token)
        cached = _SEEKER_CACHE.get(key)
        now = time.monotonic()
        if cached and now - cached[1] < _SEEKER_CACHE_TTL:
            return cached[0]
        row = seeker_repo.session_by_hash(key)
        seeker = str(row["job_seeker_id"]) if row else ""
        if len(_SEEKER_CACHE) >= _SEEKER_CACHE_MAX:
            _SEEKER_CACHE.clear()
        _SEEKER_CACHE[key] = (seeker, now)
        return seeker
    except Exception:  # noqa: BLE001 - identifying the caller is best effort
        return ""


def _detail_from(body: bytes) -> str:
    """The ``detail`` string FastAPI returned to the client, if there was one."""
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return body.decode("utf-8", "replace")[:_MAX_DETAIL_CHARS]
    if isinstance(payload, dict) and payload.get("detail") is not None:
        return str(payload["detail"])[:_MAX_DETAIL_CHARS]
    return ""


class RequestLogMiddleware:
    """Log every request, with a correlation id shared by everything it does."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        slow_seconds = max(0, settings.log_slow_request_ms) / 1000.0
        method = scope.get("method", "?")
        path = scope.get("path", "")
        query = redact_query(scope.get("query_string", b"").decode("latin-1"))
        target = f"{path}?{query}" if query else path

        state: dict[str, Any] = {"status": 0, "bytes_out": 0, "bytes_in": 0, "error_body": b""}
        started = time.perf_counter()

        with correlation_scope(_inbound_correlation_id(scope)) as cid:

            async def counting_receive() -> Message:
                message = await receive()
                if message["type"] == "http.request":
                    state["bytes_in"] += len(message.get("body", b""))
                return message

            async def counting_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    state["status"] = message["status"]
                    # Hand the id back so a person reporting a problem can
                    # quote the one string that finds it in every log file.
                    headers = list(message.get("headers", []))
                    headers.append((CORRELATION_HEADER.encode(), cid.encode()))
                    message = {**message, "headers": headers}
                elif message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    state["bytes_out"] += len(body)
                    if state["status"] >= 400 and len(state["error_body"]) < _MAX_DETAIL_CAPTURE:
                        state["error_body"] += body[:_MAX_DETAIL_CAPTURE]
                await send(message)

            try:
                await self.app(scope, counting_receive, counting_send)
            except Exception:
                self._emit(
                    method, target, 500, time.perf_counter() - started, scope, state,
                    slow_seconds, exc_info=True,
                )
                raise
            self._emit(
                method, target, state["status"], time.perf_counter() - started, scope, state,
                slow_seconds,
            )

    def _emit(
        self,
        method: str,
        target: str,
        status: int,
        elapsed: float,
        scope: Scope,
        state: dict[str, Any],
        slow_seconds: float,
        exc_info: bool = False,
    ) -> None:
        # A body that was never read leaves nothing to count; the declared
        # length is the honest answer in that case.
        bytes_in = state["bytes_in"] or _content_length(scope)
        fields: dict[str, Any] = {"bytes_in": bytes_in, "bytes_out": state["bytes_out"]}
        seeker = short_seeker(_seeker_id(scope))
        if seeker:
            fields["seeker"] = seeker
        if status >= 400 and not exc_info:
            detail = _detail_from(state["error_body"])
            if detail:
                fields["detail"] = detail

        if status >= 500 or exc_info:
            level = logging.ERROR
        elif slow_seconds and elapsed >= slow_seconds:
            level = logging.WARNING
            fields["slow_after"] = format_duration(slow_seconds)
        elif status < 400 and (scope.get("path") in _QUIET_PATHS or method == "OPTIONS"):
            level = logging.DEBUG
        else:
            level = logging.INFO

        log.log(
            level,
            "%s %s  %s  %s",
            method,
            target,
            status or "-",
            format_duration(elapsed),
            exc_info=exc_info,
            extra={"fields": fields},
        )


def _content_length(scope: Scope) -> int:
    raw = _header(scope, b"content-length")
    try:
        return int(raw)
    except ValueError:
        return 0
