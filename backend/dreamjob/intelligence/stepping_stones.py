"""Stepping-stone paths towards the dream job (FR-382, FR-284, CR-405, NFR-104).

FR-382 fires on a condition rather than on a button: when *no* current
opportunity clears the configured dream-job fit threshold, the ranked list is
answering "none of these" and the useful answer is a route rather than a
better sort.  This module proposes two to three of those routes - sequences of
roles that are reachable now and that plausibly lead to the stated dream job -
and it tags the opportunities that belong to them.

Two design rules keep the proposal honest:

* **A stepping stone is a role the job seeker can actually get.**  Candidates
  are drawn from the campaign's own opportunities, ordered by the profile-fit
  sub-score, not by how attractive they sound.
* **A stepping stone has to lead somewhere.**  Its value is measured against
  the computed gap analysis (FR-381): a role counts as a bridge only when its
  own posting describes the work behind a gap that stands between this profile
  and the dream job.

Tagging is a separate, explicit call.  FR-284 gives the tags to the job seeker,
so no automatic pass writes them; :func:`apply_tags` runs when the seeker asks
for it and merges rather than replaces.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.connection import from_json
from dreamjob.db.repositories import intelligence as repo
from dreamjob.intelligence import gap_analysis as gaps_mod
from dreamjob.intelligence.text import fold, phrase_in, tokens
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.enrichment import default_llm, load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "stepping_stones"

#: FR-382's "configurable dream-job fit threshold", in the 0..100 units the
#: ranked list already shows.  Stored in ``app_setting`` so it can be changed
#: without a deployment (FR-362), and keyed per job seeker so that one seeker's
#: idea of "close enough" does not become everybody's.
THRESHOLD_SETTING = "dream_fit_threshold"
DEFAULT_THRESHOLD = 65.0

#: A role has to be plausibly winnable to be a stepping stone at all.
REACHABLE_PROFILE_FIT = 50.0

DESTINATION_TAG = "destination"
STEPPING_STONE_TAG = "stepping_stone"

MIN_PATHS = 2
MAX_PATHS = 3
MAX_STEP_OPPORTUNITIES = 3


def _keys(job_seeker_id: str | None) -> list[str]:
    return ([f"{THRESHOLD_SETTING}:{job_seeker_id}"] if job_seeker_id else []) + [
        THRESHOLD_SETTING
    ]


def threshold(job_seeker_id: str | None = None) -> float:
    """The configured dream-job fit threshold (FR-382): this seeker's, or the default."""
    for key in _keys(job_seeker_id):
        raw = repo.get_setting(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return DEFAULT_THRESHOLD


def set_threshold(value: float, job_seeker_id: str | None = None) -> float:
    value = max(0.0, min(100.0, float(value)))
    repo.set_setting(_keys(job_seeker_id)[0], str(value))
    return value


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """One move in a path: what to do next, and what it buys."""

    position: int
    horizon_months: int
    role: str
    kind: str                       # now | intermediate | destination
    rationale: str
    companies: list[str] = field(default_factory=list)
    opportunity_ids: list[str] = field(default_factory=list)
    closes_gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "horizon_months": self.horizon_months,
            "role": self.role,
            "kind": self.kind,
            "rationale": self.rationale,
            "companies": self.companies,
            "opportunity_ids": self.opportunity_ids,
            "closes_gaps": self.closes_gaps,
        }


@dataclass
class Path:
    """One stepping-stone path (FR-382)."""

    name: str
    steps: list[Step]
    rationale: str
    grounded_in: str = "opportunities"
    horizon_months: int = 0
    basis: dict[str, Any] = field(default_factory=dict)
    path_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.path_id,
            "name": self.name,
            "rationale": self.rationale,
            "grounded_in": self.grounded_in,
            "horizon_months": self.horizon_months,
            "steps": [s.as_dict() for s in self.steps],
            "basis": self.basis,
            "opportunity_ids": sorted(
                {oid for step in self.steps for oid in step.opportunity_ids}
            ),
        }


@dataclass
class SteppingStoneReport:
    job_seeker_id: str
    campaign_id: str | None
    threshold: float
    best_dream_fit: float | None
    triggered: bool
    paths: list[Path]
    destinations: list[dict[str, Any]] = field(default_factory=list)
    generated_by: str = "deterministic"
    discretion_mode: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_seeker_id": self.job_seeker_id,
            "campaign_id": self.campaign_id,
            "threshold": self.threshold,
            "best_dream_fit": self.best_dream_fit,
            "triggered": self.triggered,
            "discretion_mode": self.discretion_mode,
            "paths": [p.as_dict() for p in self.paths],
            "destinations": self.destinations,
            "generated_by": self.generated_by,
            "warnings": self.warnings,
            "tags": {"destination": DESTINATION_TAG, "stepping_stone": STEPPING_STONE_TAG},
            "note": (
                "Paths are proposals read from the collected market and the computed gap "
                "analysis; they are not predictions, and tagging an opportunity remains the "
                "job seeker's own decision (FR-284, NFR-305)."
            ),
        }


