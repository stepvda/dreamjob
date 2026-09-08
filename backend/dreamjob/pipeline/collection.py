"""Plan execution: the collection spine (FR-181..186, NFR-401, NFR-403, NFR-603).

Collection turns the persisted plan into knowledge-base rows.  It is the part
of the pipeline that runs longest and fails most often, so the design is built
around interruption rather than around the happy path:

* every page is one unit of work, and the job checkpoints after each one, so a
  crash or a cancel loses at most the page in flight (NFR-401);
* the job is pausable, resumable and cancellable while it runs, and reports
  per-adapter progress and error counts (FR-185);
* four explicit caps - pages, companies, people and wall-clock duration - bound
  the whole run and are checked before every unit of work (FR-186);
* every record is written through the knowledge-base writer, which links it to
  the plan item that produced it (FR-166) and de-duplicates it (FR-184);
* the extraction rate of each adapter is tracked and a collapse is flagged for
  the administrator within the campaign that saw it (NFR-403);
* an adapter that is missing, disabled or blocked by robots.txt fails its own
  plan item and nothing else (IR-101, FR-182).

Stages register themselves here so any of them can be re-run from the persisted
artefacts of the previous one (NFR-603).
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from dreamjob.adapters.base import PlanItem, get_adapter
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import campaigns as repo
from dreamjob.egress.client import EgressClient, RobotsDisallowed
from dreamjob.jobs.runner import JobCancelled, JobContext, runner
from dreamjob.pipeline import knowledge_base, planning

log = logging.getLogger(__name__)

JOB_KIND = "collection"

# NFR-403: an adapter that extracts nothing from pages it used to parse has
# almost certainly been broken by a site change.
BREAKAGE_MIN_ATTEMPTS = 5
BREAKAGE_RATE = 0.5


# ---------------------------------------------------------------------------
# Caps (FR-186)
# ---------------------------------------------------------------------------


@dataclass
class Caps:
    """Hard bounds on one collection run, derived from the plan and editable."""

    max_pages: int = planning.DEFAULT_CAPS["max_pages"]
    max_pages_per_source: int = planning.DEFAULT_CAPS["max_pages_per_source"]
    max_companies: int = planning.DEFAULT_CAPS["max_companies"]
    max_people: int = planning.DEFAULT_CAPS["max_people"]
    max_duration_seconds: int = planning.DEFAULT_CAPS["max_duration_seconds"]

    @classmethod
    def from_campaign(cls, campaign: dict) -> Caps:
        caps = dict(planning.DEFAULT_CAPS)
        stored = campaign.get("caps") or {}
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in caps and isinstance(value, (int, float)):
                    caps[key] = int(value)
        return cls(**{k: caps[k] for k in caps if k in cls.__annotations__})

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class CollectionStats:
    pages: int = 0
    records: int = 0
    errors: int = 0
    companies: set[str] = field(default_factory=set)
    people: set[str] = field(default_factory=set)
    by_adapter: dict[str, dict[str, int]] = field(default_factory=dict)
    stopped_by: str | None = None

    def adapter(self, key: str) -> dict[str, int]:
        return self.by_adapter.setdefault(key, {"pages": 0, "records": 0, "errors": 0})

    def to_dict(self) -> dict:
        return {
            "pages": self.pages,
            "records": self.records,
            "errors": self.errors,
            "companies": len(self.companies),
            "people": len(self.people),
            "by_adapter": self.by_adapter,
            "stopped_by": self.stopped_by,
        }


def _cap_hit(stats: CollectionStats, caps: Caps, started: float) -> str | None:
    if stats.pages >= caps.max_pages:
        return "max_pages"
    if len(stats.companies) >= caps.max_companies:
        return "max_companies"
    if len(stats.people) >= caps.max_people:
        return "max_people"
    if time.monotonic() - started >= caps.max_duration_seconds:
        return "max_duration_seconds"
    return None


# ---------------------------------------------------------------------------
# Worker (FR-181, FR-185, NFR-401)
# ---------------------------------------------------------------------------


def administrator_caps(adapter_key: str) -> dict[str, Any]:
    """Per-source caps the administrator has set (FR-363).

    Read through the repository rather than the admin router, which would
    invert the layering.  These bound the campaign rather than the other way
    round: an operator who caps a source has capped it, and a campaign asking
    for more does not get more.
    """
    try:
        from dreamjob.db.repositories import admin as admin_repo  # noqa: PLC0415

        return admin_repo.get_setting(f"source.{adapter_key}.caps", {}) or {}
    except Exception:  # noqa: BLE001 - configuration must never stop collection
        log.exception("Could not read administrator caps for %s", adapter_key)
        return {}


def _pages_for(item: dict, caps: Caps, capabilities: dict) -> int:
    pages = int(item.get("estimated_pages") or 1)
    if not capabilities.get("pagination", True):
        pages = 1
    item_caps = item.get("caps") or {}
    if isinstance(item_caps, dict) and item_caps.get("max_pages"):
        pages = min(pages, int(item_caps["max_pages"]))
    admin_max = administrator_caps(item.get("adapter_key") or "").get("max_pages")
    if admin_max:
        pages = min(pages, int(admin_max))
    return max(1, min(pages, caps.max_pages_per_source))


def _load_adapter(adapter_key: str, egress: EgressClient) -> Any:
    """Adapters are optional at runtime: a missing one skips its plan item only."""
    try:
        return get_adapter(adapter_key, egress)
    except KeyError:
        log.warning("No adapter registered for %r; its plan item is skipped", adapter_key)
        return None
    except Exception:  # noqa: BLE001 - a broken adapter must not stop the campaign
        log.exception("Adapter %r could not be constructed", adapter_key)
        return None


def _record_extraction(adapter: Any, item: dict, campaign_id: str) -> float | None:
    """NFR-403: keep the rolling extraction rate and flag a collapse."""
    rate = getattr(adapter, "extraction_rate", None)
    attempts = getattr(adapter, "_extraction_attempts", 0)
    repo.record_extraction_rate(item["adapter_key"], rate, had_success=bool(rate))
    if rate is not None and attempts >= BREAKAGE_MIN_ATTEMPTS and rate < BREAKAGE_RATE:
        message = (
            f"extraction rate {rate:.0%} over {attempts} attempts - "
            "the source layout has probably changed (NFR-403)"
        )
        repo.bump_plan_item(item["id"], last_error=message)
        repo.record_audit(
            "adapter.breakage_suspected",
            entity_type="source_catalogue",
            entity_id=item["adapter_key"],
            detail={"campaign_id": campaign_id, "rate": rate, "attempts": attempts},
        )
        log.warning("Adapter %s: %s", item["adapter_key"], message)
    return rate


async def collection_worker(ctx: JobContext) -> None:
    """Execute every active plan item of a campaign, page by page (FR-181)."""
    campaign_id = ctx.campaign_id or ""
    campaign = repo.get_campaign_any(campaign_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id}")

    caps = Caps.from_campaign(campaign)
    stats = CollectionStats()
    completed: dict[str, int] = dict(ctx.checkpoint.get("completed") or {})
    started = time.monotonic()
    # Pages already fetched by an interrupted run count towards both the
    # progress bar and the page cap: this is the same job continuing (NFR-401).
    stats.pages = sum(int(v) for v in completed.values())

    items = [
        i
        for i in repo.list_plan_items(campaign_id, include_excluded=False)
        if i["status"] in ("planned", "running", "failed")
    ]
    # Plan items this run has already given a final status of its own.
    settled: set[str] = set()
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    total_pages = sum(
        _pages_for(i, caps, (catalogue.get(i["adapter_key"], {}).get("query_capabilities") or {}))
        for i in items
    )
    ctx.progress(stats.pages, total_pages)
    repo.set_stage(campaign_id, "collection", "running")

    try:
        async with EgressClient() as egress:
            for item in items:
                entry = catalogue.get(item["adapter_key"], {})
                capabilities = entry.get("query_capabilities") or {}
                adapter = _load_adapter(item["adapter_key"], egress)
                if adapter is None:
                    settled.add(item["id"])
                    repo.update_plan_item(
                        item["id"],
                        {
                            "status": "skipped",
                            "last_error": "no adapter registered for this source",
                        },
                    )
                    continue

                writer = knowledge_base.KnowledgeBaseWriter(
                    adapter_key=item["adapter_key"],
                    plan_item_id=item["id"],
                    campaign_id=campaign_id,
                )
                settled.add(item["id"])
                repo.update_plan_item(item["id"], {"status": "running"})
                pages = _pages_for(item, caps, capabilities)
                first_page = int(completed.get(item["id"], 0)) + 1
                failed = False

                for page in range(first_page, pages + 1):
                    reason = _cap_hit(stats, caps, started)
                    if reason:
                        stats.stopped_by = reason
                        break
                    await ctx.checkpoint_barrier()  # FR-185 pause / cancel

                    plan_item = PlanItem(
                        adapter_key=item["adapter_key"],
                        native_query={**(item.get("native_query") or {}), "page": page},
                        rationale=item.get("rationale") or "",
                        estimated_pages=1,
                        caps=item.get("caps") or {},
                    )
                    try:
                        records = await adapter.run(plan_item)
                    except RobotsDisallowed as exc:
                        repo.bump_plan_item(item["id"], errors=1, last_error=f"robots.txt: {exc}")
                        ctx.record_error(str(exc))
                        stats.adapter(item["adapter_key"])["errors"] += 1
                        stats.errors += 1
                        failed = True
                        break
                    except Exception as exc:  # noqa: BLE001 - one page must not stop the run
                        log.exception("[%s] page %d failed", item["adapter_key"], page)
                        repo.bump_plan_item(item["id"], errors=1, last_error=str(exc))
                        ctx.record_error(str(exc))
                        stats.adapter(item["adapter_key"])["errors"] += 1
                        stats.errors += 1
                        failed = True
                        break

                    written = writer.write_many(records)
                    stats.pages += 1
                    stats.records += len(written)
                    stats.companies |= writer.company_ids
                    stats.people |= writer.people_ids
                    bucket = stats.adapter(item["adapter_key"])
                    bucket["pages"] += 1
                    bucket["records"] += len(written)
                    repo.bump_plan_item(item["id"], records=len(written))

                    # NFR-401: the checkpoint is written after the unit of work,
                    # so a resume re-runs at most this page - and the writer is
                    # idempotent, so re-running it costs time, not correctness.
                    completed[item["id"]] = page
                    ctx.save_checkpoint(completed=completed, stats=stats.to_dict())
                    ctx.progress(stats.pages, total_pages)

                _record_extraction(adapter, item, campaign_id)
                if failed:
                    repo.update_plan_item(item["id"], {"status": "failed"})
                elif stats.stopped_by and completed.get(item["id"], 0) < pages:
                    # Bounded by a cap, not finished: leave it runnable so that
                    # raising the cap continues it from its checkpoint (FR-186).
                    repo.update_plan_item(
                        item["id"],
                        {
                            "status": "planned",
                            "last_error": f"stopped by cap: {stats.stopped_by} (FR-186)",
                        },
                    )
                else:
                    repo.update_plan_item(item["id"], {"status": "done"})
                if stats.stopped_by:
                    break

            if stats.stopped_by:
                # Only the items the cap never reached are rewound: an item that
                # already reported a failure keeps that failure, so a broken
                # adapter stays visible on the dashboard (FR-185, NFR-403).
                for item in items:
                    if item["id"] not in settled:
                        repo.update_plan_item(
                            item["id"],
                            {
                                "status": "planned",
                                "last_error": f"not started: {stats.stopped_by} (FR-186)",
                            },
                        )
    except JobCancelled:
        repo.set_stage(campaign_id, "collection", "cancelled")
        repo.record_audit(
            "campaign.collection_cancelled",
            job_seeker_id=ctx.job_seeker_id,
            entity_type="campaign",
            entity_id=campaign_id,
            detail=stats.to_dict(),
        )
        raise
    except Exception:
        repo.set_stage(campaign_id, "collection", "failed")
        raise

    repo.set_stage(campaign_id, "collection", "completed")
    repo.record_audit(
        "campaign.collection_finished",
        job_seeker_id=ctx.job_seeker_id,
        entity_type="campaign",
        entity_id=campaign_id,
        detail=stats.to_dict(),
    )
    ctx.save_checkpoint(completed=completed, stats=stats.to_dict(), finished_at=utcnow())


runner.register_worker(JOB_KIND, collection_worker)


# ---------------------------------------------------------------------------
# Control surface (FR-185)
# ---------------------------------------------------------------------------


async def launch(campaign_id: str, job_seeker_id: str) -> str:
    """Start collection for a campaign as a background job.  Returns the job id."""
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    items = [
        i
        for i in repo.list_plan_items(campaign_id, include_excluded=False)
        if i["status"] in ("planned", "running", "failed")
    ]
    if not items:
        raise ValueError("Nothing to collect: generate a plan first, or all sources are excluded")

    caps = Caps.from_campaign(campaign)
    total = sum(_pages_for(i, caps, {}) for i in items)
    job_id = runner.create(
        JOB_KIND,
        campaign_id=campaign_id,
        job_seeker_id=job_seeker_id,
        total=total,
        estimated_seconds=sum(int(i["estimated_seconds"] or 0) for i in items),
    )
    await runner.start(job_id, collection_worker)
    repo.set_stage(campaign_id, "collection", "running")
    repo.record_audit(
        "campaign.collection_started",
        job_seeker_id=job_seeker_id,
        entity_type="campaign",
        entity_id=campaign_id,
        detail={"job_id": job_id, "plan_items": len(items), "caps": caps.to_dict()},
    )
    return job_id


def _current_job(campaign_id: str) -> dict | None:
    return repo.latest_job(campaign_id, JOB_KIND)


def pause(campaign_id: str) -> dict:
    job = _current_job(campaign_id)
    ok = bool(job) and runner.pause(job["id"])
    if ok:
        repo.set_stage(campaign_id, "collection", "paused")
    return {"paused": ok, "job_id": (job or {}).get("id")}


def resume(campaign_id: str) -> dict:
    job = _current_job(campaign_id)
    ok = bool(job) and runner.resume(job["id"])
    if ok:
        repo.set_stage(campaign_id, "collection", "running")
    return {"resumed": ok, "job_id": (job or {}).get("id")}


def cancel(campaign_id: str) -> dict:
    job = _current_job(campaign_id)
    ok = bool(job) and runner.cancel(job["id"])
    repo.set_stage(campaign_id, "collection", "cancelled")
    return {"cancelled": bool(job), "was_running": ok, "job_id": (job or {}).get("id")}


async def resume_job(campaign_id: str, job_seeker_id: str) -> str:
    """Restart a job that a crash or a restart left unfinished (NFR-401)."""
    job = _current_job(campaign_id)
    resumable = job and job["status"] in ("pending", "paused", "running")
    if resumable and not runner.is_running(job["id"]):
        await runner.start(job["id"], collection_worker)
        repo.set_stage(campaign_id, "collection", "running")
        return job["id"]
    return await launch(campaign_id, job_seeker_id)


# ---------------------------------------------------------------------------
# Dashboard (FR-185, FR-361)
# ---------------------------------------------------------------------------


def status(campaign_id: str, job_seeker_id: str) -> dict:
    """Per-adapter progress, errors, cost and remaining time for one campaign."""
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    items = repo.list_plan_items(campaign_id)
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    jobs = repo.list_jobs(campaign_id)
    job = next((j for j in jobs if j["kind"] == JOB_KIND), None)

    sources = []
    breakage = []
    remaining_seconds = 0
    for item in items:
        entry = catalogue.get(item["adapter_key"], {})
        rate = entry.get("extraction_success_rate")
        if item["status"] not in ("done", "skipped") and not item["excluded_by_user"]:
            remaining_seconds += int(item["estimated_seconds"] or 0)
        sources.append(
            {
                "plan_item_id": item["id"],
                "adapter_key": item["adapter_key"],
                "display_name": entry.get("display_name") or item["adapter_key"],
                "status": item["status"],
                "excluded_by_user": bool(item["excluded_by_user"]),
                "records_collected": item["records_collected"],
                "error_count": item["error_count"],
                "last_error": item["last_error"],
                "estimated_pages": item["estimated_pages"],
                "extraction_success_rate": rate,
            }
        )
        if rate is not None and rate < BREAKAGE_RATE:
            breakage.append({"adapter_key": item["adapter_key"], "extraction_success_rate": rate})

    return {
        "campaign_id": campaign_id,
        "status": campaign.get("status"),
        "stage": campaign.get("stage"),
        "caps": Caps.from_campaign(campaign).to_dict(),
        "started_at": campaign.get("started_at"),
        "finished_at": campaign.get("finished_at"),
        "job": job,
        "jobs": jobs,
        "progress": {
            "pages_done": (job or {}).get("progress_done") or 0,
            "pages_total": (job or {}).get("progress_total"),
            "estimated_seconds_remaining": remaining_seconds,
        },
        "sources": sources,
        "collected": repo.collected_counts(campaign_id),
        "llm": repo.llm_totals(campaign_id),
        "budget": {
            "token_budget": campaign.get("token_budget"),
            "tokens_used": campaign.get("tokens_used"),
            "cost_eur": campaign.get("cost_eur"),
        },
        "reuse_report": campaign.get("reuse_report"),
        "adapter_breakage": breakage,
    }


# ---------------------------------------------------------------------------
# Stage registry (NFR-603)
# ---------------------------------------------------------------------------

StageRunner = Callable[..., "Awaitable[dict] | dict"]

_STAGES: dict[str, dict[str, Any]] = {}

# Stages owned by other slices.  A slice joins the re-run surface either by
# calling ``register_stage`` at import time, or simply by exposing
# ``rerun(campaign_id, job_seeker_id, **options)`` in one of these modules -
# they are imported lazily, so a module that does not exist yet costs nothing.
_OPTIONAL_STAGES: dict[str, tuple[str, str]] = {
    "profiling": ("dreamjob.pipeline.composite", "rerun"),
    "enrichment": ("dreamjob.pipeline.enrichment", "rerun"),
    "company_profile": ("dreamjob.pipeline.company_profile", "rerun"),
    "financials": ("dreamjob.pipeline.financial", "rerun"),
    "opportunities": ("dreamjob.pipeline.opportunities", "rerun"),
    "scoring": ("dreamjob.pipeline.scoring", "rerun"),
    "generation": ("dreamjob.pipeline.generation", "rerun"),
}

# Stages whose owning slice already re-runs a whole campaign from its persisted
# artefacts under its own name.  These are the fall-back: a module that grows a
# ``rerun`` of its own takes precedence, because only its author knows what its
# re-run should mean.
_CAMPAIGN_STAGES: dict[str, tuple[str, str, str]] = {
    "opportunities": (
        "dreamjob.pipeline.opportunities",
        "synthesise_campaign",
        "Re-synthesise opportunities from the collected vacancies (FR-261)",
    ),
    "scoring": (
        "dreamjob.pipeline.scoring",
        "score_campaign",
        "Re-score every opportunity in the campaign (FR-281)",
    ),
}


def register_stage(name: str, fn: StageRunner, *, description: str = "") -> None:
    """Make a pipeline stage independently re-runnable (NFR-603)."""
    _STAGES[name] = {"fn": fn, "description": description}


def _load(module_name: str, attribute: str) -> Any | None:
    """Import another slice's entry point, or ``None`` while it does not exist."""
    try:
        return getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError):
        return None


