"""Log ingestion and inspection (NFR-701, FR-361).

Three surfaces:

* **``POST /api/logs/client``** - the browser ships what it saw into
  ``logs/frontend.log``.  Deliberately unauthenticated: the most valuable
  client-side error is the one on the sign-in screen, and a job seeker who
  cannot log in cannot send an authenticated log line.  Everything that makes
  an open endpoint safe is therefore done here instead - a per-client and a
  global rate limit, a body cap, a batch cap, a length cap per entry, a fixed
  set of fields, and a level ceiling so nobody can forge a CRITICAL.  The
  worst an abuser achieves is filling a rotating file that was already
  bounded at ``DREAMJOB_LOG_MAX_MB`` x ``DREAMJOB_LOG_BACKUPS``.

* **``GET /api/logs/tail``** - the last N lines of one named file, for the
  administration screen (FR-361).  Administrator only, and the file is chosen
  from a fixed table, never from the path the caller sends.

* **``GET /api/logs/summary``** - counts by level over a window, which is what
  turns "something is wrong" into "12 warnings in the last hour".

The client's IP is never written down.  It is hashed to six characters, which
is enough to see that a hundred lines came from one machine and useless as an
identifier afterwards (NFR-301).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from dreamjob.api.deps import CurrentSeeker, current_admin
from dreamjob.observability.logs import (
    LOG_FILES,
    get_logger,
    log_path,
    redact_text,
    set_correlation_id,
)

router = APIRouter()

Admin = Annotated[CurrentSeeker, Depends(current_admin)]

frontend_log = get_logger("frontend")

# --- Ingestion limits ------------------------------------------------------
# The SPA buffers up to 200 entries and may prepend one overflow notice, so
# the batch cap is set above what its own bound can produce: rejecting a whole
# shipment would throw away the error that prompted it.
MAX_BODY_BYTES = 256 * 1024
MAX_ENTRIES_PER_BATCH = 250
MAX_MESSAGE_CHARS = 2000
MAX_CONTEXT_KEYS = 20
MAX_CONTEXT_CHARS = 300

RATE_WINDOW_SECONDS = 60
MAX_BATCHES_PER_CLIENT = 30
MAX_ENTRIES_GLOBAL = 3000

# A client cannot claim to be worse than an error; CRITICAL is reserved for
# things that happened on this side of the wire.
_CLIENT_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "log": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.ERROR,
    "critical": logging.ERROR,
}

# --- Reading limits --------------------------------------------------------
MAX_TAIL_LINES = 2000
DEFAULT_TAIL_LINES = 200
_TAIL_BLOCK_BYTES = 64 * 1024
# Never read more than this from the end of a file, whatever is asked for.
MAX_SCAN_BYTES = 4 * 1024 * 1024
# A dashboard refreshes; its scan is kept smaller still, and says so when the
# window it could afford did not reach the whole period.
SUMMARY_SCAN_LINES = 20_000
SUMMARY_SCAN_BYTES = 1024 * 1024
MAX_SUMMARY_MINUTES = 7 * 24 * 60
RECENT_PROBLEMS = 10

_LEVEL_ALIASES = {"WARN": "WARNING", "CRIT": "CRITICAL"}
_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
# ``2026-09-09 10:14:22.481  WARN   request   a1b2c3d4  ...``
_TEXT_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s\s(\S+)\s")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ClientLogEntry(BaseModel):
    """One thing the browser saw.

    The field names are the SPA shipper's (``frontend/src/observability/
    logger.js``); the shorter aliases are accepted as well so a curl, a test
    or a later client is not forced into one vocabulary.  Every field is
    capped here, so a malformed batch is refused by validation rather than by
    the log file.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    level: str = Field("info", max_length=16)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    route: str | None = Field(
        None, max_length=500, validation_alias=AliasChoices("route", "url", "path")
    )
    correlation_id: str | None = Field(
        None, max_length=64, validation_alias=AliasChoices("correlation_id", "cid")
    )
    client_id: str | None = Field(
        None, max_length=64, validation_alias=AliasChoices("client_id", "client")
    )
    user_agent: str | None = Field(
        None, max_length=300, validation_alias=AliasChoices("user_agent", "ua")
    )
    source: str | None = Field(
        None, max_length=64, validation_alias=AliasChoices("source", "logger")
    )
    ts: str | None = Field(None, max_length=40)
    context: dict[str, Any] | None = None


class ClientLogBatch(BaseModel):
    entries: list[ClientLogEntry] = Field(min_length=1, max_length=MAX_ENTRIES_PER_BATCH)


class ClientLogResult(BaseModel):
    accepted: int


class TailResult(BaseModel):
    name: str
    lines: list[str]
    returned: int
    size_bytes: int
    truncated: bool