# ---------------------------------------------------------------------------
# The trigger (FR-382)
# ---------------------------------------------------------------------------


def _dream_fit(row: dict[str, Any]) -> float | None:
    value = row.get("score_dream_fit")
    if value is None:
        detail = row.get("dream_fit_detail") or {}
        value = detail.get("score") if isinstance(detail, dict) else None
    return None if value is None else float(value)


def assess(job_seeker_id: str, campaign_id: str | None = None) -> dict[str, Any]:
    """Whether the ranked list has a destination in it at all (FR-382)."""
    limit = threshold(job_seeker_id)
    rows = repo.campaign_opportunities(job_seeker_id, campaign_id)
    fits = [(row, _dream_fit(row)) for row in rows]
    scored = [(row, fit) for row, fit in fits if fit is not None]
    best = max((fit for _row, fit in scored), default=None)
    destinations = [
        {
            "opportunity_id": row["id"],
            "title": row.get("title"),
            "company_name": row.get("company_name"),
            "dream_fit": fit,
        }
        for row, fit in sorted(scored, key=lambda pair: -pair[1])
        if fit >= limit
    ]
    return {
        "threshold": limit,
        "best_dream_fit": best,
        "scored": len(scored),
        "unscored": len(fits) - len(scored),
        "destinations": destinations,
        "triggered": bool(scored) and not destinations,
    }


# ---------------------------------------------------------------------------
# Building the paths
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    row: dict[str, Any]
    profile_fit: float
    dream_fit: float | None
    bridge: float
    closes: list[str]

    @property
    def family(self) -> str:
        return str(
            self.row.get("function_family")
            or (self.row.get("title") or "").split("(")[0].strip()
            or "other"
        )


def _gap_subjects(report: gaps_mod.GapReport) -> list[tuple[str, str, str]]:
    """(gap id, dimension, searchable subject) for the gaps a role could close."""
    out: list[tuple[str, str, str]] = []
    for gap in report.gaps:
        if gap.dimension == "skill":
            subject = gap.id.split(":", 1)[1]
        elif gap.dimension == "leadership":
            subject = "team"
        elif gap.dimension == "visibility":
            subject = "community"
        elif gap.dimension == "experience" and gap.id.startswith("experience:responsibility"):
            subject = gap.label.split(":", 1)[-1].strip()
        else:
            continue
        if subject:
            out.append((gap.id, gap.dimension, subject))
    return out


