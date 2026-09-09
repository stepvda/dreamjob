"""Administration storage: settings, catalogue, LLM log, audit (FR-361..364, NFR-701, NFR-702).

All SQL behind the administration screens lives here (CR-408).  Four groups:

* ``app_setting`` accessors, which hold the administrator's LLM configuration
  and per-source caps as versioned key/value rows (FR-362, FR-363).
* ``source_catalogue`` accessors, including the IR-101 acknowledgement stamp
  and the reconciliation that keeps a row honest about what will actually
  happen when the source is collected (FR-161, FR-185).
* ``llm_call`` reads plus the retention sweep that nulls prompt and response
  text after a retention period (FR-364).
* ``audit_event`` insert and read.  There is deliberately no update or delete
  helper: the trail is append-only (NFR-702).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from dreamjob.config import get_settings
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

log = logging.getLogger(__name__)

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
# Catalogue honesty (FR-161, FR-164, FR-182, FR-185, IR-101)
# ---------------------------------------------------------------------------
#
# ``SourceAdapter.register`` writes what an adapter says about itself.  Two
# kinds of untruth survive that, and the FR-185 dashboard reads both:
#
# * **The rate a source asks for is not the rate it gets.**  The catalogue
#   recorded ``rate_limit_rps = 1.0`` for the seven ATS hosts while
#   ``DomainLimiter`` ran every one of them at the global 0.5, and
#   ``planning.estimate()`` prices a plan off the catalogue value - so the
#   duration the user approved was half the duration the run would take
#   (docs/Data_Gathering_Plan.md section 2.2 and appendix A).  It cut the other
#   way for EURES: europa.eu publishes ``Crawl-delay: 10``, so the row's 0.5
#   promised twenty times the throughput the limiter will allow.
# * **A row can outlive its adapter.**  ``broken_board`` and ``stub_board``
#   were test fixtures that leaked into the installed database; source
#   selection (FR-164) could not tell them from real sources and spent a plan
#   item on each, every campaign (plan item C5).  Fourteen more had joined them
#   by the time this was written.
#
# :func:`reconcile_catalogue` is the pass that fixes both.  It runs after
# registration, from ``dreamjob.adapters.sync_catalogue``.

#: ``Crawl-delay``, in seconds, published by the host an adapter reads, from
#: robots.txt measured on 2026-09-09.  The egress layer honours whatever
#: robots.txt says at run time (FR-182); the catalogue needs the number
#: *before* the first request, because the estimate the user approves is
#: computed from the catalogue and nothing else.
PUBLISHED_CRAWL_DELAY_SECONDS: dict[str, float] = {
    "board.eures": 10.0,   # europa.eu, under "User-agent: *"
    "ats.lever": 1.0,      # api.lever.co (already met by the global 0.5)
}

#: Sources that stay off, with the reason a reader of the dashboard needs
#: (IR-101, FR-182; docs/Data_Gathering_Plan.md section 6).  Both rows already
#: *said* they were disabled in their legal notes while ``enabled`` was 1.
#:
#: The reconciliation is a floor, never a switch: it can turn a source off, an
#: administrator who has a licence turns it on by acknowledging it (FR-363),
#: and an acknowledged row is then left alone.
FORBIDDEN_SOURCES: dict[str, str] = {
    "board.indeed": (
        "Indeed's terms of service prohibit automated access and scraping, so "
        "the row is catalogued off as well as unacknowledged (IR-101)."
    ),
    "board.stepstone": (
        "StepStone's terms of use prohibit automated access and systematic "
        "copying; off until an administrator holds a licence (IR-101)."
    ),
}


def effective_rate_limit_rps(adapter_key: str, declared: float | None = None) -> float:
    """The rate the egress layer will actually apply to this source (FR-182).

    Three numbers claim to be the rate limit and only one of them runs.  The
    adapter's class attribute is a wish, ``DREAMJOB_PER_DOMAIN_RPS`` is the
    ceiling ``DomainLimiter`` enforces on every host, and a published
    ``Crawl-delay`` lowers that ceiling further for the hosts that ask.  The
    limiter takes the slowest of them, so the catalogue records the slowest of
    them too: it is the one number that can be believed, and the plan estimate
    is built from it.

    Never rounds *upward*: a source that asks to be read more slowly than the
    global ceiling keeps its own rate, because politeness is allowed to exceed
    the minimum.
    """
    ceiling = float(get_settings().per_domain_rps or 0.5)
    delay = PUBLISHED_CRAWL_DELAY_SECONDS.get(adapter_key)
    if delay and delay > 0:
        ceiling = min(ceiling, 1.0 / delay)
    if declared:
        ceiling = min(ceiling, float(declared))
    return round(ceiling, 4)


def prune_unknown_sources(known_adapter_keys: Iterable[str]) -> list[str]:
    """Delete catalogue rows whose adapter does not exist (FR-161, FR-164, C5).

    A catalogued source that no module implements is planned like any other
    and then collects nothing, which is the most expensive kind of lie: it
    costs a plan item, reports ``done`` and can never be diagnosed from the
    dashboard.

    Three guards keep this from eating a real row.  Nothing is deleted when the
    registry is empty (adapter discovery failed, and every row would look
    orphaned).  A row an administrator acknowledged is never deleted, and
    neither is one they switched off: both are decisions, and a decision
    deserves a human.
    """
    known = {str(k) for k in known_adapter_keys}
    if not known:
        log.warning("Skipping catalogue prune: no adapters are registered")
        return []
    orphans = [
        r["adapter_key"]
        for r in query_all(
            "SELECT adapter_key FROM source_catalogue "
            "WHERE enabled = 1 AND acknowledged_at IS NULL ORDER BY adapter_key"
        )
        if r["adapter_key"] not in known
    ]
    if not orphans:
        return []
    marks = ", ".join("?" for _ in orphans)
    with write_tx() as conn:
        conn.execute(
            f"DELETE FROM source_catalogue WHERE adapter_key IN ({marks})", tuple(orphans)
        )
    log.warning(
        "Removed %d catalogue row(s) with no adapter: %s", len(orphans), ", ".join(orphans)
    )
    return orphans


def reconcile_catalogue(known_adapter_keys: Iterable[str]) -> dict[str, list[str]]:
    """Make every catalogue row say what will actually happen (FR-161, FR-185).

    Runs after every adapter has registered, so it sees the values the adapters
    wrote and corrects the two they cannot know:

    1. rows with no adapter are removed (:func:`prune_unknown_sources`);
    2. the sources section 6 of the gathering plan forbids are switched off,
       unless an administrator has acknowledged them (IR-101);
    3. ``rate_limit_rps`` is lowered to the rate the limiter will apply
       (:func:`effective_rate_limit_rps`).

    Returns what it changed, keyed ``pruned`` / ``disabled`` / ``slowed``, so
    the caller can log it and a test can assert on it.
    """
    changed: dict[str, list[str]] = {
        "pruned": prune_unknown_sources(known_adapter_keys),
        "disabled": [],
        "slowed": [],
    }
    for key, reason in FORBIDDEN_SOURCES.items():
        row = get_source(key)
        if row is None or row.get("acknowledged_at"):
            continue
        patch: dict[str, Any] = {}
        if row.get("enabled"):
            patch["enabled"] = 0
        if not row.get("requires_ack"):
            patch["requires_ack"] = 1
        if not (row.get("legal_notes") or "").strip():
            patch["legal_notes"] = reason
        if patch:
            update_source(key, patch)
            changed["disabled"].append(key)
            log.warning("Catalogued %s as disabled: %s", key, reason)

    for row in list_sources():
        key = row["adapter_key"]
        current = row.get("rate_limit_rps")
        honest = effective_rate_limit_rps(key, current)
        if current is None or float(current) > honest + 1e-9:
            update_source(key, {"rate_limit_rps": honest})
            changed["slowed"].append(key)
            log.info(
                "Catalogue rate for %s corrected from %s to %.4f rps: the limiter "
                "applies the slower of the global ceiling and any Crawl-delay",
                key, current, honest,
            )
    return changed


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
