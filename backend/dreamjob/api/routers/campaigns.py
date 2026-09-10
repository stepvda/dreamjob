"""Campaign API: plan, review, launch, monitor, re-run (FR-161..166, FR-181..186).

The route order follows the way a campaign is actually run: create it, generate
a plan, read the plan and the knowledge-base saving, edit or exclude individual
sources, launch, watch, pause, and re-run a single stage when something needs
redoing (NFR-603).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker, owned_or_404
from dreamjob.config import get_settings
from dreamjob.db.repositories import campaigns as repo
from dreamjob.pipeline import collection, knowledge_base, planning

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

#: The six answers, in the order the dashboard reads them.
OUTCOME_STATES = ("succeeded", "blocked", "gone", "failed", "skipped", "capped")

#: ... plus the two that mean "no answer yet".
PENDING_STATES = ("running", "pending")

#: How many individual failures the payload names before it starts counting
#: instead.  ``failed`` is the list an operator reads line by line, so it gets
#: room; the other states are grouped per adapter and need none.
FAILED_DETAIL_LIMIT = 60

#: Distinct reasons carried per grouped adapter.  Generous rather than
#: illustrative for ``blocked``: "we declined 192 sources on principle" is only
#: defensible if the principle can be read source by source, and each reason
#: names the URL that was refused.  Groups that are merely informative get the
#: short list.
GROUP_REASON_LIMIT = 25
SHORT_REASON_LIMIT = 3

#: Attempts an adapter needs before "every slug came back 404" reads as adapter
#: breakage rather than as registry decay (NFR-403).  Below this the sample is
#: too small to tell the two apart, and saying so is better than guessing.
BREAKAGE_MIN_ATTEMPTS = 5

#: The measured outcome of a source, as the collection worker writes it, mapped
#: onto the answer the operator is shown.  Unknown states are deliberately not
#: defaulted here: see ``_bucket_of``.
_STATE_BUCKET: dict[str, str] = {
    "succeeded": "succeeded",
    # Declined, correctly: robots.txt (FR-182), a 403 bot wall, or terms of
    # service (IR-101).  A decision the product should be able to defend, and
    # therefore a thing to show rather than a thing to count as breakage.
    "blocked": "blocked",
    "refused": "blocked",
    "robots_disallowed": "blocked",
    "tos_prohibited": "blocked",
    # The target is not there any more - 404 or 410 on a board the registry
    # believed was live.  The registry learns this once and stops offering it.
    "gone": "gone",
    "not_found": "gone",
    "retired": "gone",
    # The page budget stopped it (FR-186): a budget signal, not an outcome.
    "capped": "capped",
    # Nothing to do.  ``no_matches`` belongs here rather than under
    # ``succeeded``: the source answered and said it holds nothing for this
    # query, which is a completed unit of work but not a collected record.
    "no_work": "skipped",
    "skipped": "skipped",
    "no_matches": "skipped",
    # Something actually went wrong.  ``extracted_nothing`` is in this list on
    # purpose: a source that was fetched and yielded no record is an adapter
    # that has stopped matching the page it reads (NFR-403).
    "failed": "failed",
    "rejected": "failed",
    "normalised_nothing": "failed",
    "extracted_nothing": "failed",
}

#: The fall-back when a plan item carries no measured outcome, keyed on the
#: status column.  ``done`` is absent because it alone needs the record count to
#: separate a source that did its job from one that had nothing to do.
_STATUS_BUCKET: dict[str, str] = {
    "failed": "failed",
    "blocked": "blocked",
    "gone": "gone",
    "capped": "capped",
    "skipped": "skipped",
}

#: How the collection worker says "the page budget stopped this one" today: the
#: item stays ``planned`` so that raising the cap continues it from its
#: checkpoint, and the reason is written into ``last_error`` citing FR-186.
#: Reading that marker is what tells 4,664 items the budget held back from the
#: 45,000 that simply have not been reached yet.  A ``capped`` state or status
#: supersedes it the moment one arrives - both are already mapped above.
_CAP_MARKERS = ("not started: ", "stopped by cap: ")


def _capped_by_budget(source: dict) -> bool:
    """FR-186: was this item held back by a cap rather than never planned?"""
    reason = source.get("last_error") or ""
    return "FR-186" in reason and reason.startswith(_CAP_MARKERS)


def _bucket_of(source: dict) -> str:
    """Which of the six answers this plan item ended on (FR-185)."""
    status = source.get("status")
    if status in ("planned", "running", None):
        # Not finished, so not an outcome: waiting, running, excluded by the job
        # seeker (FR-163), or held back by the page budget (FR-186).
        if source.get("excluded_by_user"):
            return "skipped"
        if status == "running":
            return "running"
        return "capped" if _capped_by_budget(source) else "pending"
    # ``outcome_state`` (the column migration 130 added and the collection
    # worker maintains) before ``outcome`` (the older copy inside ``caps``).
    # For an item settled since that migration the two agree.  For the 504
    # items it *relabelled* from the evidence already in ``last_error`` - 293
    # robots refusals and 211 dead boards in one campaign - only the column was
    # rewritten, and the ``caps`` blob still carries the word the pre-fix code
    # wrote there: ``failed``.  Reading ``caps`` first put every one of them
    # back into the failure count, which is the number this whole screen exists
    # to keep honest, so the migrated column wins.
    state = source.get("outcome_state") or source.get("outcome")
    if state in _STATE_BUCKET:
        return _STATE_BUCKET[state]
    if status in _STATUS_BUCKET:
        return _STATUS_BUCKET[status]
    if status == "done":
        return "succeeded" if source.get("records_collected") else "skipped"
    # An answer nobody taught this endpoint about.  It is not quietly a success:
    # writing an unrecognised end state down as "done" is the bug this whole
    # module exists to keep from coming back.
    return "failed"


def _shorten(text: str | None, limit: int = 180) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _group(
    rows: list[dict], catalogue: dict[str, dict], reason_limit: int = SHORT_REASON_LIMIT
) -> list[dict]:
    """Collapse per-item outcomes into one row per adapter, with the reasons.

    Nobody reads 308 dead slugs one at a time, so the unit shown is the adapter.
    The reasons are carried verbatim underneath it - each one names the URL that
    was refused or that answered 404 - and the count of the ones that did not
    fit is carried too, so the list never pretends to be complete when it is not.
    """
    grouped: dict[str, dict] = {}
    seen: dict[str, set[str]] = {}
    for source in rows:
        key = source["adapter_key"]
        entry = grouped.get(key)
        if entry is None:
            catalogue_row = catalogue.get(key) or {}
            seen[key] = set()
            entry = grouped[key] = {
                "adapter_key": key,
                "display_name": source.get("display_name") or key,
                "count": 0,
                "reasons": [],
                "reasons_omitted": 0,
                # IR-101: whether this source is one an administrator has had to
                # acknowledge belongs beside the refusal, because it is half the
                # answer to "why did you not collect this?".
                "tos_status": catalogue_row.get("tos_status"),
                "requires_ack": bool(catalogue_row.get("requires_ack")),
                "acknowledged_at": catalogue_row.get("acknowledged_at"),
            }
        entry["count"] += 1
        reason = _shorten(source.get("last_error"))
        if reason and reason not in seen[key]:
            seen[key].add(reason)
            if len(entry["reasons"]) < reason_limit:
                entry["reasons"].append(reason)
            else:
                entry["reasons_omitted"] += 1
    return sorted(grouped.values(), key=lambda row: (-row["count"], row["adapter_key"]))


def collection_outcomes(sources: list[dict], catalogue: dict[str, dict]) -> dict:
    """Sort the plan items into the six answers, with the detail behind each.

    ``failed`` is listed item by item because that is the list somebody works
    through.  ``blocked`` and ``gone`` are grouped per adapter because nobody
    reads 308 dead slugs one at a time - but the group carries examples, and for
    ``gone`` it carries the share, which is what separates a registry decaying
    at its measured rate from an adapter that has broken (NFR-403).
    """
    buckets: dict[str, list[dict]] = {name: [] for name in OUTCOME_STATES}
    counts = dict.fromkeys(OUTCOME_STATES + PENDING_STATES, 0)
    attempts: dict[str, int] = {}
    gone_by_adapter: dict[str, int] = {}
    records = 0
    producing = 0
    errors = 0

    for source in sources:
        bucket = _bucket_of(source)
        counts[bucket] += 1
        collected = source.get("records_collected") or 0
        records += collected
        errors += source.get("error_count") or 0
        if collected:
            producing += 1
        if bucket in buckets:
            buckets[bucket].append(source)
        if bucket in ("succeeded", "blocked", "gone", "failed"):
            # Items the run actually put a request behind.  A capped or pending
            # item never asked, so counting it would dilute the share below.
            key = source["adapter_key"]
            attempts[key] = attempts.get(key, 0) + 1
            if bucket == "gone":
                gone_by_adapter[key] = gone_by_adapter.get(key, 0) + 1

    failed = [
        {
            "plan_item_id": source.get("plan_item_id"),
            "adapter_key": source["adapter_key"],
            "display_name": source.get("display_name") or source["adapter_key"],
            "state": source.get("outcome_state") or source.get("outcome"),
            "reason": _shorten(source.get("outcome_reason") or source.get("last_error")),
            "error_count": source.get("error_count") or 0,
            "extraction_success_rate": source.get("extraction_success_rate"),
        }
        for source in sorted(
            buckets["failed"], key=lambda s: (-(s.get("error_count") or 0), s["adapter_key"])
        )[:FAILED_DETAIL_LIMIT]
    ]

    gone = _group(buckets["gone"], catalogue, GROUP_REASON_LIMIT)
    for row in gone:
        attempted = attempts.get(row["adapter_key"], row["count"])
        row["attempted"] = attempted
        row["share"] = row["count"] / attempted if attempted else None
        # NFR-403: a registry harvested from Wayback is 27.5% live, so dead
        # slugs are expected.  An adapter where *every* slug of a decent sample
        # is dead is not decay - the adapter or its URL shape has broken, and
        # that is a different problem with a different fix.
        row["suspected_breakage"] = (
            attempted >= BREAKAGE_MIN_ATTEMPTS and row["count"] == attempted
        )

    return {
        "counts": counts,
        "records": records,
        "producing_sources": producing,
        # The number the dashboard used to headline, kept because a per-page
        # retry that eventually succeeded still costs something worth seeing.
        "error_count": errors,
        "failed": failed,
        "failed_listed": len(failed),
        # FR-182, CR-402, IR-101: the refusals are the one list an operator
        # may have to defend line by line, so they are the least abridged.
        "blocked": _group(buckets["blocked"], catalogue, GROUP_REASON_LIMIT),
        "gone": gone,
        "capped": _group(buckets["capped"], catalogue),
        "skipped": _group(buckets["skipped"], catalogue),
    }


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
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"campaign_id": campaign_id, "job_id": job_id, "status": "running"}


@router.get("/{campaign_id}/status")
def campaign_status(campaign_id: str, seeker: Seeker) -> dict:
    """Live collection status, with what each source actually did (FR-185, FR-361).

    ``outcomes`` is the part the dashboard leads on.  It says nothing the rest
    of the payload does not already contain - it sorts it, so that the dozen
    real failures are not read as one number with five hundred expected
    outcomes in it.
    """
    _campaign_or_404(campaign_id, seeker.id)
    payload = collection.status(campaign_id, seeker.id)
    catalogue = {entry["adapter_key"]: entry for entry in repo.list_catalogue(enabled_only=False)}
    payload["outcomes"] = collection_outcomes(payload.get("sources") or [], catalogue)
    return payload


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
