"""Enrichment, composite profile and dream job model API (FR-121..FR-128).

Three surfaces, in the order the job seeker meets them:

* ``/settings`` and ``/run`` - the online-enrichment switch (FR-126) and the
  search itself (FR-122), which runs as a resumable job (FR-185).
* ``/findings`` - the review queue.  ``confirmed`` findings are merged without
  asking; everything else waits here with its per-signal evidence until the job
  seeker confirms or rejects it, and a rejection is permanent (FR-124, RK-02).
* ``/composite`` and ``/dream-job`` - the synthesised profile, shown in full
  with a provenance ref per statement and editable (FR-125), and the structured
  dream job model, shown for confirmation before it drives a campaign (FR-128).

Every route is scoped to ``current_seeker`` and every repository call carries
``seeker.id`` (FR-101, FR-344).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.db.repositories import enrichment as repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import composite as composite_pipeline
from dreamjob.pipeline import dreamjob_model as dream_pipeline
from dreamjob.pipeline import enrichment as enrichment_pipeline

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EnrichmentSettingsIn(BaseModel):
    enabled: bool
    detail: str | None = None


class RunEnrichmentIn(BaseModel):
    persona_id: str | None = None
    campaign_id: str | None = None
    max_queries: int = Field(6, ge=1, le=20)
    max_pages: int = Field(12, ge=1, le=50)


class RejectFindingIn(BaseModel):
    permanent: bool = True
    reason: str | None = None


class BuildCompositeIn(BaseModel):
    profile_version_id: str | None = None
    persona_id: str | None = None
    campaign_id: str | None = None
    include_enrichment: bool | None = None
    language: str = Field("en", max_length=8)


class EditCompositeIn(BaseModel):
    narrative: str | None = None
    seniority: dict | None = None
    career_trajectory: list[dict] | None = None
    core_competencies: list[dict] | None = None
    adjacent_competencies: list[dict] | None = None
    domains: list[dict] | None = None
    achievements: list[dict] | None = None
    public_footprint: list[dict] | None = None
    inferred_preferences: list[dict] | None = None
    constraints: list[dict] | None = None
    evidence_refs: dict | None = None


class BuildDreamModelIn(BaseModel):
    statement: str | None = None
    persona_id: str | None = None
    campaign_id: str | None = None
    use_composite: bool = True
    language: str = Field("en", max_length=8)


class EditDreamModelIn(BaseModel):
    statement: str | None = None
    target_roles: list[dict] | None = None
    role_families: list[dict] | None = None
    responsibilities: list[dict] | None = None
    company_characteristics: list[dict] | None = None
    culture_values: list[dict] | None = None
    deal_breakers: list[dict] | None = None
    implicit_preferences: list[dict] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_consent(seeker_id: str, enabled: bool, detail: str | None) -> None:
    """Record the FR-126 decision through the auth slice when it is present."""
    try:
        from dreamjob.security.auth_service import record_consent  # noqa: PLC0415
    except ImportError:
        repo.set_enrichment_enabled(seeker_id, enabled, detail)
        return
    record_consent(seeker_id, "enrichment", enabled, detail)


def _guard(exc: Exception) -> HTTPException:
    if isinstance(exc, composite_pipeline.ConsentRequired):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, composite_pipeline.ProfileMissing):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, dream_pipeline.StatementMissing):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    raise exc


def _composite_view(row: dict) -> dict[str, Any]:
    """The composite plus its statements flattened for the traceability view."""
    view = dict(row)
    view["statements"] = composite_pipeline.statements(row)
    refs = row.get("evidence_refs") or {}
    meta = refs.get("_meta") or {}
    view["unsupported_statements"] = meta.get("unsupported", [])
    # How this version was produced (NFR-104, CR-405).  A composite the
    # synthesis never wrote looks exactly like one it did, so the screen needs
    # to be told which it is holding.  Rows written before this was recorded
    # carry no mode, and are not claimed to be either.
    view["generation"] = meta.get("generation") or {}
    return view


# ---------------------------------------------------------------------------
# FR-126: the online-enrichment switch
# ---------------------------------------------------------------------------


@router.get("/settings")
def get_settings_(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    return {
        "enabled": enrichment_pipeline.enrichment_allowed(seeker.id),
        "excluded_domains": sorted(enrichment_pipeline.EXCLUDED_DOMAINS),
    }


@router.put("/settings")
def put_settings(
    payload: EnrichmentSettingsIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Enable or disable online enrichment entirely (FR-126)."""
    _set_consent(seeker.id, payload.enabled, payload.detail)
    return {"enabled": enrichment_pipeline.enrichment_allowed(seeker.id)}


