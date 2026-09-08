"""Outcomes and what they teach (FR-425, NFR-305, CR-404).

FR-425 asks for three things: record the outcome of each application together
with the variables that produced it, use those to adjust generation defaults
and scoring weights, and **report the learned effects transparently**.

The third is the hard one, and it is what shapes this module.  A job seeker
sends tens of applications, not thousands.  With an n of nine, "e-mails sent
on Tuesday get 40% more replies" is noise wearing a lab coat, and a system
that quietly re-tunes itself on it is worse than one that does not learn at
all.  So every effect reported here carries:

* the sample it rests on (``n``), split into the outcome and the rest;
* a Wilson score interval, which is the honest width of a rate measured on a
  small sample - a 2-of-5 success rate spans 12%-74% and looks like it;
* an ``evidence`` label - ``insufficient``, ``indicative`` or ``suggestive`` -
  and never ``significant``, because nothing at this scale is;
* whether the effect was actually applied, and if not, why not.

Only ``suggestive`` effects change anything, and even then they nudge rather
than replace: a learned generation default is a suggestion the generator may
take, and a scoring-weight adjustment is capped so that no amount of learning
can bury the seeker's own directives.  Applying it at all remains the seeker's
call (NFR-305).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from dreamjob.db.connection import from_json, to_json, utcnow
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

#: FR-425's outcome vocabulary.
OUTCOMES = ("reply", "interview", "offer", "rejection", "no_response")

#: The variables FR-425 names, plus the two the board can derive for free.
VARIABLES = (
    "email_style",
    "cv_template",
    "contact_type",
    "send_hour_band",
    "day_of_week",
    "opportunity_kind",
    "language",
    "has_introduction_path",
)

#: Below this, an effect is reported and never acted on.
MIN_SAMPLE_TO_REPORT = 3
MIN_SAMPLE_TO_APPLY = 12
#: A learned adjustment may move a scoring weight by at most this fraction.
MAX_WEIGHT_SHIFT = 0.15

_HOUR_BANDS = ((0, 8, "early"), (8, 11, "morning"), (11, 14, "midday"), (14, 17, "afternoon"))
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# ---------------------------------------------------------------------------
# Capturing the variables at send time
# ---------------------------------------------------------------------------


def _hour_band(stamp: str | None) -> str | None:
    if not stamp:
        return None
    try:
        hour = datetime.fromisoformat(stamp).hour
    except ValueError:
        return None
    for low, high, name in _HOUR_BANDS:
        if low <= hour < high:
            return name
    return "evening"


def _day_of_week(stamp: str | None) -> str | None:
    if not stamp:
        return None
    try:
        return _DAYS[datetime.fromisoformat(stamp).weekday()]
    except (ValueError, IndexError):
        return None


def _classify_contact(contact: dict | None) -> str:
    if contact is None:
        return "unknown"
    if contact.get("is_generic_mailbox"):
        return "generic_mailbox"
    department = str(contact.get("department") or "").lower()
    title = str(contact.get("role_title") or "").lower()
    if any(w in f"{department} {title}" for w in ("hr", "recruit", "talent", "people")):
        return "hr"
    if any(w in title for w in ("ceo", "founder", "director", "head", "chief", "vp", "manager")):
        return "hiring_manager"
    return "named_person"


def _email_style(body: str | None) -> str:
    """A coarse style label, computed the same way every time so it compares."""
    text = (body or "").strip()
    words = len(text.split())
    if not words:
        return "unknown"
    if words < 120:
        return "short"
    if words < 250:
        return "medium"
    return "long"


def capture_variables(job_seeker_id: str, application_package_id: str) -> dict[str, Any]:
    """Freeze what was true when this application went out (FR-425).

    Read once, at send time, and stored on the card: the package can be
    regenerated and the contact can be deleted by the retention sweep
    (NFR-303), and the learning record has to survive both.
    """
    row = repo.send_variables_source(job_seeker_id, application_package_id)
    if row is None:
        return {}
    has_intro = repo.has_introduction_path(job_seeker_id, row.get("opportunity_id"))
    sent_at = row.get("sent_at")
    return {
        "email_style": _email_style(row.get("email_body")),
        "cv_template": row.get("cv_template") or "unknown",
        "contact_type": _classify_contact(row),
        "send_hour_band": _hour_band(sent_at),
        "day_of_week": _day_of_week(sent_at),
        "opportunity_kind": row.get("opportunity_kind") or "unknown",
        "language": row.get("language") or "unknown",
        "has_introduction_path": "yes" if has_intro else "no",
        "email_validation": row.get("email_validation"),
        "mail_backend": row.get("backend"),
        "sent_at": sent_at,
        "captured_at": utcnow(),
    }


def record_send(job_seeker_id: str, application_package_id: str, opportunity_id: str) -> dict:
    """Called when an application is sent: opens the card with its variables."""
    from dreamjob.postapp import board  # noqa: PLC0415 - board calls back into capture_variables

    return board.ensure_card(
        job_seeker_id,
        application_package_id=application_package_id,
        opportunity_id=opportunity_id,
        variables=capture_variables(job_seeker_id, application_package_id),
    )


# ---------------------------------------------------------------------------
# Deriving the outcome
# ---------------------------------------------------------------------------


def outcome_of(card: dict, *, silence_days: int = 21, now: datetime | None = None) -> str | None:
    """The FR-425 outcome of one card, or ``None`` while it is still open.

    Order matters: an application that was rejected after an interview counts
    as having reached the interview *and* as a rejection, so the rates are
    computed per outcome rather than by bucketing each card once.
    """
    if card.get("outcome"):
        return "rejection" if card["outcome"] == "rejected" else str(card["outcome"])
    stage = str(card.get("stage") or "sent")
    if stage in ("offer",):
        return "offer"
    if stage == "interview":
        return "interview"
    if stage == "replied":
        return "reply"
    started = card.get("created_at")
    if started:
        try:
            age = (now or datetime.now(UTC)) - datetime.fromisoformat(started)
            if age.days >= silence_days:
                return "no_response"
        except ValueError:
            pass
    return None


def outcome_flags(card: dict, *, silence_days: int = 21) -> dict[str, bool]:
    """Which outcomes this card reached.  Several can be true at once."""
    final = outcome_of(card, silence_days=silence_days)
    return {
        "reply": bool(card.get("reached_replied_at")) or final in ("reply", "interview", "offer"),
        "interview": bool(card.get("reached_interview_at")) or final in ("interview", "offer"),
        "offer": bool(card.get("reached_offer_at")) or final == "offer",
        "rejection": final == "rejection",
        "no_response": final == "no_response",
        "closed": final is not None,
    }


def record_outcome(
    job_seeker_id: str,
    card_id: str,
    outcome: str,
    *,
    note: str | None = None,
    detail: dict | None = None,
) -> dict:
    """Close a card with an outcome (FR-425).  Always the seeker's act (NFR-305)."""
    from dreamjob.postapp import board  # noqa: PLC0415

    mapped = {"reply": "replied", "interview": "interview", "offer": "offer"}.get(outcome)
    if mapped:
        return board.transition(
            job_seeker_id, card_id, mapped, trigger="user", note=note
        ).as_dict()
    board_outcome = "rejected" if outcome == "rejection" else outcome
    return board.transition(
        job_seeker_id,
        card_id,
        "closed",
        trigger="user",
        outcome=board_outcome,
        note=note,
        detail=detail,
    ).as_dict()