class SummaryResult(BaseModel):
    window_minutes: int
    since: str
    totals: dict[str, int]
    files: dict[str, dict[str, int]]
    recent: list[str]
    scanned_files: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class _FixedWindow:
    """Requests per client per minute, held in memory.

    Single-process by design, like the rest of the deployment (CR-407).  The
    table is cleared wholesale when it grows past its bound, which costs one
    window of leniency and can never leak memory.
    """

    MAX_CLIENTS = 2048

    def __init__(self, limit: int, window: int) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, tuple[int, float]] = {}

    def allow(self, key: str, cost: int = 1) -> bool:
        now = time.monotonic()
        count, started = self._hits.get(key, (0, now))
        if now - started >= self.window:
            count, started = 0, now
        if count + cost > self.limit:
            self._hits[key] = (count, started)
            return False
        if len(self._hits) >= self.MAX_CLIENTS:
            self._hits.clear()
        self._hits[key] = (count + cost, started)
        return True

    def retry_after(self, key: str) -> int:
        _, started = self._hits.get(key, (0, time.monotonic()))
        return max(1, int(self.window - (time.monotonic() - started)))


_per_client = _FixedWindow(MAX_BATCHES_PER_CLIENT, RATE_WINDOW_SECONDS)
_global = _FixedWindow(MAX_ENTRIES_GLOBAL, RATE_WINDOW_SECONDS)


def _client_key(request: Request) -> str:
    """A stable, non-identifying handle for one caller (NFR-301)."""
    host = request.client.host if request.client else "unknown"
    return hashlib.sha256(host.encode()).hexdigest()[:6]


# ---------------------------------------------------------------------------
# POST /api/logs/client - unauthenticated ingestion
# ---------------------------------------------------------------------------


@router.post("/client", response_model=ClientLogResult, status_code=status.HTTP_202_ACCEPTED)
async def ingest_client_logs(request: Request, response: Response) -> ClientLogResult:
    """Write a batch of browser events to ``logs/frontend.log``.

    Unauthenticated on purpose (see the module docstring).  Returns 202: the
    client is told the batch was taken, never what happened to it, so the
    endpoint gives an abuser nothing to probe with.
    """
    origin = _client_key(request)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Log batch too large"
        )

    if not _per_client.allow(origin):
        response.headers["Retry-After"] = str(_per_client.retry_after(origin))
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many log batches")

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Log batch too large"
        )
    try:
        batch = ClientLogBatch.model_validate_json(raw)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Log batch could not be read"
        ) from None

    if not _global.allow("all", cost=len(batch.entries)):
        response.headers["Retry-After"] = str(_global.retry_after("all"))
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Log intake is saturated")

    for entry in batch.entries:
        _write_client_entry(entry, origin)
    return ClientLogResult(accepted=len(batch.entries))


def _write_client_entry(entry: ClientLogEntry, origin: str) -> None:
    level = _CLIENT_LEVELS.get(entry.level.strip().lower(), logging.INFO)
    fields: dict[str, Any] = {}
    if entry.route:
        fields["route"] = entry.route
    if entry.source:
        fields["source"] = entry.source
    if entry.client_id:
        fields["client"] = entry.client_id
    fields["origin"] = origin
    if entry.ts:
        fields["client_ts"] = entry.ts
    # The browser string is long and is on every line if we let it be; it earns
    # its place only where somebody is about to ask "on which browser?".
    if level >= logging.WARNING and entry.user_agent:
        fields["ua"] = entry.user_agent[:120]
    for key, value in list((entry.context or {}).items())[:MAX_CONTEXT_KEYS]:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        fields[f"ctx.{key}"] = text[:MAX_CONTEXT_CHARS]

    # A client-supplied correlation id joins the browser's story to the
    # server's; it is sanitised by ``set_correlation_id`` before it is used,
    # and the middleware's scope restores the request's own id afterwards.
    if entry.correlation_id:
        set_correlation_id(entry.correlation_id)
    # Redaction runs again on the handler; doing it here as well means a token
    # in a client message is gone before it is ever a log record at all.
    frontend_log.log(level, "%s", redact_text(entry.message), extra={"fields": fields})


# ---------------------------------------------------------------------------
# GET /api/logs/tail - administrator only
# ---------------------------------------------------------------------------


