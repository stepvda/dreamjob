"""Durable source declines: when a refusal is a fact about the source (FR-182, IR-101).

Collection already tells a single page's answers apart: a 403 bot wall, a
robots.txt rule and a 451 legal refusal are ``blocked`` - a decision this
product made and can defend - while a 5xx, a timeout and a rate limit stay
``failed``.  What did not survive the campaign was the pattern.  One autopilot
run met **403 on all five Indeed requests and all ten Jobat requests**, recorded
every plan item ``blocked``, and left the campaign with nothing collected; the
next run planned the same five sources again, refused the same requests again,
counted the same errors again, and the operator had no list of what the product
had declined or any way to say "try this one now".

This module is the pattern's memory (table ``adapter_decline``, migration 154)
and the policy that decides when scattered refusals become a durable decline:

* **One run can decline a source.**  A source refused on every request it was
  issued, at least :data:`DECLINE_MIN_REFUSALS` times, is not "having a bad
  afternoon" - it answered nothing and refused everything, and a page more will
  not change that.
* **Two runs can decline a source.**  A refusal seen in two distinct campaigns
  is the same fact confirmed, however few requests each run made.
* **Terms are immediate.**  A catalogue row that prohibits automated access,
  or waits for an administrator's acknowledgement, is declined at plan time,
  before it is ever charged a request (IR-101).
* **A real failure vetoes it.**  If any request on the source actually failed
  - a 5xx, a timeout, a crash - the adapter is broken or the network is, and
  that stays a loud failure rather than being folded into a quiet decline.
  A source that also *collected* something is serving and is not declined on
  the strength of some refused pages.

Only ``declined_at IS NOT NULL AND acknowledged_at IS NULL`` excludes a source.
Clearing a decline is an explicit administrator act with an audit event, and
it resets the counters: the licence the administrator acknowledged is a second
chance, and the next refusal has to earn its own decline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import declines as repo

log = logging.getLogger(__name__)

#: Refusals of the decline kind, within one run, that make the source decline.
#: Three, not one: a single refused request on a page that also collected
#: records is not systematic, and a first refusal is already recorded as
#: evidence for the cross-run rule.
DECLINE_MIN_REFUSALS = 3

#: Distinct campaigns that saw a refusal before it is called persistent.
#: Keep in step with :data:`DECLINE_MIN_REFUSALS`: one run needs three, or two
#: runs need one each.
DECLINE_MIN_RUNS = 2

#: IR-101: reasons an administrator has to acknowledge in person.  Nothing in
#: the product clears these, and they are never subject to a request
#: threshold - terms prohibit access whether or not we have tried.
IR101_REASONS: frozenset[str] = frozenset({"terms", "unacknowledged"})

#: Stable reason codes.  A screen groups on these; ``detail`` carries the prose.
REASON_ROBOTS = "robots"
REASON_HTTP_403 = "http_403"
REASON_HTTP_451 = "http_451"
REASON_HTTP_401 = "http_401"
REASON_TERMS = "terms"
REASON_UNACKNOWLEDGED = "unacknowledged"

#: How each code reads in one line.  ``{detail}`` is the evidence sentence.
_REASON_LABELS: dict[str, str] = {
    REASON_ROBOTS: "robots.txt disallows this source",
    REASON_HTTP_403: "the site refuses automated access (HTTP 403)",
    REASON_HTTP_451: "the source refuses for legal reasons (HTTP 451)",
    REASON_HTTP_401: "every request is refused as unauthorised (HTTP 401)",
    REASON_TERMS: "the terms of service prohibit automated access",
    REASON_UNACKNOWLEDGED: "the terms await an administrator's acknowledgement",
}


class NoUsableSources(RuntimeError):
    """A plan or a launch with no source left that this product may run.

    Carries the same machine-readable ``blockers`` the message is built from,
    so a screen can list "declined by whom, why, since when" instead of a
    sentence.  Raised instead of launching a campaign that can only charge
    pages it has already been refused (FR-182, FR-186).
    """

    def __init__(self, message: str, *, blockers: list[dict] | None = None) -> None:
        super().__init__(message)
        self.blockers: list[dict] = list(blockers or [])


# ---------------------------------------------------------------------------
# Reading declines
# ---------------------------------------------------------------------------


def active_map() -> dict[str, dict]:
    """Declined adapters by key: what planning excludes and collection skips."""
    return repo.active_declines()


def is_declined(adapter_key: str) -> dict | None:
    return repo.get_active_decline(adapter_key)


def reason_label(reason: str | None) -> str:
    return _REASON_LABELS.get(str(reason or ""), "the source is declined")


def describe(row: dict) -> str:
    """One short factual line about a decline row, evidence included."""
    parts = [reason_label(row.get("reason"))]
    if row.get("detail"):
        parts.append(str(row["detail"]))
    refusals = int(row.get("refused_count") or 0)
    runs = int(row.get("run_count") or 0)
    if refusals or runs:
        parts.append(f"{refusals} refusal(s) over {runs} run(s)")
    return " - ".join(parts)


def rejection_reason(row: dict) -> str:
    """Why planning leaves a declined source out, in words a user can act on."""
    return (
        f"declined: {describe(row)} (FR-182, IR-101). "
        "An administrator has to re-enable it before it is planned again."
    )


def blockers(rows: list[dict] | dict[str, dict]) -> list[dict]:
    """The machine-readable blocker list a failure carries."""
    values = rows.values() if isinstance(rows, dict) else rows
    return [
        {
            "adapter_key": row.get("adapter_key"),
            "reason": row.get("reason"),
            "detail": row.get("detail"),
            "evidence_url": row.get("evidence_url"),
            "declined_at": row.get("declined_at"),
            "refused_count": int(row.get("refused_count") or 0),
            "run_count": int(row.get("run_count") or 0),
        }
        for row in values
    ]


def no_sources_message(rows: list[dict] | dict[str, dict], *, what: str = "plan") -> str:
    """The precise blocker: every declined source, and why it is declined."""
    values = list(rows.values()) if isinstance(rows, dict) else list(rows)
    listed = "; ".join(
        f"{row.get('adapter_key')} ({describe(row)})" for row in values
    ) or "no source at all"
    return (
        f"No usable source to {what}: every candidate is declined and none may be "
        f"charged a request until an administrator re-enables it - {listed}"
    )


# ---------------------------------------------------------------------------
# Deciding that a refusal is a decline
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """What one settled plan item proved about its adapter's usability."""

    adapter_key: str
    reason: str
    detail: str
    evidence_url: str | None
    requests: int
    refusals: int


