"""Administration API (FR-361, FR-362, FR-363, FR-364, IR-101, NFR-701, NFR-702).

Four surfaces:

* **Dashboard (FR-361)** - stage progress, records per source, errors, token
  consumption and cost, elapsed and estimated remaining time for one campaign,
  read from ``job_run``, ``source_plan_item`` and ``llm_call``.  The owning job
  seeker sees their own campaign; an administrator sees any of them.
* **LLM configuration (FR-362)** - provider, per-task model, budgets and prompt
  templates, held as overrides in ``app_setting`` on top of the ``.env``
  defaults so the effective value is always visible next to where it came from.
* **Sources (FR-363, IR-101)** - enable/disable, rate limits and caps, plus the
  acknowledgement a source whose terms prohibit automated access needs before
  it may be enabled at all.
* **LLM call log (FR-364)** - browse prompts and responses, and the retention
  sweep that nulls their text and stamps ``redacted_at``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field

from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import admin as repo
from dreamjob.db.repositories import declines as decline_repo
from dreamjob.db.repositories import seekers as seeker_repo
from dreamjob.llm.client import invalidate_admin_config
from dreamjob.pipeline import declines
from dreamjob.security import auth_service as auth
from dreamjob.security.audit import log_metric, record_audit

router = APIRouter()

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "llm" / "prompts"

# app_setting keys.  Namespaced so ``settings_with_prefix`` can fetch a group.
SETTING_PREFIX_LLM = "llm."
SETTING_PREFIX_SOURCE = "source."
SETTING_PREFIX_PROMPT = "prompt."
DEFAULT_LOG_RETENTION_DAYS = 30

__all__ = ["router", "effective_llm_config", "source_caps", "run_redaction_job"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LLMConfigIn(BaseModel):
    """FR-362.  Every field is optional; only what is sent is overridden."""

    provider: str | None = None
    model_cheap: str | None = None
    model_strong: str | None = None
    task_models: dict[str, str] | None = None
    default_token_budget: int | None = Field(None, ge=1000)
    cost_per_1m_input_eur: float | None = Field(None, ge=0)
    cost_per_1m_output_eur: float | None = Field(None, ge=0)
    local_base_url: str | None = None
    local_model: str | None = None
    local_tasks: list[str] | None = None
    budget_degrade_at: float | None = Field(None, gt=0, le=1)
    log_retention_days: int | None = Field(None, ge=1, le=3650)


class UserCreateIn(BaseModel):
    """Administrator-created account (FR-362)."""

    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str | None = Field(
        None, description="Optional; a strong one is generated when omitted"
    )
    is_admin: bool = False
    locale: str = Field("en", min_length=2, max_length=10)


class UserUpdateIn(BaseModel):
    """Partial change to another account.  Fields left out are untouched."""

    display_name: str | None = Field(None, min_length=1, max_length=120)
    is_admin: bool | None = None
    disabled: bool | None = None
    locale: str | None = Field(None, min_length=2, max_length=10)


class UserPasswordIn(BaseModel):
    """A reset.  Left empty, a strong password is generated and returned once."""

    new_password: str | None = None


class PurgeIn(BaseModel):
    """Bulk clean-up of unused accounts (FR-362).

    ``dry_run`` defaults to true so a caller has to ask deliberately before
    anything is deleted; the count comes back either way.
    """

    exclude_admins: bool = True
    dry_run: bool = True



class SourceConfigIn(BaseModel):
    """FR-363.  Rate limits and caps for one adapter."""

    enabled: bool | None = None
    rate_limit_rps: float | None = Field(None, gt=0, le=50)
    cost_per_call_eur: float | None = Field(None, ge=0)
    max_pages: int | None = Field(None, ge=1)
    max_records: int | None = Field(None, ge=1)
    max_seconds: int | None = Field(None, ge=1)


class DeclineEnableIn(BaseModel):
    """FR-363 / IR-101: re-enable a declined source, explicitly."""

    note: str | None = Field(None, max_length=500)
    #: A terms decline may only be cleared by acknowledging the terms.  The
    #: flag exists so the acknowledgement is an act the caller states, not one
    #: the endpoint infers from a button's colour.
    acknowledge_terms: bool = False


class AcknowledgeIn(BaseModel):
    """IR-101.  The administrator takes responsibility for a prohibited source."""

    accepted: bool
    note: str | None = None


class PromptIn(BaseModel):
    body: str
    version: str | None = None


class RedactIn(BaseModel):
    older_than_days: int | None = Field(None, ge=0, le=3650)


class AdminFlagIn(BaseModel):
    is_admin: bool


class ContinuousIn(BaseModel):
    """FR-161: the administrator's switch for the endless collection cycle."""

    enabled: bool
    interval_seconds: int | None = Field(
        None, ge=300, le=86400, description="Seconds between phases (default 900)"
    )