# ---------------------------------------------------------------------------
# Effect sizes, reported with the sample they rest on
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval - the honest width of a rate on a small sample.

    The normal approximation is useless here: 0 of 4 is not "0% plus or minus
    0%".  Wilson gives 0%-49% for that, which is the point.
    """
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    denominator = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _evidence_label(n_with: int, n_without: int, low: float, high: float,
                    baseline: float) -> str:
    """How much this effect is worth, in words a person can act on."""
    if n_with < MIN_SAMPLE_TO_REPORT:
        return "insufficient"
    if n_with < MIN_SAMPLE_TO_APPLY or n_without < MIN_SAMPLE_TO_REPORT:
        return "indicative"
    # Only when the interval clears the baseline entirely is it worth acting on,
    # and even then it is called suggestive rather than significant.
    if low > baseline or high < baseline:
        return "suggestive"
    return "indicative"


@dataclass
class Effect:
    """One variable value, its outcome rate, and how much that is worth."""

    variable: str
    value: str
    outcome: str
    n: int
    successes: int
    rate: float
    interval: tuple[float, float]
    baseline: float
    baseline_n: int
    lift: float
    evidence: str
    applied: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["interval"] = [round(self.interval[0], 3), round(self.interval[1], 3)]
        data["rate"] = round(self.rate, 3)
        data["baseline"] = round(self.baseline, 3)
        data["lift"] = round(self.lift, 3)
        return data

    def sentence(self) -> str:
        """The effect as a person should read it - never without its n."""
        band = f"{self.interval[0]:.0%}-{self.interval[1]:.0%}"
        return (
            f"{self.variable.replace('_', ' ')} = {self.value}: "
            f"{self.successes} of {self.n} reached {self.outcome} "
            f"({self.rate:.0%}, plausible range {band}), against {self.baseline:.0%} "
            f"over {self.baseline_n} other applications. Evidence: {self.evidence}."
        )


@dataclass
class Analysis:
    """The whole FR-425 report, ready to be shown or stored."""

    job_seeker_id: str
    sample_size: int = 0
    closed: int = 0
    outcome_counts: dict[str, int] = field(default_factory=dict)
    effects: list[Effect] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    weight_adjustment: dict[str, Any] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    computed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_seeker_id": self.job_seeker_id,
            "sample_size": self.sample_size,
            "closed": self.closed,
            "outcome_counts": self.outcome_counts,
            "effects": [e.as_dict() for e in self.effects],
            "readable": [e.sentence() for e in self.effects],
            "defaults": self.defaults,
            "weight_adjustment": self.weight_adjustment,
            "caveats": self.caveats,
            "computed_at": self.computed_at,
        }


def analyse(
    job_seeker_id: str,
    *,
    outcome: str = "reply",
    silence_days: int = 21,
    limit: int = 1000,
) -> Analysis:
    """Effect of each recorded variable on one outcome (FR-425).

    Computed over cards that have *resolved* - an application sent yesterday
    tells us nothing yet, and counting it as "no reply" would make every
    recent choice look bad.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")

    rows = repo.outcome_rows(job_seeker_id, limit=limit)
    analysis = Analysis(job_seeker_id=job_seeker_id, computed_at=utcnow())
    analysis.sample_size = len(rows)

    resolved: list[tuple[dict, dict]] = []
    for card in rows:
        flags = outcome_flags(card, silence_days=silence_days)
        if not flags["closed"] and not flags["reply"]:
            continue          # still in flight: no information either way
        resolved.append((card, flags))
        for name, reached in flags.items():
            if name != "closed" and reached:
                analysis.outcome_counts[name] = analysis.outcome_counts.get(name, 0) + 1
    analysis.closed = len(resolved)

    if analysis.closed < MIN_SAMPLE_TO_REPORT:
        analysis.caveats.append(
            f"Only {analysis.closed} applications have resolved so far. Nothing here is "
            f"strong enough to change how material is generated; it is shown so the "
            f"pattern can be watched as the number grows."
        )
        return analysis

    total_successes = sum(1 for _, f in resolved if f[outcome])
    overall = total_successes / analysis.closed

    buckets: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for card, flags in resolved:
        variables = card.get("variables") or {}
        if isinstance(variables, str):
            variables = from_json(variables, {}) or {}
        for variable in VARIABLES:
            value = variables.get(variable)
            if value in (None, "", "unknown"):
                continue
            buckets[(variable, str(value))].append(bool(flags[outcome]))

    for (variable, value), results in sorted(buckets.items()):
        n_with = len(results)
        successes = sum(results)
        if n_with < MIN_SAMPLE_TO_REPORT:
            continue
        n_without = analysis.closed - n_with
        successes_without = total_successes - successes
        baseline = successes_without / n_without if n_without else overall
        rate = successes / n_with
        low, high = wilson_interval(successes, n_with)
        evidence = _evidence_label(n_with, n_without, low, high, baseline)
        analysis.effects.append(
            Effect(
                variable=variable,
                value=value,
                outcome=outcome,
                n=n_with,
                successes=successes,
                rate=rate,
                interval=(low, high),
                baseline=baseline,
                baseline_n=n_without,
                lift=rate - baseline,
                evidence=evidence,
                note=(
                    "Reported only; the sample is too small to act on."
                    if evidence != "suggestive"
                    else "Large enough to nudge the generation defaults."
                ),
            )
        )

    analysis.effects.sort(key=lambda e: (-abs(e.lift), -e.n))
    analysis.defaults = _defaults_from(analysis.effects)
    analysis.weight_adjustment = _weights_from(analysis.effects, job_seeker_id)
    analysis.caveats.extend(_caveats(analysis, overall))
    return analysis


