"""Entering a response by hand (extends FR-326, feeds FR-421, FR-422, FR-425).

Automatic reply detection only covers replies that arrive by email in a mailbox
the system can poll.  In practice a good share of responses do not:

* Resend has no inbox at all - it sends, and reports delivery by webhook.
* A recruiter phones, or answers on LinkedIn, or through an ATS portal.
* A reply lands in a different mailbox, or gets forwarded on.

Those responses are the most informative ones the pipeline has, so there is a
way to type them in.  A manually entered response is turned into an ordinary
``incoming_reply`` row and then flows through exactly the same path as a
detected one: classification (FR-422), stage transition on the board (FR-421),
and the outcome record that feeds learning (FR-425).

The person entering it also states what they think happened.  That stated
outcome is kept separately from the model's classification, so the two can
disagree - and where they do, the human's reading is the one that counts.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.db.connection import insert_row, update_row, utcnow
from dreamjob.db.repositories import learning as repo
from dreamjob.llm.client import LLMClient
from dreamjob.postapp import board
from dreamjob.postapp.reply_classifier import classify

log = logging.getLogger(__name__)

CHANNELS = ("email", "phone", "linkedin", "portal", "in_person", "other")

# What a person can state directly, matching the FR-422 classes so the two are
# comparable.
STATED_OUTCOMES = (
    "interest",
    "info_request",
    "interview",
    "rejection",
    "referral",
    "auto_reply",
    "other",
)

# Which stated outcomes move a card, and where to.
STAGE_FOR = {
    "interview": "interview",
    "interest": "replied",
    "info_request": "replied",
    "referral": "replied",
    "rejection": "closed",
}

#: The board's own labels for the outcomes a person can enter by hand.  Without
#: this the transition lookup was asked for "interview" while the state machine
#: keys on "interview_invitation", so recording an interview invitation moved
#: nothing and the digest never saw it either.
BOARD_LABEL_FOR = {
    "interview": "interview_invitation",
    "info_request": "request_for_information",
}


def record_response(
    job_seeker_id: str,
    *,
    dispatch_id: str | None = None,
    opportunity_id: str | None = None,
    channel: str = "email",
    raw_text: str = "",
    stated_outcome: str | None = None,
    received_at: str | None = None,
    from_address: str | None = None,
    subject: str | None = None,
    notes: str = "",
    classify_text: bool = True,
) -> dict[str, Any]:
    """Record a response received outside automatic detection.

    Either ``dispatch_id`` or ``opportunity_id`` must identify what it answers.
    """
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
    if stated_outcome and stated_outcome not in STATED_OUTCOMES:
        raise ValueError(f"stated_outcome must be one of {', '.join(STATED_OUTCOMES)}")
    if not dispatch_id and not opportunity_id:
        raise ValueError("A response must be linked to a dispatch or an opportunity")

    dispatch = repo.dispatch_for_seeker(job_seeker_id, dispatch_id) if dispatch_id else None
    if dispatch_id and dispatch is None:
        raise KeyError("No such dispatch for this job seeker")

    if not opportunity_id and dispatch:
        opportunity_id = repo.opportunity_of_dispatch(job_seeker_id, dispatch_id)

    received = received_at or utcnow()

    # The same shape a polled reply produces, so everything downstream is
    # indifferent to how it arrived.
    reply_id = insert_row(
        "incoming_reply",
        {
            "job_seeker_id": job_seeker_id,
            "dispatch_id": dispatch_id,
            "from_address": from_address or (dispatch or {}).get("recipient_email"),
            "subject": subject or (dispatch or {}).get("subject"),
            "body": raw_text,
            "received_at": received,
            "classification": stated_outcome,
            "classification_confidence": 1.0 if stated_outcome else None,
            "handled": 0,
            "created_at": utcnow(),
        },
    )

    manual_id = insert_row(
        "manual_response",
        {
            "job_seeker_id": job_seeker_id,
            "incoming_reply_id": reply_id,
            "dispatch_id": dispatch_id,
            "opportunity_id": opportunity_id,
            "channel": channel,
            "received_at": received,
            "entered_by": job_seeker_id,
            "raw_text": raw_text,
            "stated_outcome": stated_outcome,
            "notes": notes,
            "created_at": utcnow(),
        },
    )

    reply_row = repo.reply_row(job_seeker_id, reply_id) or {}

    classification: dict[str, Any] | None = None
    if classify_text and raw_text.strip():
        try:
            # The text came from outside, so it reaches the model as data, not
            # as instructions (NFR-205) - classify() handles that fencing.
            result = classify(
                reply_row,
                context={"opportunity_id": opportunity_id},
                llm=LLMClient(job_seeker_id=job_seeker_id),
            )
            classification = result.as_dict() if hasattr(result, "as_dict") else dict(result)
        except Exception:  # noqa: BLE001 - a failed reading must not lose the record
            log.exception("Could not classify manual response %s", reply_id)

    # A stated outcome overrides the model. The person was there.
    effective = stated_outcome or (classification or {}).get("classification")
    if effective and stated_outcome and classification:
        model_said = classification.get("classification")
        if model_said and model_said != stated_outcome:
            update_row(
                "incoming_reply",
                reply_id,
                {
                    "classification": BOARD_LABEL_FOR.get(stated_outcome, stated_outcome),
                    "classification_confidence": 1.0,
                },
            )
            log.info(
                "Manual response %s: keeping stated outcome %r over classified %r",
                reply_id, stated_outcome, model_said,
            )

    stage = STAGE_FOR.get(effective or "")
    board_label = BOARD_LABEL_FOR.get(effective or "", effective)
    if stage:
        try:
            # The same path a detected reply takes, so a hand-entered response
            # and a polled one move the board identically (FR-421).
            board.apply_reply(
                job_seeker_id,
                {**reply_row, "classification": board_label},
                classification=board_label,
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not move the board for reply %s", reply_id)

    return {
        "manual_response_id": manual_id,
        "incoming_reply_id": reply_id,
        "opportunity_id": opportunity_id,
        "stated_outcome": stated_outcome,
        "classification": classification,
        "effective_outcome": effective,
        "stage_moved_to": stage,
    }


def list_responses(job_seeker_id: str, limit: int = 200) -> list[dict]:
    """Every response, however it arrived, newest first."""
    return repo.responses_for_seeker(job_seeker_id, limit=limit)


def correct_outcome(job_seeker_id: str, manual_response_id: str, stated_outcome: str) -> dict:
    """Correct a classification after the fact.

    Worth having: the learning in ``segments.py`` is only as good as the
    outcomes behind it, and a misread rejection quietly poisons every rate it
    is counted in.
    """
    if stated_outcome not in STATED_OUTCOMES:
        raise ValueError(f"stated_outcome must be one of {', '.join(STATED_OUTCOMES)}")

    row = repo.manual_response_by_id(job_seeker_id, manual_response_id)
    if row is None:
        raise KeyError("No such response")

    update_row("manual_response", manual_response_id, {"stated_outcome": stated_outcome})
    if row.get("incoming_reply_id"):
        update_row(
            "incoming_reply",
            row["incoming_reply_id"],
            {"classification": stated_outcome, "classification_confidence": 1.0},
        )

    stage = STAGE_FOR.get(stated_outcome)
    if stage and row.get("opportunity_id"):
        card = repo.card_for_opportunity(job_seeker_id, row["opportunity_id"])
        if card:
            try:
                board.transition(
                    job_seeker_id,
                    card["id"],
                    stage,
                    trigger="user",
                    outcome="rejected" if stated_outcome == "rejection" else None,
                    note=f"Corrected to {stated_outcome} by the job seeker",
                )
            except Exception:  # noqa: BLE001
                log.exception("Could not move the board after correction")

    return {"manual_response_id": manual_response_id, "stated_outcome": stated_outcome}
