"""Opportunity scoring, the dream-job fit meter and weight learning.

Requirements: FR-281 (configurable weighted score), FR-282 (explainable),
FR-283 (comparable), FR-284 (manual order wins), FR-285 (learn from feedback),
FR-383 (dream-job fit meter), NFR-305 and CR-405 (advisory only).

**Every sub-score that can be computed, is computed.**  Skill overlap, seniority
band distance, location distance against the directive radius, size-band and
stage membership, financial ratios, contact validation state - all of these are
arithmetic over data already in the database.  Only two things go to the LLM:
the semantic match between the opportunity and the dream job model, and the
one-paragraph rationale (FR-282).

That split is deliberate, and it is what makes the ranking usable:

* **Reproducible** - re-running the scorer on unchanged data produces the same
  numbers, so a seeker who re-ranks after a re-collection can trust that a
  moved opportunity moved because the data moved.
* **Cheap** - one LLM call per opportunity instead of seven, which is what
  keeps a 1 000-vacancy campaign inside the NFR-104 token budget.
* **Explainable** - a deterministic sub-score can be traced to the exact
  comparison that produced it, and each one carries its reasons in
  ``score_detail`` (FR-282).  An LLM number can only be explained by asking it
  again.

The score is advisory (NFR-305, CR-405).  Nothing here decides anything: it
orders a list, and every decision - to keep, to reject, to re-order, to apply -
stays with the job seeker (FR-284).

Scores are stored on a 0-100 scale in ``opportunity.score`` and the
``score_*`` columns.  ``opportunity.plausibility`` keeps FR-262's own 0-1
scale; ``score_plausibility`` is its 0-100 projection.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import opportunities as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError, redact
from dreamjob.pipeline import compensation as comp_mod
from dreamjob.pipeline import directives as dir_mod
from dreamjob.pipeline.enrichment import load_prompt
from dreamjob.pipeline.geocode import haversine_km
from dreamjob.pipeline.opportunities import KIND_SPECULATIVE

log = logging.getLogger(__name__)

PROMPT_NAME = "score_rationale"

#: FR-281's component list, and the default weights.  A job seeker's own
#: weights live in ``scoring_weights`` and override these (FR-285).
DEFAULT_WEIGHTS: dict[str, float] = {
    "profile_fit": 0.22,
    "dream_fit": 0.22,
    "directive_fit": 0.18,
    "company": 0.14,
    "compensation": 0.10,
    "plausibility": 0.08,
    "reachability": 0.06,
}

COMPONENTS: tuple[str, ...] = tuple(DEFAULT_WEIGHTS)

#: Column each component writes to.
COMPONENT_COLUMNS: dict[str, str] = {
    "profile_fit": "score_profile_fit",
    "dream_fit": "score_dream_fit",
    "directive_fit": "score_directive_fit",
    "company": "score_company",
    "compensation": "score_compensation",
    "plausibility": "score_plausibility",
    "reachability": "score_reachability",
}

#: FR-285 keeps learned weights inside this band of the default, so feedback
#: tilts the ranking without ever letting one component decide it alone.
WEIGHT_FLOOR_FACTOR = 0.3
WEIGHT_CEILING_FACTOR = 2.5
MIN_FEEDBACK_PER_SIDE = 3

TRAJECTORY_SCORES: dict[str, float] = {
    "growing": 1.0,
    "stable": 0.65,
    "volatile": 0.45,
    "restructuring": 0.30,
    "declining": 0.15,
}

_WORD_RE = re.compile(r"[a-z0-9+#.]{3,}")
_STOPWORDS = frozenset(
    {
        "and", "the", "with", "for", "from", "into", "our", "you", "your", "are", "will",
        "who", "that", "this", "have", "has", "not", "all", "can", "new", "job", "role",
        "team", "work", "working", "years", "experience", "company", "about", "een",
        "van", "met", "voor", "les", "des", "pour", "dans", "nous", "vous",
    }
)


def _tokens(*texts: Any) -> set[str]:
    joined = " ".join(str(t) for t in texts if t).lower()
    return {w for w in _WORD_RE.findall(joined) if w not in _STOPWORDS}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _string_leaves(value: Any, depth: int = 0) -> list[str]:
    """Every string *value* inside a nested block, without its keys.

    ``company.values_culture`` is a JSON object whose keys (``stated_values``,
    ``job_ad_language``) would otherwise match a dream-job cue by accident and
    turn "nothing is recorded" into a match.
    """
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _string_leaves(v, depth + 1)]
    if isinstance(value, list):
        return [s for v in value for s in _string_leaves(v, depth + 1)]
    return []


# ---------------------------------------------------------------------------
# Sub-score container
# ---------------------------------------------------------------------------


@dataclass
class SubScore:
    """One component of FR-281, with the reasons FR-282 shows next to it."""

    value: float | None
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    method: str = "deterministic"

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else round(self.value, 4),
            "score": None if self.value is None else round(self.value * 100, 1),
            "reasons": self.reasons,
            "detail": self.detail,
            "method": self.method,
        }


def _weighted(parts: list[tuple[float, float | None]]) -> float | None:
    """Combine ``(weight, value)`` pairs, renormalising over what is known."""
    known = [(w, v) for w, v in parts if v is not None and w > 0]
    if not known:
        return None
    total = sum(w for w, _ in known)
    return sum(w * v for w, v in known) / total


# ---------------------------------------------------------------------------
# Weights (FR-281 configurable, FR-285 learned)
# ---------------------------------------------------------------------------


def normalise_weights(weights: dict[str, float] | None) -> dict[str, float]:
    """Fill in missing components from the defaults and normalise to sum 1."""
    merged = {**DEFAULT_WEIGHTS}
    for key, value in (weights or {}).items():
        if key in DEFAULT_WEIGHTS:
            try:
                merged[key] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    total = sum(merged.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in merged.items()}


def load_weights(job_seeker_id: str) -> dict[str, float]:
    stored = repo.get_weights(job_seeker_id)
    return normalise_weights((stored or {}).get("weights"))


def save_weights(
    job_seeker_id: str, weights: dict[str, float], learned_from: Any = None
) -> dict[str, float]:
    normalised = normalise_weights(weights)
    repo.save_weights(job_seeker_id, normalised, learned_from)
    return normalised


# ---------------------------------------------------------------------------
# Scoring context
# ---------------------------------------------------------------------------


@dataclass
class ScoringContext:
    """Everything the scorer reads, loaded once per campaign rather than per row."""

    job_seeker_id: str
    campaign_id: str
    weights: dict[str, float]
    directives: dir_mod.DirectiveSet | None = None
    composite: dict[str, Any] = field(default_factory=dict)
    dream: dict[str, Any] = field(default_factory=dict)
    profile_skills: set[str] = field(default_factory=set)
    do_not_disclose: set[str] = field(default_factory=set)
    language: str = "en"
    _companies: dict[str, dict] = field(default_factory=dict)
    _company_scores: dict[str, SubScore] = field(default_factory=dict)

    def company(self, company_id: str | None) -> dict | None:
        if not company_id:
            return None
        if company_id not in self._companies:
            self._companies[company_id] = repo.get_company(company_id) or {}
        return self._companies[company_id] or None


def _block(row: dict | None, key: str) -> Any:
    if not row:
        return None
    return from_json(row.get(key), None)


def build_context(campaign: dict, *, language: str | None = None) -> ScoringContext:
    inputs = campaign_repo.load_planning_inputs(campaign)
    directive_row = inputs.get("directives")
    composite = inputs.get("composite_profile") or {}
    dream = inputs.get("dream_job_model") or {}
    seeker_id = campaign["job_seeker_id"]

    skills = {
        (s.get("normalised_label") or "").strip().lower()
        for s in repo.profile_skills(seeker_id, campaign.get("profile_version_id"))
        if s.get("normalised_label")
    }
    # Composite competencies are skills too, and a campaign may run before the
    # skill table has been rebuilt.
    for key in ("core_competencies", "adjacent_competencies"):
        for statement in _block(composite, key) or []:
            text = statement.get("text") if isinstance(statement, dict) else statement
            if text:
                skills.add(str(text).strip().lower())

    return ScoringContext(
        job_seeker_id=seeker_id,
        campaign_id=campaign["id"],
        weights=load_weights(seeker_id),
        directives=dir_mod.coerce_directive_set(directive_row) if directive_row else None,
        composite={
            "row": composite,
            "seniority": _block(composite, "seniority") or composite.get("seniority"),
            "domains": _block(composite, "domains") or [],
            "core_competencies": _block(composite, "core_competencies") or [],
            "adjacent_competencies": _block(composite, "adjacent_competencies") or [],
            "achievements": _block(composite, "achievements") or [],
            "narrative": composite.get("narrative"),
        },
        dream={
            "row": dream,
            "statement": dream.get("statement"),
            "target_roles": _block(dream, "target_roles") or [],
            "role_families": _block(dream, "role_families") or [],
            "responsibilities": _block(dream, "responsibilities") or [],
            "company_characteristics": _block(dream, "company_characteristics") or [],
            "culture_values": _block(dream, "culture_values") or [],
            "deal_breakers": _block(dream, "deal_breakers") or [],
            "implicit_preferences": _block(dream, "implicit_preferences") or [],
        },
        profile_skills=skills,
        do_not_disclose=set(inputs.get("do_not_disclose") or set()),
        language=language or "en",
    )


# ---------------------------------------------------------------------------
# FR-281: profile fit (skills, seniority, domain)
# ---------------------------------------------------------------------------


def _seniority_rank(value: Any) -> int | None:
    if not value:
        return None
    text = value if isinstance(value, str) else str(
        (value or {}).get("level") or (value or {}).get("text") or ""
    )
    text = text.strip().lower().replace(" ", "_").replace("-", "_")
    for level, rank in dir_mod.SENIORITY_RANK.items():
        if level.value == text:
            return rank
    for level, rank in dir_mod.SENIORITY_RANK.items():
        if level.value in text:
            return rank
    return None


def _skill_overlap(required: list[str] | None, held: set[str]) -> float | None:
    labels = [str(s).strip().lower() for s in (required or []) if str(s).strip()]
    if not labels:
        return None
    hits = 0
    for label in labels:
        if label in held or any(label in h or h in label for h in held):
            hits += 1
    return hits / len(labels)


def profile_fit(opportunity: dict, ctx: ScoringContext) -> SubScore:
    """FR-281: skills, seniority and domain overlap with the composite profile."""
    reasons: list[str] = []
    held = ctx.profile_skills

    required = opportunity.get("required_skills") or []
    desirable = opportunity.get("desirable_skills") or []
    required_overlap = _skill_overlap(required, held)
    desirable_overlap = _skill_overlap(desirable, held)
    if required_overlap is not None:
        reasons.append(
            f"{round(required_overlap * len(required))} of {len(required)} required skills "
            "are evidenced in the profile"
        )
    elif not held:
        reasons.append("no normalised profile skills available to compare against")
    else:
        reasons.append("the posting lists no required skills")

    seeker_rank = _seniority_rank(ctx.composite.get("seniority"))
    if seeker_rank is None and ctx.directives is not None:
        low, high = ctx.directives.job_content.seniority_range()
        seeker_rank = (low + high) // 2
    opportunity_rank = _seniority_rank(opportunity.get("seniority"))
    seniority_score: float | None = None
    if seeker_rank is not None and opportunity_rank is not None:
        distance = abs(seeker_rank - opportunity_rank)
        seniority_score = _clamp(1.0 - distance / 4.0)
        reasons.append(
            "seniority matches" if distance == 0
            else f"seniority is {distance} band(s) from the profile's level"
        )

    company = ctx.company(opportunity.get("company_id")) or {}
    domain_tokens = _tokens(
        *[d.get("text") if isinstance(d, dict) else d for d in ctx.composite.get("domains") or []]
    )
    domain_score: float | None = None
    if domain_tokens:
        target = _tokens(
            opportunity.get("title"),
            (opportunity.get("description") or "")[:2000],
            company.get("business_summary"),
            company.get("sector_codes"),
        )
        if target:
            hits = domain_tokens & target
            domain_score = _clamp(len(hits) / max(min(len(domain_tokens), 8), 1))
            if hits:
                reasons.append(
                    "domain overlap on " + ", ".join(sorted(hits)[:4])
                )
            else:
                reasons.append("no overlap with the profile's recorded domains")

    value = _weighted(
        [
            (0.50, required_overlap),
            (0.15, desirable_overlap),
            (0.20, seniority_score),
            (0.15, domain_score),
        ]
    )
    return SubScore(
        value=value,
        reasons=reasons,
        detail={
            "required_skill_overlap": required_overlap,
            "desirable_skill_overlap": desirable_overlap,
            "seniority_score": seniority_score,
            "seniority_rank_seeker": seeker_rank,
            "seniority_rank_opportunity": opportunity_rank,
            "domain_score": domain_score,
        },
    )


# ---------------------------------------------------------------------------
# FR-281: directive fit (location, work arrangement, contract, company type)
# ---------------------------------------------------------------------------


def _location_score(
    opportunity: dict, directives: dir_mod.DirectiveSet
) -> tuple[float | None, str]:
    location = directives.location
    if not location.areas and not location.countries and location.home_location is None:
        return None, "no location directives"

    country = (opportunity.get("country") or "").upper()
    allowed = {c.upper() for c in location.countries}
    if location.willing_to_relocate:
        allowed |= {c.upper() for c in location.relocation_countries}

    lat, lon = opportunity.get("latitude"), opportunity.get("longitude")
    if lat is not None and lon is not None:
        best: tuple[float, float] | None = None
        for area in location.areas:
            if not area.is_geocoded:
                continue
            distance = haversine_km(area.latitude, area.longitude, lat, lon)
            radius = max(area.radius_km, 1.0)
            if best is None or distance < best[0]:
                best = (distance, radius)
        if best is not None:
            distance, radius = best
            if distance <= radius:
                return 1.0, f"{distance:.0f} km from a target area (radius {radius:.0f} km)"
            # Beyond the radius the score decays over one further radius.
            return (
                _clamp(1.0 - (distance - radius) / radius),
                f"{distance:.0f} km away, {distance - radius:.0f} km beyond the radius",
            )

    if (opportunity.get("work_arrangement") or "") == "remote":
        return 0.9, "remote, so the office distance barely matters"
    if allowed and country and country in allowed:
        return 0.75, f"in a target country ({country}), exact place unknown"
    if allowed and country:
        return 0.0, f"in {country}, which is not a target country"
    return 0.5, "location not precise enough to place"


def _work_arrangement_score(
    opportunity: dict, directives: dir_mod.DirectiveSet
) -> tuple[float | None, str]:
    wanted = {a.value for a in directives.work_arrangement.arrangements}
    if not wanted:
        return None, "no work-arrangement directive"
    actual = opportunity.get("work_arrangement")
    if not actual:
        # Not stated is not a mismatch (CR-405); it is a question to ask.
        return 0.5, "work arrangement not stated"
    if actual not in wanted:
        return 0.0, f"{actual} is not among the accepted arrangements"
    minimum = directives.work_arrangement.min_remote_days
    days = opportunity.get("remote_days")
    if minimum is not None and days is not None and days < minimum:
        return 0.6, f"{days} remote day(s) against a minimum of {minimum}"
    return 1.0, f"{actual} matches the directive"


def _contract_score(
    opportunity: dict, directives: dir_mod.DirectiveSet
) -> tuple[float | None, str]:
    wanted = {c.value for c in directives.work_arrangement.contract_types}
    actual = opportunity.get("contract_type")
    fte = opportunity.get("fte_percentage")
    parts: list[tuple[float, float | None]] = []
    notes: list[str] = []
    if wanted:
        if not actual:
            parts.append((1.0, 0.5))
            notes.append("contract type not stated")
        elif actual in wanted:
            parts.append((1.0, 1.0))
            notes.append(f"{actual} contract matches")
        else:
            parts.append((1.0, 0.0))
            notes.append(f"{actual} contract is not accepted")
    low = directives.work_arrangement.fte_percentage_min
    high = directives.work_arrangement.fte_percentage_max
    if fte is not None and (low is not None or high is not None):
        inside = (low is None or fte >= low) and (high is None or fte <= high)
        parts.append((0.5, 1.0 if inside else 0.2))
        notes.append(f"{fte}% FTE {'is' if inside else 'is not'} within the directive")
    if not parts:
        return None, "no contract directive"
    return _weighted(parts), "; ".join(notes)


def _company_type_score(
    company: dict | None, directives: dir_mod.DirectiveSet
) -> tuple[float | None, str]:
    if company is None:
        return None, "company not profiled"
    wanted = directives.company_type
    parts: list[tuple[float, float | None]] = []
    notes: list[str] = []

    def check(label: str, actual: Any, allowed: list[Any], weight: float) -> None:
        if not allowed:
            return
        values = {str(getattr(a, "value", a)) for a in allowed}
        if not actual:
            parts.append((weight, 0.5))
            notes.append(f"{label} unknown")
            return
        hit = str(actual) in values
        parts.append((weight, 1.0 if hit else 0.0))
        notes.append(f"{label} {actual} {'matches' if hit else 'is outside'} the directive")

    check("size band", company.get("size_band"), wanted.size_bands, 1.0)
    check("stage", company.get("stage"), wanted.stages, 1.0)
    check("ownership", company.get("ownership"), wanted.ownerships, 0.6)
    check("trajectory", company.get("trajectory"), wanted.trajectories, 0.8)

    if not parts:
        return None, "no company-type directive"
    return _weighted(parts), "; ".join(notes)


def _job_content_score(
    opportunity: dict, directives: dir_mod.DirectiveSet
) -> tuple[float | None, str]:
    """The FR-142 job-content directives, checked against the normalised record."""
    content = directives.job_content
    parts: list[tuple[float, float | None]] = []
    notes: list[str] = []

    titles = [t.lower() for t in content.all_titles() if t.strip()]
    if titles:
        title = (opportunity.get("title") or "").lower()
        title_tokens = _tokens(title)
        best = 0.0
        for wanted in titles:
            wanted_tokens = _tokens(wanted)
            if not wanted_tokens:
                continue
            overlap = len(wanted_tokens & title_tokens) / len(wanted_tokens)
            best = max(best, 1.0 if wanted in title else overlap)
        parts.append((1.0, best))
        notes.append(
            "title matches a target title" if best >= 0.99
            else f"title overlaps {best:.0%} with the closest target title"
        )

    families = [f.lower() for f in content.function_families if f.strip()]
    if families:
        family = (opportunity.get("function_family") or "").lower()
        hit = any(f in family or family in f for f in families if family)
        parts.append((0.7, 1.0 if hit else (0.4 if not family else 0.0)))
        notes.append("function family matches" if hit else "function family differs")

    must = [s.lower() for s in content.must_have_skills if s.strip()]
    if must:
        text = " ".join(
            str(opportunity.get(k) or "")
            for k in ("title", "description")
        ).lower() + " " + " ".join(
            str(s).lower() for s in (opportunity.get("required_skills") or [])
        )
        hits = [s for s in must if s in text]
        parts.append((0.8, len(hits) / len(must)))
        notes.append(f"{len(hits)} of {len(must)} must-have subjects appear in the role")

    if not parts:
        return None, "no job-content directive"
    return _weighted(parts), "; ".join(notes)


def directive_fit(opportunity: dict, ctx: ScoringContext) -> SubScore:
    """FR-281: location, work arrangement, contract and company type.

    The job-content directives (FR-142) are folded in as a fifth part: they are
    directives too, and leaving them out would score a perfectly-placed,
    perfectly-arranged wrong job as a perfect directive fit.
    """
    if ctx.directives is None:
        return SubScore(None, ["no directive set on this campaign"])
    directives = ctx.directives
    company = ctx.company(opportunity.get("company_id"))

    location_score, location_note = _location_score(opportunity, directives)
    arrangement_score, arrangement_note = _work_arrangement_score(opportunity, directives)
    contract_score, contract_note = _contract_score(opportunity, directives)
    company_score, company_note = _company_type_score(company, directives)
    content_score, content_note = _job_content_score(opportunity, directives)

    value = _weighted(
        [
            (0.28, location_score),
            (0.18, arrangement_score),
            (0.12, contract_score),
            (0.22, company_score),
            (0.20, content_score),
        ]
    )
    return SubScore(
        value=value,
        reasons=[location_note, arrangement_note, contract_note, company_note, content_note],
        detail={
            "location": location_score,
            "work_arrangement": arrangement_score,
            "contract": contract_score,
            "company_type": company_score,
            "job_content": content_score,
        },
    )


# ---------------------------------------------------------------------------
# FR-281: company attractiveness
# ---------------------------------------------------------------------------


def _signal_strength(company_id: str) -> tuple[float, int]:
    """Recent hiring signals, decayed - the FR-225 input to attractiveness."""
    now = datetime.now(UTC)
    total = 0.0
    counted = 0
    for signal in repo.company_signals(company_id, limit=40):
        stamp = signal.get("occurred_at") or signal.get("collected_at")
        try:
            when = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        days = max((now - when).days, 0)
        if days > 540:
            continue
        total += float(signal.get("strength") or 0.5) * math.exp(-days / 180.0)
        counted += 1
    return total, counted


def company_attractiveness(
    company_id: str | None, *, company: dict | None = None
) -> SubScore:
    """FR-281: financial trajectory, ability to pay, investment capacity, signals.

    Also used by :mod:`dreamjob.pipeline.speculative` as the deterministic prior
    that decides which companies keep their generation call when the token
    budget is nearly exhausted (NFR-104).
    """
    if not company_id:
        return SubScore(None, ["no company linked"])
    company = company if company is not None else (repo.get_company(company_id) or {})
    analysis = repo.company_analysis(company_id) or {}
    reasons: list[str] = []

    trajectory = analysis.get("trajectory") or company.get("trajectory")
    trajectory_score = TRAJECTORY_SCORES.get(str(trajectory or "").lower())
    if trajectory_score is not None:
        reasons.append(f"trajectory is {trajectory}")

    ability = analysis.get("ability_to_pay")
    ability_score = None if ability is None else _clamp(float(ability) / 100)
    if ability_score is not None:
        reasons.append(f"ability to pay {int(ability)}/100")

    capacity = analysis.get("investment_capacity")
    capacity_score = None if capacity is None else _clamp(float(capacity) / 100)
    if capacity_score is not None:
        reasons.append(f"investment capacity {int(capacity)}/100")

    strength, counted = _signal_strength(company_id)
    signal_score = _clamp(strength / 2.5) if counted else None
    if counted:
        reasons.append(f"{counted} recent hiring signal(s)")

    rating, _themes = comp_mod.employer_signal(company_id)
    rating_score = None if rating is None else _clamp(rating / 5.0)
    if rating_score is not None:
        reasons.append(f"employer review rating {rating}/5 (advisory only, FR-265)")

    value = _weighted(
        [
            (0.30, trajectory_score),
            (0.25, ability_score),
            (0.20, capacity_score),
            (0.25, signal_score),
            (0.08, rating_score),
        ]
    )
    if value is None:
        # An unprofiled company is neither attractive nor unattractive.  A
        # neutral prior keeps it in the list without letting it outrank a
        # company that was actually researched.
        value = 0.45
        reasons.append("no financial analysis or signals yet; scored as neutral")
    return SubScore(
        value=value,
        reasons=reasons,
        detail={
            "trajectory": trajectory,
            "ability_to_pay": ability,
            "investment_capacity": capacity,
            "signal_strength": round(strength, 3),
            "signal_count": counted,
            "employer_rating": rating,
            "is_estimated": bool(analysis.get("is_estimated")),
        },
    )


# ---------------------------------------------------------------------------
# FR-281: compensation, plausibility, reachability
# ---------------------------------------------------------------------------


def _estimate_from_columns(opportunity: dict) -> comp_mod.CompensationEstimate:
    """Read back the stored FR-264 estimate instead of recomputing it."""
    sources = opportunity.get("comp_sources") or {}
    return comp_mod.CompensationEstimate(
        currency=opportunity.get("comp_currency")
        or comp_mod.default_currency(opportunity.get("country")),
        comp_min=opportunity.get("comp_min"),
        comp_max=opportunity.get("comp_max"),
        confidence=float(opportunity.get("comp_confidence") or 0.0),
        is_stated=bool(opportunity.get("comp_is_stated")),
        method=str(sources.get("method") or "stored"),
    )


def compensation_fit(opportunity: dict, ctx: ScoringContext) -> SubScore:
    """FR-281 compensation fit, against the FR-146 minimum package."""
    estimate = _estimate_from_columns(opportunity)
    result = comp_mod.fit_against_directives(estimate, ctx.directives)
    value = result.get("score")
    if value is not None:
        # A guess that the range clears the minimum is worth less than a stated
        # range that clears it, so low-confidence estimates are pulled towards
        # neutral rather than trusted outright.
        confidence = estimate.confidence if not estimate.is_stated else 1.0
        value = value * confidence + 0.5 * (1 - confidence)
    return SubScore(
        value=value,
        reasons=[str(result.get("reason"))],
        detail={
            "comp_min": estimate.comp_min,
            "comp_max": estimate.comp_max,
            "currency": estimate.currency,
            "estimate_confidence": estimate.confidence,
            "is_stated": estimate.is_stated,
            "raw_fit": result.get("score"),
        },
    )


def plausibility_fit(opportunity: dict) -> SubScore:
    """FR-281: plausibility, which only means something for speculative openings."""
    if opportunity.get("kind") != KIND_SPECULATIVE:
        return SubScore(1.0, ["this is an advertised vacancy; it certainly exists"])
    value = opportunity.get("plausibility")
    if value is None:
        return SubScore(0.3, ["speculative opening with no plausibility score recorded"])
    return SubScore(
        float(value),
        [f"speculative opening, plausibility {float(value):.0%} (FR-262)"],
        {"plausibility": float(value)},
    )


def reachability(opportunity: dict, ctx: ScoringContext) -> SubScore:
    """FR-281: is there a validated contact, or an introduction path?"""
    inputs = repo.reachability_inputs(ctx.job_seeker_id, opportunity.get("company_id"))
    contacts = inputs["contacts"]
    reasons: list[str] = []

    best = 0.1
    if not contacts:
        reasons.append("no contact found at this company yet")
    else:
        for contact in contacts:
            validation = (contact.get("email_validation") or "unknown").lower()
            generic = bool(contact.get("is_generic_mailbox"))
            if not contact.get("email"):
                candidate = 0.3
            elif validation == "valid":
                candidate = 0.6 if generic else 1.0
            elif validation == "risky":
                candidate = 0.4 if generic else 0.55
            elif validation == "invalid":
                candidate = 0.15
            else:
                candidate = 0.35 if generic else 0.45
            best = max(best, candidate)
        reasons.append(f"{len(contacts)} contact(s), best validation state scores {best:.2f}")

    paths = inputs["introduction_paths"]
    if paths:
        best = _clamp(best + 0.3 * float(inputs["best_path_strength"]))
        reasons.append(f"{paths} introduction path(s) proposed (FR-302)")

    return SubScore(
        value=best,
        reasons=reasons,
        detail={
            "contacts": len(contacts),
            "introduction_paths": paths,
            "best_path_strength": inputs["best_path_strength"],
        },
    )


# ---------------------------------------------------------------------------
# FR-383: the dream-job fit meter
# ---------------------------------------------------------------------------

IMPORTANCE_WEIGHTS = {"must": 3.0, "strong": 2.0, "nice": 1.0}
STATUS_VALUES = {"met": 1.0, "partial": 0.5, "violated": 0.0}

_ATTRIBUTE_FIELDS = {
    "size": "size_band",
    "stage": "stage",
    "ownership": "ownership",
    "sector": "sector_codes",
    "geography": "country",
    "mission": "business_summary",
    "maturity": "stage",
}


def _criterion(
    cid: str, text: str, status: str, explanation: str, importance: str = "strong", **extra: Any
) -> dict[str, Any]:
    return {
        "id": cid,
        "criterion": text,
        "status": status,
        "explanation": explanation,
        "importance": importance if importance in IMPORTANCE_WEIGHTS else "strong",
        **extra,
    }


def dream_criteria(opportunity: dict, ctx: ScoringContext) -> list[dict[str, Any]]:
    """FR-383: the criteria list, assessed as far as arithmetic can take it.

    Every criterion is either checked against a field or marked ``unknown``.
    Missing information is never a violation (CR-405) - it is a question the
    seeker can answer for themselves.
    """
    dream = ctx.dream
    company = ctx.company(opportunity.get("company_id")) or {}
    title_tokens = _tokens(opportunity.get("title"), opportunity.get("function_family"))
    body_tokens = _tokens((opportunity.get("description") or "")[:4000])
    criteria: list[dict[str, Any]] = []
    culture_tokens: set[str] | None = None

    for index, role in enumerate(dream.get("target_roles") or [], start=1):
        if not isinstance(role, dict):
            role = {"title": str(role)}
        wanted = str(role.get("title") or "").strip()
        if not wanted:
            continue
        wanted_tokens = _tokens(wanted)
        overlap = (
            len(wanted_tokens & title_tokens) / len(wanted_tokens) if wanted_tokens else 0.0
        )
        if overlap >= 0.66:
            status, explanation = "met", f"the title matches the target role '{wanted}'"
        elif overlap >= 0.33 or (wanted_tokens & body_tokens):
            status, explanation = "partial", f"partly overlaps the target role '{wanted}'"
        else:
            status, explanation = "unknown", f"no textual overlap with '{wanted}'"
        importance = "must" if int(role.get("priority") or 3) <= 1 else "strong"
        criteria.append(
            _criterion(f"target_role:{index}", f"Target role: {wanted}", status, explanation,
                       importance, source="dream_job.target_roles")
        )

    for index, family in enumerate(dream.get("role_families") or [], start=1):
        if not isinstance(family, dict):
            family = {"family": str(family)}
        wanted = str(family.get("family") or "").strip()
        if not wanted:
            continue
        actual = (opportunity.get("function_family") or "").lower()
        wanted_tokens = _tokens(wanted)
        if actual and (wanted.lower() in actual or actual in wanted.lower()):
            status, explanation = "met", f"function family is {actual}"
        elif wanted_tokens & (title_tokens | body_tokens):
            status, explanation = "partial", f"the role touches '{wanted}'"
        else:
            status, explanation = "unknown", f"cannot tell whether this is '{wanted}'"
        criteria.append(
            _criterion(f"role_family:{index}", f"Role family: {wanted}", status, explanation,
                       "strong", source="dream_job.role_families")
        )

    for index, item in enumerate(dream.get("responsibilities") or [], start=1):
        if not isinstance(item, dict):
            item = {"activity": str(item)}
        activity = str(item.get("activity") or "").strip()
        if not activity:
            continue
        wanted_tokens = _tokens(activity)
        if not body_tokens:
            status, explanation = "unknown", "the role has no description to check against"
        else:
            hits = wanted_tokens & body_tokens
            coverage = len(hits) / max(len(wanted_tokens), 1)
            named = ", ".join(sorted(hits)[:3])
            if coverage >= 0.5:
                status, explanation = "met", f"the description mentions {named}"
            elif hits:
                status, explanation = "partial", f"the description touches {named}"
            else:
                status, explanation = "unknown", "the description does not mention this activity"
        criteria.append(
            _criterion(f"responsibility:{index}", activity, status, explanation,
                       str(item.get("importance") or "strong"),
                       source="dream_job.responsibilities")
        )

    for index, item in enumerate(dream.get("company_characteristics") or [], start=1):
        if not isinstance(item, dict):
            continue
        attribute = str(item.get("attribute") or "").strip().lower()
        wanted = str(item.get("value") or "").strip()
        if not wanted:
            continue
        label = f"Company {attribute or 'characteristic'}: {wanted}"
        if attribute == "work_arrangement":
            actual = opportunity.get("work_arrangement")
        else:
            actual = company.get(_ATTRIBUTE_FIELDS.get(attribute, ""), None)
        if not actual:
            status, explanation = "unknown", f"{attribute or 'this attribute'} is not recorded"
        else:
            actual_text = str(actual).lower()
            if wanted.lower() in actual_text or actual_text in wanted.lower():
                status, explanation = "met", f"recorded as {actual}"
            elif _tokens(wanted) & _tokens(actual_text):
                status, explanation = "partial", f"recorded as {actual}"
            else:
                status, explanation = "violated", f"recorded as {actual}, not {wanted}"
        criteria.append(
            _criterion(f"company_characteristic:{index}", label, status, explanation,
                       str(item.get("importance") or "strong"),
                       source="dream_job.company_characteristics")
        )

    for index, item in enumerate(dream.get("culture_values") or [], start=1):
        if not isinstance(item, dict):
            item = {"cue": str(item)}
        cue = str(item.get("cue") or item.get("value") or "").strip()
        if not cue:
            continue
        avoid = str(item.get("polarity") or "seek").strip().lower() == "avoid"
        label = f"Culture: {'avoid ' if avoid else ''}{cue}"
        # The company's own words about how it works, as far as FR-384 has
        # collected them, plus the advertisement's language.  A cue nobody has
        # written down anywhere is unknown, never violated (CR-405).
        if culture_tokens is None:
            culture_tokens = _tokens(
                *_string_leaves(from_json(company.get("values_culture"), None)),
                company.get("business_summary"),
                (opportunity.get("description") or "")[:4000],
            )
        hits = _tokens(cue) & culture_tokens
        named = ", ".join(sorted(hits)[:3])
        if not culture_tokens:
            status, explanation = "unknown", "nothing recorded about how this company works"
        elif not hits:
            status, explanation = "unknown", f"nothing recorded either way about '{cue}'"
        elif avoid:
            status, explanation = "violated", f"the company's own language mentions {named}"
        elif len(hits) >= max(1, len(_tokens(cue)) // 2):
            status, explanation = "met", f"the company's own language mentions {named}"
        else:
            status, explanation = "partial", f"the company's language touches {named}"
        criteria.append(
            _criterion(f"culture_value:{index}", label, status, explanation, "strong",
                       source="dream_job.culture_values",
                       polarity="avoid" if avoid else "seek")
        )

    for index, item in enumerate(dream.get("deal_breakers") or [], start=1):
        if not isinstance(item, dict):
            item = {"constraint": str(item)}
        constraint = str(item.get("constraint") or "").strip()
        if not constraint:
            continue
        haystack = _tokens(
            opportunity.get("title"),
            (opportunity.get("description") or "")[:4000],
            opportunity.get("work_arrangement"),
            opportunity.get("contract_type"),
            company.get("business_summary"),
        )
        hits = _tokens(constraint) & haystack
        if hits and len(hits) >= max(1, len(_tokens(constraint)) // 2):
            status = "violated"
            explanation = f"the role matches the excluded condition ({', '.join(sorted(hits)[:3])})"
        else:
            status = "unknown"
            explanation = "nothing in the record triggers this deal-breaker"
        criteria.append(
            _criterion(f"deal_breaker:{index}", f"Deal-breaker: {constraint}", status,
                       explanation, "must" if item.get("hard", True) else "strong",
                       source="dream_job.deal_breakers", hard=bool(item.get("hard", True)))
        )

    return criteria


def score_criteria(criteria: list[dict[str, Any]]) -> float | None:
    """Turn the FR-383 meter into the deterministic dream-fit number."""
    parts = [
        (IMPORTANCE_WEIGHTS.get(c.get("importance", "strong"), 2.0), STATUS_VALUES[c["status"]])
        for c in criteria
        if c.get("status") in STATUS_VALUES
    ]
    value = _weighted(parts)
    if value is None:
        return None
    hard_violation = any(
        c.get("status") == "violated" and c.get("hard") and c["id"].startswith("deal_breaker")
        for c in criteria
    )
    return min(value, 0.1) if hard_violation else value


def meter(criteria: list[dict[str, Any]], value: float | None, method: str) -> dict[str, Any]:
    """FR-383: the meter itself, grouped the way the requirement words it."""
    grouped: dict[str, list[dict]] = {"met": [], "partial": [], "violated": [], "unknown": []}
    for criterion in criteria:
        grouped.setdefault(criterion.get("status", "unknown"), []).append(criterion)
    return {
        "score": None if value is None else round(value * 100, 1),
        "method": method,
        "met": grouped["met"],
        "partially_met": grouped["partial"],
        "violated": grouped["violated"],
        "unknown": grouped["unknown"],
        "counts": {k: len(v) for k, v in grouped.items()},
        "note": (
            "The dream-job fit meter is separate from the overall score: it says what "
            "the market offers against the stated ideal, not whether to apply (FR-383, "
            "NFR-305)."
        ),
    }


# ---------------------------------------------------------------------------
# The LLM half: semantic dream fit and the rationale (FR-282)
# ---------------------------------------------------------------------------


def _llm_payload(opportunity: dict, ctx: ScoringContext) -> dict:
    company = ctx.company(opportunity.get("company_id")) or {}
    return {
        "opportunity": {
            "kind": opportunity.get("kind"),
            "title": opportunity.get("title"),
            "function_family": opportunity.get("function_family"),
            "seniority": opportunity.get("seniority"),
            "description": (opportunity.get("description") or "")[:6000],
            "location": opportunity.get("location"),
            "country": opportunity.get("country"),
            "work_arrangement": opportunity.get("work_arrangement"),
            "contract_type": opportunity.get("contract_type"),
            "required_skills": opportunity.get("required_skills"),
            "desirable_skills": opportunity.get("desirable_skills"),
            "speculative_rationale": from_json(
                opportunity.get("speculative_rationale"), opportunity.get("speculative_rationale")
            ),
            "company": {
                "name": company.get("name"),
                "business_summary": company.get("business_summary"),
                "size_band": company.get("size_band"),
                "stage": company.get("stage"),
                "trajectory": company.get("trajectory"),
                "values_culture": from_json(company.get("values_culture"), None),
            },
        },
        "seeker": redact(
            {
                "narrative": ctx.composite.get("narrative"),
                "core_competencies": ctx.composite.get("core_competencies"),
                "domains": ctx.composite.get("domains"),
                "seniority": ctx.composite.get("seniority"),
                "achievements": ctx.composite.get("achievements"),
            },
            ctx.do_not_disclose,
        ),
        "dream_job": redact(
            {k: v for k, v in ctx.dream.items() if k != "row"}, ctx.do_not_disclose
        ),
    }


def semantic_dream_fit(
    opportunity: dict,
    ctx: ScoringContext,
    subscores: dict[str, SubScore],
    criteria: list[dict[str, Any]],
    llm: LLMClient,
) -> dict[str, Any] | None:
    """One call for the two things arithmetic cannot do (FR-282, FR-383).

    Returns ``None`` when the call fails or the budget is gone; the caller then
    keeps the deterministic dream-fit value, which is why a budget-exhausted
    campaign still produces a complete, ordered, explainable list (NFR-104).
    """
    template = load_prompt(PROMPT_NAME)
    system, user = template.render(language=ctx.language)
    payload = _llm_payload(opportunity, ctx)
    try:
        data = llm.complete_json(
            template.task or "score.opportunity",
            system=system,
            user=user,
            untrusted={
                "opportunity": json.dumps(payload["opportunity"], ensure_ascii=False)[:12000],
                "seeker": json.dumps(payload["seeker"], ensure_ascii=False)[:6000],
                "dream_job": json.dumps(payload["dream_job"], ensure_ascii=False)[:6000],
                "subscores": json.dumps(
                    {k: v.as_dict() for k, v in subscores.items()}, ensure_ascii=False
                )[:4000],
                "criteria": json.dumps(criteria, ensure_ascii=False)[:6000],
            },
            entity_type="opportunity",
            entity_id=opportunity.get("id"),
            prompt_template=template.name,
            prompt_version=template.version,
            # NFR-104: one call per opportunity across a whole campaign, so the
            # cheap route is the only affordable one - a reasoning model would
            # spend the entire campaign budget on chain of thought alone.
            prefer_strong=False,
            max_tokens=1800,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("Falling back to the deterministic dream fit for %s: %s",
                 opportunity.get("id"), exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _merge_criteria(
    deterministic: list[dict[str, Any]], refined: Any
) -> list[dict[str, Any]]:
    """Keep the mechanical assessment, let the model refine wording and status."""
    if not isinstance(refined, list):
        return deterministic
    by_id = {c["id"]: dict(c) for c in deterministic}
    order = [c["id"] for c in deterministic]
    for item in refined:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        if status not in ("met", "partial", "violated", "unknown"):
            continue
        text = str(item.get("criterion") or "").strip()
        explanation = str(item.get("explanation") or "").strip()[:400]
        if cid in by_id:
            by_id[cid].update(
                {"status": status, "explanation": explanation or by_id[cid]["explanation"],
                 "refined_by": "llm"}
            )
            continue
        if not text:
            continue
        new_id = cid or f"llm:{len(order) + 1}"
        by_id[new_id] = _criterion(
            new_id, text, status, explanation, str(item.get("importance") or "strong"),
            source="llm",
        )
        order.append(new_id)
    return [by_id[cid] for cid in order]


# ---------------------------------------------------------------------------
# Scoring one opportunity
# ---------------------------------------------------------------------------


ADVISORY_NOTE = (
    "Scores are advisory. They order a list; they do not decide anything. "
    "Every decision to keep, reject or apply is the job seeker's (NFR-305, CR-405)."
)


def score_opportunity(
    opportunity: dict, ctx: ScoringContext, *, llm: LLMClient | None = None
) -> dict[str, Any]:
    """FR-281/282/383: sub-scores, overall score, rationale and the fit meter.

    Returns the ``opportunity`` column values.  ``manual_rank`` is not among
    them: recomputation must never move a row the seeker placed by hand
    (FR-284).
    """
    subscores: dict[str, SubScore] = {
        "profile_fit": profile_fit(opportunity, ctx),
        "directive_fit": directive_fit(opportunity, ctx),
        "company": company_attractiveness(opportunity.get("company_id")),
        "compensation": compensation_fit(opportunity, ctx),
        "plausibility": plausibility_fit(opportunity),
        "reachability": reachability(opportunity, ctx),
    }

    criteria = dream_criteria(opportunity, ctx)
    deterministic_dream = score_criteria(criteria)
    subscores["dream_fit"] = SubScore(
        deterministic_dream,
        ["dream-job criteria assessed mechanically"],
        {"criteria": len(criteria)},
    )

    rationale: str | None = None
    meter_method = "deterministic"
    llm_extra: dict[str, Any] = {}
    if llm is not None:
        data = semantic_dream_fit(opportunity, ctx, subscores, criteria, llm)
        if data is not None:
            try:
                semantic = _clamp(float(data.get("dream_fit")))
            except (TypeError, ValueError):
                semantic = None
            if semantic is not None:
                # The mechanical meter grounds the number; the model adjusts it.
                blended = semantic if deterministic_dream is None else (
                    0.6 * semantic + 0.4 * deterministic_dream
                )
                subscores["dream_fit"] = SubScore(
                    blended,
                    [
                        str(data.get("dream_fit_reason") or "semantic match with the dream job"),
                        "blended with the mechanical criteria assessment",
                    ],
                    {"semantic": semantic, "deterministic": deterministic_dream},
                    method="llm+deterministic",
                )
                meter_method = "llm+deterministic"
            criteria = _merge_criteria(criteria, data.get("criteria"))
            rationale = str(data.get("rationale") or "").strip() or None
            llm_extra = {
                "strongest_match": str(data.get("strongest_match") or "").strip() or None,
                "biggest_gap": str(data.get("biggest_gap") or "").strip() or None,
            }

    weights = ctx.weights
    overall = _weighted([(weights.get(name, 0.0), sub.value) for name, sub in subscores.items()])
    if rationale is None:
        rationale = _fallback_rationale(opportunity, subscores)

    columns: dict[str, Any] = {
        "score": None if overall is None else round(overall * 100, 1),
        "rationale": rationale,
        "dream_fit_detail": meter(criteria, score_criteria(criteria), meter_method),
        "score_detail": {
            "weights": weights,
            "components": {name: sub.as_dict() for name, sub in subscores.items()},
            "advisory": ADVISORY_NOTE,
            **llm_extra,
        },
        "scored_at": utcnow(),
    }
    for name, sub in subscores.items():
        columns[COMPONENT_COLUMNS[name]] = (
            None if sub.value is None else round(sub.value * 100, 1)
        )
    return columns


def _fallback_rationale(opportunity: dict, subscores: dict[str, SubScore]) -> str:
    """A plain, honest paragraph when no LLM is available (NFR-104 degradation)."""
    kind_note = (
        "This role is not advertised; it is a speculative opening (FR-263). "
        if opportunity.get("kind") == KIND_SPECULATIVE
        else ""
    )
    ranked = sorted(
        ((name, sub) for name, sub in subscores.items() if sub.value is not None),
        key=lambda pair: -pair[1].value,
    )
    best = ranked[:2]
    worst = ranked[-1:] if len(ranked) > 2 else []
    strengths = "; ".join(f"{name.replace('_', ' ')} {sub.value:.0%}" for name, sub in best)
    weakness = "; ".join(f"{name.replace('_', ' ')} {sub.value:.0%}" for name, sub in worst)
    return (
        f"{kind_note}Computed without a narrative model. Strongest components: {strengths}."
        + (f" Weakest: {weakness}." if weakness else "")
        + " See the sub-scores for the comparisons behind each number."
    )


# ---------------------------------------------------------------------------
# Scoring a campaign
# ---------------------------------------------------------------------------


@dataclass
class ScoringReport:
    campaign_id: str
    scored: int = 0
    with_llm: int = 0
    manual_ranks_preserved: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "scored": self.scored,
            "with_llm": self.with_llm,
            "manual_ranks_preserved": self.manual_ranks_preserved,
            "errors": self.errors,
            "advisory": ADVISORY_NOTE,
        }


def score_campaign(
    campaign: dict,
    *,
    use_llm: bool = True,
    llm: LLMClient | None = None,
    limit: int = 1000,
    language: str | None = None,
) -> ScoringReport:
    """Recalculate every opportunity in a campaign (FR-281, FR-284).

    The seeker's manual order is read before and counted after: recomputation
    writes only the ``score_*`` columns, so a manual position is arithmetically
    incapable of moving.  The count is reported so the campaign screen can say
    so out loud.
    """
    seeker_id = campaign["job_seeker_id"]
    campaign_id = campaign["id"]
    report = ScoringReport(campaign_id=campaign_id)
    ctx = build_context(campaign, language=language)

    if use_llm and llm is None:
        llm = LLMClient(campaign_id=campaign_id, job_seeker_id=seeker_id)
        if not llm.settings.deepseek_api_key and not llm.settings.local_llm_base_url:
            log.info("No LLM configured; scoring deterministically only")
            llm = None
    if not use_llm:
        llm = None

    rows = repo.list_opportunities(
        seeker_id, campaign_id=campaign_id, limit=limit, respect_manual_order=False
    )
    for opportunity in rows:
        if opportunity.get("manual_rank") is not None:
            report.manual_ranks_preserved += 1
        try:
            columns = score_opportunity(opportunity, ctx, llm=llm)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
            log.exception("Scoring failed for opportunity %s", opportunity.get("id"))
            report.errors.append(f"{opportunity.get('id')}: {exc}")
            continue
        repo.save_scores(opportunity["id"], columns, job_seeker_id=seeker_id)
        report.scored += 1
        if (columns.get("score_detail") or {}).get("components", {}).get(
            "dream_fit", {}
        ).get("method", "").startswith("llm"):
            report.with_llm += 1
    return report


# ---------------------------------------------------------------------------
# FR-285: learning from pins and rejections
# ---------------------------------------------------------------------------

_REJECTION_BUCKETS: list[tuple[str, re.Pattern[str], str]] = [
    ("location", re.compile(r"\b(commut|distance|too far|travel|verplaats|afstand|trajet)\b", re.I),
     "location.areas / location.max_commute_minutes"),
    ("work_arrangement", re.compile(
        r"\b(remote|onsite|on-site|office|hybrid|thuiswerk|telewerk|t[ée]l[ée]travail)\b", re.I),
     "work_arrangement.arrangements"),
    ("compensation", re.compile(
        r"\b(salary|pay|compensation|loon|salaris|r[ée]mun[ée]ration|too low|budget)\b", re.I),
     "compensation.minimum_package"),
    ("company_type", re.compile(
        r"\b(too (big|small)|corporate|startup|scale-?up|consultancy|agency|multinational)\b",
        re.I),
     "company_type.size_bands / company_type.stages"),
    ("seniority", re.compile(
        r"\b(too (junior|senior)|under-?qualified|over-?qualified|level|niveau)\b", re.I),
     "job_content.seniority_min / seniority_max"),
    ("industry", re.compile(
        r"\b(industry|sector|domain|defen[cs]e|gambling|tobacco|sector)\b", re.I),
     "job_content.industries_exclude"),
    ("contract", re.compile(
        r"\b(freelance|interim|contract|permanent|temporary|zelfstandig|cdd|cdi)\b", re.I),
     "work_arrangement.contract_types"),
]


def _sides(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    positive = [
        r for r in rows
        if r.get("pinned") or r.get("user_status") in ("interested", "applied")
    ]
    negative = [r for r in rows if r.get("user_status") == "not_interested"]
    return positive, negative


def learn_weights(job_seeker_id: str, *, apply: bool = True) -> dict[str, Any]:
    """FR-285: re-tune this seeker's weights from their pins and rejections.

    A component whose sub-score separates the kept opportunities from the
    rejected ones is a component this seeker actually cares about, so its weight
    goes up - within a band around the default, because feedback on a few dozen
    rows is evidence, not proof.  The explanation is stored alongside the
    weights (FR-425), and nothing is applied without the seeker asking for it.
    """
    rows = repo.feedback_rows(job_seeker_id)
    positive, negative = _sides(rows)
    current = load_weights(job_seeker_id)
    result: dict[str, Any] = {
        "job_seeker_id": job_seeker_id,
        "positive": len(positive),
        "negative": len(negative),
        "current_weights": current,
        "applied": False,
        "advisory": ADVISORY_NOTE,
    }
    if len(positive) < MIN_FEEDBACK_PER_SIDE or len(negative) < MIN_FEEDBACK_PER_SIDE:
        result["weights"] = current
        result["reason"] = (
            f"needs at least {MIN_FEEDBACK_PER_SIDE} pinned/interested and "
            f"{MIN_FEEDBACK_PER_SIDE} rejected opportunities before the weights move"
        )
        return result

    separations: dict[str, dict[str, float]] = {}
    proposed: dict[str, float] = {}
    for component, column in COMPONENT_COLUMNS.items():
        kept = [r[column] for r in positive if r.get(column) is not None]
        dropped = [r[column] for r in negative if r.get(column) is not None]
        base = DEFAULT_WEIGHTS[component]
        if len(kept) < 2 or len(dropped) < 2:
            proposed[component] = current.get(component, base)
            continue
        mean_kept = sum(kept) / len(kept)
        mean_dropped = sum(dropped) / len(dropped)
        delta = (mean_kept - mean_dropped) / 100.0
        separations[component] = {
            "mean_kept": round(mean_kept, 1),
            "mean_rejected": round(mean_dropped, 1),
            "separation": round(delta, 3),
        }
        multiplier = 1.0 + 0.8 * delta
        weight = current.get(component, base) * multiplier
        proposed[component] = min(
            base * WEIGHT_CEILING_FACTOR, max(base * WEIGHT_FLOOR_FACTOR, weight)
        )

    normalised = normalise_weights(proposed)
    learned_from = {
        "method": "mean sub-score separation between kept and rejected opportunities",
        "positive": len(positive),
        "negative": len(negative),
        "separations": separations,
        "previous_weights": current,
        "computed_at": utcnow(),
    }
    result["weights"] = normalised
    result["learned_from"] = learned_from
    if apply:
        save_weights(job_seeker_id, normalised, learned_from)
        result["applied"] = True
    return result


def suggest_directive_refinements(job_seeker_id: str) -> list[dict[str, Any]]:
    """FR-285: what the rejections say about the directives, as suggestions only.

    Nothing here is applied.  Each suggestion names the directive field, the
    evidence behind it and how strong that evidence is, and the seeker decides
    (NFR-305).
    """
    rows = repo.feedback_rows(job_seeker_id)
    positive, negative = _sides(rows)
    suggestions: list[dict[str, Any]] = []
    if not negative:
        return suggestions

    bucket_counts: dict[str, int] = {}
    bucket_examples: dict[str, list[str]] = {}
    for row in negative:
        reason = row.get("not_interested_reason") or ""
        for name, pattern, _field in _REJECTION_BUCKETS:
            if pattern.search(reason):
                bucket_counts[name] = bucket_counts.get(name, 0) + 1
                bucket_examples.setdefault(name, []).append(reason[:160])
    for name, count in sorted(bucket_counts.items(), key=lambda kv: -kv[1]):
        field_path = next(f for n, _, f in _REJECTION_BUCKETS if n == name)
        share = count / len(negative)
        suggestions.append(
            {
                "field": field_path,
                "topic": name,
                "suggestion": (
                    f"{count} of {len(negative)} rejections mention {name.replace('_', ' ')}. "
                    f"Tightening {field_path} would keep those out of the next campaign."
                ),
                "evidence": bucket_examples[name][:3],
                "confidence": round(min(0.9, 0.3 + share), 2),
            }
        )

    # Attribute distributions say things free text does not.
    for attribute, label in (
        ("work_arrangement", "work_arrangement.arrangements"),
        ("contract_type", "work_arrangement.contract_types"),
        ("country", "location.countries"),
        ("seniority", "job_content.seniority_min / seniority_max"),
    ):
        rejected_values: dict[str, int] = {}
        kept_values: dict[str, int] = {}
        for row in negative:
            value = row.get(attribute)
            if value:
                rejected_values[str(value)] = rejected_values.get(str(value), 0) + 1
        for row in positive:
            value = row.get(attribute)
            if value:
                kept_values[str(value)] = kept_values.get(str(value), 0) + 1
        for value, count in rejected_values.items():
            if count < MIN_FEEDBACK_PER_SIDE or kept_values.get(value):
                continue
            suggestions.append(
                {
                    "field": label,
                    "topic": attribute,
                    "suggestion": (
                        f"Every opportunity with {attribute} '{value}' was rejected "
                        f"({count}), and none was kept. Consider excluding it."
                    ),
                    "evidence": [f"{count} rejected, 0 kept"],
                    "confidence": round(min(0.85, 0.35 + 0.1 * count), 2),
                }
            )
    return suggestions