def _campaign_shim(fn: Any) -> StageRunner:
    """Adapt a ``fn(campaign, **options)`` stage to the re-run signature.

    The campaign is loaded with the seeker's own id, so the re-run cannot reach
    another job seeker's campaign (FR-101).
    """

    def run(campaign_id: str, job_seeker_id: str, **options: Any) -> Any:
        campaign = repo.get_campaign(campaign_id, job_seeker_id)
        if campaign is None:
            raise LookupError(f"No campaign {campaign_id} for this job seeker")
        return fn(campaign, **options)

    return run


def _resolve_stage(name: str) -> dict | None:
    if name in _STAGES:
        return _STAGES[name]
    target = _OPTIONAL_STAGES.get(name)
    if target:
        fn = _load(*target)
        if fn is not None:
            register_stage(name, fn, description=f"provided by {target[0]}")
            return _STAGES[name]
    fallback = _CAMPAIGN_STAGES.get(name)
    if fallback:
        module_name, attribute, description = fallback
        fn = _load(module_name, attribute)
        if fn is not None:
            register_stage(name, _campaign_shim(fn), description=description)
            return _STAGES[name]
    return None


def available_stages() -> list[dict]:
    for name in (*_OPTIONAL_STAGES, *_CAMPAIGN_STAGES):
        _resolve_stage(name)
    return [
        {"stage": name, "description": meta["description"]}
        for name, meta in sorted(_STAGES.items())
    ]