class ContinuousRunIn(BaseModel):
    """Run one phase now, whether or not it is due."""

    phase: str | None = Field(
        None, description="discover | contacts | enrich | score; default: the current phase"
    )
    force: bool = True


# ---------------------------------------------------------------------------
# Configuration helpers (FR-362, FR-363)
# ---------------------------------------------------------------------------


def effective_llm_config() -> dict[str, Any]:
    """The configuration in force: ``.env`` defaults with administrator overrides.

    Exposed so the pipeline can pick a per-task model (FR-362) without each
    slice re-implementing the precedence rule.
    """
    s = get_settings()
    overrides = repo.settings_with_prefix(SETTING_PREFIX_LLM)

    def value(name: str, default: Any) -> Any:
        return overrides.get(f"{SETTING_PREFIX_LLM}{name}", default)

    return {
        "provider": value("provider", s.llm_provider),
        "model_cheap": value("model_cheap", s.llm_model_cheap),
        "model_strong": value("model_strong", s.llm_model_strong),
        "task_models": value("task_models", {}),
        "default_token_budget": int(value("default_token_budget", s.default_token_budget)),
        "cost_per_1m_input_eur": float(
            value("cost_per_1m_input_eur", s.llm_cost_per_1m_input_eur)
        ),
        "cost_per_1m_output_eur": float(
            value("cost_per_1m_output_eur", s.llm_cost_per_1m_output_eur)
        ),
        "local_base_url": value("local_base_url", s.local_llm_base_url),
        "local_model": value("local_model", s.local_llm_model),
        "local_tasks": value("local_tasks", sorted(s.local_llm_task_set)),
        "budget_degrade_at": float(value("budget_degrade_at", 0.85)),
        "log_retention_days": int(value("log_retention_days", DEFAULT_LOG_RETENTION_DAYS)),
    }


def source_caps(adapter_key: str) -> dict[str, Any]:
    """Per-source caps set by the administrator (FR-363).

    Kept in ``app_setting`` rather than in a new ``source_catalogue`` column so
    the catalogue stays the adapters' own declaration (IR-101) and the caps
    stay an operator decision.
    """
    return repo.get_setting(f"{SETTING_PREFIX_SOURCE}{adapter_key}.caps", {}) or {}


def _model_for_task(task: str, config: dict[str, Any]) -> str:
    per_task = config.get("task_models") or {}
    return per_task.get(task) or per_task.get(task.split(".")[0]) or config["model_strong"]


# ---------------------------------------------------------------------------
# Campaign dashboard (FR-361)
# ---------------------------------------------------------------------------


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timing(campaign: dict, jobs: list[dict]) -> dict:
    """Elapsed and estimated remaining time (FR-361).

    The estimate extrapolates the observed rate of completed work.  When no
    job has reported progress yet it falls back on the planner's own
    ``estimated_seconds``, and when neither is available it says so rather
    than inventing a number.
    """
    started = _parse_ts(campaign.get("started_at"))
    finished = _parse_ts(campaign.get("finished_at"))
    now = datetime.now(UTC)
    elapsed = int(((finished or now) - started).total_seconds()) if started else 0

    done = sum(int(j["progress_done"] or 0) for j in jobs)
    total = sum(int(j["progress_total"] or 0) for j in jobs)
    fraction = (done / total) if total else 0.0

    remaining: int | None = None
    basis = "unknown"
    if finished:
        remaining, basis = 0, "finished"
    elif fraction > 0 and elapsed > 0:
        remaining, basis = int(elapsed * (1 - fraction) / fraction), "observed_rate"
    else:
        planned = sum(int(j["estimated_seconds"] or 0) for j in jobs if j["status"] != "done")
        if planned:
            remaining, basis = planned, "planner_estimate"

    return {
        "started_at": campaign.get("started_at"),
        "finished_at": campaign.get("finished_at"),
        "elapsed_seconds": elapsed,
        "progress_done": done,
        "progress_total": total or None,
        "progress_fraction": round(fraction, 4),
        "estimated_remaining_seconds": remaining,
        "estimate_basis": basis,
    }