def _caveats(analysis: Analysis, overall: float) -> list[str]:
    out = [
        f"Baseline {analysis.outcome_counts.get('reply', 0)} replies over "
        f"{analysis.closed} resolved applications ({overall:.0%}).",
        "Ranges are Wilson score intervals. Nothing at this sample size is statistically "
        "significant, and no effect here is claimed to be.",
        "Applications are not randomised: a variable may be standing in for the "
        "opportunities it was used on rather than causing anything.",
    ]
    if not any(e.evidence == "suggestive" for e in analysis.effects):
        out.append("No effect is strong enough to change a default yet.")
    return out


def _defaults_from(effects: list[Effect]) -> dict[str, Any]:
    """Generation defaults FR-425 would set - only from usable effects."""
    defaults: dict[str, Any] = {}
    for effect in effects:
        if effect.evidence != "suggestive" or effect.lift <= 0:
            continue
        if effect.variable in ("email_style", "cv_template", "language", "contact_type",
                               "send_hour_band", "day_of_week"):
            if effect.variable not in defaults:
                defaults[effect.variable] = {
                    "value": effect.value,
                    "because": effect.sentence(),
                    "n": effect.n,
                }
                effect.applied = True
    return defaults


def _weights_from(effects: list[Effect], job_seeker_id: str) -> dict[str, Any]:
    """A capped nudge to the scoring weights, with the reason attached (FR-285).

    Only two variables can move a weight, because only two of them are about
    the opportunity rather than about the letter: whether it was a real vacancy
    or a speculative opening, and whether a warm introduction existed.
    """
    current = (opp_repo.get_weights(job_seeker_id) or {}).get("weights") or {}
    proposed: dict[str, float] = {}
    reasons: list[str] = []

    mapping = {
        "opportunity_kind": ("score_plausibility", "speculative"),
        "has_introduction_path": ("score_reachability", "yes"),
    }
    for effect in effects:
        target = mapping.get(effect.variable)
        if not target or effect.evidence != "suggestive":
            continue
        column, trigger = target
        if effect.value != trigger:
            continue
        base = float(current.get(column, 1.0))
        shift = max(-MAX_WEIGHT_SHIFT, min(MAX_WEIGHT_SHIFT, effect.lift))
        proposed[column] = round(base * (1 + shift), 3)
        reasons.append(effect.sentence())
        effect.applied = True

    return {
        "current": current,
        "proposed": proposed,
        "reasons": reasons,
        "cap": MAX_WEIGHT_SHIFT,
        "note": (
            "Weights are nudged by at most "
            f"{MAX_WEIGHT_SHIFT:.0%} so that learning can never outweigh the seeker's "
            "own directives."
        ),
    }


