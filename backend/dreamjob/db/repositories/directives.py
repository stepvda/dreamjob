"""Persistence for directive sets (FR-141, FR-148, FR-385, FR-344).

Directive sets are private data: every statement here filters on
``job_seeker_id`` (FR-344).  Versioning is append-only - editing a set that a
campaign already used creates a new version rather than rewriting the old one,
so a finished campaign can always be explained by the directives it actually
ran under (FR-148, FR-166).

The two read helpers at the bottom belong to the FR-147 pre-launch estimate:
the source catalogue it is measured against, and the profile rows the
proposal is pre-filled from.  They live here because all SQL lives in
repositories (CR-408).
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    execute,
    from_json,
    insert_row,
    query_all,
    query_one,
    update_row,
    utcnow,
)
from dreamjob.pipeline.directives import DirectiveSetPayload, to_columns

_JSON_COLUMNS = (
    "job_content",
    "company_type",
    "location",
    "work_arrangement",
    "compensation",
    "discretion_excluded_companies",
    "discretion_excluded_contacts",
)


def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Turn the stored JSON columns back into Python structures."""
    if row is None:
        return None
    out = dict(row)
    for column in _JSON_COLUMNS:
        default: Any = [] if column.startswith("discretion_") else {}
        out[column] = from_json(out.get(column), default) or default
    out["spontaneous_only"] = bool(out.get("spontaneous_only"))
    out["discretion_mode"] = bool(out.get("discretion_mode"))
    return out


def next_version(job_seeker_id: str, name: str) -> int:
    """The next version number for a directive-set name (FR-148)."""
    row = query_one(
        "SELECT MAX(version) AS v FROM directive_set WHERE job_seeker_id = ? AND name = ?",
        (job_seeker_id, name),
    )
    return int(row["v"] or 0) + 1 if row else 1


def create(job_seeker_id: str, payload: DirectiveSetPayload) -> str:
    """Store a new directive set, versioned within its name (FR-148)."""
    values = to_columns(payload)
    values.update(
        {
            "job_seeker_id": job_seeker_id,
            "version": next_version(job_seeker_id, payload.name),
            "created_at": utcnow(),
        }
    )
    return insert_row("directive_set", values)


def get(directive_set_id: str, job_seeker_id: str) -> dict[str, Any] | None:
    return _decode(
        query_one(
            "SELECT * FROM directive_set WHERE id = ? AND job_seeker_id = ?",
            (directive_set_id, job_seeker_id),
        )
    )


def get_latest_by_name(job_seeker_id: str, name: str) -> dict[str, Any] | None:
    return _decode(
        query_one(
            "SELECT * FROM directive_set WHERE job_seeker_id = ? AND name = ? "
            "ORDER BY version DESC LIMIT 1",
            (job_seeker_id, name),
        )
    )


def list_for_seeker(job_seeker_id: str, *, all_versions: bool = False) -> list[dict[str, Any]]:
    """Saved directive sets - the latest version of each name by default."""
    if all_versions:
        sql = (
            "SELECT * FROM directive_set WHERE job_seeker_id = ? "
            "ORDER BY name, version DESC"
        )
    else:
        sql = (
            "SELECT * FROM directive_set d WHERE d.job_seeker_id = ? AND d.version = ("
            "  SELECT MAX(v.version) FROM directive_set v "
            "  WHERE v.job_seeker_id = d.job_seeker_id AND v.name = d.name"
            ") ORDER BY d.created_at DESC"
        )
    return [r for r in (_decode(row) for row in query_all(sql, (job_seeker_id,))) if r]


def list_versions(job_seeker_id: str, name: str) -> list[dict[str, Any]]:
    rows = query_all(
        "SELECT * FROM directive_set WHERE job_seeker_id = ? AND name = ? ORDER BY version DESC",
        (job_seeker_id, name),
    )
    return [r for r in (_decode(row) for row in rows) if r]


def update_in_place(
    directive_set_id: str, job_seeker_id: str, payload: DirectiveSetPayload
) -> dict[str, Any] | None:
    """Edit a version that no campaign has used yet (FR-148)."""
    current = get(directive_set_id, job_seeker_id)
    if current is None:
        return None
    values = to_columns(payload)
    if values["name"] != current["name"]:
        # A rename moves the row into another name's version line; keeping the
        # old number would collide with a version that name already carries and
        # the set would drop out of the latest-per-name listing.
        values["version"] = next_version(job_seeker_id, values["name"])
    update_row("directive_set", directive_set_id, values)
    return get(directive_set_id, job_seeker_id)


def update_columns(
    directive_set_id: str, job_seeker_id: str, values: dict[str, Any]
) -> dict[str, Any] | None:
    """Partial update of named columns - used by the discretion controls (FR-385)."""
    current = get(directive_set_id, job_seeker_id)
    if current is None:
        return None
    update_row("directive_set", directive_set_id, values)
    return get(directive_set_id, job_seeker_id)


def campaign_usage(directive_set_id: str, job_seeker_id: str) -> int:
    """How many campaigns reference this set - a used set is never rewritten."""
    row = query_one(
        "SELECT COUNT(*) AS n FROM campaign WHERE directive_set_id = ? AND job_seeker_id = ?",
        (directive_set_id, job_seeker_id),
    )
    return int(row["n"]) if row else 0


def delete(directive_set_id: str, job_seeker_id: str) -> bool:
    """Delete an unused directive set.  Sets a campaign used stay (FR-166)."""
    if campaign_usage(directive_set_id, job_seeker_id):
        raise ValueError("Directive set is used by a campaign and cannot be deleted")
    return bool(
        execute(
            "DELETE FROM directive_set WHERE id = ? AND job_seeker_id = ?",
            (directive_set_id, job_seeker_id),
        )
    )


# ---------------------------------------------------------------------------
# Reads for the FR-147 proposal and estimate
# ---------------------------------------------------------------------------


def list_enabled_sources() -> list[dict[str, Any]]:
    """Source catalogue rows the estimate is measured against (FR-147, FR-161)."""
    return query_all(
        "SELECT adapter_key, display_name, source_type, coverage_countries, "
        "       coverage_industries, query_capabilities, access_method, rate_limit_rps, "
        "       cost_per_call_eur, tos_status, enabled, requires_ack, acknowledged_at "
        "FROM source_catalogue ORDER BY source_type, adapter_key"
    )


def latest_composite_profile(job_seeker_id: str, persona_id: str | None = None) -> dict | None:
    """Newest composite profile, optionally for one persona (FR-147, FR-442)."""
    if persona_id:
        return query_one(
            "SELECT * FROM composite_profile WHERE job_seeker_id = ? AND persona_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (job_seeker_id, persona_id),
        )
    return query_one(
        "SELECT * FROM composite_profile WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
        (job_seeker_id,),
    )


def latest_dream_job_model(job_seeker_id: str, persona_id: str | None = None) -> dict | None:
    if persona_id:
        return query_one(
            "SELECT * FROM dream_job_model WHERE job_seeker_id = ? AND persona_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (job_seeker_id, persona_id),
        )
    return query_one(
        "SELECT * FROM dream_job_model WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
        (job_seeker_id,),
    )


def latest_profile_version(job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM profile_version WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
        (job_seeker_id,),
    )
