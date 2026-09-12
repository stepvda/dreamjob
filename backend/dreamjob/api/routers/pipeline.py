"""Post-application API: board, replies, scheduling, rehearsal, negotiation.

Covers FR-421 (the pipeline board and its stage transitions), FR-422 (reply
classification and the drafted answer), FR-423 (interview scheduling against
the connected calendar), FR-424 (the mock interview), FR-425 (outcomes and
what they teach) and FR-444 (the salary negotiation brief).

Everything here is private data, so every route depends on ``current_seeker``
and passes ``seeker.id`` into the repository (FR-101, FR-344).

Two rules hold across the whole router and are worth stating once.  **Nothing
sends** - drafts are produced and approved here, and the mail slice sends what
was approved (NFR-305).  And **nothing decides** - a card closes because the
job seeker closed it, an outcome is recorded because they recorded it, and the
learned effects of FR-425 change a default only when they ask for them to.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.config import get_settings
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.llm.client import LLMClient
from dreamjob.postapp import board as board_mod
from dreamjob.postapp import calendar_sync, mock_interview, negotiation, outcomes
from dreamjob.postapp import reply_classifier as replies_mod
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

router = APIRouter()

Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]


def _llm(seeker: CurrentSeeker, use_llm: bool = True) -> LLMClient | None:
    """An LLM client, or ``None`` when the model is off or not configured.

    Returning ``None`` rather than raising is what makes every generator's
    fallback path reachable from the API (NFR-104).
    """
    if not use_llm or not get_settings().deepseek_api_key:
        return None
    return LLMClient(job_seeker_id=seeker.id)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TransitionIn(BaseModel):
    stage: str
    outcome: str | None = None
    note: str | None = Field(default=None, max_length=4000)
    next_action: str | None = Field(default=None, max_length=500)
    next_action_due: str | None = None


class CardUpdateIn(BaseModel):
    next_action: str | None = Field(default=None, max_length=500)
    next_action_due: str | None = None
    notes: str | None = Field(default=None, max_length=20000)
    append_note: str | None = Field(default=None, max_length=4000)


class OutcomeIn(BaseModel):
    outcome: str
    note: str | None = Field(default=None, max_length=4000)
    detail: dict[str, Any] | None = None


class ProcessReplyIn(BaseModel):
    use_llm: bool = True
    draft: bool = True
    move_card: bool = True


class DraftEditIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)
    subject: str | None = Field(default=None, max_length=300)


class DraftApproveIn(BaseModel):
    """Approving a draft unchanged is the normal case, so both fields are optional."""

    body: str | None = Field(default=None, min_length=1, max_length=20000)
    subject: str | None = Field(default=None, max_length=300)


class ScheduleIn(BaseModel):
    use_llm: bool = True
    duration_minutes: int = Field(default=calendar_sync.DEFAULT_DURATION_MINUTES, ge=15, le=480)


class ConfirmIn(BaseModel):
    start: str | None = None
    end: str | None = None
    location: str | None = Field(default=None, max_length=300)
    create_entry: bool = True


class MockStartIn(BaseModel):
    opportunity_id: str
    language: str | None = Field(default=None, max_length=5)
    focus: str = "mixed"
    questions: int = Field(
        default=mock_interview.DEFAULT_QUESTION_COUNT,
        ge=mock_interview.MIN_QUESTIONS,
        le=mock_interview.MAX_QUESTIONS,
    )
    use_llm: bool = True


class MockAnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=20000)
    use_llm: bool = True


class NegotiationIn(BaseModel):
    language: str | None = Field(default=None, max_length=5)
    use_llm: bool = True
    force: bool = False


class CalendarConnectIn(BaseModel):
    provider: str = "google"
    redirect_uri: str | None = None
    include_drive: bool = False


# ---------------------------------------------------------------------------
# FR-421: the board
# ---------------------------------------------------------------------------


@router.get("/board")
def get_board(
    seeker: Seeker,
    campaign_id: str | None = None,
    include_closed: bool = True,
) -> dict:
    """The board: one column per stage, cards ordered by what is due first."""
    return board_mod.board(seeker.id, campaign_id=campaign_id, include_closed=include_closed)


@router.get("/summary")
def get_summary(seeker: Seeker) -> dict:
    return board_mod.summary(seeker.id)


@router.post("/sync", status_code=status.HTTP_200_OK)
def sync_board(seeker: Seeker) -> dict:
    """Open a card for every sent application the board does not know about."""
    return board_mod.sync_sent_applications(seeker.id)


@router.get("/cards")
def list_cards(
    seeker: Seeker,
    stage: str | None = None,
    campaign_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
) -> list[dict]:
    return repo.list_cards(seeker.id, stage=stage, campaign_id=campaign_id, limit=limit)


@router.get("/cards/due")
def cards_due(seeker: Seeker, within_days: int = Query(0, ge=0, le=90)) -> list[dict]:
    return board_mod.due(seeker.id, within_days=within_days)


@router.get("/cards/silent")
def cards_silent(
    seeker: Seeker, days: int = Query(board_mod.SILENCE_DAYS, ge=1, le=365)
) -> list[dict]:
    """Applications that were never answered.  Reported only - closing is a decision."""
    return board_mod.silent(seeker.id, days=days)


@router.get("/cards/{card_id}")
def get_card(card_id: str, seeker: Seeker) -> dict:
    try:
        return board_mod.card_detail(seeker.id, card_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.patch("/cards/{card_id}")
def update_card(card_id: str, payload: CardUpdateIn, seeker: Seeker) -> dict:
    try:
        return board_mod.update_card(seeker.id, card_id, **payload.model_dump(exclude_unset=True))
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete("/cards/{card_id}")
def delete_card(card_id: str, seeker: Seeker) -> dict:
    """Remove a card from the board (FR-421).

    The card is the seeker's own; its stage events cascade with it.  A card
    that is not theirs is a 404, like every other read and write here.
    """
    if not repo.delete_card(card_id, seeker.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pipeline card not found")
    record_audit(
        "pipeline_card.deleted",
        entity_type="pipeline_card",
        entity_id=card_id,
        seeker_id=seeker.id,
    )
    return {"card_id": card_id, "deleted": True}


@router.post("/cards/{card_id}/stage")
def move_card(card_id: str, payload: TransitionIn, seeker: Seeker) -> dict:
    """Move a card by hand (FR-421).  Manual moves may go in any direction."""
    try:
        return board_mod.transition(
            seeker.id,
            card_id,
            payload.stage,
            trigger="user",
            outcome=payload.outcome,
            note=payload.note,
            next_action=payload.next_action,
            next_action_due=payload.next_action_due,
        ).as_dict()
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except board_mod.StageError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.post("/cards/{card_id}/outcome")
def record_outcome(card_id: str, payload: OutcomeIn, seeker: Seeker) -> dict:
    """Record the outcome of one application (FR-421, FR-425).

    Both vocabularies are accepted, because they answer the same question from
    two sides: FR-425 measures what an application *reached* (reply, interview,
    offer, rejection, no response) and FR-421 closes a card with how it *ended*
    (accepted, rejected, withdrawn, no response).  A seeker who was offered the
    job and took it has to be able to say so here.
    """
    allowed = (*outcomes.OUTCOMES, *board_mod.OUTCOMES)
    if payload.outcome not in allowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"outcome must be one of {', '.join(dict.fromkeys(allowed))}",
        )
    try:
        return outcomes.record_outcome(
            seeker.id, card_id, payload.outcome, note=payload.note, detail=payload.detail
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except board_mod.StageError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/cards/{card_id}/events")
def card_events(card_id: str, seeker: Seeker) -> list[dict]:
    """Who or what moved this card, and when (FR-421)."""
    if repo.get_card(card_id, seeker.id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pipeline card not found")
    return repo.card_events(card_id, seeker.id)


# ---------------------------------------------------------------------------
# FR-422: replies and drafts
# ---------------------------------------------------------------------------


@router.get("/replies")
def list_replies(
    seeker: Seeker,
    handled: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict]:
    return repo.list_replies(seeker.id, handled=handled, limit=limit)


@router.get("/replies/{reply_id}")
def get_reply(reply_id: str, seeker: Seeker) -> dict:
    reply = repo.get_reply(reply_id, seeker.id)
    if reply is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reply not found")
    return {"reply": reply, "drafts": repo.drafts_for_reply(reply_id, seeker.id)}


@router.post("/replies/{reply_id}/process")
def process_reply(reply_id: str, payload: ProcessReplyIn, seeker: Seeker) -> dict:
    """Classify a reply, move its card and draft the answer (FR-421, FR-422)."""
    try:
        return replies_mod.process_reply(
            seeker.id,
            reply_id,
            llm=_llm(seeker, payload.use_llm),
            draft=payload.draft,
            move_card=payload.move_card,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/drafts")
def list_drafts(
    seeker: Seeker, status_filter: str | None = Query(None, alias="status")
) -> list[dict]:
    return repo.list_drafts(seeker.id, status=status_filter)


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str, seeker: Seeker) -> dict:
    draft = repo.get_draft(draft_id, seeker.id)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "draft not found")
    return draft


@router.patch("/drafts/{draft_id}")
def edit_draft(draft_id: str, payload: DraftEditIn, seeker: Seeker) -> dict:
    try:
        return replies_mod.edit_draft(
            seeker.id, draft_id, body=payload.body, subject=payload.subject
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: str, seeker: Seeker, payload: DraftApproveIn | None = None) -> dict:
    """Approve a draft for sending.  Approval is not sending (NFR-305)."""
    try:
        return replies_mod.approve_draft(
            seeker.id,
            draft_id,
            body=payload.body if payload else None,
            subject=payload.subject if payload else None,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except replies_mod.AlreadySent as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/drafts/{draft_id}/discard")
def discard_draft(draft_id: str, seeker: Seeker) -> dict:
    try:
        return replies_mod.discard_draft(seeker.id, draft_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# ---------------------------------------------------------------------------
# FR-423: interview scheduling
# ---------------------------------------------------------------------------


@router.get("/calendar")
def calendar_accounts(seeker: Seeker) -> dict:
    """Connected calendars.  Never returns the stored credentials (NFR-204)."""
    accounts = repo.list_calendar_accounts(seeker.id)
    settings = get_settings()
    return {
        "accounts": accounts,
        "google_configured": bool(settings.google_calendar_client_id),
        "note": (
            "Without a connected calendar the proposed slots are not checked against your "
            "diary, and a confirmed interview is written as an .ics file with the briefing "
            "attached rather than created in a calendar."
        ),
    }


@router.post("/calendar/authorize")
def calendar_authorize(payload: CalendarConnectIn, seeker: Seeker) -> dict:
    """The consent URL for connecting a calendar (FR-423)."""
    from dreamjob.security.crypto import new_session_token  # noqa: PLC0415

    state, _ = new_session_token()
    try:
        url = calendar_sync.authorization_url(
            payload.provider,
            state=state,
            redirect_uri=payload.redirect_uri
            or "http://127.0.0.1:8000/api/pipeline/calendar/callback",
            drive=payload.include_drive,
        )
    except calendar_sync.CalendarUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {"authorization_url": url, "state": state}


@router.delete("/calendar/{account_id}")
def disconnect_calendar(account_id: str, seeker: Seeker) -> dict:
    """Revoke a calendar grant: the row and its token are deleted (NFR-204)."""
    removed = repo.delete_calendar_account(account_id, seeker.id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "calendar account not found")
    return {"disconnected": True}


@router.post("/replies/{reply_id}/schedule")
async def schedule(reply_id: str, payload: ScheduleIn, seeker: Seeker) -> dict:
    """Extract the proposed slots, check them and draft the confirmation (FR-423)."""
    try:
        return await calendar_sync.schedule_from_reply(
            seeker.id,
            reply_id,
            llm=_llm(seeker, payload.use_llm),
            duration_minutes=payload.duration_minutes,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/appointments")
def list_appointments(
    seeker: Seeker, status_filter: str | None = Query(None, alias="status")
) -> list[dict]:
    return repo.list_appointments(seeker.id, status=status_filter)


@router.get("/appointments/{appointment_id}")
def get_appointment(appointment_id: str, seeker: Seeker) -> dict:
    appointment = repo.get_appointment(appointment_id, seeker.id)
    if appointment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "appointment not found")
    return appointment


@router.post("/appointments/{appointment_id}/confirm")
async def confirm_appointment(
    appointment_id: str, payload: ConfirmIn, seeker: Seeker
) -> dict:
    """Approve a slot and create the entry with the briefing attached (FR-423)."""
    try:
        return await calendar_sync.confirm(
            seeker.id,
            appointment_id,
            start=payload.start,
            end=payload.end,
            location=payload.location,
            create_entry=payload.create_entry,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


# ---------------------------------------------------------------------------
# FR-424: the mock interview
# ---------------------------------------------------------------------------


@router.post("/mock-interviews", status_code=status.HTTP_201_CREATED)
def start_mock_interview(payload: MockStartIn, seeker: Seeker) -> dict:
    """Open a session and return the first question (FR-424)."""
    try:
        return mock_interview.start(
            seeker.id,
            payload.opportunity_id,
            language=payload.language,
            focus=payload.focus,
            questions=payload.questions,
            llm=_llm(seeker, payload.use_llm),
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/mock-interviews")
def list_mock_interviews(seeker: Seeker, opportunity_id: str | None = None) -> list[dict]:
    return mock_interview.history(seeker.id, opportunity_id=opportunity_id)


@router.get("/mock-interviews/{session_id}")
def get_mock_interview(session_id: str, seeker: Seeker) -> dict:
    try:
        return mock_interview.get(seeker.id, session_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/mock-interviews/{session_id}/answer")
def answer_mock_interview(session_id: str, payload: MockAnswerIn, seeker: Seeker) -> dict:
    """Answer the open question and receive feedback plus the next one (FR-424)."""
    try:
        return mock_interview.answer(
            seeker.id, session_id, payload.answer, llm=_llm(seeker, payload.use_llm)
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except mock_interview.SessionClosed as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/mock-interviews/{session_id}/finish")
def finish_mock_interview(session_id: str, seeker: Seeker, use_llm: bool = True) -> dict:
    """End the session and return the weak spots to rehearse (FR-424)."""
    try:
        return mock_interview.finish(seeker.id, session_id, llm=_llm(seeker, use_llm))
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# ---------------------------------------------------------------------------
# FR-444: the negotiation brief
# ---------------------------------------------------------------------------


@router.get("/negotiation")
def list_negotiation_briefs(seeker: Seeker) -> list[dict]:
    return repo.list_briefs(seeker.id)


@router.get("/negotiation/{opportunity_id}")
def get_negotiation_brief(opportunity_id: str, seeker: Seeker) -> dict:
    brief = negotiation.get(seeker.id, opportunity_id)
    if brief is None:
        eligible, reason = negotiation.eligible(seeker.id, opportunity_id)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no negotiation brief yet" + ("" if eligible else f": {reason}"),
        )
    return brief


@router.post("/negotiation/{opportunity_id}")
def build_negotiation_brief(
    opportunity_id: str, payload: NegotiationIn, seeker: Seeker
) -> dict:
    """Generate the salary negotiation brief and its PDF (FR-444)."""
    try:
        return negotiation.build(
            seeker.id,
            opportunity_id,
            language=payload.language,
            llm=_llm(seeker, payload.use_llm),
            force=payload.force,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except negotiation.NotEligible as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# ---------------------------------------------------------------------------
# FR-425: outcomes and what they teach
# ---------------------------------------------------------------------------


@router.get("/learning")
def learning_report(
    seeker: Seeker,
    outcome: str = Query("reply"),
    recompute: bool = True,
) -> dict:
    """The learned effects, each with the sample it rests on (FR-425).

    Nothing here is applied by reading it: see the ``apply`` route.
    """
    if outcome not in outcomes.OUTCOMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"outcome must be one of {', '.join(outcomes.OUTCOMES)}",
        )
    if not recompute:
        stored = outcomes.current_report(seeker.id)
        if stored is not None:
            return stored
    return outcomes.report(seeker.id, outcome=outcome)


@router.post("/learning/apply")
def apply_learning(seeker: Seeker, outcome: str = Query("reply")) -> dict:
    """Adopt the learned generation defaults and the capped weight nudge (FR-425)."""
    if outcome not in outcomes.OUTCOMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"outcome must be one of {', '.join(outcomes.OUTCOMES)}",
        )
    return outcomes.apply_learning(seeker.id, outcome=outcome)


@router.get("/learning/defaults")
def learned_defaults(seeker: Seeker) -> dict:
    """The defaults the generators should read - empty until they were applied."""
    return outcomes.generation_defaults(seeker.id)
