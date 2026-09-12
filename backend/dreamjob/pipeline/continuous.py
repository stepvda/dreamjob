"""The endless background data-collection cycle (FR-161..166, FR-185, FR-221..246, FR-301..303, FR-281).

An administrator switches this on and the product keeps expanding its corpus on
its own, one *phase* per interval, for as long as it stays on:

============================  ==================================================
phase                         what it advances
============================  ==================================================
``discover``                  the next ready job seeker's autopilot run: the
                              resumable chain that builds a profile into a
                              planned, collected, ranked campaign (FR-161..166)
``contacts``                  the FR-301/FR-303 corpus: fill in e-mail
                              addresses for stored contacts, and rotate in a
                              discovery pass so newly collected companies get
                              somebody to write to as well
``enrich``                    the cursor seeker's campaign first - company
                              profiles and their financials (FR-221..246) - or
                              the shared knowledge base when there is no
                              campaign yet
``score``                     the deterministic ranking pass over the cursor
                              seeker's campaign (FR-281); no model is called
============================  ==================================================

The cycle is a state machine in ``app_setting``, not in memory: ``enabled``,
``interval_seconds``, ``phase``, ``seeker_cursor``, ``last_tick``,
``last_phase_at`` and ``last_report``.  A restart therefore resumes where it
stopped, and the admin screen reads the same rows the engine writes.

Two properties make it safe to run inside the API process:

*It gives way.*  A phase does not start while a collection or contacts
discovery job owns the registries, egress and single writer
(:func:`_heavy_job_running`); it defers and the next tick tries again.  The
scheduler task that calls it ticks every five minutes, and the engine itself
enforces the configured interval, so a short tick never runs a phase early.

*It never raises out of a tick.*  A phase that fails is recorded in
``last_report`` with its error and the cycle advances to the next phase, so one
broken source or one unready profile cannot stop the loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import query_all, query_one, to_json, update_row, utcnow
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import seekers as seeker_repo
from dreamjob.jobs.runner import QUEUED_ERROR_MARKER, runner
from dreamjob.pipeline import (
    apply_contacts,
    autopilot,
    company_enrichment,
    company_profile,
    contact_email_backfill,
    financial,
    scoring,
)

log = logging.getLogger(__name__)

#: The phases, in the order one cycle walks them.
PHASES: tuple[str, ...] = ("discover", "contacts", "enrich", "score")

#: Default time between phases; the administrator can configure this.
DEFAULT_INTERVAL_SECONDS = 900
MIN_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 86400

#: ``app_setting`` keys.  All under ``continuous.`` so one prefix fetch sees
#: the whole state machine.
SETTING_ENABLED = "continuous.enabled"
SETTING_INTERVAL = "continuous.interval_seconds"
SETTING_PHASE = "continuous.phase"
SETTING_CURSOR = "continuous.seeker_cursor"
SETTING_LAST_TICK = "continuous.last_tick"
SETTING_LAST_PHASE_AT = "continuous.last_phase_at"
SETTING_LAST_REPORT = "continuous.last_report"
SETTING_CAMPAIGN_PREFIX = "continuous.campaign."
#: Alternates the contacts phase between backfill and discovery.
SETTING_CONTACTS_TICK = "continuous.contacts_tick"

#: How much work one contacts phase may start (FR-185: bounded passes).
CONTACT_LIMIT = 300
#: How many companies one enrichment phase covers.
ENRICH_LIMIT = 10
#: The contacts job kinds the phase will not stack (FR-185, NFR-102).
CONTACT_JOB_KINDS = ("contact_email_backfill", "contacts_discovery")


# ---------------------------------------------------------------------------
# State (app_setting)
# ---------------------------------------------------------------------------


def _setting(key: str, default: Any = None) -> Any:
    return admin_repo.get_setting(key, default)


def _store(key: str, value: Any) -> None:
    admin_repo.set_setting(key, value)


def enabled() -> bool:
    """The administrator's switch, read on its own so a tick can check it cheaply."""
    return bool(_setting(SETTING_ENABLED, False))


def _interval() -> int:
    try:
        value = int(_setting(SETTING_INTERVAL, DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, value))


