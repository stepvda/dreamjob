"""The weekly digest (FR-403, NFR-305).

One digest per active job seeker per week, containing new opportunities,
replies received, follow-ups due, watchlist changes and **one** recommended
next action, delivered in-app as a ``notification`` row and optionally by
e-mail.

The single recommended action is the part worth getting right.  A digest that
lists nine things a person could do is a to-do list, and a to-do list is what
they already have; FR-403 asks for one action, which means the digest has to
take a position.  It does so with a fixed priority ladder rather than a model:
the ranking rule is a product decision, it has to be identical every week so
the seeker learns to trust it, and it has to be explainable in one line.  The
ladder runs from things with a deadline attached (an interview invitation
waiting for an answer) down to things that are merely available (a strong new
opportunity).  Every rung names the reason, so the digest can say why this and
not that.

Recommending is not deciding (NFR-305): the action is a suggestion with a
link, never an instruction, and nothing acts on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import to_json, utcnow
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.postapp import board as board_mod
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

DEFAULT_PERIOD_DAYS = 7
#: How many entries of each kind the digest carries; the rest is a count.
SECTION_LIMIT = 8


@dataclass
class Action:
    """The one recommended next action (FR-403)."""

    key: str
    title: str
    reason: str
    entity_type: str | None = None
    entity_id: str | None = None
    url: str | None = None
    priority: int = 99

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "reason": self.reason,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "url": self.url,
            "priority": self.priority,
        }


@dataclass
class Digest:
    job_seeker_id: str
    period_start: str
    period_end: str
    sections: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    action: Action | None = None
    id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_seeker_id": self.job_seeker_id,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "counts": self.counts,
            "sections": self.sections,
            "recommended_action": self.action.as_dict() if self.action else None,
        }

    @property
    def empty(self) -> bool:
        return not any(self.counts.values())


# ---------------------------------------------------------------------------
# The ladder (FR-403: exactly one recommended next action)
# ---------------------------------------------------------------------------


def choose_action(sections: dict[str, Any]) -> Action | None:
    """The one thing to do next, and why it beat everything else.

    Ordered by what is lost by not doing it: an unanswered invitation can
    expire, an overdue follow-up decays with every day, an offer without a
    negotiation brief is a conversation entered unprepared, and a new
    opportunity is simply available.
    """
    invitations = [
        r for r in sections.get("replies", [])
        if r.get("classification") == "interview_invitation" and not r.get("handled")
    ]
    if invitations:
        reply = invitations[0]
        return Action(
            key="answer_interview_invitation",
            title=(
                "Answer the interview invitation from "
                f"{reply.get('company_name') or 'the employer'}"
            ),
            reason=(
                "An interview invitation is waiting for an answer. It is the only thing this "
                "week with a date attached to it."
            ),
            entity_type="incoming_reply",
            entity_id=reply.get("id"),
            url=f"/pipeline/replies/{reply.get('id')}",
            priority=1,
        )

    drafts = sections.get("drafts_waiting", [])
    if drafts:
        draft = drafts[0]
        return Action(
            key="review_reply_draft",
            title="Review and send the drafted reply",
            reason=(
                f"{len(drafts)} drafted repl{'y is' if len(drafts) == 1 else 'ies are'} "
                "waiting for you; nothing is sent until you approve it."
            ),
            entity_type="reply_draft",
            entity_id=draft.get("id"),
            url=f"/pipeline/drafts/{draft.get('id')}",
            priority=2,
        )

    overdue = sections.get("follow_ups_due", [])
    if overdue:
        item = overdue[0]
        return Action(
            key="send_follow_up",
            title=f"Follow up with {item.get('company_name') or item.get('recipient_email')}",
            reason=(
                f"The follow-up was due on {str(item.get('follow_up_due_at') or '')[:10]}. "
                "A follow-up sent late is worth much less than one sent on time."
            ),
            entity_type="dispatch",
            entity_id=item.get("dispatch_id"),
            # A dispatch can be due a follow-up before its card exists, and a
            # link to /cards/None is worse than a link to the board.
            url=(
                f"/pipeline/cards/{item['pipeline_card_id']}"
                if item.get("pipeline_card_id")
                else "/pipeline/board"
            ),
            priority=3,
        )

    offers = [c for c in sections.get("stage_changes", []) if c.get("stage") == "offer"]
    if offers:
        card = offers[0]
        return Action(
            key="prepare_negotiation",
            title=f"Read the negotiation brief for {card.get('company_name') or 'the offer'}",
            reason=(
                "An offer is on the table. The brief has the company's ability to pay and "
                "the market range on one page."
            ),
            entity_type="opportunity",
            entity_id=card.get("opportunity_id"),
            url=f"/pipeline/cards/{card.get('id')}",
            priority=4,
        )

    interviews = [c for c in sections.get("stage_changes", []) if c.get("stage") == "interview"]
    if interviews:
        card = interviews[0]
        return Action(
            key="rehearse_interview",
            title=f"Run a mock interview for {card.get('opportunity_title') or 'the interview'}",
            reason=(
                "An interview is coming. One rehearsal produces the list of weak spots "
                "worth working on."
            ),
            entity_type="opportunity",
            entity_id=card.get("opportunity_id"),
            url=f"/pipeline/cards/{card.get('id')}",
            priority=5,
        )

    silent = sections.get("silent_applications", [])
    if len(silent) >= 3:
        return Action(
            key="close_silent_applications",
            title=f"Decide on {len(silent)} applications that never got an answer",
            reason=(
                "Closing them as 'no response' is what lets the outcome analysis learn "
                "anything from them (FR-425)."
            ),
            entity_type="pipeline_card",
            entity_id=silent[0].get("id"),
            url="/pipeline",
            priority=6,
        )

    opportunities = sections.get("new_opportunities", [])
    if opportunities:
        best = opportunities[0]
        flagged = " and the timing is favourable" if best.get("timing_flag") else ""
        return Action(
            key="review_opportunity",
            title=f"Look at {best.get('title')} at {best.get('company_name') or 'a new company'}",
            reason=(
                f"It is the highest-scoring opportunity found this week{flagged}. "
                "The ranking is advisory; you decide whether it is worth an application."
            ),
            entity_type="opportunity",
            entity_id=best.get("id"),
            url=f"/opportunities/{best.get('id')}",
            priority=7,
        )

    watch_changes = sections.get("watchlist_changes", [])
    if watch_changes:
        change = watch_changes[0]
        return Action(
            key="review_watchlist_change",
            title=change.get("title") or "Review the watchlist change",
            reason="Nothing else needs an answer this week; this is the only new signal.",
            entity_type="notification",
            entity_id=change.get("id"),
            url="/monitoring",
            priority=8,
        )
    return None


# ---------------------------------------------------------------------------
# Assembling
# ---------------------------------------------------------------------------


def _period(period_days: int, end: datetime | None = None) -> tuple[str, str]:
    finish = end or datetime.now(UTC)
    return (
        (finish - timedelta(days=period_days)).isoformat(timespec="seconds"),
        finish.isoformat(timespec="seconds"),
    )


def build(
    job_seeker_id: str, *, period_days: int = DEFAULT_PERIOD_DAYS, end: datetime | None = None
) -> Digest:
    """Assemble the digest for one seeker (FR-403).  Reads only; stores nothing."""
    start, finish = _period(period_days, end)
    digest = Digest(job_seeker_id=job_seeker_id, period_start=start, period_end=finish)

    opportunities = repo.new_opportunities(job_seeker_id, start, limit=SECTION_LIMIT * 3)
    replies = repo.list_replies(job_seeker_id, since=start, limit=SECTION_LIMIT * 3)
    follow_ups = repo.due_follow_ups(job_seeker_id, finish, limit=SECTION_LIMIT * 3)
    changes = repo.watch_changes(job_seeker_id, start, limit=SECTION_LIMIT * 3)
    drafts = repo.list_drafts(job_seeker_id, status="draft", limit=SECTION_LIMIT * 3)
    stage_changes = [
        c
        for c in repo.list_cards(job_seeker_id, include_closed=False)
        if (c.get("stage_changed_at") or "") >= start
    ]
    silent = board_mod.silent(job_seeker_id)

    digest.sections = {
        "new_opportunities": opportunities[:SECTION_LIMIT],
        "replies": [_slim_reply(r) for r in replies[:SECTION_LIMIT]],
        "follow_ups_due": follow_ups[:SECTION_LIMIT],
        "watchlist_changes": changes[:SECTION_LIMIT],
        "drafts_waiting": [_slim_draft(d) for d in drafts[:SECTION_LIMIT]],
        "stage_changes": [_slim_card(c) for c in stage_changes[:SECTION_LIMIT]],
        "silent_applications": silent[:SECTION_LIMIT],
        "pipeline": board_mod.summary(job_seeker_id),
    }
    digest.counts = {
        "new_opportunities": len(opportunities),
        "replies": len(replies),
        "follow_ups_due": len(follow_ups),
        "watchlist_changes": len(changes),
        "drafts_waiting": len(drafts),
        "stage_changes": len(stage_changes),
        "silent_applications": len(silent),
    }
    # The ladder reads the full lists, not the truncated ones.
    digest.action = choose_action(
        {
            "replies": [_slim_reply(r) for r in replies],
            "drafts_waiting": [_slim_draft(d) for d in drafts],
            "follow_ups_due": follow_ups,
            "stage_changes": [_slim_card(c) for c in stage_changes],
            "silent_applications": silent,
            "new_opportunities": opportunities,
            "watchlist_changes": changes,
        }
    )
    return digest


def _slim_reply(reply: dict) -> dict:
    return {
        "id": reply.get("id"),
        "from_address": reply.get("from_address"),
        "subject": reply.get("subject"),
        "received_at": reply.get("received_at"),
        "classification": reply.get("classification"),
        "handled": bool(reply.get("handled")),
        "company_name": reply.get("company_name"),
        "opportunity_title": reply.get("opportunity_title"),
    }


def _slim_draft(draft: dict) -> dict:
    return {
        "id": draft.get("id"),
        "kind": draft.get("kind"),
        "classification": draft.get("classification"),
        "subject": draft.get("subject"),
        "created_at": draft.get("created_at"),
    }


def _slim_card(card: dict) -> dict:
    return {
        "id": card.get("id"),
        "stage": card.get("stage"),
        "opportunity_id": card.get("opportunity_id"),
        "opportunity_title": card.get("opportunity_title"),
        "company_name": card.get("company_name"),
        "stage_changed_at": card.get("stage_changed_at"),
        "next_action": card.get("next_action"),
        "next_action_due": card.get("next_action_due"),
    }


# ---------------------------------------------------------------------------
# Rendering and delivery
# ---------------------------------------------------------------------------


def render_text(digest: Digest, *, display_name: str = "") -> str:
    """The digest as plain text - the in-app body and the e-mail body."""
    lines = [
        f"Dream Job weekly digest{f' for {display_name}' if display_name else ''}",
        f"{digest.period_start[:10]} to {digest.period_end[:10]}",
        "",
    ]
    if digest.action:
        lines += [
            "NEXT ACTION",
            f"  {digest.action.title}",
            f"  {digest.action.reason}",
            "",
        ]
    sections = [
        ("New opportunities", "new_opportunities", lambda o:
         f"{o.get('title')} - {o.get('company_name') or 'unknown company'}"
         + (f" (score {o['score']:.0f})" if o.get("score") is not None else "")
         + (" [timing favourable]" if o.get("timing_flag") else "")),
        ("Replies received", "replies", lambda r:
         f"{r.get('company_name') or r.get('from_address')} - "
         f"{(r.get('classification') or 'unclassified').replace('_', ' ')}"),
        ("Follow-ups due", "follow_ups_due", lambda f:
         f"{f.get('company_name') or f.get('recipient_email')} - due "
         f"{str(f.get('follow_up_due_at') or '')[:10]}"),
        ("Watchlist", "watchlist_changes", lambda w: str(w.get("title") or "")),
        ("Drafts waiting for you", "drafts_waiting", lambda d: str(d.get("subject") or "")),
    ]
    for heading, key, formatter in sections:
        items = digest.sections.get(key) or []
        total = digest.counts.get(key, len(items))
        if not total:
            continue
        lines.append(f"{heading.upper()} ({total})")
        lines += [f"  - {formatter(item)}" for item in items]
        if total > len(items):
            lines.append(f"  ... and {total - len(items)} more")
        lines.append("")

    pipeline = digest.sections.get("pipeline") or {}
    if pipeline.get("total"):
        stages = ", ".join(f"{k} {v}" for k, v in (pipeline.get("stages") or {}).items() if v)
        lines += [f"PIPELINE: {stages}", ""]
    if digest.empty:
        lines += [
            "Nothing new this week. That is information too: if your campaigns have "
            "finished collecting, the next move is yours rather than the system's.",
            "",
        ]
    lines.append(
        "Everything in this digest is advisory. Nothing is sent or decided on your "
        "behalf (NFR-305)."
    )
    return "\n".join(lines)


def store(digest: Digest, *, display_name: str = "") -> str:
    """Persist the digest and raise the in-app notification (FR-403)."""
    digest_id = repo.save_digest(
        digest.job_seeker_id,
        {
            "period_start": digest.period_start,
            "period_end": digest.period_end,
            "content": to_json(
                {"counts": digest.counts, "sections": digest.sections}
            ),
            "recommended_action": digest.action.title if digest.action else None,
            "recommended_action_detail": to_json(
                digest.action.as_dict() if digest.action else None
            ),
        },
    )
    digest.id = digest_id
    repo.notify(
        digest.job_seeker_id,
        {
            "kind": "digest",
            "title": f"Weekly digest - {digest.period_end[:10]}",
            "body": render_text(digest, display_name=display_name)[:4000],
            "payload": {
                "digest_id": digest_id,
                "counts": digest.counts,
                "recommended_action": digest.action.as_dict() if digest.action else None,
            },
            "severity": "action" if digest.action else "info",
            "dedup_key": f"digest:{digest.period_end[:10]}",
        },
    )
    return digest_id


def _send_email(digest: Digest, seeker: dict) -> str | None:
    """Optional e-mail delivery (FR-403).  Returns an error string, or ``None``.

    The mail slice owns sending, so this reaches for it lazily and treats its
    absence as "e-mail is not configured" rather than as a failure - the digest
    has already been delivered in-app by the time this runs.
    """
    address = str(seeker.get("email") or "").strip()
    if not address:
        return "the job seeker has no e-mail address on file"
    try:
        from dreamjob.mail.base import MailBackendError, OutgoingMessage  # noqa: PLC0415
        from dreamjob.mail.composer import new_message_id  # noqa: PLC0415
        from dreamjob.mail.dispatcher import resolve_backend  # noqa: PLC0415
    except ImportError:
        return "no mail backend is configured; the digest was delivered in-app only"
    try:
        backend, account = resolve_backend(digest.job_seeker_id)
        from_email = str(account.get("address") or "")
        message = OutgoingMessage(
            to_email=address,
            to_name=str(seeker.get("display_name") or "") or None,
            subject=f"Dream Job weekly digest - {digest.period_end[:10]}",
            body_text=render_text(digest, display_name=str(seeker.get("display_name") or "")),
            from_email=from_email,
            message_id=new_message_id(from_email),
            language=str(seeker.get("locale") or "en"),
        )
        # The seeker's own address, so none of the outreach guards apply: this
        # is a notification, not a message to a contact.
        result = backend.send(message)
        return None if result.ok else f"the mail backend reported: {result.status}"
    except MailBackendError as exc:
        log.info("Digest e-mail not sent: %s", exc)
        return str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 - the in-app digest already exists
        log.info("Digest e-mail not sent: %s", exc)
        return str(exc)[:300]


def generate(
    job_seeker_id: str,
    *,
    period_days: int = DEFAULT_PERIOD_DAYS,
    email: bool = False,
    end: datetime | None = None,
) -> dict[str, Any]:
    """Build, store and optionally e-mail one digest (FR-403)."""
    seeker = repo.seeker(job_seeker_id) or {}
    digest = build(job_seeker_id, period_days=period_days, end=end)
    digest_id = store(digest, display_name=str(seeker.get("display_name") or ""))

    email_error = None
    if email:
        email_error = _send_email(digest, seeker)
        repo.save_digest(
            job_seeker_id,
            {
                "period_start": digest.period_start,
                "period_end": digest.period_end,
                "content": to_json({"counts": digest.counts, "sections": digest.sections}),
                "recommended_action": digest.action.title if digest.action else None,
                "recommended_action_detail": to_json(
                    digest.action.as_dict() if digest.action else None
                ),
                "emailed_at": None if email_error else utcnow(),
                "email_error": email_error,
            },
        )
    record_audit(
        "digest.generated",
        "digest",
        digest_id,
        seeker_id=job_seeker_id,
        actor="system",
        detail={"counts": digest.counts, "emailed": email and not email_error},
    )
    return {**digest.as_dict(), "id": digest_id, "text": render_text(
        digest, display_name=str(seeker.get("display_name") or "")
    ), "email_error": email_error}


def due_seekers(
    *, period_days: int = DEFAULT_PERIOD_DAYS, now: datetime | None = None
) -> list[str]:
    """Active seekers whose last digest is older than the period (FR-403)."""
    moment = now or datetime.now(UTC)
    since = (moment - timedelta(days=period_days * 4)).isoformat(timespec="seconds")
    cutoff = (moment - timedelta(days=period_days)).isoformat(timespec="seconds")
    due: list[str] = []
    for seeker_id in repo.active_seeker_ids(since):
        latest = repo.latest_digest(seeker_id)
        if latest is None or str(latest.get("period_end") or "") <= cutoff:
            due.append(seeker_id)
    return due


def run_cycle(
    *, period_days: int = DEFAULT_PERIOD_DAYS, email: bool = False, limit: int = 100
) -> dict[str, Any]:
    """Generate every digest that is due.  One failure never stops the rest."""
    generated: list[str] = []
    errors: list[dict] = []
    for seeker_id in due_seekers(period_days=period_days)[:limit]:
        try:
            result = generate(seeker_id, period_days=period_days, email=email)
            generated.append(result["id"])
        except Exception as exc:  # noqa: BLE001 - one seeker must not stop the sweep
            log.exception("Digest generation failed for %s", seeker_id)
            errors.append({"job_seeker_id": seeker_id, "error": str(exc)[:300]})
    return {"generated": len(generated), "digest_ids": generated, "errors": errors}
