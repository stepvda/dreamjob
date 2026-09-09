"""Logging backbone (NFR-701, NFR-702).

Everything the operator needs after the fact lands in one directory, one file
per concern, so a question has an obvious file to open:

===============  =========================================================
``app.log``      application events - anything that is not one of the below
``requests.log`` one line per HTTP request (``observability.middleware``)
``database.log`` database activity, written through ``get_logger("database")``
``frontend.log`` client-side events shipped to ``POST /api/logs/client``
``errors.log``   every WARNING and above from all of the above, together
``audit.log``    a mirror of the ``audit_event`` writes (NFR-702)
===============  =========================================================

Three decisions shape the rest of the module.

* **One line per event, in columns.**  Logs are read by a person first and a
  machine second, so the default format is aligned text.  Set
  ``DREAMJOB_LOG_FORMAT=json`` and the same records come out as JSON lines for
  a shipper.  Colour is decided per handler and only ever on a TTY - a file
  with escape codes in it is a file nobody can grep.

* **A correlation id in every line.**  The middleware puts a short id in a
  context variable for the duration of a request; every line logged while
  handling it carries that id, so the eight characters at the start of a line
  are enough to pull the whole story of one request out of six files.

* **Redaction happens on the way in, not on the way out.**  A filter on every
  handler rewrites the record before any formatter sees it, so a password that
  is accidentally interpolated into a message never reaches the disk - not
  app.log, not errors.log, not the console.  ``test_logging.py`` proves it.

``errors.log`` is deliberately fed by a handler at WARNING regardless of
``DREAMJOB_LOG_LEVEL``: the level knob controls how much detail is kept, never
whether a problem is recorded.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

from dreamjob.config import get_settings

# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

# Short name -> logger name.  The short names are what get_logger() takes and
# what the /api/logs endpoints accept, so they are the vocabulary of the whole
# logging surface, UI included.
CHANNELS: dict[str, str] = {
    "app": "dreamjob",
    "request": "dreamjob.request",
    "database": "dreamjob.database",
    "frontend": "dreamjob.frontend",
    "audit": "dreamjob.audit",
}

# Short name -> file name.  ``errors`` has no logger of its own; it is a second
# handler on all of the others.
LOG_FILES: dict[str, str] = {
    "app": "app.log",
    "requests": "requests.log",
    "database": "database.log",
    "frontend": "frontend.log",
    "errors": "errors.log",
    "audit": "audit.log",
}

# The audit trail is written by ``security.audit``, which logs one line per
# ``audit_event`` row.  Mirroring is done by attaching the audit handler to
# that logger rather than by asking the module to write the file itself, so
# NFR-702 evidence lands in audit.log without the audited code knowing.
AUDIT_LOGGERS = ("dreamjob.audit", "dreamjob.security.audit")

# Chatty third parties.  Their DEBUG is never what an operator came for.
QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "asyncio", "multipart", "PIL")

_LEVEL_LABELS = {"WARNING": "WARN", "CRITICAL": "CRIT"}

# Logger name -> column label, where the derivation below would be unhelpful.
_CHANNEL_LABELS = {"dreamjob.security.audit": "audit"}


def channel_of(logger_name: str) -> str:
    """The column label for a logger: the slice it belongs to, not its module.

    ``dreamjob.egress.client`` reads as ``egress``; that is the level at which
    someone scanning a file wants to filter.
    """
    if logger_name in _CHANNEL_LABELS:
        return _CHANNEL_LABELS[logger_name]
    parts = logger_name.split(".")
    if parts[0] == "dreamjob":
        return parts[1] if len(parts) > 1 else "app"
    return parts[0]


# ---------------------------------------------------------------------------
# Correlation id (contextvars)
# ---------------------------------------------------------------------------

_correlation_id: ContextVar[str] = ContextVar("dreamjob_correlation_id", default="")
_seeker_id: ContextVar[str] = ContextVar("dreamjob_seeker_id", default="")

CID_LENGTH = 8
_CID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def new_correlation_id() -> str:
    """Eight hex characters - long enough to be unique across a day's traffic,
    short enough that a person can compare two of them at a glance."""
    return uuid4().hex[:CID_LENGTH]


def get_correlation_id() -> str:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> str:
    """Adopt a caller-supplied id (a browser or a proxy may send one).

    Sanitised and truncated: it ends up in a log line, and a log line must not
    be forgeable into looking like two.
    """
    cleaned = _CID_SAFE_RE.sub("", value or "")[:32]
    cid = cleaned or new_correlation_id()
    _correlation_id.set(cid)
    return cid


def get_seeker_id() -> str:
    return _seeker_id.get()


def bind_seeker(seeker_id: str | None) -> None:
    """Attach the acting job seeker to the current context.

    Only the id, never the session token (NFR-202).  Lines show the first six
    characters, which is enough to follow one person through a file and not
    enough to be a useful identifier if the file leaks.
    """
    _seeker_id.set(seeker_id or "")


def short_seeker(seeker_id: str | None) -> str:
    return (seeker_id or "")[:6]


@contextmanager
def correlation_scope(cid: str | None = None, seeker_id: str | None = None) -> Iterator[str]:
    """Give a block of work its own correlation id.

    Used by the middleware per request, and available to background jobs so a
    campaign run is as traceable as a click.
    """
    cid_token = _correlation_id.set(_CID_SAFE_RE.sub("", cid or "")[:32] or new_correlation_id())
    seeker_token = _seeker_id.set(seeker_id or _seeker_id.get())
    try:
        yield _correlation_id.get()
    finally:
        _correlation_id.reset(cid_token)
        _seeker_id.reset(seeker_token)


# ---------------------------------------------------------------------------
# Redaction (NFR-202, CR-410)
# ---------------------------------------------------------------------------

REDACTED = "[redacted]"

# Ordered longest-first so the alternation prefers the specific name.
_SECRET_NAME = (
    r"authorization|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"session[_-]?token|auth[_-]?token|bearer[_-]?token|client[_-]?secret|master[_-]?key|"
    r"private[_-]?key|secret[_-]?key|session[_-]?secret|set[-_]?cookie|credentials?|"
    r"passphrase|password|passwd|secret|token|cookie|session|csrf|xsrf|totp|otp|"
    r"signature|auth|pwd"
)
# ``_`` and ``-`` count as boundaries, so ``x_api_key`` matches and ``author``
# does not.
_LEFT = r"(?<![A-Za-z0-9])"
_RIGHT = r"(?![A-Za-z0-9])"

_JSON_SECRET_RE = re.compile(rf'(?i)("(?:{_SECRET_NAME})"\s*:\s*)("(?:[^"\\]|\\.)*"|\d+)')
_KV_SECRET_RE = re.compile(
    rf"""(?ix)
    ({_LEFT}(?:{_SECRET_NAME}){_RIGHT})   # the key
    (\s*[=:]\s*)                          # = or :
    (['"]?)                               # optional quote
    (?:bearer|basic|token)?\s*            # ``Authorization: Bearer <jwt>``
    [^\s,;&'"()\[\]{{}}]+                 # the value, up to a delimiter
    """
)
# A bare ``Bearer <jwt>`` or ``Basic <base64>``, with no key in front of it.
_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=-]{6,})")

_SECRET_NAME_RE = re.compile(rf"(?i){_LEFT}(?:{_SECRET_NAME}){_RIGHT}")

# Query-string keys worth hiding that are too generic to redact in prose: an
# OAuth ``code`` is a credential, while a ``status_code=500`` in a message is
# not, so these apply to a query string only.
_QUERY_EXTRA_RE = re.compile(r"(?i)^(?:code|state|key|sig|access[_-]?code)$")
_QUERY_PAIR_RE = re.compile(r"([^&=?]+)=([^&]*)")


def redact_text(text: str) -> str:
    """Remove anything credential-shaped from a line about to be written.

    Applied to every record by :class:`RedactingFilter`, so this is the single
    place that decides what may not reach the disk.  It is deliberately
    over-eager: a redacted value that was harmless costs a debugging session, a
    leaked one costs an account.

    Idempotent - brackets are not part of a value, so ``[redacted]`` is not
    itself redactable.  That matters because a caller may redact defensively
    before logging and the handler filter then runs the rule again.
    """
    if not text:
        return text
    out = _JSON_SECRET_RE.sub(lambda m: f'{m.group(1)}"{REDACTED}"', text)
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", out)
    return _SCHEME_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", out)


def redact_mapping(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Same rule applied to structured fields, by key name and by value."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if _SECRET_NAME_RE.search(str(key)):
            out[key] = REDACTED
        elif isinstance(value, Mapping):
            out[key] = redact_mapping(value)
        elif isinstance(value, str):
            out[key] = redact_text(value)
        else:
            out[key] = value
    return out


def redact_query(query: str) -> str:
    """Redact a URL query string before the path it belongs to is logged.

    Substituted in place rather than re-encoded, so what reaches the file is
    the query as it arrived apart from the values that had to go.
    """
    if not query:
        return ""

    def _pair(match: re.Match[str]) -> str:
        key = match.group(1)
        if _SECRET_NAME_RE.search(key) or _QUERY_EXTRA_RE.match(key):
            return f"{key}={REDACTED}"
        return match.group(0)

    return _QUERY_PAIR_RE.sub(_pair, query)


class RedactingFilter(logging.Filter):
    """Rewrite the record itself, once, before any handler formats it.

    Interpolation is resolved here (``msg`` becomes the finished string and
    ``args`` is cleared) because a secret usually arrives as an argument, not
    as part of the format string.  The record is marked so the filter is cheap
    when the same record passes through the second and third handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_dreamjob_redacted", False):
            return True
        try:
            record.msg = redact_text(record.getMessage())
        except Exception:  # noqa: BLE001 - a bad format string must not lose the line
            record.msg = f"<unformattable log record from {record.name}>"
        record.args = ()
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            record.fields = redact_mapping(fields)
        record._dreamjob_redacted = True
        return True


class ContextFilter(logging.Filter):
    """Stamp the correlation id and job seeker onto every record.

    Done at emit time rather than at format time: the context variables belong
    to the task that logged the line, and by the time a handler formats it we
    are still in that task - but stamping makes that guarantee explicit and
    lets a caller override either value with ``extra=``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "cid", ""):
            record.cid = get_correlation_id()
        if not getattr(record, "seeker", ""):
            seeker = get_seeker_id()
            if seeker:
                record.seeker = short_seeker(seeker)
        return True


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_DIM = "\033[2m"
_LEVEL_COLOUR = {
    "DEBUG": "\033[38;5;244m",
    "INFO": "\033[38;5;36m",
    "WARNING": "\033[38;5;178m",
    "ERROR": "\033[38;5;167m",
    "CRITICAL": "\033[1;38;5;167m",
}


def _timestamp(created: float, msecs: float) -> str:
    return f"{datetime.fromtimestamp(created):%Y-%m-%d %H:%M:%S}.{int(msecs):03d}"


class HumanFormatter(logging.Formatter):
    """The default: aligned columns, one event per line.

        2026-09-09 10:14:22.481  INFO   request   a1b2c3d4  POST /api/x  201  1.24s  seeker=7995a3

    ``WARNING`` and ``CRITICAL`` are abbreviated so the level column is five
    characters wide for every level and the rest of the line stays aligned;
    ``grep WARN`` still finds them.
    """

    def __init__(self, colour: bool = False) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        level = _LEVEL_LABELS.get(record.levelname, record.levelname)
        cid = getattr(record, "cid", "") or "-" * CID_LENGTH
        channel = channel_of(record.name)
        message = record.getMessage()

        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping) and fields:
            message += "  " + " ".join(f"{k}={_render(v)}" for k, v in fields.items())
        seeker = getattr(record, "seeker", "")
        if seeker and "seeker=" not in message:
            message += f"  seeker={seeker}"

        if self.colour:
            colour = _LEVEL_COLOUR.get(record.levelname, "")
            head = (
                f"{_DIM}{_timestamp(record.created, record.msecs)}{_RESET}  "
                f"{colour}{level:<5}{_RESET}  {channel:<8}  {_DIM}{cid:<8}{_RESET}  "
            )
        else:
            head = (
                f"{_timestamp(record.created, record.msecs)}  "
                f"{level:<5}  {channel:<8}  {cid:<8}  "
            )

        line = head + message
        if record.exc_info:
            # Indented so the one-event-per-line scan still works: a traceback
            # is a continuation of the line above it, not a new event.
            trace = redact_text(self.formatException(record.exc_info))
            line += "\n" + "\n".join(f"    {ln}" for ln in trace.splitlines())
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a shipper (NFR-701)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "channel": channel_of(record.name),
            "logger": record.name,
            "cid": getattr(record, "cid", ""),
            "msg": record.getMessage(),
        }
        seeker = getattr(record, "seeker", "")
        if seeker:
            payload["seeker"] = seeker
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def _render(value: Any) -> str:
    """Field values must not break the one-line contract."""
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\n", " ").replace("\r", " ")
    return f'"{text}"' if " " in text else text


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

_MARK = "_dreamjob_handler"
_configured_dir: Path | None = None


def _formatter(colour: bool = False) -> logging.Formatter:
    if get_settings().log_format.strip().lower() == "json":
        return JsonFormatter()
    return HumanFormatter(colour=colour)


def _level(name: str, default: int = logging.INFO) -> int:
    resolved = logging.getLevelName(name.strip().upper())
    return resolved if isinstance(resolved, int) else default


def _file_handler(path: Path, level: int, max_bytes: int, backups: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(_formatter())
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactingFilter())
    setattr(handler, _MARK, True)
    return handler


def _drop_our_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _MARK, False):
            logger.removeHandler(handler)
            handler.close()


def setup_logging(force: bool = False) -> Path:
    """Configure the handlers, levels and rotation.  Called from the lifespan.

    Idempotent: calling it twice for the same directory is a no-op, which
    matters because a test client may start the app more than once in a
    process.  ``force=True`` rebuilds against the current settings.
    """
    global _configured_dir

    settings = get_settings()
    log_dir = settings.abs_log_dir
    if _configured_dir == log_dir and not force:
        return log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    level = _level(settings.log_level)
    max_bytes = max(1, settings.log_max_mb) * 1024 * 1024
    backups = max(0, settings.log_backups)

    # WARNING and above are recorded whatever the configured level says: the
    # level knob is about detail, not about whether a problem is kept.
    logger_level = min(level, logging.WARNING)
    errors = _file_handler(log_dir / LOG_FILES["errors"], logging.WARNING, max_bytes, backups)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(_formatter(colour=sys.stderr.isatty()))
    console.addFilter(ContextFilter())
    console.addFilter(RedactingFilter())
    setattr(console, _MARK, True)

    # Root: everything that is not one of the dedicated channels, plus every
    # library that logs through the standard hierarchy.
    root = logging.getLogger()
    _drop_our_handlers(root)
    root.setLevel(logger_level)
    root.addHandler(_file_handler(log_dir / LOG_FILES["app"], level, max_bytes, backups))
    root.addHandler(errors)
    root.addHandler(console)

    # Dedicated channels.  ``propagate = False`` keeps requests out of app.log;
    # each one carries the errors handler itself so errors.log stays complete.
    dedicated = {
        "dreamjob.request": LOG_FILES["requests"],
        "dreamjob.database": LOG_FILES["database"],
        "dreamjob.frontend": LOG_FILES["frontend"],
    }
    for logger_name, filename in dedicated.items():
        logger = logging.getLogger(logger_name)
        _drop_our_handlers(logger)
        logger.setLevel(logger_level)
        logger.propagate = False
        logger.addHandler(_file_handler(log_dir / filename, level, max_bytes, backups))
        logger.addHandler(errors)
        logger.addHandler(console)

    # NFR-702: the audit mirror.  One handler shared by both logger names, so
    # the file has one open handle and rotation stays coherent.
    audit_handler = _file_handler(log_dir / LOG_FILES["audit"], logging.INFO, max_bytes, backups)
    for logger_name in AUDIT_LOGGERS:
        logger = logging.getLogger(logger_name)
        _drop_our_handlers(logger)
        logger.setLevel(min(logger_level, logging.INFO))
        logger.propagate = False
        logger.addHandler(audit_handler)
        logger.addHandler(errors)
        logger.addHandler(console)

    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    # uvicorn keeps its own handlers; let its records reach ours instead, and
    # drop its access log entirely - the middleware writes a better one.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False

    _configured_dir = log_dir
    logging.getLogger("dreamjob.observability").info(
        "Logging ready",
        extra={
            "fields": {
                "dir": str(log_dir),
                "level": settings.log_level,
                "format": settings.log_format,
                "rotate_mb": settings.log_max_mb,
                "keep": backups,
            }
        },
    )
    return log_dir


def get_logger(name: str = "app") -> logging.Logger:
    """A logger already wired to the right file.

    ``get_logger("request")`` / ``"database"`` / ``"frontend"`` / ``"audit"``
    return the dedicated channels; ``get_logger(__name__)`` returns a module
    logger under the application tree, which lands in app.log.
    """
    if name in CHANNELS:
        return logging.getLogger(CHANNELS[name])
    if name == "dreamjob" or name.startswith("dreamjob."):
        return logging.getLogger(name)
    return logging.getLogger(f"dreamjob.{name}")


def log_path(name: str) -> Path:
    """Absolute path of one log file.  Raises for an unknown name, which is
    what keeps ``GET /api/logs/tail`` off the rest of the filesystem."""
    if name not in LOG_FILES:
        raise KeyError(name)
    return get_settings().abs_log_dir / LOG_FILES[name]


# ---------------------------------------------------------------------------
# Slow operations
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


@contextmanager
def operation(
    name: str,
    logger: logging.Logger | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    """Log the start and the outcome of something slow, with its duration.

    The product owner's requirement is that the log confirms things *worked*,
    not only that they broke, so an upload, a campaign run, an LLM call or a
    document generation brackets itself with this::

        with operation("cv.parse", seeker=seeker.id) as op:
            ...
            op["pages"] = 3

    Fields added to the yielded dict appear on the completion line.  Failure
    re-raises after logging at ERROR with the traceback; the caller's error
    handling is unchanged.
    """
    log = logger or get_logger("app")
    started = time.perf_counter()
    log.log(level, "%s start", name, extra={"fields": dict(fields)})
    extra: dict[str, Any] = {}
    try:
        yield extra
    except Exception:
        elapsed = time.perf_counter() - started
        log.error(
            "%s failed",
            name,
            exc_info=True,
            extra={"fields": {**fields, **extra, "duration": format_duration(elapsed)}},
        )
        raise
    else:
        elapsed = time.perf_counter() - started
        log.log(
            level,
            "%s ok",
            name,
            extra={"fields": {**fields, **extra, "duration": format_duration(elapsed)}},
        )