# ---------------------------------------------------------------------------
# Storing and applying
# ---------------------------------------------------------------------------


def report(job_seeker_id: str, *, outcome: str = "reply", store: bool = True) -> dict[str, Any]:
    """Compute the analysis and store it, without changing anything (FR-425)."""
    analysis = analyse(job_seeker_id, outcome=outcome)
    if store:
        repo.save_learning(
            job_seeker_id,
            {
                "sample_size": analysis.closed,
                "effects": to_json([e.as_dict() for e in analysis.effects]),
                "defaults": to_json(analysis.defaults),
                "weight_adjustment": to_json(analysis.weight_adjustment),
                "notes": "\n".join(analysis.caveats),
                "applied": 0,
            },
        )
    return analysis.as_dict()


def apply_learning(job_seeker_id: str, *, outcome: str = "reply") -> dict[str, Any]:
    """Adopt the learned defaults and the capped weight nudge (FR-425, FR-285).

    Separate from :func:`report` on purpose: seeing what the numbers say and
    letting them change the system are two decisions, and the second is the
    seeker's (NFR-305).
    """
    analysis = analyse(job_seeker_id, outcome=outcome)
    if not analysis.defaults and not analysis.weight_adjustment.get("proposed"):
        repo.save_learning(
            job_seeker_id,
            {
                "sample_size": analysis.closed,
                "effects": to_json([e.as_dict() for e in analysis.effects]),
                "defaults": to_json({}),
                "weight_adjustment": to_json(analysis.weight_adjustment),
                "notes": "\n".join(analysis.caveats),
                "applied": 0,
            },
        )
        return {
            **analysis.as_dict(),
            "applied": False,
            "reason": "No effect reached the threshold at which it may change a default.",
        }

    proposed = analysis.weight_adjustment.get("proposed") or {}
    if proposed:
        current = (opp_repo.get_weights(job_seeker_id) or {}).get("weights") or {}
        opp_repo.save_weights(
            job_seeker_id,
            {**current, **proposed},
            learned_from={
                "source": "FR-425 outcome learning",
                "sample_size": analysis.closed,
                "reasons": analysis.weight_adjustment.get("reasons"),
                "cap": MAX_WEIGHT_SHIFT,
                "computed_at": analysis.computed_at,
            },
        )

    repo.save_learning(
        job_seeker_id,
        {
            "sample_size": analysis.closed,
            "effects": to_json([e.as_dict() for e in analysis.effects]),
            "defaults": to_json(analysis.defaults),
            "weight_adjustment": to_json(analysis.weight_adjustment),
            "notes": "\n".join(analysis.caveats),
            "applied": 1,
            "applied_at": utcnow(),
        },
    )
    record_audit(
        "outcomes.learning_applied",
        "outcome_learning",
        job_seeker_id,
        seeker_id=job_seeker_id,
        detail={
            "sample_size": analysis.closed,
            "defaults": list(analysis.defaults),
            "weights": list(proposed),
        },
    )
    return {**analysis.as_dict(), "applied": True}


def generation_defaults(job_seeker_id: str) -> dict[str, Any]:
    """The learned defaults, for the generators to read (FR-425).

    Returns the stored values only when they were explicitly applied - an
    analysis nobody adopted must not silently change what is generated.
    """
    record = repo.get_learning(job_seeker_id)
    if not record or not record.get("applied"):
        return {}
    return record.get("defaults") or {}


def current_report(job_seeker_id: str) -> dict[str, Any] | None:
    record = repo.get_learning(job_seeker_id)
    if not record:
        return None
    return {
        "sample_size": record.get("sample_size"),
        "effects": record.get("effects") or [],
        "defaults": record.get("defaults") or {},
        "weight_adjustment": record.get("weight_adjustment") or {},
        "applied": bool(record.get("applied")),
        "applied_at": record.get("applied_at"),
        "caveats": (record.get("notes") or "").splitlines(),
        "computed_at": record.get("computed_at"),
    }
