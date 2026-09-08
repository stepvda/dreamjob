"""Administration storage: settings, catalogue, LLM log, audit (FR-361..364, NFR-701, NFR-702).

All SQL behind the administration screens lives here (CR-408).  Four groups:

* ``app_setting`` accessors, which hold the administrator's LLM configuration
  and per-source caps as versioned key/value rows (FR-362, FR-363).
* ``source_catalogue`` accessors, including the IR-101 acknowledgement stamp.
* ``llm_call`` reads plus the retention sweep that nulls prompt and response
  text after a retention period (FR-364).
* ``audit_event`` insert and read.  There is deliberately no update or delete
  helper: the trail is append-only (NFR-702).
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    from_json,
    insert_row,
    query_all,
    query_one,
    to_json,
    upsert_row,
    utcnow,
    write_tx,
)

# ---------------------------------------------------------------------------
# app_setting (FR-362, FR-363)
# ---------------------------------------------------------------------------


def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM app_setting WHERE key = ?", (key,))
    if row is None:
        return default
    return from_json(row["value"], row["value"])


def set_setting(key: str, value: Any) -> None:
    upsert_row(
        "app_setting",
        {"key": key, "value": to_json(value) if not isinstance(value, str) else value,
         "updated_at": utcnow()},
        ["key"],
    )


def delete_setting(key: str) -> int:
    with write_tx() as conn:
        return conn.execute("DELETE FROM app_setting WHERE key = ?", (key,)).rowcount


def settings_with_prefix(prefix: str) -> dict[str, Any]:
    rows = query_all(
        "SELECT key, value, updated_at FROM app_setting WHERE key LIKE ? ORDER BY key",
        (f"{prefix}%",),
    )
    return {r["key"]: from_json(r["value"], r["value"]) for r in rows}


# ---------------------------------------------------------------------------
# source_catalogue (FR-363, IR-101)
# ---------------------------------------------------------------------------


def list_sources() -> list[dict]:
    return query_all("SELECT * FROM source_catalogue ORDER BY adapter_key")


def get_source(adapter_key: str) -> dict | None:
    return query_one("SELECT * FROM source_catalogue WHERE adapter_key = ?", (adapter_key,))


def update_source(adapter_key: str, values: dict[str, Any]) -> int:
    """Patch catalogue columns.  Only the administrator-controllable ones."""
    allowed = {
        "enabled", "rate_limit_rps", "cost_per_call_eur", "requires_ack",
        "acknowledged_at", "legal_notes", "tos_status",
    }
    payload = {k: v for k, v in values.items() if k in allowed}
    if not payload:
        return 0
    payload["updated_at"] = utcnow()
    sets = ", ".join(f"{k} = :{k}" for k in payload)
    payload["__key"] = adapter_key
    with write_tx() as conn:
        return conn.execute(
            f"UPDATE source_catalogue SET {sets} WHERE adapter_key = :__key", payload
        ).rowcount


# ---------------------------------------------------------------------------
# Campaign dashboard (FR-361)
# ---------------------------------------------------------------------------


def campaign_row(campaign_id: str) -> dict | None:
    return query_one("SELECT * FROM campaign WHERE id = ?", (campaign_id,))


def job_runs_for_campaign(campaign_id: str) -> list[dict]:
    return query_all(
        "SELECT id, kind, adapter_key, status, progress_done, progress_total, error_count, "
        "       last_error, estimated_seconds, started_at, finished_at, created_at "
        "FROM job_run WHERE campaign_id = ? ORDER BY created_at",
        (campaign_id,),
    )


def plan_items_for_campaign(campaign_id: str) -> list[dict]:
    return query_all(
        "SELECT id, adapter_key, status, estimated_pages, estimated_seconds, estimated_cost_eur, "
        "       records_collected, error_count, last_error, excluded_by_user, created_at "
        "FROM source_plan_item WHERE campaign_id = ? ORDER BY adapter_key",
        (campaign_id,),
    )


def llm_totals_for_campaign(campaign_id: str) -> dict:
    row = query_one(
        "SELECT COUNT(*) AS calls, "
        "       COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "       COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "       COALESCE(SUM(cost_eur), 0) AS cost_eur, "
        "       COALESCE(SUM(status <> 'ok'), 0) AS failed, "
        "       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms "
        "FROM llm_call WHERE campaign_id = ?",
        (campaign_id,),
    )
    return dict(row or {})


def llm_by_task(campaign_id: str | None = None) -> list[dict]:
    if campaign_id:
        return query_all(
            "SELECT task, model, provider, COUNT(*) AS calls, "
            "       COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens, "
            "       COALESCE(SUM(cost_eur), 0) AS cost_eur "
            "FROM llm_call WHERE campaign_id = ? GROUP BY task, model, provider "
            "ORDER BY tokens DESC",
            (campaign_id,),
        )
    return query_all(
        "SELECT task, model, provider, COUNT(*) AS calls, "
        "       COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens, "
        "       COALESCE(SUM(cost_eur), 0) AS cost_eur "
        "FROM llm_call GROUP BY task, model, provider ORDER BY tokens DESC"
    )


def campaign_summaries(limit: int = 50) -> list[dict]:
    """One row per campaign for the administrator overview (FR-361, NFR-701)."""
    return query_all(
        "SELECT c.id, c.name, c.status, c.stage, c.token_budget, c.tokens_used, c.cost_eur, "
        "       c.started_at, c.finished_at, c.created_at, c.job_seeker_id, "
        "       (SELECT COUNT(*) FROM job_run r WHERE r.campaign_id = c.id "
        "        AND r.status = 'failed') AS failed_jobs, "
        "       (SELECT COALESCE(SUM(p.records_collected), 0) FROM source_plan_item p "
        "        WHERE p.campaign_id = c.id) AS records_collected "
        "FROM campaign c ORDER BY c.created_at DESC LIMIT ?",
        (limit,),
    )


def adapter_metrics() -> list[dict]:
    """Per-adapter collection and error counters (NFR-701, NFR-403)."""
    return query_all(
        "SELECT s.adapter_key, s.display_name, s.source_type, s.access_method, s.enabled, "
        "       s.tos_status, s.requires_ack, s.acknowledged_at, s.rate_limit_rps, "
        "       s.extraction_success_rate, s.last_success_at, "
        "       COALESCE(p.items, 0) AS plan_items, "
        "       COALESCE(p.records_collected, 0) AS records_collected, "
        "       COALESCE(p.errors, 0) AS errors "
        "FROM source_catalogue s "
        "LEFT JOIN (SELECT adapter_key, COUNT(*) AS items, "
        "                  SUM(records_collected) AS records_collected, "
        "                  SUM(error_count) AS errors "
        "           FROM source_plan_item GROUP BY adapter_key) p "
        "  ON p.adapter_key = s.adapter_key "
        "ORDER BY s.adapter_key"
    )


def global_counters() -> dict:
    """Coarse platform counters for the overview screen (NFR-701)."""
    row = query_one(
        "SELECT (SELECT COUNT(*) FROM job_seeker) AS seekers, "
        "       (SELECT COUNT(*) FROM campaign) AS campaigns, "
        "       (SELECT COUNT(*) FROM campaign WHERE status = 'running') AS running_campaigns, "
        "       (SELECT COUNT(*) FROM raw_document) AS pages_fetched, "
        "       (SELECT COUNT(*) FROM company) AS companies, "
        "       (SELECT COUNT(*) FROM vacancy) AS vacancies, "
        "       (SELECT COUNT(*) FROM llm_call) AS llm_calls, "
        "       (SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_call) AS tokens, "
        "       (SELECT COALESCE(SUM(cost_eur), 0) FROM llm_call) AS cost_eur, "
        "       (SELECT COUNT(*) FROM job_run WHERE status = 'failed') AS failed_jobs"
    )
    return dict(row or {})


# ---------------------------------------------------------------------------
# LLM call log and retention (FR-364)
# ---------------------------------------------------------------------------

_LLM_COLUMNS_NO_TEXT = (
    "id, job_seeker_id, campaign_id, task, prompt_template, prompt_version, model, provider, "
    "entity_type, entity_id, input_tokens, output_tokens, cost_eur, latency_ms, status, error, "
    "redacted_at, created_at, "
    "(prompt_text IS NOT NULL) AS has_prompt_text, "
    "(response_text IS NOT NULL) AS has_response_text"
)


def list_llm_calls(
    *,
    campaign_id: str | None = None,
    job_seeker_id: str | None = None,
    task: str | None = None,
    status: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("campaign_id", campaign_id),
        ("job_seeker_id", job_seeker_id),
        ("task", task),
        ("status", status),
        ("entity_id", entity_id),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.extend([limit, offset])
    return query_all(
        f"SELECT {_LLM_COLUMNS_NO_TEXT} FROM llm_call {clause} "
        "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        tuple(params),
    )


def count_llm_calls(campaign_id: str | None = None) -> int:
    if campaign_id:
        row = query_one("SELECT COUNT(*) AS n FROM llm_call WHERE campaign_id = ?", (campaign_id,))
    else:
        row = query_one("SELECT COUNT(*) AS n FROM llm_call")
    return int(row["n"]) if row else 0


def get_llm_call(call_id: str) -> dict | None:
    return query_one("SELECT * FROM llm_call WHERE id = ?", (call_id,))


def redact_llm_calls(cutoff: str) -> int:
    """Null prompt and response text for calls older than ``cutoff`` (FR-364).

    Tokens, cost, model and the entity reference survive: they are what the
    dashboard and the cost reports need, and they are not personal data.
    """
    with write_tx() as conn:
        return conn.execute(
            "UPDATE llm_call SET prompt_text = NULL, response_text = NULL, redacted_at = ? "
            "WHERE created_at < ? AND redacted_at IS NULL "
            "AND (prompt_text IS NOT NULL OR response_text IS NOT NULL)",
            (utcnow(), cutoff),
        ).rowcount


def pending_redaction_count(cutoff: str) -> int:
    row = query_one(
        "SELECT COUNT(*) AS n FROM llm_call WHERE created_at < ? AND redacted_at IS NULL "
        "AND (prompt_text IS NOT NULL OR response_text IS NOT NULL)",
        (cutoff,),
    )
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# audit_event (NFR-702) - append and read only
# ---------------------------------------------------------------------------


def insert_audit(
    *,
    action: str,
    entity_type: str | None,
    entity_id: str | None,
    job_seeker_id: str | None,
    actor: str | None,
    detail: str | None,
) -> str:
    return insert_row(
        "audit_event",
        {
            "job_seeker_id": job_seeker_id,
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": detail,
            "created_at": utcnow(),
        },
    )


def list_audit(
    *,
    job_seeker_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    since: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("job_seeker_id", job_seeker_id),
        ("entity_type", entity_type),
        ("entity_id", entity_id),
        ("action", action),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if since:
        where.append("created_at >= ?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.extend([limit, offset])
    return query_all(
        f"SELECT * FROM audit_event {clause} ORDER BY created_at DESC, rowid DESC "
        "LIMIT ? OFFSET ?",
        tuple(params),
    )
