"""Application generation and review API (FR-321..FR-331, NFR-206, NFR-502).

The review loop of FR-324 in URL form: generate, preview, edit, regenerate with
instructions, approve, discard - plus bulk approval, which will not proceed
without a summary of what is about to be sent to whom.

Two things this router deliberately does *not* do:

* it never returns a briefing or motivation PDF to anything but the job
  seeker's own download route, because FR-321 makes those two documents job
  seeker material.  ``package.document_path`` is what enforces that;
* it never approves a package whose factual-consistency check has not passed
  (FR-322) or whose leakage scan failed (NFR-206).  A consistency failure can
  be overridden by the job seeker with a recorded reason - a human in the loop,
  NFR-305 - and a leak failure cannot be overridden at all.

Generating for more than one opportunity runs as a resumable job with progress
and a cancel control (FR-185, NFR-502); a single one runs inline, because
waiting for a job to appear in a list to see one CV is worse than waiting.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.config import get_settings
from dreamjob.db.repositories import applications as repo
from dreamjob.documents import briefing as briefing_module
from dreamjob.documents import motivation as motivation_module
from dreamjob.documents import package as package_module
from dreamjob.documents.cv_generator import templates as cv_templates
from dreamjob.documents.package import (
    GenerationError,
    GenerationOptions,
    NeverSent,
    NotApprovable,
)
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.security.audit import record_audit

router = APIRouter()

MEDIA_TYPES = {
    "cv_pdf": "application/pdf",
    "briefing": "application/pdf",
    "motivation": "application/pdf",
    "cv_docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class GenerateIn(BaseModel):
    opportunity_ids: list[str] = Field(default_factory=list)
    campaign_id: str | None = None
    selected_only: bool = True
    language: str | None = Field(None, max_length=8)
    cv_template: str | None = None
    briefing_template: str | None = None
    motivation_template: str | None = None
    contact_id: str | None = None
    parts: list[str] | None = None
    use_llm: bool = True
    #: True (the default) returns a job immediately; False waits for the
    #: package and returns it, which is what a script or a test wants and what
    #: a browser must not, because the work is minutes of model calls.
    background: bool = True


class RegenerateIn(BaseModel):
    instructions: str | None = Field(None, max_length=4000)
    parts: list[str] | None = None
    language: str | None = Field(None, max_length=8)
    cv_template: str | None = None
    briefing_template: str | None = None
    motivation_template: str | None = None
    contact_id: str | None = None
    use_llm: bool = True


class EditIn(BaseModel):
    email_subject: str | None = Field(None, max_length=300)
    email_body: str | None = None
    contact_id: str | None = None
    language: str | None = Field(None, max_length=8)


class TemplateIn(BaseModel):
    template: str


class ApproveIn(BaseModel):
    package_ids: list[str] = Field(default_factory=list)
    summary: str | None = Field(None, max_length=4000)
    override_reason: str | None = Field(None, max_length=2000)


class DiscardIn(BaseModel):
    reason: str | None = Field(None, max_length=1000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _options(payload: GenerateIn | RegenerateIn) -> GenerationOptions:
    parts = tuple(payload.parts) if payload.parts else package_module.PARTS
    unknown = set(parts) - set(package_module.PARTS)
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown parts: {', '.join(sorted(unknown))}"
        )
    return GenerationOptions(
        language=payload.language,
        cv_template=payload.cv_template,
        briefing_template=payload.briefing_template,
        motivation_template=payload.motivation_template,
        contact_id=payload.contact_id,
        instructions=getattr(payload, "instructions", None),
        parts=parts,
        use_llm=payload.use_llm,
    )


def _owned(package_id: str, seeker_id: str) -> dict[str, Any]:
    package = repo.get_package(package_id, seeker_id)
    if package is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application package not found")
    return package


# ---------------------------------------------------------------------------
# Templates (FR-322, FR-331)
# ---------------------------------------------------------------------------


@router.get("/templates")
def list_templates(seeker: CurrentSeeker = Depends(current_seeker)) -> dict[str, Any]:
    return {
        "cv": cv_templates(),
        "briefing": sorted(briefing_module.TEMPLATES),
        "motivation": sorted(motivation_module.TEMPLATES),
        "parts": list(package_module.PARTS),
        "never_sent": ["briefing", "motivation"],
    }


# ---------------------------------------------------------------------------
# Generation (FR-321)
# ---------------------------------------------------------------------------


@router.post("/generate")
async def generate(
    payload: GenerateIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    ids = list(payload.opportunity_ids)
    if not ids and payload.selected_only:
        ids = repo.selected_opportunity_ids(seeker.id, payload.campaign_id)
    if not ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Select at least one opportunity, or pass opportunity_ids (FR-321)",
        )
    options = _options(payload)

    # Generating a package is four model calls - CV, briefing, motivation,
    # email - plus a consistency pass and the PDF renders, so even a single
    # opportunity is around two and a half minutes of work.  By default that is
    # a job, because holding the HTTP request open for the whole of it showed
    # the browser a spinner with no progress and, behind a proxy or a browser
    # timeout, gave up and looked stuck.  ``background=False`` keeps the
    # synchronous form for callers that genuinely want the package back.
    if not payload.background:
        try:
            package = await asyncio.to_thread(
                package_module.generate, seeker.id, ids[0], options, actor=seeker.email
            )
        except GenerationError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"mode": "inline", "package": package_module.preview(package)}

    job_id = runner.create(
        "generation",
        campaign_id=payload.campaign_id,
        job_seeker_id=seeker.id,
        total=len(ids),
        estimated_seconds=len(ids) * 45,
    )

    async def worker(ctx: JobContext) -> Any:
        done = int((ctx.checkpoint or {}).get("done") or 0)
        for index, opportunity_id in enumerate(ids):
            if index < done:
                continue
            try:
                await asyncio.to_thread(
                    package_module.generate, seeker.id, opportunity_id, options,
                    actor=seeker.email,
                )
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
                ctx.record_error(f"{opportunity_id}: {exc}")
            ctx.progress(index + 1, len(ids))
            ctx.save_checkpoint(done=index + 1)
            yield opportunity_id

    await runner.start(job_id, worker)
    return {"mode": "job", "job_id": job_id, "count": len(ids)}


@router.post("/{package_id}/regenerate")
async def regenerate(
    package_id: str, payload: RegenerateIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """FR-324: regenerate with instructions, keeping the same package."""
    package = _owned(package_id, seeker.id)
    try:
        updated = await asyncio.to_thread(
            package_module.generate,
            seeker.id,
            package["opportunity_id"],
            _options(payload),
            package_id=package_id,
            actor=seeker.email,
        )
    except GenerationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return package_module.preview(updated)


@router.post("/{package_id}/briefing/refresh")
async def refresh_briefing(
    package_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """FR-329: regenerate the briefing on demand, just before an interview."""
    package = _owned(package_id, seeker.id)
    try:
        updated = await asyncio.to_thread(
            package_module.generate,
            seeker.id,
            package["opportunity_id"],
            GenerationOptions(parts=("briefing",), language=package.get("language")),
            package_id=package_id,
            actor=seeker.email,
        )
    except GenerationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return package_module.preview(updated)


@router.post("/{package_id}/cv-template")
def set_cv_template(
    package_id: str, payload: TemplateIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """Re-render the same facts in another template - no model call (FR-322)."""
    _owned(package_id, seeker.id)
    updated = package_module.regenerate_cv_only(seeker.id, package_id, template=payload.template)
    if updated is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This package has no generated CV to re-render"
        )
    return package_module.preview(updated)


# ---------------------------------------------------------------------------
# Review (FR-324)
# ---------------------------------------------------------------------------


@router.get("/")
def list_packages(
    campaign_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    opportunity_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict[str, Any]:
    rows = repo.list_packages(
        seeker.id,
        campaign_id=campaign_id,
        status=status_filter,
        opportunity_id=opportunity_id,
        limit=limit,
        offset=offset,
    )
    return {
        "counts": repo.count_packages(seeker.id, campaign_id=campaign_id),
        "packages": [package_module.preview(row) for row in rows],
    }


@router.get("/by-opportunity/{opportunity_id}")
def package_for_opportunity(
    opportunity_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """FR-331: the opportunity view resolves its package without knowing the id."""
    package = repo.latest_for_opportunity(seeker.id, opportunity_id)
    if package is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Nothing has been generated for this opportunity yet"
        )
    return package_module.preview(package)


@router.get("/{package_id}")
def get_package(package_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict[str, Any]:
    return package_module.preview(_owned(package_id, seeker.id))


@router.patch("/{package_id}")
def edit_package(
    package_id: str, payload: EditIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    _owned(package_id, seeker.id)
    changes = payload.model_dump(exclude_none=True)
    try:
        updated = package_module.edit(seeker.id, package_id, changes)
    except GenerationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application package not found")
    return package_module.preview(updated)


@router.post("/{package_id}/consistency")
def recheck(package_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict[str, Any]:
    """Re-run the FR-322 check and the NFR-206 scan over the current text.

    The judge runs here too.  It did not before: the router called
    ``run_consistency`` without a client, so the re-check silently reported the
    deterministic half alone and could turn a fully-failed package green - the
    screen then showed "pass" for a check that had skipped half its work
    (E2E_1500, section 8.6).  A missing key or an exhausted budget still
    degrades gracefully, and the stored report says ``judge_ran`` so the screen
    can say which check it actually ran.
    """
    from dreamjob.llm.client import LLMClient  # noqa: PLC0415

    package = _owned(package_id, seeker.id)
    settings = get_settings()
    llm = None
    if settings.deepseek_api_key or settings.local_llm_base_url:
        llm = LLMClient(campaign_id=package.get("campaign_id"), job_seeker_id=seeker.id)
    updated = package_module.run_consistency(seeker.id, package, llm=llm)
    return package_module.preview(updated)


@router.post("/{package_id}/approve")
def approve_one(
    package_id: str, payload: ApproveIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    _owned(package_id, seeker.id)
    try:
        result = package_module.approve(
            seeker.id,
            [package_id],
            actor=seeker.email,
            summary=payload.summary,
            override_reason=payload.override_reason,
        )
    except NotApprovable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if result["refused"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": "The package did not pass the checks required before dispatch",
                "refused": result["refused"],
            },
        )
    return package_module.preview(_owned(package_id, seeker.id))


@router.post("/approval-summary")
def approval_summary(
    payload: ApproveIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """FR-324: what a bulk approval would send, and to whom, before approving."""
    if not payload.package_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "package_ids is required")
    return package_module.bulk_summary(seeker.id, payload.package_ids)


@router.post("/approve")
def approve_many(
    payload: ApproveIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """Bulk approval.  The summary is mandatory and is written to the audit log."""
    if not payload.package_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "package_ids is required")
    try:
        return package_module.approve(
            seeker.id,
            payload.package_ids,
            actor=seeker.email,
            summary=payload.summary,
            override_reason=payload.override_reason,
        )
    except NotApprovable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/{package_id}/discard")
def discard_package(
    package_id: str, payload: DiscardIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    _owned(package_id, seeker.id)
    try:
        package_module.discard(seeker.id, package_id, actor=seeker.email, reason=payload.reason)
    except GenerationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"package_id": package_id, "status": "discarded"}


@router.delete("/{package_id}")
def delete_package(
    package_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """Hard-delete a package that was never sent (FR-321, FR-331).

    A sent package is part of the dispatch record and is kept; the existing
    discard route is how it is retired.  A draft or discarded package is the
    seeker's working material, so it and its generated files go together.
    """
    package = _owned(package_id, seeker.id)
    if package["status"] == "sent":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a sent package is kept as a dispatch record; discard it instead",
        )
    if not repo.delete_package(package_id, seeker.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a sent package is kept as a dispatch record; discard it instead",
        )
    # The row is gone first, so these paths can no longer be reached through
    # the API; a file that cannot be unlinked is logged, not resurrected.
    removed = package_module.remove_artifacts(
        package.get(column) for column in package_module.ARTIFACT_PATH_COLUMNS
    )
    record_audit(
        "application_package.deleted",
        entity_type="application_package",
        entity_id=package_id,
        seeker_id=seeker.id,
        detail={
            "status": package.get("status"),
            "opportunity_id": package.get("opportunity_id"),
            "files_removed": removed,
        },
    )
    return {"package_id": package_id, "deleted": True, "files_removed": removed}


# ---------------------------------------------------------------------------
# Downloads (FR-331: stored with the package, downloadable from the opportunity)
# ---------------------------------------------------------------------------


@router.get("/{package_id}/documents/{kind}")
def download(
    package_id: str, kind: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> FileResponse:
    package = _owned(package_id, seeker.id)
    try:
        path = package_module.document_path(package, kind)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown document {kind!r}") from exc
    except NeverSent as exc:  # pragma: no cover - only reachable via dispatch
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    company = (package.get("company_name") or "application").replace("/", "-")
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(kind, "application/octet-stream"),
        filename=f"{company} - {kind}{path.suffix}",
        # Inline, so the Apply screen can show the document in the page rather
        # than only offering it as a download.  ``filename`` alone makes
        # Starlette send ``Content-Disposition: attachment``, which forces a
        # download and leaves nothing for an <iframe> to render.  The download
        # buttons are unaffected: they fetch the blob and save it themselves.
        content_disposition_type="inline",
    )
