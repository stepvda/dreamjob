"""Campaign API: plan, review, launch, monitor, re-run (FR-161..166, FR-181..186).

The route order follows the way a campaign is actually run: create it, generate
a plan, read the plan and the knowledge-base saving, edit or exclude individual
sources, launch, watch, pause, and re-run a single stage when something needs
redoing (NFR-603).
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker, owned_or_404
from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import campaigns as repo
from dreamjob.pipeline import collection, knowledge_base, planning
from dreamjob.pipeline.declines import NoUsableSources

router = APIRouter()

# FR-101: every route in this module is scoped to the authenticated job seeker.
Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CampaignIn(BaseModel):
    name: str
    directive_set_id: str
    profile_version_id: str
    persona_id: str | None = None
    composite_profile_id: str | None = None
    dream_job_model_id: str | None = None
    token_budget: int | None = None
    caps: dict[str, int] | None = None


class CampaignPatch(BaseModel):
    name: str | None = None
    token_budget: int | None = None
    caps: dict[str, int] | None = None
    directive_set_id: str | None = None
    composite_profile_id: str | None = None
    dream_job_model_id: str | None = None


class PlanRequest(BaseModel):
    use_llm: bool = True
    assess_knowledge_base: bool = True


class PlanItemPatch(BaseModel):
    """FR-163: the job seeker edits or excludes an individual source plan."""

    native_query: dict[str, Any] | None = None
    rationale: str | None = None
    estimated_pages: int | None = Field(default=None, ge=0, le=500)
    caps: dict[str, Any] | None = None
    excluded_by_user: bool | None = None


class StalenessPolicyIn(BaseModel):
    policy: dict[str, int]


class RerunRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


def _campaign_or_404(campaign_id: str, seeker_id: str) -> dict:
    campaign = repo.get_campaign(campaign_id, seeker_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    return campaign


# ---------------------------------------------------------------------------
# Knowledge base, independent of any campaign (FR-343, FR-345)
# ---------------------------------------------------------------------------


@router.get("/knowledge-base/companies")
def browse_companies(
    seeker: Seeker,
    q: str | None = None,
    country: str | None = None,
    sector: str | None = None,
    size_band: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Search the shared company knowledge base (FR-345)."""
    return knowledge_base.browse_companies(
        q, country=country, sector=sector, size_band=size_band, limit=limit, offset=offset
    )


