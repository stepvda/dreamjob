"""Response capture and redirection advice API (extends FR-285, FR-326, FR-425).

Two things the product owner asked for after the specification was baselined:

* responses that arrive outside a pollable mailbox can be entered by hand and
  are then treated exactly like detected ones;
* outcomes are analysed by the *kind of job and company* applied for, and where
  a segment underperforms the system proposes a concrete change of direction.

Advice is never applied on its own.  Accepting a proposal writes a new
directive-set version, which the job seeker can review and revert (NFR-305).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.db.repositories import learning as repo
from dreamjob.postapp import redirection, response_intake
from dreamjob.postapp.segments import analyse_segments
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

router = APIRouter()


# --- Responses --------------------------------------------------------------


class ResponseIn(BaseModel):
    dispatch_id: str | None = None
    opportunity_id: str | None = None
    channel: str = Field("email", description="email|phone|linkedin|portal|in_person|other")
    raw_text: str = ""
    stated_outcome: str | None = Field(
        None, description="interest|info_request|interview|rejection|referral|auto_reply|other"
    )
    received_at: str | None = None
    from_address: str | None = None
    subject: str | None = None
    notes: str = ""
    classify: bool = True


class CorrectionIn(BaseModel):
    stated_outcome: str


@router.get("/responses")
def list_responses(
    limit: int = 200, seeker: CurrentSeeker = Depends(current_seeker)
) -> list[dict]:
    """Every response received, detected or entered by hand."""
    return response_intake.list_responses(seeker.id, limit=limit)


@router.get("/responses/awaiting")
def awaiting(limit: int = 200, seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    """Applications with no response recorded — the picking list for entry."""
    return repo.sent_awaiting_response(seeker.id, limit=limit)


@router.post("/responses", status_code=status.HTTP_201_CREATED)
def create_response(body: ResponseIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Record a response that automatic detection could not see."""
    try:
        result = response_intake.record_response(
            seeker.id,
            dispatch_id=body.dispatch_id,
            opportunity_id=body.opportunity_id,
            channel=body.channel,
            raw_text=body.raw_text,
            stated_outcome=body.stated_outcome,
            received_at=body.received_at,
            from_address=body.from_address,
            subject=body.subject,
            notes=body.notes,
            classify_text=body.classify,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    record_audit(
        "response.recorded",
        entity_type="incoming_reply",
        entity_id=result["incoming_reply_id"],
        seeker_id=seeker.id,
        detail={"channel": body.channel, "stated_outcome": body.stated_outcome},
    )
    return result


@router.patch("/responses/{manual_response_id}")
def correct_response(
    manual_response_id: str,
    body: CorrectionIn,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Correct what a response actually was.

    Matters more than it looks: a misread rejection silently distorts every
    rate it is counted in.
    """
    try:
        return response_intake.correct_outcome(
            seeker.id, manual_response_id, body.stated_outcome
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# --- Patterns ---------------------------------------------------------------


@router.get("/patterns")
def patterns(
    outcome: str = "reply",
    refresh: bool = False,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Outcome rates by kind of job and company, with sample sizes.

    Cached to the last stored run unless ``refresh`` is set — the figures only
    move when a response is recorded.
    """
    if not refresh:
        stored = repo.latest_segment_run(seeker.id, outcome)
        if stored:
            return {
                "outcome": outcome,
                "sample_size": stored["sample_size"],
                "resolved_size": stored["resolved_size"],
                "baseline_rate": stored["baseline_rate"],
                "segments": stored["segments"],
                "caveats": stored["caveats"],
                "computed_at": stored["computed_at"],
                "from_cache": True,
            }
    try:
        return {**analyse_segments(seeker.id, outcome=outcome).as_dict(), "from_cache": False}
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# --- Advice -----------------------------------------------------------------


class DismissIn(BaseModel):
    reason: str = ""


@router.get("/advice")
def list_advice(
    outcome: str | None = None, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    return {
        "open": repo.open_advice(seeker.id, outcome),
        "history": repo.advice_history(seeker.id),
    }


@router.post("/advice/generate")
def generate(outcome: str = "reply", seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Analyse outcomes and propose where to redirect the search."""
    try:
        return redirection.generate_advice(seeker.id, outcome=outcome)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/advice/{advice_id}/apply")
def apply(advice_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Accept a proposal, creating a new directive-set version (FR-148)."""
    try:
        result = redirection.apply_advice(seeker.id, advice_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    record_audit(
        "advice.applied",
        entity_type="redirection_advice",
        entity_id=advice_id,
        seeker_id=seeker.id,
        detail={"directive_set_id": result["directive_set"].get("id")},
    )
    return result


@router.post("/advice/{advice_id}/dismiss")
def dismiss(
    advice_id: str, body: DismissIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    try:
        redirection.dismiss_advice(seeker.id, advice_id, body.reason)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"advice_id": advice_id, "status": "dismissed"}
