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

import json
import logging
from typing import Any

from dreamjob.db.connection import to_json
from dreamjob.db.repositories import admin as admin_repo

log = logging.getLogger(__name__)
metrics_log = logging.getLogger("dreamjob.metrics")


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
    """
    payload = detail if isinstance(detail, str) or detail is None else to_json(detail)
    try:
        event_id = admin_repo.insert_audit(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            job_seeker_id=seeker_id,
            actor=actor or seeker_id or "system",
            detail=payload,
        )
    except Exception:  # noqa: BLE001 - auditing must not break the audited action
        log.exception("Failed to record audit event %s on %s %s", action, entity_type, entity_id)
        return None
    log.info(
        "audit action=%s entity=%s:%s seeker=%s", action, entity_type, entity_id, seeker_id
    )
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