@router.get("/knowledge-base/vacancies")
def browse_vacancies(
    seeker: Seeker,
    q: str | None = None,
    country: str | None = None,
    company_id: str | None = None,
    fresh_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Search the shared vacancy knowledge base (FR-345)."""
    return knowledge_base.browse_vacancies(
        q,
        country=country,
        company_id=company_id,
        fresh_only=fresh_only,
        limit=limit,
        offset=offset,
    )


@router.get("/staleness-policy")
def read_staleness_policy(seeker: Seeker) -> dict:
    """FR-343: the configurable staleness policy per record type, in days."""
    return {
        "policy_days": knowledge_base.get_staleness_policy(),
        "defaults": knowledge_base.DEFAULT_STALENESS_DAYS,
    }


@router.put("/staleness-policy")
def write_staleness_policy(
    body: StalenessPolicyIn, seeker: Seeker
) -> dict:
    try:
        policy = knowledge_base.set_staleness_policy(body.policy)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"policy_days": policy}


@router.get("/stages")
def list_stages(seeker: Seeker) -> list[dict]:
    """Stages that can be re-run from persisted artefacts (NFR-603)."""
    return collection.available_stages()


@router.get("/sources")
def list_sources(seeker: Seeker) -> list[dict]:
    """The source catalogue the planner selects from (FR-161)."""
    return repo.list_catalogue(enabled_only=False)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
def create_campaign(body: CampaignIn, seeker: Seeker) -> dict:
    owned_or_404("directive_set", body.directive_set_id, seeker.id)
    owned_or_404("profile_version", body.profile_version_id, seeker.id)
    if body.composite_profile_id:
        owned_or_404("composite_profile", body.composite_profile_id, seeker.id)
    if body.dream_job_model_id:
        owned_or_404("dream_job_model", body.dream_job_model_id, seeker.id)

    values: dict[str, Any] = {
        "name": body.name,
        "directive_set_id": body.directive_set_id,
        "profile_version_id": body.profile_version_id,
        "persona_id": body.persona_id,
        "composite_profile_id": body.composite_profile_id,
        "dream_job_model_id": body.dream_job_model_id,
        "token_budget": body.token_budget or get_settings().default_token_budget,
        "caps": {**planning.DEFAULT_CAPS, **(body.caps or {})},
    }
    campaign_id = repo.create_campaign(seeker.id, values)
    repo.record_audit(
        "campaign.created",
        job_seeker_id=seeker.id,
        actor=seeker.email,
        entity_type="campaign",
        entity_id=campaign_id,
        detail={"name": body.name},
    )
    return repo.get_campaign(campaign_id, seeker.id) or {}


@router.get("")
def list_campaigns(seeker: Seeker) -> list[dict]:
    return repo.list_campaigns(seeker.id)


@router.get("/{campaign_id}")
def read_campaign(campaign_id: str, seeker: Seeker) -> dict:
    campaign = _campaign_or_404(campaign_id, seeker.id)
    campaign["plan"] = repo.list_plan_items(campaign_id)
    return campaign


@router.patch("/{campaign_id}")
def update_campaign(
    campaign_id: str, body: CampaignPatch, seeker: Seeker
) -> dict:
    campaign = _campaign_or_404(campaign_id, seeker.id)
    values: dict[str, Any] = {}
    if body.name is not None:
        values["name"] = body.name
    if body.token_budget is not None:
        values["token_budget"] = body.token_budget
    if body.caps is not None:
        values["caps"] = {**(campaign.get("caps") or planning.DEFAULT_CAPS), **body.caps}
    for field_name in ("directive_set_id", "composite_profile_id", "dream_job_model_id"):
        value = getattr(body, field_name)
        if value:
            owned_or_404(field_name[:-3], value, seeker.id)
            values[field_name] = value
    if values:
        repo.update_campaign(campaign_id, values)
    return repo.get_campaign(campaign_id, seeker.id) or {}


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign(campaign_id: str, seeker: Seeker) -> None:
    _campaign_or_404(campaign_id, seeker.id)
    repo.delete_campaign(campaign_id, seeker.id)


# ---------------------------------------------------------------------------
# Planning (FR-161..166)
# ---------------------------------------------------------------------------


@router.post("/{campaign_id}/plan")
def generate_plan(
    campaign_id: str,
    seeker: Seeker,
    body: PlanRequest | None = None,
) -> dict:
    """Generate the source plan and show it before execution (FR-162, FR-163)."""
    _campaign_or_404(campaign_id, seeker.id)
    options = body or PlanRequest()
    try:
        return planning.generate_plan(
            campaign_id,
            seeker.id,
            use_llm=options.use_llm,
            assess_knowledge_base=options.assess_knowledge_base,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except NoUsableSources as exc:
        # FR-182/FR-186: zero runnable sources is a blocker with named causes,
        # not an empty plan.  The detail is the same machine-readable list the
        # campaign audit carries.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "reason": "no_usable_sources",
                "message": str(exc),
                "blockers": exc.blockers,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{campaign_id}/plan")
def read_plan(campaign_id: str, seeker: Seeker) -> dict:
    _campaign_or_404(campaign_id, seeker.id)
    return planning.plan_summary(campaign_id, seeker.id)


@router.patch("/{campaign_id}/plan/{plan_item_id}")
def update_plan_item(
    campaign_id: str,
    plan_item_id: str,
    body: PlanItemPatch,
    seeker: Seeker,
) -> dict:
    """Edit or exclude one source plan before launching (FR-163)."""
    _campaign_or_404(campaign_id, seeker.id)
    item = repo.get_plan_item(plan_item_id, campaign_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan item not found")

    values: dict[str, Any] = {}
    if body.native_query is not None:
        values["native_query"] = body.native_query
    if body.rationale is not None:
        values["rationale"] = body.rationale
    if body.caps is not None:
        values["caps"] = body.caps
    if body.estimated_pages is not None:
        entry = repo.get_catalogue_entry(item["adapter_key"]) or {}
        seconds, cost = planning.estimate(entry, body.estimated_pages)
        values.update(
            {
                "estimated_pages": body.estimated_pages,
                "estimated_seconds": seconds,
                "estimated_cost_eur": cost,
            }
        )
    if body.excluded_by_user is not None:
        values["excluded_by_user"] = 1 if body.excluded_by_user else 0
    if values:
        repo.update_plan_item(plan_item_id, values)
        repo.record_audit(
            "campaign.plan_item_edited",
            job_seeker_id=seeker.id,
            actor=seeker.email,
            entity_type="source_plan_item",
            entity_id=plan_item_id,
            detail={"fields": sorted(values)},
        )
    return repo.get_plan_item(plan_item_id, campaign_id) or {}


@router.post("/{campaign_id}/plan/reuse")
def reassess_reuse(campaign_id: str, seeker: Seeker) -> dict:
    """Re-compare the plan with the knowledge base and report the saving (FR-342)."""
    campaign = _campaign_or_404(campaign_id, seeker.id)
    inputs = repo.load_planning_inputs(campaign)
    countries = planning.target_countries(inputs["directives"])
    return knowledge_base.assess_reuse(campaign_id, countries=countries).to_dict()


# ---------------------------------------------------------------------------
# What the run actually did, per plan item (FR-185, FR-361, NFR-403)
# ---------------------------------------------------------------------------
#
# A campaign that reports "538 errors" is not reporting anything.  308 of those
# were ATS boards answering 404 - the measured cost of a registry harvested from
# Common Crawl and Wayback, where a Wayback-only slug is live 27.5% of the time.
# 192 were robots.txt refusals, which is this product declining a source on
# purpose (FR-182, CR-402) and is the opposite of a defect.  Burying the dozen
# real failures among five hundred expected outcomes is how the next real
# failure gets missed.
#
# So a finished plan item is sorted into one of six answers.  Only ``failed``
# means something went wrong, and nothing else may be folded into it - that is
# the whole point, and it is why the dashboard can afford to shout about it.
#
# The counterweight, and it matters: the previous bug here was the opposite one.
# A plan item that fetched nothing, was blocked, or crashed was written down as
# "done, 0 records, 0 errors", and that single lie hid a total retrieval failure
# for hours.  Nothing below may quiet a failure: an answer this module does not
# recognise is counted as ``failed``, never as success.

# The outcome classification itself lives in the pipeline (see
# ``dreamjob.pipeline.outcomes``) because ``collection.status()`` computes the
# ledger over every plan item before it trims the per-source list to a page, and
# the router has to share that one implementation.  Re-exported under the names
# this module used to define so existing importers keep working.
from dreamjob.pipeline.outcomes import (  # noqa: E402, F401
    OUTCOME_STATES,
    PENDING_STATES,
    _shorten,
    collection_outcomes,
)

# ---------------------------------------------------------------------------
# The activity feed (FR-361)
#
# The progress bar says how far a four-hour run has got.  It cannot say that
# Ashby's board is gone, that Jobat refused a bot, or that a source collected
# eight vacancies thirty seconds ago - and those are the things the person
# watching a run is actually watching for.
#
# Everything below is a pure function over rows, for the same reason
# ``collection_outcomes`` is: the wording of a verdict is the part that is
# easiest to get subtly wrong and the part a database fixture is least able to
# check.
# ---------------------------------------------------------------------------

#: What one collected record *is*, keyed on the source it came from.
#: ``records_collected`` mixes entity types - a registry filing and a vacancy
#: are both one record - so the noun is read off the source and never guessed
#: from the count.
_RECORD_NOUN: dict[str, tuple[str, str]] = {
    "ats": ("vacancy", "vacancies"),
    "job_board": ("vacancy", "vacancies"),
    "registry": ("filing", "filings"),
    "directory": ("company", "companies"),
    "website": ("page", "pages"),
    "news": ("article", "articles"),
    "compensation": ("pay benchmark", "pay benchmarks"),
    "events": ("event", "events"),
}
_DEFAULT_NOUN = ("record", "records")

#: ``audit_event.action`` is namespaced; the feed's kinds are not, so the
#: prefix is stripped.  ``campaign.created`` is the one action whose noun is the
#: prefix: stripped bare it is a kind called "created", which says nothing about
#: what was.
_MILESTONE_PREFIX = "campaign."
_MILESTONE_KINDS = {"campaign.created": "campaign_created"}

#: A server that fell over, told from a bot wall and a dead board by its status.
_HTTP_5XX = re.compile(r"HTTP (5\d\d)")

#: The most events one poll may carry.  The panel shows six at a time, so this
#: is a ceiling on the payload rather than a page size anyone reads to the end.
ACTIVITY_MAX_LIMIT = 200


def _event_text(kind: str, row: dict) -> str:
    """One plain clause saying what happened, never a raw exception (FR-361).

    The exception string is what ``last_error`` already holds and what the
    ``detail`` field carries to a tooltip.  A log line six of which are visible
    at once has room for the verdict and not for the stack it came off.
    """
    evidence = f"{row.get('outcome_reason') or ''} {row.get('last_error') or ''}"
    if kind == "started":
        return "started"
    if kind == "succeeded":
        count = int(row.get("records_collected") or 0)
        if not count:
            # It read the source and everything in it was already known.  That
            # is not "0 records": nothing was collected and nothing went wrong.
            return "read, nothing new"
        singular, plural = _RECORD_NOUN.get(str(row.get("source_type") or ""), _DEFAULT_NOUN)
        return f"{count:,} {singular if count == 1 else plural}"
    if kind == "no_matches":
        return "read, nothing to collect"
    if kind == "blocked":
        if "robots.txt" in evidence:
            return "declined, robots.txt says no"
        if "HTTP 403" in evidence:
            return "the site refused a bot (HTTP 403)"
        if "HTTP 451" in evidence:
            return "blocked for legal reasons (HTTP 451)"
        return "declined on principle"
    if kind == "gone":
        if "HTTP 404" in evidence:
            return "the target is gone (HTTP 404)"
        if "HTTP 410" in evidence:
            return "the target is gone (HTTP 410)"
        return "the target is gone"
    if kind == "failed":
        if "RateLimited" in evidence or "rate limit" in evidence.lower():
            return "rate limited, nothing collected"
        if "Timeout" in evidence:
            return "the connection timed out"
        server = _HTTP_5XX.search(evidence)
        if server:
            return f"the server failed (HTTP {server.group(1)})"
        return "failed"
    if kind == "rejected":
        return "the knowledge base refused what it produced"
    if kind == "normalised_nothing":
        return "parsed records, none usable"
    if kind == "extracted_nothing":
        return "fetched a page, read nothing from it"
    if kind == "no_work":
        return "nothing to do"
    if kind == "cancelled":
        return "cancelled, resumable"
    # An end state nobody taught this endpoint about.  It is shown as itself
    # rather than dropped: a feed that quietly omits what it does not recognise
    # is how "done, 0 records, 0 errors" hid a total retrieval failure.
    return kind.replace("_", " ")


def _source_event(row: dict) -> dict:
    """One line about one plan item: when, what, and which target it was.

    The source is named the way the per-source list names it - the catalogue's
    display name without its parenthetical, then the item's own label - so a
    reader moving between the two screens is reading about the same row
    (FR-162, FR-361).
    """
    kind = str(row.get("kind") or "").strip()
    at = str(row.get("at") or "")
    adapter_key = str(row.get("adapter_key") or "")
    label = planning.plan_item_label(adapter_key, row.get("native_query"))[0]
    name = planning.short_source_name(str(row.get("display_name") or adapter_key))
    return {
        "id": f"{row.get('plan_item_id')}:{at}",
        "at": at,
        "kind": kind,
        "source": f"{name} · {label}" if label else name,
        "text": _event_text(kind, row),
        "detail": _shorten(row.get("outcome_reason") or row.get("last_error")),
        "adapter_key": adapter_key or None,
        "plan_item_id": row.get("plan_item_id"),
    }


def _milestone_text(kind: str, detail: dict) -> str:
    """What a campaign-level audit event says, in the numbers it recorded.

    A key the audit row does not carry is left out of the clause rather than
    printed as zero: an older campaign recorded fewer of them, and "Plan ready -
    0 sources" is a false statement about a plan that has 4,843.
    """
    if kind == "campaign_created":
        return "Campaign created"
    if kind == "plan_generated":
        sources = detail.get("sources")
        targets = detail.get("targets")
        bits = []
        if isinstance(sources, int):
            bits.append(f"{sources:,} sources")
        if isinstance(targets, int):
            bits.append(f"over {targets:,} targets")
        return "Plan ready" + (f" — {' '.join(bits)}" if bits else "")
    if kind == "collection_started":
        bits = []
        items = detail.get("plan_items")
        if isinstance(items, int):
            bits.append(f"{items:,} sources")
        max_pages = (detail.get("caps") or {}).get("max_pages")
        if isinstance(max_pages, int):
            bits.append(f"{max_pages:,}-page cap")
        return "Collection started" + (f" — {', '.join(bits)}" if bits else "")
    if kind == "collection_finished":
        bits = []
        records = detail.get("records")
        pages = detail.get("pages")
        if isinstance(records, int):
            bits.append(f"{records:,} records")
        if isinstance(pages, int):
            bits.append(f"from {pages:,} pages")
        return "Collection finished" + (f" — {' '.join(bits)}" if bits else "")
    if kind == "collection_cancelled":
        return "Collection cancelled"
    if kind == "stage_rerun":
        stage = str(detail.get("stage") or "").strip()
        return f"Stage re-run — {stage}" if stage else "Stage re-run"
    return kind.replace("_", " ").capitalize()


def _milestone_event(row: dict) -> dict | None:
    """A campaign-level line: created, planned, started, finished (FR-361).

    Only the campaign's own actions.  ``audit_event`` is the whole product's
    ledger and this endpoint is one campaign's chronology, so an action from
    another namespace is not this feed's to render.
    """
    action = str(row.get("action") or "")
    if not action.startswith(_MILESTONE_PREFIX):
        return None
    kind = _MILESTONE_KINDS.get(action, action[len(_MILESTONE_PREFIX):])
    detail = from_json(row.get("detail"), {}) or {}
    if not isinstance(detail, dict):
        detail = {}
    return {
        "id": str(row.get("id")),
        "at": str(row.get("at") or ""),
        "kind": kind,
        "source": None,
        "text": _milestone_text(kind, detail),
        "detail": None,
        "adapter_key": None,
        "plan_item_id": None,
    }


def _job_events(jobs: list[dict]) -> list[dict]:
    """The collection job's own two moments, which no plan item records.

    A run that started and has settled nothing yet - the first minutes of a
    plan whose first pages are still in flight - would otherwise have an empty
    feed, which reads like a failure rather than like a beginning.
    """
    events: list[dict] = []
    for job in jobs:
        job_id = str(job.get("id"))
        started = job.get("started_at")
        if started:
            events.append(
                {
                    "id": f"{job_id}:started",
                    "at": str(started),
                    "kind": "job_started",
                    "source": None,
                    "text": "Collection job started",
                    "detail": None,
                    "adapter_key": None,
                    "plan_item_id": None,
                }
            )
        finished = job.get("finished_at")
        if finished:
            pages = job.get("progress_done")
            done = f" — {int(pages):,} pages" if isinstance(pages, int) else ""
            events.append(
                {
                    "id": f"{job_id}:finished",
                    "at": str(finished),
                    "kind": "job_finished",
                    "source": None,
                    "text": f"Collection job finished{done}",
                    "detail": None,
                    "adapter_key": None,
                    "plan_item_id": None,
                }
            )
    return events


def _activity_events(sources: list[dict], audits: list[dict], jobs: list[dict]) -> list[dict]:
    """Merge the three chronologies into one, newest first (FR-361).

    Ordering is ``(at, id)`` descending and not ``at`` alone: :func:`utcnow` is
    second-resolution and a wave settles a dozen sources inside one second, so
    without the tiebreak the same poll can return them in a different order
    each time and the client's list reshuffles under the reader.
    """
    events = [_source_event(row) for row in sources if row.get("at")]
    for row in audits:
        milestone = _milestone_event(row)
        if milestone is not None:
            events.append(milestone)
    events.extend(_job_events(jobs))
    events.sort(key=lambda event: (event["at"], event["id"]), reverse=True)
    return events


# ---------------------------------------------------------------------------
# Execution (FR-181..186)
# ---------------------------------------------------------------------------


@router.post("/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: str, seeker: Seeker
) -> dict:
    _campaign_or_404(campaign_id, seeker.id)
    try:
        job_id = await collection.launch(campaign_id, seeker.id)
    except NoUsableSources as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "reason": "no_usable_sources",
                "message": str(exc),
                "blockers": exc.blockers,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"campaign_id": campaign_id, "job_id": job_id, "status": "running"}


@router.get("/{campaign_id}/status")
def campaign_status(
    campaign_id: str,
    seeker: Seeker,
    response: Response,
    sources_limit: Annotated[int | None, Query(ge=0, le=collection.MAX_SOURCES_LIMIT)] = None,
    sources_offset: Annotated[int, Query(ge=0)] = 0,
    since: Annotated[str, Query(max_length=64)] = "",
    if_none_match: Annotated[str | None, Header()] = None,
) -> Any:
    """Live collection status, with what each source actually did (FR-185, FR-361).

    ``outcomes`` is the part the dashboard leads on.  It says nothing the rest
    of the payload does not already contain - it sorts it, so that the dozen
    real failures are not read as one number with five hundred expected
    outcomes in it.

    The plan can hold tens of thousands of items, so ``sources`` is one bounded
    page of them (``sources_limit``/``sources_offset``) and ``source_groups``
    carries the exact per-adapter totals the dashboard's group heads draw.  The
    response is conditional: an ``If-None-Match`` that matches ``rev``, or a
    ``?since=`` equal to it, means nothing the dashboard renders has changed.
    """
    _campaign_or_404(campaign_id, seeker.id)
    payload = collection.status(
        campaign_id,
        seeker.id,
        sources_limit=sources_limit,
        sources_offset=sources_offset,
    )
    rev = str(payload.get("rev") or "")
    etag = f'"{rev}"'
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    if since and since == rev:
        return JSONResponse({"campaign_id": campaign_id, "rev": rev, "unchanged": True})
    response.headers["ETag"] = etag
    # The poll is owner-scoped and changes every few seconds while running, so
    # a shared cache must not serve one seeker's campaign to another.
    response.headers["Cache-Control"] = "no-cache, private"
    return payload


@router.get("/{campaign_id}/sources")
def campaign_sources(
    campaign_id: str,
    seeker: Seeker,
    adapter_key: Annotated[str | None, Query(max_length=120)] = None,
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=collection.MAX_SOURCES_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """The full per-source list, paginated (FR-162, FR-166, FR-361).

    The status poll carries only a bounded page of sources; this is where the
    per-source table reaches the rest, one adapter or one search at a time.  It
    returns the same compact rows the status page does, so a row fetched here
    and a row seen there cannot disagree.
    """
    _campaign_or_404(campaign_id, seeker.id)
    try:
        return collection.source_rows(
            campaign_id,
            seeker.id,
            adapter_key=adapter_key,
            query=q,
            limit=limit,
            offset=offset,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{campaign_id}/activity")
def campaign_activity(
    campaign_id: str,
    seeker: Seeker,
    since: str = Query("", max_length=40),
    limit: int = Query(60, ge=1, le=200),
) -> dict:
    """What this campaign has been doing, newest first (FR-361, FR-101).

    The dashboard's progress bar says how far a four-hour run has got and
    nothing about what it is doing; this is the other half.  Ownership is
    checked before any read, and every row returned is reached through
    ``campaign_id``, so nothing here can cross a tenant even if an id were
    guessed - and the check raises 404 rather than 403, which is how the rest of
    this router avoids confirming that another seeker's campaign exists.

    ``since`` is **inclusive**.  ``utcnow`` writes seconds, and a wave settles a
    dozen sources inside one of them, so an exclusive cursor would drop every
    event in the boundary second.  One or two rows come back twice per poll
    instead, and the client dedupes on ``id``.
    """
    _campaign_or_404(campaign_id, seeker.id)
    # Belt and braces: the query parameter is bounded above, and a direct call
    # from another module is bounded here.  200 lines is already 33 screenfuls
    # of a six-line panel.
    limit = max(1, min(ACTIVITY_MAX_LIMIT, int(limit)))
    since = (since or "").strip()
    # Read the clock *before* the rows, not after.  The cursor is a watermark -
    # "everything at or before this has been delivered" - and a watermark taken
    # after the reads claims more than the reads can support: a source stamped
    # in the second the reads began, but after they ran, is in neither this
    # response nor the next one, because the next asks for ``>= now`` and the
    # row is older than that.  ``activity_at`` is one column per item, so
    # nothing ever restates it and the line is lost for the rest of the run.
    # Taking it first costs only the overlap the inclusive cursor already
    # exists for, and the client dedupes on the event id (FR-361).
    now = utcnow()
    source_rows = repo.list_plan_activity(campaign_id, since, limit)
    audits = repo.list_campaign_audit(campaign_id, since, limit)
    # Only this campaign's collection jobs: ``list_jobs`` is every job the
    # campaign has ever had, and a scoring run is not what "Collection job
    # started" means.
    jobs = [j for j in repo.list_jobs(campaign_id) if j.get("kind") == collection.JOB_KIND]
    events = _activity_events(source_rows, audits, jobs)
    if since:
        # The two SQL reads are already bounded by ``since``; the job rows are
        # not, because a job's two moments have two different timestamps and
        # the row itself has neither.
        events = [event for event in events if event["at"] >= since]
    return {
        "campaign_id": campaign_id,
        "server_time": now,
        "cursor": now,
        # The per-source query filled its page, so there are older events this
        # response does not carry.  Informational: with six lines visible, the
        # newest are the only ones anybody was going to read.
        "truncated": len(source_rows) == limit,
        "events": events[:limit],
    }


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: str, seeker: Seeker) -> dict:
    _campaign_or_404(campaign_id, seeker.id)
    return collection.pause(campaign_id)


@router.post("/{campaign_id}/resume")
async def resume_campaign(
    campaign_id: str, seeker: Seeker
) -> dict:
    _campaign_or_404(campaign_id, seeker.id)
    result = collection.resume(campaign_id)
    if not result["resumed"]:
        # The job is not in this process any more (a restart, NFR-401):
        # start it again from its checkpoint.
        result = {"resumed": True, "job_id": await collection.resume_job(campaign_id, seeker.id)}
    return result


@router.post("/{campaign_id}/cancel")
def cancel_campaign(campaign_id: str, seeker: Seeker) -> dict:
    _campaign_or_404(campaign_id, seeker.id)
    return collection.cancel(campaign_id)


@router.post("/{campaign_id}/stages/{stage}/rerun")
async def rerun_stage(
    campaign_id: str,
    stage: str,
    seeker: Seeker,
    body: RerunRequest | None = None,
) -> dict:
    """Re-run one pipeline stage from persisted artefacts (NFR-603)."""
    _campaign_or_404(campaign_id, seeker.id)
    options = (body or RerunRequest()).options
    try:
        return await collection.rerun_stage(stage, campaign_id, seeker.id, **options)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown stage {stage!r}") from exc
    except TypeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
