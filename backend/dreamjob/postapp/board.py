"""Pipeline board for sent applications (FR-421, NFR-305).

Five stages - ``sent``, ``replied``, ``interview``, ``offer``, ``closed`` -
and one rule about how a card moves between them: reply detection (FR-326)
moves it where the reply is unambiguous, and the job seeker moves it
everywhere else.  Both paths write a ``pipeline_card_event`` with the trigger
that caused them, because a board whose automatic moves cannot be told from
its manual ones is a board that has to be re-checked by hand.

Automatic movement is deliberately conservative.  An interview invitation and
a rejection say what stage the application is in; "thank you for your
interest, we will be in touch" does not, so it moves the card to ``replied``
and stops.  Nothing here closes a card on its own: a silent application is
*reported* as silent (:func:`silent`) and the seeker decides whether that is a
``no_response`` outcome (NFR-305).

Stage order is used for one thing only - to decide whether an automatic
transition would move the card backwards, which it never may.  A manual
transition may go anywhere, including back, because the seeker knows things
the mailbox does not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import from_json, to_json, utcnow
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

#: The board's columns, in the order an application moves through them.
STAGES: tuple[str, ...] = ("sent", "replied", "interview", "offer", "closed")

#: Only ``closed`` carries an outcome (FR-421); FR-425 reads the same vocabulary.
OUTCOMES: tuple[str, ...] = ("accepted", "rejected", "withdrawn", "no_response")

_ORDER = {stage: index for index, stage in enumerate(STAGES)}

#: Reply class -> stage, for the automatic path (FR-421 via FR-422).
#: ``interest`` and ``request_for_information`` are answers, not decisions, so
#: they stop at ``replied``; an automatic out-of-office is not an answer at all.
AUTOMATIC_TRANSITIONS: dict[str, str] = {
    "interview_invitation": "interview",
    "rejection": "closed",
    "interest": "replied",
    "request_for_information": "replied",
    "referral": "replied",
    "offer": "offer",
}

#: What to do next in each stage, and how long the seeker has before it nags.
_NEXT_ACTION: dict[str, tuple[str, int]] = {
    "sent": ("Wait for a reply, then follow up", 10),
    "replied": ("Answer the reply", 2),
    "interview": ("Prepare the interview: run a mock interview and read the briefing", 3),
    "offer": ("Review the negotiation brief before answering the offer", 3),
    "closed": ("", 0),
}

#: How long a sent application stays silent before the board says so (FR-425).
SILENCE_DAYS = 21


class StageError(ValueError):
    """Raised for a stage or outcome outside the vocabulary of FR-421."""


@dataclass
class Transition:
    """One stage change, as the board reports it back."""

    card_id: str
    from_stage: str
    to_stage: str
    trigger: str
    changed: bool
    outcome: str | None = None
    note: str | None = None
    card: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "trigger": self.trigger,
            "changed": self.changed,
            "outcome": self.outcome,
            "note": self.note,
            "card": self.card,
        }


def _validate(stage: str | None = None, outcome: str | None = None) -> None:
    if stage is not None and stage not in STAGES:
        raise StageError(f"stage must be one of {', '.join(STAGES)}, got {stage!r}")
    if outcome is not None and outcome not in OUTCOMES:
        raise StageError(f"outcome must be one of {', '.join(OUTCOMES)}, got {outcome!r}")


def _due(days: int, *, now: datetime | None = None) -> str | None:
    if days <= 0:
        return None
    return ((now or datetime.now(UTC)) + timedelta(days=days)).isoformat(timespec="seconds")


def default_next_action(
    stage: str, *, now: datetime | None = None
) -> tuple[str | None, str | None]:
    text, days = _NEXT_ACTION.get(stage, ("", 0))
    return (text or None), _due(days, now=now)


# ---------------------------------------------------------------------------
# Creating cards from what was actually sent
# ---------------------------------------------------------------------------


def ensure_card(
    job_seeker_id: str,
    *,
    application_package_id: str | None,
    opportunity_id: str,
    variables: dict[str, Any] | None = None,
    sent_at: str | None = None,
    notes: str | None = None,
) -> dict:
    """Return the card for one application, creating it if the board lacks one.

    Idempotent: the unique index on ``application_package_id`` means a second
    call for the same package returns the same card rather than a duplicate.
    """
    existing = None
    if application_package_id:
        existing = repo.card_for_package(job_seeker_id, application_package_id)
    if existing is None and not application_package_id:
        existing = repo.card_for_opportunity(job_seeker_id, opportunity_id)
    if existing is not None:
        if variables and not existing.get("variables"):
            repo.update_card(existing["id"], {"variables": to_json(variables)})
            existing["variables"] = variables
        return existing

    action, due = default_next_action("sent")
    stamp = sent_at or utcnow()
    card_id = repo.create_card(
        job_seeker_id,
        {
            "opportunity_id": opportunity_id,
            "application_package_id": application_package_id,
            "stage": "sent",
            "stage_dates": to_json({"sent": stamp}),
            "next_action": action,
            "next_action_due": due,
            "notes": notes,
            "variables": to_json(variables or {}),
        },
    )
    repo.record_card_event(
        job_seeker_id,
        card_id,
        {"from_stage": None, "to_stage": "sent", "trigger": "system", "note": "application sent"},
    )
    log.info("Pipeline card %s opened for opportunity %s", card_id, opportunity_id)
    return repo.get_card(card_id, job_seeker_id) or {}


def sync_sent_applications(job_seeker_id: str) -> dict[str, Any]:
    """Open a card for every sent application that has none yet (FR-421).

    Cheap to run repeatedly, so the board can be reconciled on load and by the
    scheduler without either of them owning the responsibility.
    """
    from dreamjob.postapp import outcomes  # noqa: PLC0415 - two halves of one feature

    created: list[str] = []
    for row in repo.sent_packages_without_card(job_seeker_id):
        card = ensure_card(
            job_seeker_id,
            application_package_id=row["application_package_id"],
            opportunity_id=row["opportunity_id"],
            variables=outcomes.capture_variables(job_seeker_id, row["application_package_id"]),
            sent_at=row.get("sent_at"),
        )
        if card:
            created.append(card["id"])
    return {"created": len(created), "card_ids": created}


# ---------------------------------------------------------------------------
# Moving a card
# ---------------------------------------------------------------------------


def transition(
    job_seeker_id: str,
    card_id: str,
    to_stage: str,
    *,
    trigger: str = "user",
    outcome: str | None = None,
    note: str | None = None,
    incoming_reply_id: str | None = None,
    detail: dict[str, Any] | None = None,
    next_action: str | None = None,
    next_action_due: str | None = None,
) -> Transition:
    """Move a card, recording what moved it (FR-421).

    ``trigger`` is ``user`` for a manual move and ``reply_detection`` for the
    automatic one.  An automatic move never goes backwards and never sets an
    outcome other than the rejection it read; deciding that an application is
    over is the seeker's (NFR-305).
    """
    _validate(stage=to_stage, outcome=outcome)
    card = repo.get_card(card_id, job_seeker_id)
    if card is None:
        raise LookupError(f"No pipeline card {card_id} for this job seeker")

    from_stage = str(card.get("stage") or "sent")
    if trigger != "user" and _ORDER[to_stage] < _ORDER[from_stage]:
        log.debug(
            "Ignoring automatic %s -> %s on card %s: automatic moves never go backwards",
            from_stage, to_stage, card_id,
        )
        return Transition(card_id, from_stage, from_stage, trigger, False, note=note, card=card)
    if trigger != "user" and from_stage == "closed":
        # A closed card is terminal.  A late reply classified as a rejection
        # must not overwrite an outcome the seeker already recorded (or an
        # acceptance), nor append a second transition to the history.
        log.debug(
            "Ignoring automatic %s on closed card %s", to_stage, card_id
        )
        return Transition(card_id, from_stage, from_stage, trigger, False, note=note, card=card)
    if trigger != "user" and to_stage == from_stage:
        # The same message classified twice must not emit a duplicate event.
        return Transition(card_id, from_stage, from_stage, trigger, False, note=note, card=card)

    now = utcnow()
    stage_dates = dict(card.get("stage_dates") or {})
    stage_dates.setdefault(to_stage, now)

    values: dict[str, Any] = {
        "stage": to_stage,
        "stage_dates": to_json(stage_dates),
        "stage_changed_at": now,
    }
    # FR-425 needs "did this application ever reach interview", which is not
    # the same question as "where is it now".
    for stage, column in (
        ("replied", "reached_replied_at"),
        ("interview", "reached_interview_at"),
        ("offer", "reached_offer_at"),
    ):
        if _ORDER[to_stage] >= _ORDER[stage] and not card.get(column):
            values[column] = now

    if to_stage == "closed":
        values["outcome"] = outcome
        values["outcome_at"] = now
        if detail:
            values["outcome_detail"] = to_json(detail)
    elif outcome:
        raise StageError("an outcome can only be recorded when a card is closed")

    if next_action is not None or next_action_due is not None:
        values["next_action"] = next_action
        values["next_action_due"] = next_action_due
    elif to_stage != from_stage:
        action, due = default_next_action(to_stage)
        values["next_action"] = action
        values["next_action_due"] = due

    if note:
        values["notes"] = _append_note(card.get("notes"), note)

    repo.update_card(card_id, values)
    repo.record_card_event(
        job_seeker_id,
        card_id,
        {
            "from_stage": from_stage,
            "to_stage": to_stage,
            "trigger": trigger,
            "incoming_reply_id": incoming_reply_id,
            "note": note,
            "detail": to_json({**(detail or {}), "outcome": outcome} if outcome else detail),
        },
    )
    record_audit(
        "pipeline.stage_changed",
        "pipeline_card",
        card_id,
        seeker_id=job_seeker_id,
        actor=job_seeker_id if trigger == "user" else "system",
        detail={"from": from_stage, "to": to_stage, "trigger": trigger, "outcome": outcome},
    )
    return Transition(
        card_id=card_id,
        from_stage=from_stage,
        to_stage=to_stage,
        trigger=trigger,
        changed=to_stage != from_stage or bool(outcome),
        outcome=outcome,
        note=note,
        card=repo.get_card(card_id, job_seeker_id) or {},
    )


def _append_note(existing: str | None, note: str) -> str:
    stamped = f"[{utcnow()[:16]}] {note}"
    return f"{existing}\n{stamped}" if existing else stamped


def apply_reply(
    job_seeker_id: str,
    reply: dict[str, Any],
    *,
    classification: str | None = None,
) -> Transition | None:
    """Move the card an incoming reply belongs to (FR-421, FR-422).

    Returns ``None`` when there is nothing to move: a reply that cannot be
    tied to a dispatch, or a classification that says nothing about the stage
    (an out-of-office).
    """
    label = classification or reply.get("classification")
    stage = AUTOMATIC_TRANSITIONS.get(str(label or ""))
    if not stage:
        return None

    dispatch_id = reply.get("dispatch_id")
    card = repo.card_for_dispatch(job_seeker_id, dispatch_id) if dispatch_id else None
    if card is None:
        log.debug("Reply %s has no pipeline card to move", reply.get("id"))
        return None

    outcome = "rejected" if stage == "closed" else None
    note = f"Reply classified as {str(label).replace('_', ' ')}"
    return transition(
        job_seeker_id,
        card["id"],
        stage,
        trigger="reply_detection",
        outcome=outcome,
        note=note,
        incoming_reply_id=reply.get("id"),
        detail={"from_address": reply.get("from_address"), "subject": reply.get("subject")},
    )


# ---------------------------------------------------------------------------
# Card content the seeker owns (FR-421: next action and free notes)
# ---------------------------------------------------------------------------


def update_card(
    job_seeker_id: str,
    card_id: str,
    *,
    next_action: str | None = None,
    next_action_due: str | None = None,
    notes: str | None = None,
    append_note: str | None = None,
) -> dict:
    card = repo.get_card(card_id, job_seeker_id)
    if card is None:
        raise LookupError(f"No pipeline card {card_id} for this job seeker")
    values: dict[str, Any] = {}
    if next_action is not None:
        values["next_action"] = next_action or None
    if next_action_due is not None:
        values["next_action_due"] = next_action_due or None
    if notes is not None:
        values["notes"] = notes or None
    if append_note:
        values["notes"] = _append_note(values.get("notes", card.get("notes")), append_note)
    repo.update_card(card_id, values)
    return repo.get_card(card_id, job_seeker_id) or {}


# ---------------------------------------------------------------------------
# Reading the board
# ---------------------------------------------------------------------------


def board(
    job_seeker_id: str, *, campaign_id: str | None = None, include_closed: bool = True
) -> dict[str, Any]:
    """The board as the UI renders it: one list per stage, plus the totals."""
    cards = repo.list_cards(
        job_seeker_id, campaign_id=campaign_id, include_closed=include_closed
    )
    columns: dict[str, list[dict]] = {stage: [] for stage in STAGES}
    for card in cards:
        columns.setdefault(str(card.get("stage") or "sent"), []).append(_present(card))
    now = utcnow()
    return {
        "stages": [
            {"stage": stage, "count": len(columns[stage]), "cards": columns[stage]}
            for stage in STAGES
        ],
        "totals": {stage: len(columns[stage]) for stage in STAGES},
        "overdue": [c["id"] for c in cards if (c.get("next_action_due") or now) < now],
        "generated_at": now,
    }


def _present(card: dict) -> dict:
    """Trim a card row to what a board column shows."""
    keep = (
        "id", "opportunity_id", "application_package_id", "stage", "stage_dates", "next_action",
        "next_action_due", "notes", "outcome", "outcome_at", "created_at", "updated_at",
        "opportunity_title", "opportunity_kind", "opportunity_score", "company_name",
        "company_id", "recipient_email", "sent_at", "thread_id", "follow_up_due_at",
        "delivery_status",
    )
    out = {k: card.get(k) for k in keep}
    out["overdue"] = bool(card.get("next_action_due") and card["next_action_due"] < utcnow())
    return out


def card_detail(job_seeker_id: str, card_id: str) -> dict[str, Any]:
    card = repo.get_card(card_id, job_seeker_id)
    if card is None:
        raise LookupError(f"No pipeline card {card_id} for this job seeker")
    replies = [
        r
        for r in repo.list_replies(job_seeker_id, limit=50)
        if r.get("application_package_id") == card.get("application_package_id")
    ]
    return {
        "card": card,
        "events": repo.card_events(card_id, job_seeker_id),
        "replies": replies,
        "drafts": [
            d
            for reply in replies
            for d in repo.drafts_for_reply(reply["id"], job_seeker_id)
        ],
        "mock_interviews": repo.list_sessions(
            job_seeker_id, opportunity_id=card.get("opportunity_id"), limit=10
        ),
        "negotiation_brief": repo.get_brief(job_seeker_id, str(card.get("opportunity_id") or "")),
    }


def due(job_seeker_id: str, *, within_days: int = 0) -> list[dict]:
    """Cards whose next action is due now, or within ``within_days``."""
    horizon = (datetime.now(UTC) + timedelta(days=max(0, within_days))).isoformat(
        timespec="seconds"
    )
    return [_present(c) for c in repo.cards_due(job_seeker_id, horizon)]


def silent(job_seeker_id: str, *, days: int = SILENCE_DAYS) -> list[dict]:
    """Sent applications that have never been answered (FR-425 'no response').

    Reported, never acted on: closing an application is a decision (NFR-305).
    """
    cutoff = (datetime.now(UTC) - timedelta(days=max(1, days))).isoformat(timespec="seconds")
    out = []
    for card in repo.silent_cards(job_seeker_id, cutoff):
        presented = _present(card)
        sent = (card.get("stage_dates") or {}).get("sent") or card.get("created_at")
        presented["silent_since"] = sent
        presented["silent_days"] = _days_between(sent, utcnow())
        out.append(presented)
    return out


def _days_between(start: str | None, end: str) -> int | None:
    if not start:
        return None
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
    except ValueError:
        return None


def summary(job_seeker_id: str) -> dict[str, Any]:
    """Counts per stage plus the two queues that need attention."""
    counts = repo.stage_counts(job_seeker_id)
    return {
        "stages": {stage: counts.get(stage, 0) for stage in STAGES},
        "total": sum(counts.values()),
        "due_now": len(due(job_seeker_id)),
        "silent": len(silent(job_seeker_id)),
    }


def decode_variables(card: dict) -> dict[str, Any]:
    """``variables`` as a dict whether it arrived decoded or as stored JSON."""
    value = card.get("variables")
    if isinstance(value, dict):
        return value
    return from_json(value, {}) or {}