def _campaign_or_403(campaign_id: str, seeker: CurrentSeeker) -> dict:
    campaign = repo.campaign_row(campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    # FR-344: a campaign belongs to one job seeker; only they or an
    # administrator may look at its dashboard.
    if campaign["job_seeker_id"] != seeker.id and not seeker.is_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    return campaign


@router.get("/campaigns/{campaign_id}/dashboard")
def campaign_dashboard(
    campaign_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Stage progress, records per source, errors, tokens, cost and timing (FR-361)."""
    campaign = _campaign_or_403(campaign_id, seeker)
    jobs = repo.job_runs_for_campaign(campaign_id)
    plan = repo.plan_items_for_campaign(campaign_id)
    llm = repo.llm_totals_for_campaign(campaign_id)

    stages = [
        {
            "job_id": j["id"],
            "kind": j["kind"],
            "adapter_key": j["adapter_key"],
            "status": j["status"],
            "done": j["progress_done"],
            "total": j["progress_total"],
            "errors": j["error_count"],
            "last_error": j["last_error"],
            "started_at": j["started_at"],
            "finished_at": j["finished_at"],
        }
        for j in jobs
    ]
    errors = [
        {
            "source": j["adapter_key"] or j["kind"],
            "count": j["error_count"],
            "last": j["last_error"],
        }
        for j in jobs
        if j["error_count"] or j["status"] == "failed"
    ] + [
        {"source": p["adapter_key"], "count": p["error_count"], "last": p["last_error"]}
        for p in plan
        if p["error_count"]
    ]

    budget = int(campaign["token_budget"] or 0)
    used = int(campaign["tokens_used"] or 0)
    dashboard = {
        "campaign": {
            "id": campaign["id"],
            "name": campaign["name"],
            "status": campaign["status"],
            "stage": campaign["stage"],
            "created_at": campaign["created_at"],
        },
        "timing": _timing(campaign, jobs),
        "stages": stages,
        "sources": [
            {
                "adapter_key": p["adapter_key"],
                "status": p["status"],
                "excluded_by_user": bool(p["excluded_by_user"]),
                "records_collected": p["records_collected"],
                "estimated_pages": p["estimated_pages"],
                "errors": p["error_count"],
                "last_error": p["last_error"],
            }
            for p in plan
        ],
        "records_total": sum(int(p["records_collected"] or 0) for p in plan),
        "errors": errors,
        "tokens": {
            "budget": budget,
            "used": used,
            "remaining": max(0, budget - used),
            "fraction_used": round(used / budget, 4) if budget else 0.0,
            "input_tokens": llm.get("input_tokens", 0),
            "output_tokens": llm.get("output_tokens", 0),
            "calls": llm.get("calls", 0),
            "failed_calls": llm.get("failed", 0),
            "avg_latency_ms": round(float(llm.get("avg_latency_ms") or 0)),
        },
        "cost_eur": round(float(campaign["cost_eur"] or 0), 4),
        "cost_by_task": repo.llm_by_task(campaign_id),
    }
    # NFR-701: the same numbers as a single structured line per campaign.
    log_metric(
        "campaign.dashboard",
        campaign_id=campaign_id,
        records=dashboard["records_total"],
        tokens=used,
        cost_eur=dashboard["cost_eur"],
        errors=len(errors),
    )
    return dashboard


@router.get("/overview")
def overview(admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """Platform-wide counters and the most recent campaigns (FR-361, NFR-701)."""
    return {
        "counters": repo.global_counters(),
        "campaigns": repo.campaign_summaries(),
        "llm_by_task": repo.llm_by_task(),
        "adapters": repo.adapter_metrics(),
    }


@router.get("/metrics")
def metrics(admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """Per-campaign and per-adapter metrics (NFR-701)."""
    adapters = repo.adapter_metrics()
    for row in adapters:
        log_metric(
            "adapter.health",
            adapter_key=row["adapter_key"],
            records=row["records_collected"],
            errors=row["errors"],
            extraction_success_rate=row["extraction_success_rate"],
        )
    return {
        "generated_at": utcnow(),
        "global": repo.global_counters(),
        "per_campaign": repo.campaign_summaries(),
        "per_adapter": adapters,
    }


# ---------------------------------------------------------------------------
# Continuous data collection (FR-161..166)
# ---------------------------------------------------------------------------


@router.get("/continuous")
def continuous_status(admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """The endless collection cycle: switch, interval, cursor and last report."""
    from dreamjob.pipeline import continuous  # noqa: PLC0415 - avoids an import cycle

    return continuous.get_state()


@router.put("/continuous")
def set_continuous(
    payload: ContinuousIn, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    """Turn the cycle on or off and set how often a phase runs (FR-161)."""
    from dreamjob.pipeline import continuous  # noqa: PLC0415

    try:
        state = continuous.set_enabled(
            payload.enabled, interval_seconds=payload.interval_seconds
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    record_audit(
        "admin.continuous_changed",
        "app_setting",
        None,
        seeker_id=admin.id,
        detail=payload.model_dump(),
    )
    return state


#: Strong references to the "run now" phases still in flight.  ``create_task``
#: alone does not keep a task alive and a phase can take minutes, so the set is
#: what stops the interpreter from collecting a running tick; the done callback
#: drops it again.
_BACKGROUND_RUNS: set[asyncio.Task] = set()


@router.post("/continuous/run", status_code=status.HTTP_202_ACCEPTED)
async def run_continuous_phase(
    payload: ContinuousRunIn | None = None, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    """Schedule one phase now and return immediately (administrator's manual tick).

    The phase itself can take minutes - a contacts pass crawls sites and an
    enrich phase drives the model - and awaiting it here blocked the HTTP
    request for 323,881 ms on the live installation, which the browser read as
    a timeout.  The phase therefore runs in the background on the server loop;
    its outcome lands in ``continuous.last_report`` and the admin tab, which
    already polls ``GET /api/admin/continuous``, shows it when it finishes.
    """
    from dreamjob.pipeline import continuous  # noqa: PLC0415

    body = payload or ContinuousRunIn()
    if body.phase and body.phase not in continuous.PHASES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"phase must be one of {', '.join(continuous.PHASES)}",
        )
    phase = body.phase or continuous._current_phase()
    at = utcnow()
    task = asyncio.create_task(continuous.run_phase(force=body.force, phase=body.phase))
    _BACKGROUND_RUNS.add(task)
    task.add_done_callback(_BACKGROUND_RUNS.discard)
    record_audit(
        "admin.continuous_run",
        "app_setting",
        None,
        seeker_id=admin.id,
        detail={"phase": body.phase, "force": body.force, "outcome": "scheduled"},
    )
    return {"scheduled": True, "phase": phase, "at": at}


# ---------------------------------------------------------------------------
# LLM configuration (FR-362)
# ---------------------------------------------------------------------------


@router.get("/llm-config")
def get_llm_config(admin: CurrentSeeker = Depends(current_admin)) -> dict:
    s = get_settings()
    return {
        "effective": effective_llm_config(),
        "overrides": repo.settings_with_prefix(SETTING_PREFIX_LLM),
        "defaults_from_env": {
            "provider": s.llm_provider,
            "model_cheap": s.llm_model_cheap,
            "model_strong": s.llm_model_strong,
            "default_token_budget": s.default_token_budget,
            "cost_per_1m_input_eur": s.llm_cost_per_1m_input_eur,
            "cost_per_1m_output_eur": s.llm_cost_per_1m_output_eur,
            "local_base_url": s.local_llm_base_url,
            "local_model": s.local_llm_model,
            "local_tasks": sorted(s.local_llm_task_set),
        },
        "api_key_configured": bool(s.deepseek_api_key),
    }


@router.put("/llm-config")
def put_llm_config(payload: LLMConfigIn, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """Persist provider, per-task model, budget and retention overrides (FR-362)."""
    changed = {k: v for k, v in payload.model_dump().items() if v is not None}
    for key, value in changed.items():
        repo.set_setting(f"{SETTING_PREFIX_LLM}{key}", value)
    # The client caches these; drop the cache so the change is in force now.
    invalidate_admin_config()
    record_audit(
        "admin.llm_config_changed", "app_setting", None, seeker_id=admin.id, detail=changed
    )
    return {"changed": changed, "effective": effective_llm_config()}


@router.delete("/llm-config/{key}")
def clear_llm_override(key: str, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """Drop one override and fall back to the ``.env`` default."""
    removed = repo.delete_setting(f"{SETTING_PREFIX_LLM}{key}")
    invalidate_admin_config()
    record_audit("admin.llm_config_cleared", "app_setting", key, seeker_id=admin.id)
    return {"removed": bool(removed), "effective": effective_llm_config()}


@router.get("/llm-config/task-routing")
def task_routing(admin: CurrentSeeker = Depends(current_admin)) -> list[dict]:
    """Which model each task that has actually run would use (FR-362)."""
    config = effective_llm_config()
    seen = {row["task"] for row in repo.llm_by_task()}
    return [
        {"task": task, "model": _model_for_task(task, config)} for task in sorted(seen)
    ]


# --- prompt templates (FR-362, NFR-602) ------------------------------------

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _front_matter(text: str) -> dict[str, str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def _prompt_override(name: str) -> dict | None:
    return repo.get_setting(f"{SETTING_PREFIX_PROMPT}{name}")


@router.get("/prompts")
def list_prompts(admin: CurrentSeeker = Depends(current_admin)) -> list[dict]:
    """Versioned prompt templates on disk, with any administrator override."""
    out: list[dict] = []
    for path in sorted(PROMPTS_DIR.glob("*.md")) if PROMPTS_DIR.is_dir() else []:
        meta = _front_matter(path.read_text(encoding="utf-8"))
        override = _prompt_override(path.stem)
        out.append(
            {
                "name": path.stem,
                "task": meta.get("task"),
                "version": meta.get("version"),
                "requirements": meta.get("requirements"),
                "updated": meta.get("updated"),
                "overridden": override is not None,
                "override_version": (override or {}).get("version"),
            }
        )
    return out


@router.get("/prompts/{name}")
def get_prompt(name: str, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "prompt template not found")
    body = path.read_text(encoding="utf-8")
    override = _prompt_override(name)
    return {
        "name": name,
        "metadata": _front_matter(body),
        "body": body,
        "override": override,
        "effective_body": (override or {}).get("body", body),
    }


@router.put("/prompts/{name}")
def put_prompt(name: str, payload: PromptIn, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """Override a prompt template (FR-362).

    The shipped file is left alone: the override is stored as configuration so
    the version that ran is recoverable and an upgrade does not silently lose
    the operator's edit (NFR-602).
    """
    if not (PROMPTS_DIR / f"{name}.md").is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "prompt template not found")
    previous = _prompt_override(name) or {}
    record = {
        "body": payload.body,
        "version": payload.version or f"override-{int(previous.get('revision', 0)) + 1}",
        "revision": int(previous.get("revision", 0)) + 1,
        "updated_at": utcnow(),
        "updated_by": admin.id,
    }
    repo.set_setting(f"{SETTING_PREFIX_PROMPT}{name}", record)
    record_audit(
        "admin.prompt_overridden", "prompt", name, seeker_id=admin.id,
        detail={"version": record["version"], "revision": record["revision"]},
    )
    return record


@router.delete("/prompts/{name}")
def clear_prompt_override(name: str, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    removed = repo.delete_setting(f"{SETTING_PREFIX_PROMPT}{name}")
    record_audit("admin.prompt_override_cleared", "prompt", name, seeker_id=admin.id)
    return {"removed": bool(removed)}


# ---------------------------------------------------------------------------
# Source adapters (FR-363, IR-101)
# ---------------------------------------------------------------------------


def _source_view(row: dict) -> dict:
    caps = source_caps(row["adapter_key"])
    blocked = bool(row["requires_ack"]) and not row["acknowledged_at"]
    return {
        **row,
        "caps": caps,
        "effective_enabled": bool(row["enabled"]) and not blocked,
        "blocked_pending_acknowledgement": blocked,
    }


@router.get("/sources")
def list_sources(admin: CurrentSeeker = Depends(current_admin)) -> list[dict]:
    """The source catalogue with caps and acknowledgement state (FR-363, IR-101)."""
    return [_source_view(r) for r in repo.list_sources()]


@router.get("/sources/{adapter_key}")
def get_source(adapter_key: str, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    row = repo.get_source(adapter_key)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "adapter not in the source catalogue")
    return _source_view(row)


@router.patch("/sources/{adapter_key}")
def configure_source(
    adapter_key: str, payload: SourceConfigIn, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    """Enable/disable a source and set its rate limit and caps (FR-363).

    IR-101 is enforced here rather than only in the adapter: a source whose
    terms prohibit automated access cannot be switched on until an
    administrator has acknowledged that.
    """
    row = repo.get_source(adapter_key)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "adapter not in the source catalogue")
    values = {k: v for k, v in payload.model_dump().items() if v is not None}

    if values.get("enabled") and row["requires_ack"] and not row["acknowledged_at"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{adapter_key} is marked '{row['tos_status']}'; acknowledge the terms first "
            f"(POST /api/admin/sources/{adapter_key}/acknowledge)",
        )

    caps = {k: values.pop(k) for k in ("max_pages", "max_records", "max_seconds") if k in values}
    if caps:
        repo.set_setting(
            f"{SETTING_PREFIX_SOURCE}{adapter_key}.caps", {**source_caps(adapter_key), **caps}
        )
    if "enabled" in values:
        values["enabled"] = 1 if values["enabled"] else 0
    if values:
        repo.update_source(adapter_key, values)

    record_audit(
        "admin.source_configured", "source_catalogue", adapter_key, seeker_id=admin.id,
        detail={**values, "caps": caps},
    )
    return _source_view(repo.get_source(adapter_key) or {})


@router.post("/sources/{adapter_key}/acknowledge")
def acknowledge_source(
    adapter_key: str, payload: AcknowledgeIn, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    """IR-101: record the administrator's acknowledgement for a prohibited source.

    The acknowledgement is a named, timestamped act in the audit trail
    (NFR-702); enabling the source is a separate, later decision.
    """
    row = repo.get_source(adapter_key)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "adapter not in the source catalogue")
    if not payload.accepted:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The terms must be accepted explicitly"
        )
    stamped = utcnow()
    repo.update_source(adapter_key, {"acknowledged_at": stamped})
    record_audit(
        "admin.source_acknowledged", "source_catalogue", adapter_key, seeker_id=admin.id,
        detail={
            "tos_status": row["tos_status"],
            "legal_notes": row["legal_notes"],
            "note": payload.note,
            "acknowledged_at": stamped,
        },
    )
    return _source_view(repo.get_source(adapter_key) or {})


@router.delete("/sources/{adapter_key}/acknowledge")
def revoke_acknowledgement(
    adapter_key: str, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    """Withdraw the acknowledgement and disable the source again (IR-101)."""
    if repo.get_source(adapter_key) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "adapter not in the source catalogue")
    repo.update_source(adapter_key, {"acknowledged_at": None, "enabled": 0})
    record_audit(
        "admin.source_acknowledgement_revoked", "source_catalogue", adapter_key, seeker_id=admin.id
    )
    return _source_view(repo.get_source(adapter_key) or {})


# ---------------------------------------------------------------------------
# Declined sources (FR-182, IR-101)
# ---------------------------------------------------------------------------
#
# A source that was refused across requests or across campaigns is declined
# automatically and left out of every plan until an administrator re-enables
# it.  It used to be rediscovered by every run: the same five sources were
# selected, refused, counted as errors and selected again.  These routes are
# the list and the single explicit act that clears a decline.

#: Decline reasons an administrator has to acknowledge explicitly (IR-101).
_TERMS_DECLINE_REASONS = declines.IR101_REASONS


def _decline_view(row: dict, source: dict | None) -> dict:
    requires_ack = bool((source or {}).get("requires_ack")) or (
        (source or {}).get("tos_status") == "prohibited"
    )
    return {
        **row,
        "display_name": (source or {}).get("display_name") or row.get("adapter_key"),
        "source_type": (source or {}).get("source_type"),
        "tos_status": (source or {}).get("tos_status"),
        "enabled": (source or {}).get("enabled"),
        # The catalogue's own IR-101 stamp, named apart from the decline row's
        # ``acknowledged_at`` (which is when the decline itself was cleared).
        "source_acknowledged_at": (source or {}).get("acknowledged_at"),
        "active": bool(row.get("declined_at")) and not row.get("acknowledged_at"),
        "reason_label": declines.reason_label(row.get("reason")),
        # IR-101: clearing a terms decline is an acknowledgement, and the
        # screen has to ask for it rather than relabel a button.
        "requires_acknowledgement": bool(row.get("reason")) and (
            row["reason"] in _TERMS_DECLINE_REASONS or requires_ack
        ),
    }


@router.get("/source-declines")
def list_source_declines(
    include_cleared: bool = Query(False, description="Also list declines an administrator cleared"),
    admin: CurrentSeeker = Depends(current_admin),
) -> list[dict]:
    """The sources this installation has declined, and why (FR-182, IR-101)."""
    sources = {row["adapter_key"]: row for row in repo.list_sources()}
    return [
        _decline_view(row, sources.get(row["adapter_key"]))
        for row in decline_repo.list_declines(active_only=not include_cleared)
    ]


@router.post("/source-declines/{adapter_key}/enable")
def enable_declined_source(
    adapter_key: str,
    payload: DeclineEnableIn,
    admin: CurrentSeeker = Depends(current_admin),
) -> dict:
    """Re-enable one declined source, with an audit event (FR-363, IR-101).

    A terms decline requires ``acknowledge_terms``: the administrator states
    that they accept the terms, and the catalogue row is stamped accordingly.
    Clearing any other decline is the same explicit act - asking again is what
    the decline exists to stop, so a person has to decide to ask again.
    """
    row = decline_repo.get_decline(adapter_key)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no decline recorded for this source")
    source = repo.get_source(adapter_key)
    needs_terms = (row.get("reason") in _TERMS_DECLINE_REASONS) or bool(
        (source or {}).get("requires_ack")
    ) or ((source or {}).get("tos_status") == "prohibited")
    if needs_terms and not payload.acknowledge_terms:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{adapter_key} is declined for terms reasons; pass acknowledge_terms=true to "
            "state that you accept them and re-enable the source (IR-101)",
        )

    cleared = declines.clear(adapter_key, actor=admin.email or admin.id, note=payload.note)
    if source is not None:
        values: dict[str, Any] = {"enabled": 1}
        if needs_terms:
            values["acknowledged_at"] = utcnow()
        repo.update_source(adapter_key, values)
        record_audit(
            "admin.source_re_enabled",
            "source_catalogue",
            adapter_key,
            seeker_id=admin.id,
            detail={"decline_reason": row.get("reason"), "note": payload.note},
        )
    return _decline_view(cleared or row, repo.get_source(adapter_key))


# ---------------------------------------------------------------------------
# LLM call log and retention (FR-364)
# ---------------------------------------------------------------------------


@router.get("/llm-calls")
def list_llm_calls(
    campaign_id: str | None = None,
    job_seeker_id: str | None = None,
    task: str | None = None,
    call_status: str | None = Query(None, alias="status"),
    entity_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: CurrentSeeker = Depends(current_admin),
) -> dict:
    """Browse the LLM audit log (FR-364).

    Prompt and response text are not in the listing: they are fetched one call
    at a time, which keeps casual browsing away from personal data.
    """
    rows = repo.list_llm_calls(
        campaign_id=campaign_id,
        job_seeker_id=job_seeker_id,
        task=task,
        status=call_status,
        entity_id=entity_id,
        limit=limit,
        offset=offset,
    )
    return {
        "total": repo.count_llm_calls(
            campaign_id,
            job_seeker_id=job_seeker_id,
            task=task,
            status=call_status,
            entity_id=entity_id,
        ),
        "limit": limit,
        "offset": offset,
        "items": rows,
    }


@router.get("/llm-calls/{call_id}")
def get_llm_call(call_id: str, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    row = repo.get_llm_call(call_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "llm_call not found")
    record_audit("admin.llm_call_viewed", "llm_call", call_id, seeker_id=admin.id)
    return row


def run_redaction_job(older_than_days: int | None = None) -> dict:
    """Null prompt and response text past the retention period (FR-364).

    Importable so a scheduler can run it; the endpoint below is the manual
    trigger.  Counters, model and the linked record are kept, because the
    dashboard needs them and they are not personal data.
    """
    days = older_than_days
    if days is None:
        days = int(effective_llm_config()["log_retention_days"])
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    pending = repo.pending_redaction_count(cutoff)
    redacted = repo.redact_llm_calls(cutoff)
    log_metric("llm_log.redacted", cutoff=cutoff, rows=redacted)
    return {"cutoff": cutoff, "retention_days": days, "pending": pending, "redacted": redacted}


@router.get("/llm-calls-retention")
def retention_state(admin: CurrentSeeker = Depends(current_admin)) -> dict:
    days = int(effective_llm_config()["log_retention_days"])
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    return {
        "retention_days": days,
        "cutoff": cutoff,
        "pending_redaction": repo.pending_redaction_count(cutoff),
        "total_calls": repo.count_llm_calls(),
    }


@router.post("/llm-calls/redact")
def redact_llm_calls(payload: RedactIn, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """Run the retention sweep now (FR-364)."""
    result = run_redaction_job(payload.older_than_days)
    record_audit("admin.llm_log_redacted", "llm_call", None, seeker_id=admin.id, detail=result)
    return result


# ---------------------------------------------------------------------------
# Audit trail (NFR-702) and accounts
# ---------------------------------------------------------------------------


@router.get("/audit")
def audit_trail(
    job_seeker_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    since: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    admin: CurrentSeeker = Depends(current_admin),
) -> list[dict]:
    """Read the append-only trail (NFR-702).  There is no write or delete route."""
    return repo.list_audit(
        job_seeker_id=job_seeker_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        since=since,
        limit=limit,
        offset=offset,
    )


@router.get("/seekers")
def list_seekers(admin: CurrentSeeker = Depends(current_admin)) -> list[dict]:
    """Accounts on this installation.  No profile content, only the account row."""
    return seeker_repo.list_seekers()


@router.post("/seekers/{seeker_id}/admin")
def set_admin_flag(
    seeker_id: str, payload: AdminFlagIn, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    if seeker_repo.get_seeker(seeker_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job seeker not found")
    if seeker_id == admin.id and not payload.is_admin:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Refusing to remove your own administrator role"
        )
    seeker_repo.set_admin(seeker_id, payload.is_admin)
    record_audit(
        "admin.role_changed", "job_seeker", seeker_id, seeker_id=admin.id,
        detail={"is_admin": payload.is_admin},
    )
    return {"id": seeker_id, "is_admin": payload.is_admin}


# ---------------------------------------------------------------------------
# User management (FR-362, FR-101, NFR-202)
#
# The administrator's view of the people on this installation: who is here,
# who may administer, and the handful of actions an operator needs when an
# account has to be closed, recovered or handed on. Everything acts through
# ``security.auth_service`` so the policy (password strength, session
# revocation, the last-administrator guard) lives in one place rather than in
# this router.
# ---------------------------------------------------------------------------


def _user_view(row: dict) -> dict:
    """One account for the administration list — never the password or secret."""
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "locale": row.get("locale"),
        "is_admin": bool(row.get("is_admin")),
        "disabled": bool(row.get("disabled")),
        "disabled_at": row.get("disabled_at"),
        "last_login_at": row.get("last_login_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "mfa_enrolled": bool(row.get("mfa_enrolled")),
        "passwordless": bool(row.get("passwordless")),
        "active_sessions": int(row.get("active_sessions") or 0),
    }


def _translate(exc: Exception) -> HTTPException:
    """Map an auth-service refusal onto the right status code."""
    if isinstance(exc, auth.LastAdministrator):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, auth.WeakPassword):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if isinstance(exc, seeker_repo.EmailAlreadyRegistered):
        return HTTPException(
            status.HTTP_409_CONFLICT, "An account already exists for this e-mail address"
        )
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/users")
def list_users(
    q: str | None = Query(None, description="Match the e-mail or the name"),
    include_disabled: bool = True,
    admin: CurrentSeeker = Depends(current_admin),
) -> dict:
    """Every account, searched and ordered for the management screen (FR-362)."""
    rows = seeker_repo.list_seekers(q, include_disabled=include_disabled)
    return {
        "users": [_user_view(r) for r in rows],
        "total": len(rows),
        "administrators": seeker_repo.count_admins(enabled_only=True),
        "me": admin.id,
    }


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateIn, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    """Create an account on behalf of someone else (FR-362)."""
    password = payload.password or auth.suggest_password()
    try:
        row = auth.admin_create_user(
            str(payload.email),
            payload.display_name,
            password,
            is_admin=payload.is_admin,
            locale=payload.locale,
            actor=admin.id,
        )
    except Exception as exc:  # noqa: BLE001 - translated to a status code
        raise _translate(exc) from exc
    # The generated password is shown once, here, and never stored in clear.
    return {
        "user": _user_view({**row, "active_sessions": 0}),
        "password": password,
        "password_generated": payload.password is None,
    }


@router.get("/users/{seeker_id}")
def get_user(seeker_id: str, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    row = seeker_repo.get_seeker_row(seeker_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    row.setdefault("active_sessions", len(seeker_repo.list_sessions(seeker_id)))
    return _user_view(row)


@router.patch("/users/{seeker_id}")
def update_user(
    seeker_id: str,
    payload: UserUpdateIn,
    admin: CurrentSeeker = Depends(current_admin),
) -> dict:
    """Change a role, suspend or restore an account (FR-362).

    An administrator cannot suspend or demote their own account here: doing so
    would end the session they are working in. They can do it from another
    administrator's account, or by signing in as someone else. The last active
    administrator cannot be demoted or suspended by anyone.
    """
    row = seeker_repo.get_seeker_row(seeker_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if seeker_id == admin.id and (payload.is_admin is False or payload.disabled is True):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Refusing to remove administrator access or suspend your own account",
        )

    if payload.display_name is not None or payload.locale is not None:
        values = {
            k: v
            for k, v in {"display_name": payload.display_name, "locale": payload.locale}.items()
            if v is not None
        }
        seeker_repo.update_seeker(seeker_id, values)
        record_audit(
            "admin.user_updated", "job_seeker", seeker_id, seeker_id=admin.id,
            actor=admin.id, detail=values,
        )
    try:
        if payload.is_admin is not None:
            auth.admin_set_admin(seeker_id, payload.is_admin, actor=admin.id)
        if payload.disabled is not None:
            auth.admin_set_disabled(seeker_id, payload.disabled, actor=admin.id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc

    updated = seeker_repo.get_seeker_row(seeker_id) or {}
    updated.setdefault("active_sessions", len(seeker_repo.list_sessions(seeker_id)))
    return _user_view(updated)


@router.post("/users/{seeker_id}/reset-password")
def reset_user_password(
    seeker_id: str,
    payload: UserPasswordIn,
    admin: CurrentSeeker = Depends(current_admin),
) -> dict:
    """Set a new password for another account and end its sessions (NFR-202)."""
    if seeker_repo.get_seeker_row(seeker_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    password = payload.new_password or auth.suggest_password()
    try:
        auth.admin_reset_password(seeker_id, password, actor=admin.id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return {"id": seeker_id, "password": password, "password_generated": payload.new_password is None}


@router.post("/users/{seeker_id}/logout")
def logout_user(seeker_id: str, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """End every session an account has open."""
    try:
        revoked = auth.admin_logout(seeker_id, actor=admin.id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return {"id": seeker_id, "sessions_revoked": revoked}


@router.delete("/users/{seeker_id}")
def delete_user(seeker_id: str, admin: CurrentSeeker = Depends(current_admin)) -> dict:
    """Delete an account and erase its private data (FR-108).

    Irreversible. The shared knowledge base is left intact, as it carries no
    link back to any job seeker (FR-344).
    """
    if seeker_id == admin.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Refusing to delete your own account"
        )
    try:
        counts = auth.admin_delete_user(seeker_id, actor=admin.id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return {"id": seeker_id, "deleted": True, "counts": counts}


@router.post("/users/purge-without-sessions")
def purge_users_without_sessions(
    payload: PurgeIn, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    """Delete every account that has no open session (FR-362).

    Always preview first: the same call with ``dry_run`` left at its default
    returns the count and a sample without deleting anything. Only accounts
    with no unexpired session are candidates - the same figure the user list
    shows - administrators are excluded by default, and the caller is always
    excluded.
    """
    return auth.purge_users_without_sessions(
        admin.id, exclude_admins=payload.exclude_admins, dry_run=payload.dry_run
    )