# ---------------------------------------------------------------------------
# FR-122: run the search
# ---------------------------------------------------------------------------


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def run_enrichment(
    payload: RunEnrichmentIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Start an enrichment run in the background (FR-122, FR-185)."""
    if not enrichment_pipeline.enrichment_allowed(seeker.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Online enrichment is switched off for this job seeker (FR-126).",
        )

    job_id = runner.create(
        "profiling",
        campaign_id=payload.campaign_id,
        job_seeker_id=seeker.id,
        adapter_key="enrichment",
        total=payload.max_pages,
    )

    async def worker(ctx: JobContext) -> None:
        report = await enrichment_pipeline.run_enrichment(
            seeker.id,
            persona_id=payload.persona_id,
            campaign_id=payload.campaign_id,
            max_queries=payload.max_queries,
            max_pages=payload.max_pages,
        )
        ctx.progress(report.pages_fetched, report.urls_considered or payload.max_pages)
        ctx.save_checkpoint(report=report.to_dict())

    await runner.start(job_id, worker)
    return {"job_id": job_id, "status": "running"}


@router.get("/run/{job_id}")
def run_status(job_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    row = runner.status(job_id)
    if row is None or row["job_seeker_id"] != seeker.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return row


# ---------------------------------------------------------------------------
# FR-123, FR-124: the review queue
# ---------------------------------------------------------------------------


@router.get("/findings")
def list_findings(
    status_filter: str | None = Query(None, alias="status"),
    classification: str | None = Query(None),
    include_rejected: bool = Query(True),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> list[dict]:
    return repo.list_findings(
        seeker.id,
        status=status_filter,
        classification=classification,
        include_rejected=include_rejected,
    )


@router.get("/findings/pending")
def pending_findings(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    """What the job seeker still has to rule on (FR-124).

    'Confirmed' findings are merged automatically and are not in this list.
    """
    return [
        f
        for f in repo.list_findings(seeker.id, status="pending", include_rejected=False)
        if f["classification"] != "confirmed"
    ]


@router.get("/findings/{finding_id}")
def get_finding(finding_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    row = repo.get_finding(seeker.id, finding_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")
    return row


@router.post("/findings/{finding_id}/confirm")
def confirm_finding(finding_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Accept a probable/doubtful finding into the profile (FR-124)."""
    row = repo.set_finding_status(seeker.id, finding_id, "accepted")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")
    return row


@router.post("/findings/{finding_id}/reject")
def reject_finding(
    finding_id: str,
    payload: RejectFindingIn | None = None,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Reject a finding.  A permanent rejection is never re-proposed (FR-124)."""
    permanent = payload.permanent if payload else True
    row = repo.set_finding_status(seeker.id, finding_id, "rejected", permanent=permanent)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")
    return row


# ---------------------------------------------------------------------------
# FR-121, FR-125: the composite profile
# ---------------------------------------------------------------------------


@router.get("/composite")
def latest_composite(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    row = repo.latest_composite(seeker.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no composite profile yet")
    return _composite_view(row)


@router.get("/composite/history")
def composite_history(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return repo.list_composites(seeker.id)


@router.get("/composite/{composite_id}")
def get_composite(composite_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    row = repo.get_composite(seeker.id, composite_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "composite profile not found")
    return _composite_view(row)


@router.post("/composite", status_code=status.HTTP_201_CREATED)
def build_composite(
    payload: BuildCompositeIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Synthesise a new composite profile version (FR-121, FR-125, FR-126)."""
    try:
        row = composite_pipeline.build_composite(
            seeker.id,
            profile_version_id=payload.profile_version_id,
            persona_id=payload.persona_id,
            campaign_id=payload.campaign_id,
            include_enrichment=payload.include_enrichment,
            language=payload.language,
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a status code below
        raise _guard(exc) from exc
    return _composite_view(row)


@router.patch("/composite/{composite_id}")
def edit_composite(
    composite_id: str,
    payload: EditCompositeIn,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Apply the job seeker's edits to the composite profile (FR-125)."""
    patch = payload.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to update")
    row = composite_pipeline.edit_composite(seeker.id, composite_id, patch)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "composite profile not found")
    return _composite_view(row)


# ---------------------------------------------------------------------------
# FR-128: the dream job model
# ---------------------------------------------------------------------------


@router.get("/dream-job")
def latest_dream_model(
    confirmed_only: bool = Query(False), seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    row = dream_pipeline.latest(seeker.id, confirmed_only=confirmed_only)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no dream job model yet")
    return row


@router.get("/dream-job/history")
def dream_model_history(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return repo.list_dream_models(seeker.id)


@router.get("/dream-job/{model_id}")
def get_dream_model(model_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    row = repo.get_dream_model(seeker.id, model_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dream job model not found")
    return row


@router.post("/dream-job", status_code=status.HTTP_201_CREATED)
def build_dream_model(
    payload: BuildDreamModelIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Turn the free-text statement into the structured model (FR-109, FR-128)."""
    composite = (
        repo.latest_composite(seeker.id, payload.persona_id) if payload.use_composite else None
    )
    try:
        return dream_pipeline.build_dream_job_model(
            seeker.id,
            statement=payload.statement,
            persona_id=payload.persona_id,
            campaign_id=payload.campaign_id,
            composite=composite,
            language=payload.language,
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a status code below
        raise _guard(exc) from exc


@router.patch("/dream-job/{model_id}")
def edit_dream_model(
    model_id: str, payload: EditDreamModelIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    patch = payload.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to update")
    row = dream_pipeline.edit(seeker.id, model_id, patch)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dream job model not found")
    return row


@router.post("/dream-job/{model_id}/confirm")
def confirm_dream_model(model_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """The job seeker approves the model before it drives a campaign (FR-128)."""
    row = dream_pipeline.confirm(seeker.id, model_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dream job model not found")
    return row
