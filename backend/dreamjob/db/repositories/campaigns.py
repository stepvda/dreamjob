"""Campaign, plan and source-catalogue SQL (FR-161..166, FR-185, NFR-403).

Campaigns and their source plans are *private* data: every read here takes a
``job_seeker_id`` and filters on it, except the deliberately named
``*_any`` helpers, which background jobs use once the request that owns them
has already been authorised (FR-101, FR-344).
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

CAMPAIGN_JSON_COLUMNS = ("caps", "reuse_report")
PLAN_JSON_COLUMNS = ("native_query", "caps")


def _decode(row: dict | None, json_columns: tuple[str, ...]) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in json_columns:
        if col in out:
            out[col] = from_json(out[col], None)
    return out


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def create_campaign(job_seeker_id: str, values: dict) -> str:
    payload = dict(values)
    payload["job_seeker_id"] = job_seeker_id
    payload.setdefault("status", "draft")
    payload.setdefault("created_at", utcnow())
    return insert_row("campaign", payload)


def get_campaign(campaign_id: str, job_seeker_id: str) -> dict | None:
    row = query_one(
        "SELECT * FROM campaign WHERE id = ? AND job_seeker_id = ?", (campaign_id, job_seeker_id)
    )
    return _decode(row, CAMPAIGN_JSON_COLUMNS)


def get_campaign_any(campaign_id: str) -> dict | None:
    """Background-job read: the request that scheduled the job authorised it."""
    row = query_one("SELECT * FROM campaign WHERE id = ?", (campaign_id,))
    return _decode(row, CAMPAIGN_JSON_COLUMNS)


def list_campaigns(job_seeker_id: str, limit: int = 100) -> list[dict]:
    rows = query_all(
        "SELECT * FROM campaign WHERE job_seeker_id = ? ORDER BY created_at DESC LIMIT ?",
        (job_seeker_id, limit),
    )
    return [_decode(r, CAMPAIGN_JSON_COLUMNS) or {} for r in rows]


def update_campaign(campaign_id: str, values: dict) -> None:
    update_row("campaign", campaign_id, values)


def set_stage(campaign_id: str, stage: str, status: str | None = None) -> None:
    values: dict[str, Any] = {"stage": stage}
    if status:
        values["status"] = status
        if status == "running":
            row = query_one("SELECT started_at FROM campaign WHERE id = ?", (campaign_id,))
            if row and not row["started_at"]:
                values["started_at"] = utcnow()
        if status in ("completed", "cancelled", "failed"):
            values["finished_at"] = utcnow()
    update_row("campaign", campaign_id, values)


def delete_campaign(campaign_id: str, job_seeker_id: str) -> int:
    return execute(
        "DELETE FROM campaign WHERE id = ? AND job_seeker_id = ?", (campaign_id, job_seeker_id)
    )


# ---------------------------------------------------------------------------
# Planning inputs (read-only views of other slices' private tables)
# ---------------------------------------------------------------------------


def load_planning_inputs(campaign: dict) -> dict:
    """Everything the planner grounds its queries in (FR-162).

    Falls back to the newest composite profile / dream job model when the
    campaign does not pin one, so a plan can be generated before those steps
    have been re-confirmed.
    """
    seeker = campaign["job_seeker_id"]
    directives = query_one(
        "SELECT * FROM directive_set WHERE id = ? AND job_seeker_id = ?",
        (campaign.get("directive_set_id"), seeker),
    )
    composite = None
    if campaign.get("composite_profile_id"):
        composite = query_one(
            "SELECT * FROM composite_profile WHERE id = ? AND job_seeker_id = ?",
            (campaign["composite_profile_id"], seeker),
        )
    if composite is None:
        composite = query_one(
            "SELECT * FROM composite_profile WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (seeker,),
        )
    dream = None
    if campaign.get("dream_job_model_id"):
        dream = query_one(
            "SELECT * FROM dream_job_model WHERE id = ? AND job_seeker_id = ?",
            (campaign["dream_job_model_id"], seeker),
        )
    if dream is None:
        dream = query_one(
            "SELECT * FROM dream_job_model WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (seeker,),
        )
    profile = query_one(
        "SELECT * FROM profile_version WHERE id = ? AND job_seeker_id = ?",
        (campaign.get("profile_version_id"), seeker),
    )
    return {
        "directives": directives,
        "composite_profile": composite,
        "dream_job_model": dream,
        "profile_version": profile,
        "do_not_disclose": do_not_disclose_paths(seeker),
    }


def do_not_disclose_paths(job_seeker_id: str) -> set[str]:
    """FR-106: fields that must be stripped before anything leaves the machine."""
    rows = query_all(
        "SELECT field_path FROM disclosure_flag WHERE job_seeker_id = ? AND do_not_disclose = 1",
        (job_seeker_id,),
    )
    return {r["field_path"] for r in rows}


def has_consent(job_seeker_id: str, kind: str) -> bool:
    row = query_one(
        "SELECT granted FROM consent_record WHERE job_seeker_id = ? AND kind = ? "
        "ORDER BY granted_at DESC LIMIT 1",
        (job_seeker_id, kind),
    )
    return bool(row and row["granted"])


# ---------------------------------------------------------------------------
# Source catalogue (FR-161, FR-164, NFR-403)
# ---------------------------------------------------------------------------


def list_catalogue(enabled_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM source_catalogue"
    if enabled_only:
        sql += " WHERE enabled = 1 AND (requires_ack = 0 OR acknowledged_at IS NOT NULL)"
    sql += " ORDER BY source_type, adapter_key"
    rows = query_all(sql)
    for row in rows:
        row["coverage_countries"] = from_json(row.get("coverage_countries"), []) or []
        row["coverage_industries"] = from_json(row.get("coverage_industries"), []) or []
        row["query_capabilities"] = from_json(row.get("query_capabilities"), {}) or {}
    return rows


def get_catalogue_entry(adapter_key: str) -> dict | None:
    row = query_one("SELECT * FROM source_catalogue WHERE adapter_key = ?", (adapter_key,))
    if row:
        row["coverage_countries"] = from_json(row.get("coverage_countries"), []) or []
        row["coverage_industries"] = from_json(row.get("coverage_industries"), []) or []
        row["query_capabilities"] = from_json(row.get("query_capabilities"), {}) or {}
    return row


def record_extraction_rate(adapter_key: str, rate: float | None, had_success: bool) -> None:
    """NFR-403: keep a rolling extraction-success rate per adapter."""
    row = query_one(
        "SELECT extraction_success_rate FROM source_catalogue WHERE adapter_key = ?", (adapter_key,)
    )
    if row is None:
        return
    values: dict[str, Any] = {"updated_at": utcnow()}
    if rate is not None:
        previous = row["extraction_success_rate"]
        values["extraction_success_rate"] = (
            rate if previous is None else round(0.7 * float(previous) + 0.3 * rate, 4)
        )
    if had_success:
        values["last_success_at"] = utcnow()
    sets = ", ".join(f"{k} = :{k}" for k in values)
    values["__key"] = adapter_key
    execute(f"UPDATE source_catalogue SET {sets} WHERE adapter_key = :__key", values)


# ---------------------------------------------------------------------------
# Source plan items (FR-163, FR-166)
# ---------------------------------------------------------------------------


def insert_plan_item(campaign_id: str, values: dict) -> str:
    payload = dict(values)
    payload["campaign_id"] = campaign_id
    payload.setdefault("status", "planned")
    payload.setdefault("created_at", utcnow())
    return insert_row("source_plan_item", payload)


def list_plan_items(campaign_id: str, include_excluded: bool = True) -> list[dict]:
    sql = "SELECT * FROM source_plan_item WHERE campaign_id = ?"
    if not include_excluded:
        sql += " AND excluded_by_user = 0"
    sql += " ORDER BY created_at, adapter_key"
    return [_decode(r, PLAN_JSON_COLUMNS) or {} for r in query_all(sql, (campaign_id,))]


def get_plan_item(plan_item_id: str, campaign_id: str | None = None) -> dict | None:
    if campaign_id:
        row = query_one(
            "SELECT * FROM source_plan_item WHERE id = ? AND campaign_id = ?",
            (plan_item_id, campaign_id),
        )
    else:
        row = query_one("SELECT * FROM source_plan_item WHERE id = ?", (plan_item_id,))
    return _decode(row, PLAN_JSON_COLUMNS)


def update_plan_item(plan_item_id: str, values: dict) -> None:
    update_row("source_plan_item", plan_item_id, values)


def bump_plan_item(
    plan_item_id: str, *, records: int = 0, errors: int = 0, last_error: str | None = None
) -> None:
    execute(
        "UPDATE source_plan_item SET records_collected = records_collected + ?, "
        "error_count = error_count + ?, last_error = COALESCE(?, last_error) WHERE id = ?",
        (records, errors, last_error[:2000] if last_error else None, plan_item_id),
    )


def delete_plan_items(campaign_id: str, keep_ids: list[str] | None = None) -> int:
    if keep_ids:
        marks = ", ".join("?" for _ in keep_ids)
        return execute(
            f"DELETE FROM source_plan_item WHERE campaign_id = ? AND id NOT IN ({marks})",
            (campaign_id, *keep_ids),
        )
    return execute("DELETE FROM source_plan_item WHERE campaign_id = ?", (campaign_id,))


def plan_items_with_provenance(campaign_id: str) -> set[str]:
    """Plan items a collected record still points at (FR-166).

    Deleting one of these would leave the record with a dangling provenance
    link, so re-planning keeps the row instead.
    """
    rows = query_all(
        "SELECT DISTINCT s.id AS id FROM source_plan_item s "
        "JOIN provenance p ON p.source_plan_item_id = s.id WHERE s.campaign_id = ?",
        (campaign_id,),
    )
    return {r["id"] for r in rows}


def reset_plan_progress(campaign_id: str) -> int:
    """NFR-603: re-running collection starts from a clean per-item counter."""
    return execute(
        "UPDATE source_plan_item SET status = 'planned', records_collected = 0, "
        "error_count = 0, last_error = NULL WHERE campaign_id = ? AND excluded_by_user = 0",
        (campaign_id,),
    )


# ---------------------------------------------------------------------------
# Dashboard (FR-185, FR-361)
# ---------------------------------------------------------------------------


def llm_totals(campaign_id: str) -> dict:
    row = query_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens, "
        "COALESCE(SUM(cost_eur), 0) AS cost_eur FROM llm_call WHERE campaign_id = ?",
        (campaign_id,),
    )
    return dict(row or {"calls": 0, "tokens": 0, "cost_eur": 0.0})


def collected_counts(campaign_id: str) -> dict[str, int]:
    rows = query_all(
        "SELECT p.entity_type AS entity_type, COUNT(DISTINCT p.entity_id) AS n FROM provenance p "
        "JOIN source_plan_item s ON s.id = p.source_plan_item_id "
        "WHERE s.campaign_id = ? GROUP BY p.entity_type",
        (campaign_id,),
    )
    return {r["entity_type"]: int(r["n"]) for r in rows}


def list_jobs(campaign_id: str, limit: int = 20) -> list[dict]:
    return query_all(
        "SELECT * FROM job_run WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
        (campaign_id, limit),
    )


def latest_job(campaign_id: str, kind: str | None = None) -> dict | None:
    if kind:
        return query_one(
            "SELECT * FROM job_run WHERE campaign_id = ? AND kind = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (campaign_id, kind),
        )
    return query_one(
        "SELECT * FROM job_run WHERE campaign_id = ? ORDER BY created_at DESC LIMIT 1",
        (campaign_id,),
    )


def record_audit(
    action: str,
    *,
    job_seeker_id: str | None = None,
    actor: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict | None = None,
) -> str:
    return insert_row(
        "audit_event",
        {
            "job_seeker_id": job_seeker_id,
            "actor": actor or "system",
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": detail,
            "created_at": utcnow(),
        },
    )