def _reason_of(outcome: Any) -> str | None:
    """The stable code for this outcome's refusals, or ``None``.

    Robots first: a refused rule is about the whole source and outranks one
    URL's status.  Then the HTTP spelling, then 401, which collection keeps
    classified as a failure but which is evidence of a declined source all the
    same when it is all a source ever says.
    """
    if int(getattr(outcome, "robots_blocked", 0) or 0) > 0:
        return REASON_ROBOTS
    status = getattr(outcome, "blocked_status", None)
    if status == 451:
        return REASON_HTTP_451
    if status == 403:
        return REASON_HTTP_403
    if int(getattr(outcome, "unauthorized", 0) or 0) > 0:
        return REASON_HTTP_401
    return None


def observation_of(outcome: Any, *, adapter_key: str) -> Observation | None:
    """Read one item's outcome as decline evidence, or ``None``.

    ``None`` is the important answer: a source that collected anything, or
    that had a genuine failure on the way, is not evidence of a declined
    source.  What is left is a source that was refused and refused, and the
    counters say how often.
    """
    refusals = int(getattr(outcome, "blocked", 0) or 0) + int(
        getattr(outcome, "unauthorized", 0) or 0
    )
    if refusals <= 0 or int(getattr(outcome, "written", 0) or 0):
        return None
    # A 401 is classified as a failure, because it is our own defect to fix -
    # but it is the *only* failure here, so it does not veto the decline.  Any
    # other failure does: a 5xx or a timeout is the adapter being broken, and
    # hiding it in a durable decline is the failure-hiding bug in a new coat.
    other_failures = int(getattr(outcome, "failed_requests", 0) or 0) - int(
        getattr(outcome, "unauthorized", 0) or 0
    )
    if other_failures > 0:
        return None
    reason = _reason_of(outcome)
    if reason is None:
        return None
    detail = str(
        getattr(outcome, "block_reason", None)
        or getattr(outcome, "last_error", None)
        or reason_label(reason)
    )
    return Observation(
        adapter_key=adapter_key,
        reason=reason,
        detail=detail,
        evidence_url=getattr(outcome, "evidence_url", None),
        requests=int(getattr(outcome, "requests", 0) or 0),
        refusals=refusals,
    )