def _candidates(
    rows: list[dict[str, Any]], subjects: list[tuple[str, str, str]]
) -> list[Candidate]:
    out: list[Candidate] = []
    for row in rows:
        profile_fit = row.get("score_profile_fit")
        if profile_fit is None or float(profile_fit) < REACHABLE_PROFILE_FIT:
            continue
        text = " ".join(
            str(x) for x in (row.get("title"), row.get("description")) if x
        )[:6000]
        closes: list[str] = []
        for gap_id, dimension, subject in subjects:
            if dimension == "leadership":
                hit = any(phrase_in(p, text) for p in gaps_mod.LEADERSHIP_PHRASES)
            elif dimension == "visibility":
                hit = any(phrase_in(p, text) for p in gaps_mod.VISIBILITY_PHRASES)
            else:
                wanted = tokens(subject)
                hit = bool(wanted) and len(wanted & tokens(text)) >= max(1, len(wanted) // 2)
            if hit:
                closes.append(gap_id)
        out.append(
            Candidate(
                row=row,
                profile_fit=float(profile_fit),
                dream_fit=_dream_fit(row),
                bridge=len(closes) / max(len(subjects), 1) if subjects else 0.0,
                closes=closes,
            )
        )
    # Reachable first, then how much of the distance the role actually covers.
    out.sort(key=lambda c: (-(0.6 * c.profile_fit / 100 + 0.4 * c.bridge), -(c.dream_fit or 0)))
    return out


def _target_role(dream: dict[str, Any] | None) -> str:
    for role in from_json((dream or {}).get("target_roles"), []) or []:
        if isinstance(role, dict) and role.get("title"):
            return str(role["title"])
        if isinstance(role, str) and role.strip():
            return role.strip()
    return "the dream job"


def _intermediate_role(family: str, closes: list[str], report: gaps_mod.GapReport) -> str:
    """The role between here and the destination, named after what it must add."""
    by_id = {gap.id: gap for gap in report.gaps}
    dimensions = {by_id[g].dimension for g in closes if g in by_id}
    base = family.strip() or "the same function"
    if "leadership" in dimensions:
        return f"{base} lead, with a small team or a programme budget"
    if "skill" in dimensions:
        subjects = [
            by_id[g].id.split(":", 1)[1]
            for g in closes
            if g in by_id and by_id[g].dimension == "skill"
        ]
        if subjects:
            return f"{base} role that owns {subjects[0]}"
    if "visibility" in dimensions:
        return f"{base} role with an external, published face"
    return f"senior {base}"


def _path_from_group(
    family: str,
    members: list[Candidate],
    report: gaps_mod.GapReport,
    destination: str,
) -> Path:
    head = members[:MAX_STEP_OPPORTUNITIES]
    closes = sorted({gap for c in head for gap in c.closes})
    by_id = {gap.id: gap for gap in report.gaps}
    companies = [c.row.get("company_name") for c in head if c.row.get("company_name")]
    titles = Counter(str(c.row.get("title") or "").strip() for c in head if c.row.get("title"))
    now_role = titles.most_common(1)[0][0] if titles else family

    step_now = Step(
        position=1,
        horizon_months=0,
        role=now_role,
        kind="now",
        rationale=(
            f"Reachable today: {len(head)} opportunit{'y' if len(head) == 1 else 'ies'} in this "
            f"campaign score {head[0].profile_fit:.0f}/100 or better on profile fit, which is "
            "the sub-score that says the profile matches what the posting asks for."
        ),
        companies=[str(c) for c in companies],
        opportunity_ids=[str(c.row["id"]) for c in head],
        closes_gaps=closes,
    )
    labels = [by_id[gap].label for gap in closes if gap in by_id]
    step_mid = Step(
        position=2,
        horizon_months=24,
        role=_intermediate_role(family, closes, report),
        kind="intermediate",
        rationale=(
            "The move that closes the gaps the first step opens: "
            + (
                "; ".join(label.lower() for label in labels[:3])
                if labels
                else "scope and evidence that the current profile does not yet show"
            )
            + "."
        ),
        closes_gaps=closes,
    )
    step_end = Step(
        position=3,
        horizon_months=48,
        role=destination,
        kind="destination",
        rationale=(
            "The stated dream job. By this point the profile carries the evidence the "
            "postings for it ask for."
        ),
    )
    return Path(
        name=f"Via {family}".strip(),
        steps=[step_now, step_mid, step_end],
        rationale=(
            f"{family} is where this profile is currently hireable, and the postings in it "
            f"describe {len(closes)} of the gaps that stand between the profile and "
            f"{destination}."
        ),
        grounded_in="opportunities",
        horizon_months=48,
        basis={
            "candidates": len(members),
            "best_profile_fit": round(head[0].profile_fit, 1),
            "best_dream_fit": max((c.dream_fit or 0.0) for c in head),
            "gaps_addressed": closes,
        },
    )


#: When the campaign has no reachable opportunity to build on, these are the
#: three routes that remain describable from the profile and the gaps alone.
#: They are archetypes, and they are labelled as such.
ARCHETYPES: tuple[tuple[str, str, str], ...] = (
    (
        "Deepen, then step up",
        "a role at the current level that owns the missing subject end to end",
        "Stay at the current seniority but move to work that forces the missing skills, then "
        "step up once they are evidenced.",
    ),
    (
        "Smaller company, wider scope",
        "the same function at a smaller company, where the role is broader",
        "A smaller employer buys scope years earlier than a large one: the same title covers "
        "more of what the dream job asks for.",
    ),
    (
        "Adjacent domain, same craft",
        "the same craft in the domain the dream job is in",
        "Keeping the craft and changing the domain is the cheaper of the two moves; the "
        "domain vocabulary is what the postings for the dream job screen on.",
    ),
)


def _archetype_paths(report: gaps_mod.GapReport, destination: str) -> list[Path]:
    closes = [gap.id for gap in report.gaps[:4]]
    paths: list[Path] = []
    for index, (name, role, rationale) in enumerate(ARCHETYPES[:MAX_PATHS], start=1):
        paths.append(
            Path(
                name=name,
                steps=[
                    Step(
                        position=1,
                        horizon_months=0,
                        role=role,
                        kind="now",
                        rationale=(
                            "No collected opportunity is both reachable and on the way, so "
                            "this step describes the kind of role to look for rather than a "
                            "posting that exists today."
                        ),
                        closes_gaps=closes,
                    ),
                    Step(
                        position=2,
                        horizon_months=24,
                        role=_intermediate_role("the function", closes, report),
                        kind="intermediate",
                        rationale="The move that turns the first step into evidence.",
                        closes_gaps=closes,
                    ),
                    Step(
                        position=3,
                        horizon_months=48,
                        role=destination,
                        kind="destination",
                        rationale="The stated dream job.",
                    ),
                ],
                rationale=rationale,
                grounded_in="dream_job_model",
                horizon_months=48,
                basis={"archetype": index, "gaps_addressed": closes},
            )
        )
    return paths


# ---------------------------------------------------------------------------
# LLM refinement (NFR-104, NFR-205)
# ---------------------------------------------------------------------------


def _refine(paths: list[Path], destination: str, llm: LLMClient, campaign_id: str | None) -> bool:
    template = load_prompt(PROMPT_NAME)
    system, user = template.render(language="en", destination=destination)
    payload = {"destination": destination, "paths": [p.as_dict() for p in paths]}
    try:
        data = llm.complete_json(
            template.task or "plan.stepping_stones",
            system=system,
            user=user,
            untrusted={"paths": json.dumps(payload, ensure_ascii=False)[:12000]},
            entity_type="stepping_stone_path",
            entity_id=campaign_id,
            prompt_template=template.name,
            prompt_version=template.version,
            # The steps and the opportunities behind them are computed; the
            # model writes the reasons. Chat, not reasoning, for the same
            # reason as in the gap analysis.
            prefer_strong=False,
            temperature=0.35,
            max_tokens=2200,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("Stepping stones stay deterministic: %s", exc)
        return False
    if not isinstance(data, dict):
        return False

    by_name = {fold(p.name): p for p in paths}
    for index, item in enumerate(data.get("paths") or []):
        if not isinstance(item, dict):
            continue
        path = by_name.get(fold(str(item.get("name") or ""))) or (
            paths[index] if index < len(paths) else None
        )
        if path is None:
            continue
        rationale = str(item.get("rationale") or "").strip()
        if rationale:
            path.rationale = rationale[:1200]
        name = str(item.get("display_name") or "").strip()
        if name:
            path.name = name[:120]
        for step_item in item.get("steps") or []:
            if not isinstance(step_item, dict):
                continue
            position = step_item.get("position")
            step = next((s for s in path.steps if s.position == position), None)
            if step is None:
                continue
            step_rationale = str(step_item.get("rationale") or "").strip()
            if step_rationale:
                step.rationale = step_rationale[:800]
            role = str(step_item.get("role") or "").strip()
            if role and step.kind != "destination":
                step.role = role[:160]
    return True


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def propose(
    job_seeker_id: str,
    campaign_id: str | None = None,
    *,
    use_llm: bool = True,
    persist: bool = True,
    force: bool = False,
    llm: LLMClient | None = None,
    report: gaps_mod.GapReport | None = None,
) -> SteppingStoneReport:
    """Propose two to three stepping-stone paths (FR-382).

    ``force`` builds the paths even when an opportunity already clears the
    threshold - useful when the seeker wants to see the longer route anyway.
    """
    campaign = (
        repo.get_campaign(campaign_id, job_seeker_id)
        if campaign_id
        else repo.latest_campaign(job_seeker_id)
    )
    resolved_campaign_id = (campaign or {}).get("id")
    state = assess(job_seeker_id, resolved_campaign_id)
    directive_row = repo.directive_set(job_seeker_id, (campaign or {}).get("directive_set_id"))
    discretion = bool((directive_row or {}).get("discretion_mode"))

    warnings: list[str] = []
    if not state["triggered"] and not force:
        if state["destinations"]:
            warnings.append(
                f"{len(state['destinations'])} opportunit"
                f"{'y' if len(state['destinations']) == 1 else 'ies'} already clear the "
                f"{state['threshold']:.0f}-point dream-fit threshold, so no stepping stone is "
                "needed. Pass force=true to see the longer route anyway."
            )
        else:
            warnings.append(
                "No opportunity carries a dream-fit score yet; score the ranked list first "
                "(FR-281) so the threshold means something."
            )
        return SteppingStoneReport(
            job_seeker_id=job_seeker_id,
            campaign_id=resolved_campaign_id,
            threshold=state["threshold"],
            best_dream_fit=state["best_dream_fit"],
            triggered=False,
            paths=[],
            destinations=state["destinations"],
            discretion_mode=discretion,
            warnings=warnings,
        )

    gap_report = report or gaps_mod.analyse(
        job_seeker_id, resolved_campaign_id, use_llm=False, persist=False
    )
    dream = repo.latest_dream_model(job_seeker_id, (campaign or {}).get("persona_id")) or (
        repo.latest_dream_model(job_seeker_id)
    )
    destination = _target_role(dream)

    rows = repo.campaign_opportunities(job_seeker_id, resolved_campaign_id)
    subjects = _gap_subjects(gap_report)
    candidates = _candidates(rows, subjects)

    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.family].append(candidate)
    ordered_groups = sorted(
        grouped.items(),
        key=lambda kv: -max(0.6 * c.profile_fit / 100 + 0.4 * c.bridge for c in kv[1]),
    )

    paths = [
        _path_from_group(family, members, gap_report, destination)
        for family, members in ordered_groups[:MAX_PATHS]
    ]
    if len(paths) < MIN_PATHS:
        needed = MIN_PATHS - len(paths)
        if not paths:
            warnings.append(
                "No collected opportunity is both reachable and on the way to the dream job, "
                "so the paths below describe the kind of role to look for rather than "
                "postings that exist today."
            )
        paths += _archetype_paths(gap_report, destination)[:needed]

    generated_by = "deterministic"
    client = llm
    if use_llm and client is None:
        client = default_llm(job_seeker_id, resolved_campaign_id)
    if client is not None and paths and _refine(paths, destination, client, resolved_campaign_id):
        generated_by = "llm+deterministic"

    if persist:
        ids = repo.replace_stepping_stones(
            job_seeker_id,
            resolved_campaign_id,
            [
                {
                    "name": path.name[:200],
                    "steps": [s.as_dict() for s in path.steps],
                    "rationale": path.rationale,
                    "basis": {
                        **path.basis,
                        "threshold": state["threshold"],
                        "best_dream_fit": state["best_dream_fit"],
                        "grounded_in": path.grounded_in,
                        "destination": destination,
                    },
                    "generated_by": generated_by,
                    "horizon_months": path.horizon_months,
                }
                for path in paths
            ],
        )
        for path, path_id in zip(paths, ids, strict=False):
            path.path_id = path_id

    return SteppingStoneReport(
        job_seeker_id=job_seeker_id,
        campaign_id=resolved_campaign_id,
        threshold=state["threshold"],
        best_dream_fit=state["best_dream_fit"],
        triggered=True,
        paths=paths,
        destinations=state["destinations"],
        generated_by=generated_by,
        discretion_mode=discretion,
        warnings=warnings,
    )


def stored(job_seeker_id: str, campaign_id: str | None = None) -> list[dict[str, Any]]:
    return repo.list_stepping_stones(job_seeker_id, campaign_id)


def apply_tags(
    job_seeker_id: str, campaign_id: str | None = None, *, paths: list[Path] | None = None
) -> dict[str, Any]:
    """Tag the ranked list with ``destination`` and ``stepping_stone`` (FR-382).

    Explicit by design: FR-284 gives tags to the job seeker, so this runs when
    they ask for it, and it merges with the tags already on the row instead of
    replacing them.
    """
    state = assess(job_seeker_id, campaign_id)
    stone_ids: set[str] = set()
    if paths is None:
        for row in repo.list_stepping_stones(job_seeker_id, campaign_id):
            for step in row.get("steps") or []:
                stone_ids.update(str(i) for i in (step.get("opportunity_ids") or []))
    else:
        stone_ids = {oid for path in paths for step in path.steps for oid in step.opportunity_ids}

    destination_ids = {str(d["opportunity_id"]) for d in state["destinations"]}
    stone_ids -= destination_ids

    written: dict[str, list[str]] = {DESTINATION_TAG: [], STEPPING_STONE_TAG: []}
    for tag, ids in ((DESTINATION_TAG, destination_ids), (STEPPING_STONE_TAG, stone_ids)):
        for opportunity_id in sorted(ids):
            existing = repo.opportunity_tags(opportunity_id, job_seeker_id)
            if existing is None:
                continue
            if tag in existing:
                continue
            merged = [*existing, tag][:12]
            if repo.set_opportunity_tags(opportunity_id, job_seeker_id, merged):
                written[tag].append(opportunity_id)
    return {
        "threshold": state["threshold"],
        "tagged": {tag: len(ids) for tag, ids in written.items()},
        "opportunity_ids": written,
        "note": (
            "Tags are the job seeker's (FR-284): this call only adds the two FR-382 labels "
            "and leaves every other tag in place."
        ),
    }
