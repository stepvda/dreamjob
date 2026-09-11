"""Autopilot API (FR-121..128, FR-161..166, FR-181..186, FR-261..285).

The three-step product needs one endpoint that starts the middle of the
pipeline, one that says whether it can, and one that reports how it went. That
is all this router is; the work lives in ``pipeline.autopilot``.

``GET /preflight`` is deliberately separate from ``POST /start``: the start
screen asks "can this run, and if not why?" before it offers a button, so a
missing document or an unrecorded consent is an explanation with a link, not a
failed request.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.jobs.runner import runner

# Importing the pipeline module registers the autopilot worker with the runner.
from dreamjob.pipeline import autopilot

log = logging.getLogger(__name__)

router = APIRouter()


class AutopilotStartIn(BaseModel):
    """Every knob, all optional — the defaults are the product's opinion."""

    campaign_name: str | None = Field(None, max_length=120)
    directive_name: str | None = Field(None, max_length=120)
    company_limit: int | None = Field(None, ge=0, le=100)
    use_llm: bool | None = None
    max_pages: int | None = Field(None, ge=1, le=5000)
    max_companies: int | None = Field(None, ge=1, le=500)


@router.get("/preflight")
def preflight(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """What autopilot would do for this job seeker, and what blocks it."""
    return autopilot.preflight(seeker.id)


@router.post("/start", status_code=status.HTTP_202_ACCEPTED)
async def start(
    payload: AutopilotStartIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Start the chain: profile → directives → campaign → plan → collect → rank.

    Refuses with 409 and the reason when a blocker remains, rather than
    starting a run that is certain to fail.
    """
    state = autopilot.preflight(seeker.id)
    if not state["ready"]:
        blocker = state["blockers"][0]
        raise HTTPException(status.HTTP_409_CONFLICT, blocker["message"])

    existing = autopilot.latest_for_seeker(seeker.id)
    # Only a run that is actually in flight blocks a new one.  ``pending`` is
    # not in flight: a job left ``pending`` by a restart is an orphan that
    # nothing resumes, and treating it as active would refuse every future run
    # for ever.
    if existing and existing.get("status") in ("running", "paused"):
        return {
            "job_id": existing["id"],
            "already_running": True,
            "status": existing["status"],
        }

    defaults = autopilot.AutopilotOptions()
    options = autopilot.AutopilotOptions(
        campaign_name=payload.campaign_name or defaults.campaign_name,
        directive_name=payload.directive_name or defaults.directive_name,
        company_limit=(
            defaults.company_limit if payload.company_limit is None else payload.company_limit
        ),
        use_llm=defaults.use_llm if payload.use_llm is None else payload.use_llm,
        max_pages=payload.max_pages or defaults.max_pages,
        max_companies=payload.max_companies or defaults.max_companies,
    )
    job_id = await autopilot.start(seeker.id, options=options)
    return {"job_id": job_id, "already_running": False, "options": options.to_dict()}


@router.get("/status")
def status_for_seeker(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """The most recent autopilot run, with its stage report if it has one."""
    row = autopilot.latest_for_seeker(seeker.id)
    if row is None:
        return {"run": None, "preflight": autopilot.preflight(seeker.id)}
    checkpoint = row.get("checkpoint") or {}
    return {
        "run": {
            "job_id": row["id"],
            "status": row["status"],
            "stage": checkpoint.get("stage"),
            "stage_index": checkpoint.get("stage_index"),
            "total_steps": autopilot.TOTAL_STEPS,
            "progress_done": row.get("progress_done"),
            "progress_total": row.get("progress_total"),
            "estimated_seconds": row.get("estimated_seconds"),
            "error_count": row.get("error_count"),
            "last_error": row.get("last_error"),
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "campaign_id": checkpoint.get("campaign_id"),
            "report": checkpoint.get("report") or {},
        }
    }


@router.post("/{job_id}/pause")
def pause(job_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    _owned(job_id, seeker.id)
    return {"paused": runner.pause(job_id)}


@router.post("/{job_id}/resume")
def resume(job_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    _owned(job_id, seeker.id)
    return {"resumed": runner.resume(job_id)}


@router.post("/{job_id}/cancel")
def cancel(job_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    _owned(job_id, seeker.id)
    return {"cancelled": runner.cancel(job_id)}


def _owned(job_id: str, seeker_id: str) -> dict:
    row = runner.status(job_id)
    if row is None or row.get("job_seeker_id") != seeker_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run")
    return row