def should_decline(
    observation: Observation,
    existing: dict | None,
    *,
    campaign_id: str | None,
) -> tuple[bool, int]:
    """Is this observation persistent enough to decline the adapter?

    Returns ``(declined, run_count)`` where ``run_count`` is the number of
    distinct campaigns that have evidenced a refusal, this one included.
    """
    if observation.reason in IR101_REASONS:
        return True, int((existing or {}).get("run_count") or 0) + 1
    previous_run = str((existing or {}).get("last_campaign_id") or "")
    new_run = bool(campaign_id) and previous_run != campaign_id
    runs = int((existing or {}).get("run_count") or 0) + (1 if new_run else 0)
    total_refusals = int((existing or {}).get("refused_count") or 0) + observation.refusals
    decline_now = (
        observation.refusals >= DECLINE_MIN_REFUSALS
        or total_refusals >= DECLINE_MIN_REFUSALS
        or runs >= DECLINE_MIN_RUNS
    )
    return decline_now, runs


def observe(
    adapter_key: str,
    outcome: Any,
    campaign_id: str | None,
    *,
    had_success: bool = False,
) -> dict | None:
    """Record a settled item's refusals and decline the adapter if persistent.

    ``had_success`` is the adapter's answer for the *whole run*: if any plan
    item of it wrote a record, the source is serving and the refused targets
    are target-specific - a per-tenant ATS vendor with a few dead boards is
    working, and declining the vendor would stop every live tenant with it.
    Evidence is not accumulated for a source that worked.

    Never raises: the memory is an optimisation of the next campaign and must
    not be able to fail the one that is running.
    """
    if had_success:
        return None
    try:
        observation = observation_of(outcome, adapter_key=adapter_key)
        if observation is None:
            return None
        existing = repo.get_decline(adapter_key)
        declined, runs = should_decline(observation, existing, campaign_id=campaign_id)
        was_active = bool((existing or {}).get("declined_at")) and not (
            existing or {}
        ).get("acknowledged_at")
        row = repo.record_observation(
            adapter_key,
            reason=observation.reason,
            detail=observation.detail,
            evidence_url=observation.evidence_url,
            requests=observation.requests,
            refusals=observation.refusals,
            campaign_id=campaign_id,
            declined=declined,
        )
        if declined and not was_active:
            campaign_repo.record_audit(
                "source.declined",
                entity_type="source_catalogue",
                entity_id=adapter_key,
                detail={
                    "reason": observation.reason,
                    "detail": observation.detail,
                    "evidence_url": observation.evidence_url,
                    "refusals": observation.refusals,
                    "requests": observation.requests,
                    "runs": runs,
                    "campaign_id": campaign_id,
                },
            )
            log.warning(
                "Declined source %s: %s (FR-182, IR-101). Planning will not select it "
                "until an administrator re-enables it.",
                adapter_key,
                describe(row),
            )
        return row
    except Exception:  # noqa: BLE001 - a memory must never break the run that writes it
        log.exception("Could not record the decline evidence for %s", adapter_key)
        return None


# ---------------------------------------------------------------------------
# IR-101: terms declines come from the catalogue, at plan time
# ---------------------------------------------------------------------------


def record_terms_decline(
    adapter_key: str, *, reason: str, detail: str, evidence_url: str | None = None
) -> dict | None:
    """Decline a source whose catalogue row says we may not read it (IR-101).

    Recorded at plan time, before a request is issued, and never subject to a
    threshold or an automatic expiry: only an administrator's acknowledgement
    clears it.
    """
    try:
        existing = repo.get_active_decline(adapter_key)
        if existing is not None:
            return existing
        return repo.record_observation(
            adapter_key,
            reason=reason if reason in IR101_REASONS else REASON_TERMS,
            detail=detail,
            evidence_url=evidence_url,
            requests=0,
            refusals=0,
            campaign_id=None,
            declined=True,
        )
    except Exception:  # noqa: BLE001 - catalogue bookkeeping never fails a plan
        log.exception("Could not record the terms decline for %s", adapter_key)
        return None


def clear(adapter_key: str, *, actor: str | None = None, note: str | None = None) -> dict | None:
    """Administrator's explicit re-enable of a declined source (FR-363)."""
    row = repo.acknowledge_decline(adapter_key, actor=actor, note=note)
    if row is not None:
        campaign_repo.record_audit(
            "admin.source_decline_cleared",
            actor=actor,
            entity_type="source_catalogue",
            entity_id=adapter_key,
            detail={
                "reason": row.get("reason"),
                "detail": row.get("detail"),
                "evidence_url": row.get("evidence_url"),
                "note": note,
                "acknowledged_at": row.get("acknowledged_at"),
            },
        )
    return row