def _current_phase() -> str:
    phase = _setting(SETTING_PHASE, PHASES[0])
    return phase if phase in PHASES else PHASES[0]


def _next_phase(phase: str) -> str:
    try:
        return PHASES[(PHASES.index(phase) + 1) % len(PHASES)]
    except ValueError:
        return PHASES[0]


def _moment(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _next_due(last_phase_at: str | None, interval: int) -> str | None:
    moment = _moment(last_phase_at)
    if moment is None:
        return None
    return (moment + timedelta(seconds=interval)).isoformat(timespec="seconds")


def _due(last_phase_at: str | None, interval: int, now: datetime | None = None) -> bool:
    moment = _moment(last_phase_at)
    if moment is None:
        return True
    return (now or datetime.now(UTC)) >= moment + timedelta(seconds=interval)


def get_state() -> dict[str, Any]:
    """The whole state machine, for the admin screen.

    Deliberately cheap: a handful of settings reads and one small count query,
    so the scheduler's five-minute tick can afford it and the admin GET never
    waits on the corpus-wide counters (``admin.global_counters``).
    """
    interval = _interval()
    last_phase_at = _setting(SETTING_LAST_PHASE_AT)
    return {
        "enabled": enabled(),
        "interval_seconds": interval,
        "phase": _current_phase(),
        "seeker_cursor": _setting(SETTING_CURSOR),
        "last_tick": _setting(SETTING_LAST_TICK),
        "last_phase_at": last_phase_at,
        "next_due": _next_due(last_phase_at, interval),
        "last_report": _setting(SETTING_LAST_REPORT),
        "scheduler": _scheduler_state(),
        "counts": _counts(),
    }


def set_enabled(enabled: bool, interval_seconds: int | None = None) -> dict[str, Any]:
    """Turn the cycle on or off, optionally changing its interval.

    Enabling does not run anything here: the next scheduler tick sees the flag
    and runs the current phase (or the first due one).  Disabling leaves the
    cursor and last report in place so the state is readable while off.
    """
    if interval_seconds is not None:
        try:
            interval = int(interval_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("interval_seconds must be an integer") from exc
        if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
            raise ValueError(
                f"interval_seconds must be between {MIN_INTERVAL_SECONDS} "
                f"and {MAX_INTERVAL_SECONDS}"
            )
        _store(SETTING_INTERVAL, interval)
    _store(SETTING_ENABLED, bool(enabled))
    return get_state()


def _scheduler_state() -> dict[str, Any]:
    """What the process-wide scheduler knows about this task, if it is loaded."""
    try:
        from dreamjob.monitoring import scheduler as scheduler_mod  # noqa: PLC0415 - import cycle

        task = scheduler_mod.scheduler.task("continuous_collection")
        return {
            "task": "continuous_collection",
            "interval_seconds": task.interval_seconds if task else None,
            "enabled": bool(task.enabled) if task else None,
            "last_run": scheduler_mod._last_run("continuous_collection"),
        }
    except Exception:  # noqa: BLE001 - status must render even without the scheduler
        log.debug("Could not read the scheduler state", exc_info=True)
        return {}


def _counts() -> dict[str, Any]:
    row = query_one(
        "SELECT (SELECT COUNT(*) FROM job_seeker) AS seekers, "
        "       (SELECT COUNT(*) FROM campaign WHERE name = ?) AS campaigns",
        (campaign_repo.CONTINUOUS_CAMPAIGN_NAME,),
    ) or {}
    return {
        "seekers": int(row.get("seekers") or 0),
        "continuous_campaigns": int(row.get("campaigns") or 0),
        "heavy_job": _heavy_job_running(),
    }


# ---------------------------------------------------------------------------
# Deferral guards
# ---------------------------------------------------------------------------


def _heavy_job_running() -> str | None:
    """The kind of a running collection/discovery job, if there is one.

    The scheduler owns the definition (which kinds are heavy and why); this
    wrapper exists so the engine, the tests and the scheduler answer the same
    question with the same code.
    """
    from dreamjob.monitoring import scheduler as scheduler_mod  # noqa: PLC0415 - import cycle

    return scheduler_mod._heavy_job_running()


def _contact_job_running() -> str | None:
    """A backfill or discovery job that is running or genuinely queued."""
    marks = ", ".join("?" for _ in CONTACT_JOB_KINDS)
    row = query_one(
        f"SELECT kind FROM job_run WHERE kind IN ({marks}) AND "
        "(status = 'running' OR (status = 'pending' AND last_error = ?)) LIMIT 1",
        (*CONTACT_JOB_KINDS, QUEUED_ERROR_MARKER),
    )
    return str(row["kind"]) if row else None


def _autopilot_running(seeker_id: str) -> bool:
    row = query_one(
        "SELECT 1 AS hit FROM job_run WHERE job_seeker_id = ? AND kind = 'autopilot' "
        "AND status IN ('pending', 'running', 'paused') LIMIT 1",
        (seeker_id,),
    )
    return row is not None


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


async def run_phase(*, force: bool = False, phase: str | None = None) -> dict[str, Any]:
    """Run one phase of the cycle and advance the round-robin.

    ``force`` bypasses both the administrator's switch and the interval gate -
    it is the admin screen's "run now".  ``phase`` runs one named phase
    instead of the current one and deliberately does not move the cycle: a
    manual look at ``enrich`` must not make the next tick skip it.

    Returns a compact report suitable for ``scheduler.last_run``:
    ``{"phase", "at", ...counters}`` on a run, and one of the early exits
    ``{"enabled": False}``, ``{"skipped": "not due"}`` or
    ``{"deferred": "heavy job running"}`` when nothing ran.
    """
    if phase is not None and phase not in PHASES:
        raise ValueError(f"Unknown phase {phase!r}; expected one of {PHASES}")
    current = phase or PHASES[0]
    try:
        current = phase or _current_phase()
        if not enabled() and not force:
            return {"enabled": False}
        if not force and not _due(_setting(SETTING_LAST_PHASE_AT), _interval()):
            return {"skipped": "not due"}
        busy = _heavy_job_running()
        if busy:
            log.info("Continuous collection deferred: a %s job is running", busy)
            return {"deferred": "heavy job running"}

        try:
            report = await _run_phase(current)
        except Exception as exc:  # noqa: BLE001 - one tick must never stop the loop
            log.exception("Continuous collection phase %s failed", current)
            report = {"error": f"{type(exc).__name__}: {exc}"[:300]}

        payload = dict(report) if isinstance(report, dict) else {"result": report}
        payload.setdefault("phase", current)
        payload.setdefault("at", utcnow())

        _store(SETTING_LAST_PHASE_AT, payload["at"])
        _store(SETTING_LAST_TICK, payload["at"])
        _store(SETTING_LAST_REPORT, payload)
        if phase is None:
            _store(SETTING_PHASE, _next_phase(current))
        log.info(
            "Continuous collection %s finished: %s",
            current,
            {k: v for k, v in payload.items() if isinstance(v, (int, str, bool))},
        )
        return payload
    except Exception as exc:  # noqa: BLE001 - the loop outlives any single tick
        log.exception("Continuous collection tick failed")
        return {"phase": current, "at": utcnow(), "error": f"{type(exc).__name__}: {exc}"[:300]}


async def _run_phase(phase: str) -> dict[str, Any]:
    """Dispatch one phase.  Kept separate so a test can record the order."""
    if phase == "discover":
        return await _phase_discover()
    if phase == "contacts":
        return await _phase_contacts()
    if phase == "enrich":
        return await _phase_enrich()
    return await _phase_score()


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


def _seeker_pool() -> list[str]:
    """Every account that could run, round-robin order.

    All enabled accounts, not only the "active" ones ``pipeline_cards`` knows
    about: the cycle exists to give a profile its first campaign, and a seeker
    with no campaign yet is exactly who ``active_seeker_ids`` cannot see.
    """
    rows = seeker_repo.list_seekers(include_disabled=False)
    return sorted(str(row["id"]) for row in rows if row.get("id"))


def _campaign_for(seeker_id: str) -> str | None:
    """The seeker's continuous campaign, creating it on first use."""
    stored = _setting(f"{SETTING_CAMPAIGN_PREFIX}{seeker_id}")
    if isinstance(stored, str) and stored:
        row = campaign_repo.get_campaign_any(stored)
        if row is not None and str(row.get("job_seeker_id")) == seeker_id:
            return stored
    campaign_id = campaign_repo.get_or_create_continuous_campaign(seeker_id)
    if campaign_id:
        _store(f"{SETTING_CAMPAIGN_PREFIX}{seeker_id}", campaign_id)
    return campaign_id


def _stored_campaign(seeker_id: str | None) -> str | None:
    """The stored continuous campaign, without creating anything."""
    if not seeker_id:
        return None
    stored = _setting(f"{SETTING_CAMPAIGN_PREFIX}{seeker_id}")
    if not isinstance(stored, str) or not stored:
        return None
    row = campaign_repo.get_campaign_any(stored)
    if row is None or str(row.get("job_seeker_id")) != str(seeker_id):
        return None
    return stored


async def _phase_discover() -> dict[str, Any]:
    """Start the next ready seeker's autopilot run (at most one per phase)."""
    pool = _seeker_pool()
    if not pool:
        return {"skipped": "no seekers"}

    cursor = _setting(SETTING_CURSOR)
    start = pool.index(cursor) + 1 if cursor in pool else 0
    skipped: list[dict[str, Any]] = []
    for offset in range(len(pool)):
        candidate = pool[(start + offset) % len(pool)]
        verdict = autopilot.preflight(candidate)
        if not verdict.get("ready"):
            skipped.append(
                {
                    "seeker_id": candidate,
                    "reasons": [b.get("code") for b in verdict.get("blockers") or []],
                }
            )
            continue
        if _autopilot_running(candidate):
            skipped.append({"seeker_id": candidate, "reasons": ["autopilot_running"]})
            continue
        campaign_id = _campaign_for(candidate)
        if not campaign_id:
            skipped.append({"seeker_id": candidate, "reasons": ["no_campaign"]})
            continue
        job_id = await autopilot.start(candidate, campaign_id=campaign_id)
        _store(SETTING_CURSOR, candidate)
        return {
            "seeker_id": candidate,
            "campaign_id": campaign_id,
            "job_id": job_id,
            "skipped_seekers": skipped[:3],
        }

    # Nobody could run.  Move the cursor past the whole pool so the next
    # discover phase starts from the beginning rather than the same profile.
    _store(SETTING_CURSOR, pool[-1])
    return {"skipped": "no ready seeker", "reasons": skipped[:5]}


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------


async def _launch_job(
    kind: str, options: dict[str, Any], *, estimated_seconds: int
) -> str:
    """Create, checkpoint and start a contact job (the router's sequence)."""
    job_id = await asyncio.to_thread(
        runner.create,
        kind,
        job_seeker_id=None,
        total=1,
        estimated_seconds=estimated_seconds,
    )
    await asyncio.to_thread(
        update_row, "job_run", job_id, {"checkpoint": to_json({"options": options})}
    )
    await runner.start(job_id)
    return job_id


async def _phase_contacts() -> dict[str, Any]:
    """Start one bounded contacts pass, alternating backfill and discovery.

    Backfill answers the rows that already name a person but have no address;
    discovery walks companies that have nobody at all, which is how the
    companies the discover phase adds get contacts too.  Alternating is why
    the cycle does both instead of one starving the other.
    """
    running = _contact_job_running()
    if running:
        return {"skipped": "job running", "job_kind": running}

    try:
        tick = int(_setting(SETTING_CONTACTS_TICK, 0) or 0)
    except (TypeError, ValueError):
        tick = 0
    if tick % 2 == 0:
        kind = contact_email_backfill.BACKFILL_JOB_KIND
        options = {
            "limit": CONTACT_LIMIT,
            "scope": "all",
            "max_companies": None,
            "allow_smtp": False,
            "crawl_site": True,
            "use_lookup_service": False,
            "backup_methods": True,
        }
        mode = "backfill"
        estimated = max(60, min(CONTACT_LIMIT, 1000) * 2)
    else:
        kind = apply_contacts.DISCOVERY_JOB_KIND
        options = {
            "limit": CONTACT_LIMIT,
            "scope": "all",
            "max_companies": None,
            "allow_smtp": False,
            "crawl_site": True,
            "derive_domains": True,
            "allow_generic": True,
            "refresh": False,
            "order": "vacancies",
            "backup_methods": True,
        }
        mode = "discovery"
        estimated = max(60, min(CONTACT_LIMIT, 1000) * 8)

    job_id = await _launch_job(kind, options, estimated_seconds=estimated)
    _store(SETTING_CONTACTS_TICK, tick + 1)
    return {"mode": mode, "job_kind": kind, "job_id": job_id, "limit": CONTACT_LIMIT}


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------


def _summarise(result: Any) -> Any:
    """Keep a pass report small enough to store in ``last_report``."""
    if isinstance(result, dict):
        keep = ("companies", "profiled", "reused", "built", "refreshed", "skipped", "failed",
                "analysed", "estimated", "years_collected", "signals", "competitors")
        return {k: result[k] for k in keep if k in result}
    return str(result)[:300]


def _enrich_campaign_off_loop(campaign_id: str, seeker_id: str, limit: int) -> dict[str, Any]:
    """Company profiles then financials, on a worker thread (NFR-102).

    Both passes look async but their leaves are synchronous - the profile
    synthesis drives the synchronous LLM client and the registries are blocking
    HTTP - so they run in a thread with their own loop, exactly as the
    scheduled enrichment sweep does.  A failed pass is recorded, not raised:
    the next tick or the next phase is free to try again.
    """
    report: dict[str, Any] = {}
    try:
        report["profiles"] = _summarise(
            asyncio.run(company_profile.rerun(campaign_id, seeker_id, limit=limit))
        )
    except Exception as exc:  # noqa: BLE001 - an optional pass must not fail the tick
        log.exception("Continuous enrichment: company profiling failed")
        report["profiles"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        report["financials"] = _summarise(
            asyncio.run(
                financial.rerun(campaign_id, seeker_id, limit=limit, collect=True)
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Continuous enrichment: financial analysis failed")
        report["financials"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return report


def _companies_for_shared_enrichment(limit: int) -> list[str]:
    rows = query_all(
        "SELECT o.company_id AS id, COUNT(*) AS n FROM opportunity o "
        "WHERE o.company_id IS NOT NULL GROUP BY o.company_id "
        "ORDER BY n DESC LIMIT ?",
        (limit,),
    )
    return [str(row["id"]) for row in rows]


def _enrich_shared_off_loop(company_ids: list[str]) -> dict[str, Any]:
    """The shared knowledge base belongs to no campaign (FR-344)."""
    try:
        report = asyncio.run(
            company_enrichment.enrich_companies(
                company_ids, campaign_id=None, job_seeker_id=None, limit=len(company_ids)
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Continuous enrichment: shared knowledge-base pass failed")
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return _summarise(report)


async def _phase_enrich() -> dict[str, Any]:
    seeker_id = _setting(SETTING_CURSOR)
    campaign_id = _stored_campaign(seeker_id)
    if campaign_id:
        data = await asyncio.to_thread(
            _enrich_campaign_off_loop, campaign_id, str(seeker_id), ENRICH_LIMIT
        )
        return {"seeker_id": seeker_id, "campaign_id": campaign_id, **data}

    company_ids = _companies_for_shared_enrichment(ENRICH_LIMIT)
    if not company_ids:
        return {"skipped": "no companies"}
    data = await asyncio.to_thread(_enrich_shared_off_loop, company_ids)
    return {"companies": len(company_ids), **data}


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


async def _phase_score() -> dict[str, Any]:
    """The deterministic ranking pass; no model is called (FR-281)."""
    seeker_id = _setting(SETTING_CURSOR)
    campaign_id = _stored_campaign(seeker_id)
    if not seeker_id or not campaign_id:
        return {"skipped": "no campaign"}
    scored = await asyncio.to_thread(
        scoring.score_unscored, str(seeker_id), campaign_id
    )
    return {
        "seeker_id": seeker_id,
        "campaign_id": campaign_id,
        "scored": int(scored or 0),
    }