@router.get("/tail", response_model=TailResult)
def tail(
    admin: Admin,
    name: str = Query("errors", description="One of " + ", ".join(sorted(LOG_FILES))),
    lines: int = Query(DEFAULT_TAIL_LINES, ge=1, le=MAX_TAIL_LINES),
    level: str | None = Query(None, description="Keep only lines at this level or above"),
    contains: str | None = Query(None, max_length=200),
) -> TailResult:
    """The end of one log file, for the administration screen (FR-361)."""
    path = _resolve(name)
    size = path.stat().st_size if path.exists() else 0
    found, truncated = _tail_lines(path, lines)

    floor = _level_index(level) if level else -1
    if floor >= 0:
        found = [ln for ln in found if _level_index(_parse_line(ln)[1]) >= floor]
    if contains:
        needle = contains.lower()
        found = [ln for ln in found if needle in ln.lower()]

    return TailResult(
        name=name,
        lines=found,
        returned=len(found),
        size_bytes=size,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# GET /api/logs/summary - administrator only
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=SummaryResult)
def summary(
    admin: Admin,
    minutes: int = Query(60, ge=1, le=MAX_SUMMARY_MINUTES),
) -> SummaryResult:
    """Counts by level over a window - "12 warnings in the last hour"."""
    since = datetime.now() - timedelta(minutes=minutes)
    totals = dict.fromkeys(_LEVEL_NAMES, 0)
    per_file: dict[str, dict[str, int]] = {}
    scanned: list[dict[str, Any]] = []
    recent: list[str] = []

    for name in LOG_FILES:
        path = _resolve(name)
        counts = dict.fromkeys(_LEVEL_NAMES, 0)
        clipped = False
        if path.exists():
            lines, clipped = _scan_from_end(path)
            for line in lines:
                stamp, level = _parse_line(line)
                if stamp is None or level not in counts:
                    continue
                if stamp < since:
                    # Lines are appended in order, so the first line older
                    # than the window ends the scan for this file - and the
                    # window was reached, so nothing was missed.
                    clipped = False
                    break
                counts[level] += 1
                if name == "errors" and len(recent) < RECENT_PROBLEMS:
                    recent.append(line)
        per_file[name] = counts
        for level_name, count in counts.items():
            # errors.log duplicates lines that are already counted in the file
            # they came from; counting it again would double every warning.
            if name != "errors":
                totals[level_name] += count
        scanned.append(
            {
                "name": name,
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "clipped": clipped,
            }
        )

    return SummaryResult(
        window_minutes=minutes,
        since=since.isoformat(timespec="seconds"),
        totals=totals,
        files=per_file,
        recent=recent,
        scanned_files=scanned,
    )


# ---------------------------------------------------------------------------
# Reading helpers
# ---------------------------------------------------------------------------


def _resolve(name: str) -> Path:
    """Names come from a table, never from the caller's string."""
    try:
        return log_path(name)
    except KeyError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown log '{name}'. Available: {', '.join(sorted(LOG_FILES))}",
        ) from None


def _tail_lines(path: Path, count: int, max_bytes: int = MAX_SCAN_BYTES) -> tuple[list[str], bool]:
    """The last ``count`` lines, read backwards in blocks.

    Stops as soon as it has enough newlines, so showing 200 lines of a 10 MB
    file touches one block rather than the file.  ``max_bytes`` is the hard
    ceiling for the case where the lines are enormous.  Returns the lines and
    whether anything above them was left unread.
    """
    if not path.exists():
        return [], False
    size = path.stat().st_size
    if size == 0:
        return [], False

    chunks: list[bytes] = []
    read = 0
    newlines = 0
    position = size
    with path.open("rb") as handle:
        while position > 0 and newlines <= count and read < max_bytes:
            step = min(_TAIL_BLOCK_BYTES, position, max_bytes - read)
            position -= step
            handle.seek(position)
            data = handle.read(step)
            chunks.append(data)
            read += step
            newlines += data.count(b"\n")

    text = b"".join(reversed(chunks)).decode("utf-8", "replace")
    if position > 0:
        # The first line of the window is almost certainly a fragment.
        text = text.split("\n", 1)[-1]
    found = text.splitlines()
    return found[-count:], len(found) > count or position > 0


def _scan_from_end(path: Path) -> tuple[list[str], bool]:
    """Lines newest first, so a scan can stop once it leaves the window."""
    found, truncated = _tail_lines(path, SUMMARY_SCAN_LINES, SUMMARY_SCAN_BYTES)
    found.reverse()
    return found, truncated


def _parse_line(line: str) -> tuple[datetime | None, str]:
    """Timestamp and level of one line, in either supported format."""
    if line.startswith("{"):
        try:
            payload = json.loads(line)
            stamp = datetime.fromisoformat(str(payload.get("ts", "")))
            return stamp, _LEVEL_ALIASES.get(payload.get("level", ""), payload.get("level", ""))
        except (ValueError, TypeError):
            return None, ""
    match = _TEXT_LINE_RE.match(line)
    if not match:
        return None, ""
    try:
        stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None, ""
    level = match.group(2)
    return stamp, _LEVEL_ALIASES.get(level, level)


def _level_index(level: str | None) -> int:
    normalised = _LEVEL_ALIASES.get((level or "").upper(), (level or "").upper())
    return _LEVEL_NAMES.index(normalised) if normalised in _LEVEL_NAMES else -1