async def rerun_stage(stage: str, campaign_id: str, job_seeker_id: str, **options: Any) -> dict:
    """Re-run one stage from what the previous stage persisted (NFR-603)."""
    meta = _resolve_stage(stage)
    if meta is None:
        raise KeyError(f"Unknown pipeline stage {stage!r}")
    repo.record_audit(
        "campaign.stage_rerun",
        job_seeker_id=job_seeker_id,
        entity_type="campaign",
        entity_id=campaign_id,
        detail={"stage": stage, "options": options},
    )
    fn = meta["fn"]
    if inspect.iscoroutinefunction(fn):
        result = await fn(campaign_id, job_seeker_id, **options)
    else:
        # A synchronous stage (planning calls the LLM) runs off the event loop
        # so a re-run never blocks the collection jobs already in flight.
        result = await asyncio.to_thread(fn, campaign_id, job_seeker_id, **options)
    return _as_dict(stage, result)


def _as_dict(stage: str, result: Any) -> dict:
    """Every slice reports its own way; the API contract is a JSON object."""
    if isinstance(result, dict):
        return result
    for method in ("as_dict", "to_dict"):
        rendered = getattr(result, method, None)
        if callable(rendered):
            value = rendered()
            if isinstance(value, dict):
                return value
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    return {"stage": stage, "result": result}


def _stage_planning(campaign_id: str, job_seeker_id: str, **options: Any) -> dict:
    """Re-plan from the persisted directives, composite profile and dream job model."""
    return planning.generate_plan(campaign_id, job_seeker_id, **options)


def _stage_reuse(campaign_id: str, job_seeker_id: str, **options: Any) -> dict:
    """Re-run the knowledge-base comparison against the persisted plan (FR-342)."""
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    inputs = repo.load_planning_inputs(campaign)
    countries = planning.target_countries(inputs["directives"])
    return knowledge_base.assess_reuse(campaign_id, countries=countries, **options).to_dict()


async def _stage_collection(campaign_id: str, job_seeker_id: str, **options: Any) -> dict:
    """Re-collect from the persisted source plan; counters start clean."""
    if options.get("reset", True):
        repo.reset_plan_progress(campaign_id)
    job_id = await launch(campaign_id, job_seeker_id)
    return {"stage": "collection", "job_id": job_id}


register_stage("planning", _stage_planning, description="Regenerate the source plan (FR-161..166)")
register_stage("reuse", _stage_reuse, description="Re-assess knowledge-base reuse (FR-342)")
register_stage(
    "collection", _stage_collection, description="Re-run collection from the plan (FR-181..186)"
)
