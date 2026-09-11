"""Append-only audit trail and structured metrics (NFR-702, NFR-701).

NFR-702 asks for an immutable record of who approved and sent each
application, and which profile and document versions were used.  Every slice
that performs such an act calls :func:`record_audit`; the signature is fixed
so callers elsewhere in the code base keep working:

    record_audit("application.approved", "application_package", pkg_id,
                 seeker_id=seeker.id,
                 detail={"profile_version_id": ..., "cv_pdf_path": ...})

Immutability is a property of the write path, not of SQLite: this module and
``db.repositories.admin`` offer insert and read only, and nothing anywhere
updates or deletes an ``audit_event`` row.  The single exception is erasure
of a job seeker (FR-108), which removes their rows and leaves an anonymous
marker behind - see ``repositories.seekers.record_erasure_marker``.

Auditing must never break the operation it is recording, so a failed write is
logged and swallowed rather than raised.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
from collections import deque
from typing import Any

from dreamjob.db.connection import to_json
from dreamjob.db.repositories import admin as admin_repo

log = logging.getLogger(__name__)
metrics_log = logging.getLogger("dreamjob.metrics")

#: How many audit events may wait in memory when the write path is unavailable.
#: Bounded so a database that stays locked cannot grow the process without
#: limit; oldest-first eviction keeps the most recent trail when it overflows.
_MAX_PENDING_AUDITS = 500
#: Events flushed opportunistically after each successful audit write.  Small,
#: so a healthy request does not pay for the backlog it is draining.
_FLUSH_PER_SUCCESS = 10

_pending_audits: deque[dict[str, Any]] = deque()
_pending_lock = threading.Lock()
_pending_dropped = 0
_atexit_registered = False


def _enqueue_audit(params: dict[str, Any]) -> None:
    """Keep one audit event alive in memory after a failed write."""
    global _atexit_registered, _pending_dropped
    with _pending_lock:
        if len(_pending_audits) >= _MAX_PENDING_AUDITS:
            _pending_audits.popleft()
            _pending_dropped += 1
            log.error(
                "audit buffer full (%d); dropped the oldest event",
                _MAX_PENDING_AUDITS,
            )
        _pending_audits.append(params)
    if not _atexit_registered:
        atexit.register(flush_pending_audits)
        _atexit_registered = True


def pending_audit_count() -> int:
    """How many audit events are waiting for the database (tests, diagnostics)."""
    with _pending_lock:
        return len(_pending_audits)


def flush_pending_audits(limit: int = _FLUSH_PER_SUCCESS) -> int:
    """Try to write buffered audit events.  Never raises; returns how many landed.

    Called after a successful audit write and at interpreter exit.  It writes
    through the same repository path, so a flush inherits the lock retry; an
    event that still cannot land stays buffered for the next attempt.
    """
    flushed = 0
    while flushed < limit:
        with _pending_lock:
            if not _pending_audits:
                return flushed
            params = _pending_audits[0]
        try:
            admin_repo.insert_audit(**params)
        except Exception:  # noqa: BLE001 - the buffer is the fallback, not a new failure
            log.warning("Could not flush a buffered audit event", exc_info=True)
            return flushed
        with _pending_lock:
            if _pending_audits and _pending_audits[0] is params:
                _pending_audits.popleft()
        flushed += 1
    return flushed


def record_audit(
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    seeker_id: str | None = None,
    detail: Any = None,
    actor: str | None = None,
) -> str | None:
    """Append one row to the audit trail (NFR-702).  Returns its id.

    ``actor`` defaults to the job seeker id, which is who acted in almost
    every case; background work passes ``actor="system"``.

    The write goes through ``insert_row``/``write_tx``, so it retries a
    transient lock for the full budget.  If it still cannot land - the
    database held for longer than any request should wait - the event is kept
    in a bounded in-process buffer and retried by the next audit write or at
    exit, rather than being dropped.  It never raises: auditing must not break
    the operation it is recording.
    """
    payload = detail if isinstance(detail, str) or detail is None else to_json(detail)
    params = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "job_seeker_id": seeker_id,
        "actor": actor or seeker_id or "system",
        "detail": payload,
    }
    try:
        event_id = admin_repo.insert_audit(**params)
    except Exception:  # noqa: BLE001 - auditing must not break the audited action
        _enqueue_audit(params)
        log.exception("Failed to record audit event %s on %s %s", action, entity_type, entity_id)
        return None
    log.info(
        "audit action=%s entity=%s:%s seeker=%s", action, entity_type, entity_id, seeker_id
    )
    # A healthy write is proof the database is reachable again: drain a little
    # of whatever the last lock left behind.
    if pending_audit_count():
        flush_pending_audits()
    return event_id



def trail_for_entity(entity_type: str, entity_id: str, limit: int = 100) -> list[dict]:
    """The full history of one record - what NFR-702 is read for."""
    return admin_repo.list_audit(entity_type=entity_type, entity_id=entity_id, limit=limit)


def trail_for_seeker(seeker_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
    return admin_repo.list_audit(job_seeker_id=seeker_id, limit=limit, offset=offset)


def log_metric(name: str, **fields: Any) -> None:
    """Emit one structured metric line (NFR-701).

    Per-campaign and per-adapter counters - pages fetched, extraction success
    rate, tokens, cost - are emitted as single-line JSON so a log shipper can
    consume them without parsing prose.
    """
    metrics_log.info(json.dumps({"metric": name, **fields}, default=str))
