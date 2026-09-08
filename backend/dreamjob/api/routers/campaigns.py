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
    _campaign_or_404(campaign_id, seeker.id)
    return collection.status(campaign_id, seeker.id)


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
