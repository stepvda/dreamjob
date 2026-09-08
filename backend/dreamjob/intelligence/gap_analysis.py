"""Dream-job gap analysis (FR-381, FR-385, CR-405, NFR-104, NFR-205).

FR-381 asks what stands between the composite profile (FR-125) and the dream
job model (FR-128), across six dimensions - skills, experience, certifications,
languages, leadership scope and public visibility - and it asks for three
things per gap: a concrete closing action, an estimated effort, and a link to
the opportunities where the gap is decisive.

The last of those is the one that decides whether this module is useful or
merely plausible, so decisiveness is **computed, not asserted**.  For every
gap, the opportunities listed are the ones that measurably lost points on that
dimension: the arithmetic mirrors :func:`dreamjob.pipeline.scoring.profile_fit`
- the required-skill overlap is half of the profile-fit sub-score, seniority a
fifth of it - and each link carries the number of profile-fit points the gap
cost that opportunity, so the seeker can see the price of the gap in the same
units the ranked list is scored in.

Everything here works without the LLM.  A model, when one is configured, is
used only to sharpen the wording of a closing action and to name a concrete
course or certification; it never invents a fact about the job seeker
(CR-405), and its absence costs phrasing, not the analysis (NFR-104).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.connection import from_json
from dreamjob.db.repositories import intelligence as repo
from dreamjob.intelligence.text import (
    canonical_skill,
    canonical_skills,
    fold,
    phrase_in,
    skill_held,
    statement_texts,
    tokens,
)
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline import directives as dir_mod
from dreamjob.pipeline.enrichment import default_llm, load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "gap_analysis"

#: The six dimensions FR-381 names, in the order the report presents them.
DIMENSIONS: tuple[str, ...] = (
    "skill",
    "experience",
    "certification",
    "language",
    "leadership",
    "visibility",
)

#: Mirrors the component weights inside ``scoring.profile_fit`` so that "points
#: lost" is expressed in the units of the sub-score the seeker already sees.
PROFILE_FIT_WEIGHTS = {
    "required_skill_overlap": 0.50,
    "desirable_skill_overlap": 0.15,
    "seniority_score": 0.20,
    "domain_score": 0.15,
}

#: Seniority levels that evidence leadership scope (composite ``seniority``).
LEADERSHIP_LEVELS = frozenset({"lead", "manager", "senior_manager", "director", "executive"})

LEADERSHIP_PHRASES: tuple[str, ...] = (
    "team lead", "teamlead", "people manager", "line manager", "direct reports",
    "head of", "manage a team", "managing a team", "leading a team", "lead a team",
    "p&l", "budget responsibility", "budget ownership", "department head",
    "people management", "hiring and coaching", "teamleider", "leidinggevende",
    "chef d equipe", "responsable d equipe", "manage the team",
)

VISIBILITY_PHRASES: tuple[str, ...] = (
    "conference", "speaker", "speaking", "keynote", "publication", "publish",
    "open source", "opensource", "community", "thought leadership", "evangelist",
    "meetup", "webinar", "blog", "whitepaper", "spreker", "conferentie",
)

VISIBILITY_EVIDENCE_KINDS = frozenset(
    {"publication", "talk", "repository", "article", "case_study"}
)

#: Certifications the corpus actually asks for, with the study effort that a
#: working professional typically needs, in months.  The list is deliberately
#: short and specific: a pattern that matches a skill rather than a credential
#: ("azure", "kubernetes") would turn every cloud posting into a certification
#: gap.
CERTIFICATIONS: tuple[tuple[str, str, float], ...] = (
    ("aws certified", "AWS certification (Solutions Architect or equivalent)", 2.5),
    ("solutions architect associate", "AWS Solutions Architect Associate", 2.5),
    ("microsoft certified", "Microsoft certification (Azure role-based)", 2.5),
    ("azure solutions architect", "Azure Solutions Architect Expert", 3.0),
    ("google cloud certified", "Google Cloud certification", 3.0),
    ("professional cloud architect", "Google Professional Cloud Architect", 3.0),
    ("certified kubernetes administrator", "Certified Kubernetes Administrator (CKA)", 2.0),
    ("cka", "Certified Kubernetes Administrator (CKA)", 2.0),
    ("cissp", "CISSP", 6.0),
    ("cisa", "CISA", 4.0),
    ("cism", "CISM", 4.0),
    ("security+", "CompTIA Security+", 2.0),
    ("iso 27001 lead auditor", "ISO 27001 Lead Auditor", 1.0),
    ("pmp", "PMP", 4.0),
    ("prince2", "PRINCE2 Practitioner", 1.5),
    ("scrum master", "Professional or Certified Scrum Master", 0.5),
    ("psm i", "Professional Scrum Master I", 0.5),
    ("safe agilist", "SAFe Agilist", 0.5),
    ("itil", "ITIL Foundation", 1.0),
    ("togaf", "TOGAF", 2.0),
    ("six sigma", "Lean Six Sigma (Green or Black Belt)", 3.0),
    ("cfa", "CFA Level I", 12.0),
    ("acca", "ACCA", 18.0),
)

#: Language requirements, as the corpus states them.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "nl": "Dutch",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
}

_LANGUAGE_PHRASES: dict[str, tuple[str, ...]] = {
    "nl": ("dutch", "nederlands", "nederlandstalig", "neerlandais", "flemish"),
    "fr": ("french", "francais", "frans", "franstalig"),
    "en": ("english", "engels", "anglais"),
    "de": ("german", "duits", "allemand", "deutsch"),
    "es": ("spanish", "spaans", "espagnol"),
    "it": ("italian", "italiaans", "italien"),
}

_LANGUAGE_REQUIREMENT_RE = re.compile(
    r"(fluent|native|proficient|professional|excellent|good command|vloeiend|"
    r"moedertaal|courant|maitrise)\D{0,40}?"
    r"(dutch|nederlands|french|francais|frans|english|engels|anglais|german|duits|deutsch)",
    re.IGNORECASE,
)

#: A gap is only worth showing when the market actually asks for it.
MIN_POSTINGS_FOR_DEMAND = 2
MAX_GAPS_PER_DIMENSION = 4
MAX_DECISIVE_LINKS = 8
DEFAULT_MAX_GAPS = 12


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DecisiveLink:
    """One opportunity that measurably lost points on this gap's dimension."""

    opportunity_id: str
    title: str
    company_name: str | None
    score: float | None
    sub_score: str
    sub_score_value: float | None
    points_lost: float
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "title": self.title,
            "company_name": self.company_name,
            "score": self.score,
            "sub_score": self.sub_score,
            "sub_score_value": self.sub_score_value,
            "points_lost": round(self.points_lost, 1),
            "why": self.why,
        }


@dataclass
class Gap:
    """One gap, with the three things FR-381 requires of it."""

    id: str
    dimension: str
    label: str
    evidence: str
    closing_action: dict[str, Any]
    effort: dict[str, Any]
    decisive_opportunities: list[DecisiveLink] = field(default_factory=list)
    severity: str = "significant"
    demand: dict[str, Any] = field(default_factory=dict)
    basis: str = "score_delta"
    source: str = "deterministic"

    @property
    def total_points_lost(self) -> float:
        return round(sum(link.points_lost for link in self.decisive_opportunities), 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dimension": self.dimension,
            "label": self.label,
            "evidence": self.evidence,
            "closing_action": self.closing_action,
            "effort": self.effort,
            "severity": self.severity,
            "demand": self.demand,
            "basis": self.basis,
            "source": self.source,
            "decisive_opportunities": [link.as_dict() for link in self.decisive_opportunities],
            "decisive_count": len(self.decisive_opportunities),
            "total_points_lost": self.total_points_lost,
        }


@dataclass
class GapReport:
    """The FR-381 analysis for one campaign."""

    job_seeker_id: str
    campaign_id: str | None
    gaps: list[Gap]
    market: dict[str, Any]
    summary: str = ""
    generated_by: str = "deterministic"
    dream_job_model_id: str | None = None
    composite_profile_id: str | None = None
    discretion_mode: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        by_dimension: Counter[str] = Counter(g.dimension for g in self.gaps)
        return {
            "job_seeker_id": self.job_seeker_id,
            "campaign_id": self.campaign_id,
            "dream_job_model_id": self.dream_job_model_id,
            "composite_profile_id": self.composite_profile_id,
            "summary": self.summary,
            "generated_by": self.generated_by,
            "discretion_mode": self.discretion_mode,
            "gaps": [g.as_dict() for g in self.gaps],
            "counts": {
                "gaps": len(self.gaps),
                "with_decisive_opportunities": sum(
                    1 for g in self.gaps if g.decisive_opportunities
                ),
                "by_dimension": dict(by_dimension),
            },
            "market": self.market,
            "warnings": self.warnings,
            "note": (
                "Gaps are read from the profile and the collected postings only; nothing "
                "here is a statement about the job seeker that the material does not "
                "support (CR-405)."
            ),
        }


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class Posting:
    """One opportunity, reduced to what the gap analysis reads."""

    id: str
    title: str
    company_name: str | None
    score: float | None
    score_profile_fit: float | None
    required: list[str]
    desirable: list[str]
    text: str
    language: str | None
    seniority: str | None
    profile_fit_detail: dict[str, Any]

    @property
    def haystack(self) -> str:
        return self.text


@dataclass
class GapInputs:
    job_seeker_id: str
    campaign: dict[str, Any] | None
    composite: dict[str, Any] | None
    dream: dict[str, Any] | None
    sections: dict[str, Any]
    held_skills: set[str]
    held_certifications: set[str]
    held_languages: set[str]
    evidence: list[dict[str, Any]]
    postings: list[Posting]
    directives: Any | None
    seniority_rank: int | None
    leadership_evidence: list[str]
    visibility_items: list[str]


def _seniority_rank(value: Any) -> int | None:
    if not value:
        return None
    if isinstance(value, dict):
        text = str(value.get("level") or value.get("text") or "")
    else:
        text = str(value)
    text = fold(text).replace(" ", "_").replace("-", "_")
    for level, rank in dir_mod.SENIORITY_RANK.items():
        if level.value == text:
            return rank
    for level, rank in dir_mod.SENIORITY_RANK.items():
        if level.value in text:
            return rank
    return None


def _held_skills(job_seeker_id: str, composite: dict | None, sections: dict) -> set[str]:
    held: set[str] = set()
    for row in repo.profile_skills(job_seeker_id):
        for label in (row.get("normalised_label"), row.get("raw_label")):
            canonical = canonical_skill(label)
            if canonical:
                held.add(canonical)
    for key in ("core_competencies", "adjacent_competencies"):
        for text in statement_texts(from_json(composite.get(key) if composite else None, [])):
            canonical = canonical_skill(text)
            if canonical:
                held.add(canonical)
    for entry in sections.get("top_skills") or []:
        canonical = canonical_skill(entry if not isinstance(entry, dict) else entry.get("name"))
        if canonical:
            held.add(canonical)
    return held


def _held_certifications(sections: dict) -> set[str]:
    out: set[str] = set()
    for entry in sections.get("certifications") or []:
        text = entry.get("title") if isinstance(entry, dict) else entry
        if text:
            out.add(fold(text))
    for entry in sections.get("courses") or []:
        text = entry.get("title") if isinstance(entry, dict) else entry
        if text:
            out.add(fold(text))
    return out


def _held_languages(sections: dict) -> set[str]:
    """Language codes the profile lists, from the LinkedIn ``languages`` section."""
    out: set[str] = set()
    for entry in sections.get("languages") or []:
        text = entry.get("name") if isinstance(entry, dict) else entry
        folded = fold(text)
        if not folded:
            continue
        for code, phrases in _LANGUAGE_PHRASES.items():
            if any(p in folded for p in phrases):
                out.add(code)
    return out


def _leadership_evidence(composite: dict | None, sections: dict) -> list[str]:
    """Statements in the seeker's own material that evidence leading people."""
    found: list[str] = []
    seniority = from_json(composite.get("seniority") if composite else None, None)
    level = fold((seniority or {}).get("level") if isinstance(seniority, dict) else seniority)
    if level.replace(" ", "_") in LEADERSHIP_LEVELS:
        found.append(f"composite seniority is '{level}'")
    for key in ("achievements", "career_trajectory"):
        for text in statement_texts(from_json(composite.get(key) if composite else None, [])):
            if any(phrase_in(p, text) for p in LEADERSHIP_PHRASES):
                found.append(text[:200])
    for entry in sections.get("experience") or []:
        title = (entry.get("title") if isinstance(entry, dict) else str(entry)) or ""
        if re.search(r"\b(lead|head|manager|director|chief|vp|cto|cio)\b", fold(title)):
            found.append(f"role title '{title}'")
    return found[:8]


def _visibility_items(composite: dict | None, evidence: list[dict]) -> list[str]:
    items = statement_texts(
        from_json(composite.get("public_footprint") if composite else None, [])
    )
    items += [
        str(row.get("title"))
        for row in evidence
        if str(row.get("kind") or "") in VISIBILITY_EVIDENCE_KINDS and row.get("title")
    ]
    return items[:20]


def _posting(row: dict[str, Any]) -> Posting:
    required = canonical_skills(row.get("required_skills") or row.get("vacancy_required_skills"))
    desirable = canonical_skills(
        row.get("desirable_skills") or row.get("vacancy_desirable_skills")
    )
    text = " ".join(
        str(x)
        for x in (
            row.get("title"),
            row.get("description") or row.get("vacancy_description"),
        )
        if x
    )
    detail = (
        ((row.get("score_detail") or {}).get("components") or {}).get("profile_fit") or {}
    ).get("detail") or {}
    return Posting(
        id=str(row["id"]),
        title=str(row.get("title") or ""),
        company_name=row.get("company_name"),
        score=row.get("score"),
        score_profile_fit=row.get("score_profile_fit"),
        required=required,
        desirable=desirable,
        text=text[:8000],
        language=(row.get("language") or row.get("vacancy_language")),
        seniority=row.get("seniority"),
        profile_fit_detail=detail if isinstance(detail, dict) else {},
    )


def load_inputs(job_seeker_id: str, campaign_id: str | None = None) -> GapInputs:
    """Everything the analysis reads, in one pass over the private tables."""
    campaign = (
        repo.get_campaign(campaign_id, job_seeker_id)
        if campaign_id
        else repo.latest_campaign(job_seeker_id)
    )
    persona_id = (campaign or {}).get("persona_id")
    composite = repo.latest_composite(job_seeker_id, persona_id) or repo.latest_composite(
        job_seeker_id
    )
    dream = repo.latest_dream_model(job_seeker_id, persona_id) or repo.latest_dream_model(
        job_seeker_id
    )
    profile = repo.latest_profile_version(job_seeker_id) or {}
    sections = profile.get("sections") or {}
    if not isinstance(sections, dict):
        sections = {}
    evidence = repo.evidence_items(job_seeker_id)
    rows = repo.campaign_opportunities(job_seeker_id, (campaign or {}).get("id"))
    directive_row = repo.directive_set(job_seeker_id, (campaign or {}).get("directive_set_id"))
    directives = dir_mod.coerce_directive_set(directive_row) if directive_row else None

    seniority_block = from_json(composite.get("seniority") if composite else None, None)
    return GapInputs(
        job_seeker_id=job_seeker_id,
        campaign=campaign,
        composite=composite,
        dream=dream,
        sections=sections,
        held_skills=_held_skills(job_seeker_id, composite, sections),
        held_certifications=_held_certifications(sections),
        held_languages=_held_languages(sections),
        evidence=evidence,
        postings=[_posting(r) for r in rows],
        directives=directives,
        seniority_rank=_seniority_rank(seniority_block),
        leadership_evidence=_leadership_evidence(composite, sections),
        visibility_items=_visibility_items(composite, evidence),
    )


# ---------------------------------------------------------------------------
# Points lost (the FR-381 link, computed)
# ---------------------------------------------------------------------------


def _present_weight_sum(posting: Posting, fallback: tuple[str, ...]) -> float:
    """Sum of the profile-fit component weights that actually scored.

    ``scoring._weighted`` normalises over the components that had a value, so
    a posting with no listed desirable skills gives its required-skill overlap
    more than half the sub-score.  Using the same denominator keeps "points
    lost" truthful rather than merely indicative.
    """
    detail = posting.profile_fit_detail
    present = [k for k, w in PROFILE_FIT_WEIGHTS.items() if detail.get(k) is not None]
    if not present:
        present = list(fallback)
    return sum(PROFILE_FIT_WEIGHTS[k] for k in present) or 1.0


def _skill_points_lost(posting: Posting) -> float:
    """Profile-fit points one missing required skill costs this posting."""
    if not posting.required:
        return 0.0
    denominator = _present_weight_sum(posting, ("required_skill_overlap",))
    share = 1.0 / len(posting.required)
    return 100.0 * PROFILE_FIT_WEIGHTS["required_skill_overlap"] * share / denominator


def _seniority_points_lost(posting: Posting) -> float:
    detail = posting.profile_fit_detail
    seniority_score = detail.get("seniority_score")
    if seniority_score is None:
        return 0.0
    denominator = _present_weight_sum(posting, ("seniority_score",))
    return (
        100.0 * PROFILE_FIT_WEIGHTS["seniority_score"] * (1.0 - float(seniority_score))
        / denominator
    )


def _link(
    posting: Posting, points: float, why: str, sub_score: str = "profile_fit"
) -> DecisiveLink:
    return DecisiveLink(
        opportunity_id=posting.id,
        title=posting.title,
        company_name=posting.company_name,
        score=posting.score,
        sub_score=sub_score,
        sub_score_value=posting.score_profile_fit,
        points_lost=points,
        why=why,
    )


def _rank_links(links: list[DecisiveLink]) -> list[DecisiveLink]:
    links.sort(key=lambda link: (-link.points_lost, -(link.score or 0.0)))
    return links[:MAX_DECISIVE_LINKS]


def _severity(share: float, hard: bool = False) -> str:
    if hard or share >= 0.5:
        return "blocking"
    if share >= 0.2:
        return "significant"
    return "minor"


def _effort(months: float, detail: str) -> dict[str, Any]:
    if months <= 1:
        level = "weeks"
    elif months <= 4:
        level = "months"
    elif months <= 12:
        level = "quarters"
    else:
        level = "years"
    return {"months": round(months, 1), "level": level, "detail": detail}


# ---------------------------------------------------------------------------
# The six dimensions
# ---------------------------------------------------------------------------


def _skill_gaps(inputs: GapInputs) -> list[Gap]:
    """Skills the corpus requires that the profile does not evidence."""
    postings = [p for p in inputs.postings if p.required]
    if not postings:
        return []
    demand: Counter[str] = Counter()
    for posting in postings:
        for skill in set(posting.required):
            if not skill_held(skill, inputs.held_skills):
                demand[skill] += 1

    total = len(postings)
    threshold = max(MIN_POSTINGS_FOR_DEMAND, round(total * 0.15))
    gaps: list[Gap] = []
    for skill, count in demand.most_common(MAX_GAPS_PER_DIMENSION * 2):
        if count < threshold:
            continue
        links = [
            _link(
                posting,
                _skill_points_lost(posting),
                f"'{skill}' is one of {len(posting.required)} required skills that this "
                "posting lists and the profile does not evidence",
            )
            for posting in postings
            if skill in posting.required
        ]
        links = [link for link in links if link.points_lost > 0]
        share = count / total
        gaps.append(
            Gap(
                id=f"skill:{skill}",
                dimension="skill",
                label=f"Skill not evidenced: {skill}",
                evidence=(
                    f"{count} of {total} collected postings require it; no profile skill, "
                    "competency or listed strength covers it"
                ),
                closing_action=_skill_action(skill),
                effort=_effort(
                    3.0,
                    "a focused course alongside work, followed by one project that produces "
                    "something showable",
                ),
                decisive_opportunities=_rank_links(links),
                severity=_severity(share),
                demand={"postings": count, "corpus": total, "share": round(share, 3)},
                basis="score_delta",
            )
        )
        if len(gaps) >= MAX_GAPS_PER_DIMENSION:
            break
    return gaps


def _skill_action(skill: str) -> dict[str, Any]:
    """A closing action that names a thing to do, not a thing to be."""
    for needle, name, _months in CERTIFICATIONS:
        if needle in skill:
            return {
                "kind": "certification",
                "action": f"Obtain {name}",
                "detail": "The corpus names this credential explicitly.",
            }
    return {
        "kind": "project",
        "action": f"Build and publish one piece of work that uses {skill}",
        "detail": (
            f"Take a course to cover the fundamentals of {skill}, then apply it in a "
            "project - internal or open - that can be named on the CV and shown in an "
            "interview. A listed course without an artefact does not close this gap."
        ),
    }


def _certification_gaps(inputs: GapInputs) -> list[Gap]:
    postings = inputs.postings
    if not postings:
        return []
    total = len(postings)
    demand: dict[str, tuple[str, float, list[Posting]]] = {}
    for needle, name, months in CERTIFICATIONS:
        if any(needle in cert for cert in inputs.held_certifications):
            continue
        if any(needle in skill for skill in inputs.held_skills):
            continue
        matched = [p for p in postings if phrase_in(needle, p.haystack)]
        if len(matched) < MIN_POSTINGS_FOR_DEMAND:
            continue
        demand[name] = (needle, months, matched)

    gaps: list[Gap] = []
    for name, (needle, months, matched) in sorted(
        demand.items(), key=lambda kv: -len(kv[1][2])
    )[:MAX_GAPS_PER_DIMENSION]:
        links = []
        for posting in matched:
            points = _skill_points_lost(posting) if needle in " ".join(posting.required) else 0.0
            links.append(
                _link(
                    posting,
                    points,
                    f"the posting asks for {name}"
                    + (
                        "; it is listed among the required skills, so it costs profile-fit "
                        "points directly"
                        if points
                        else "; it is named in the posting text rather than in the required "
                        "skills, so it filters rather than scores"
                    ),
                )
            )
        share = len(matched) / total
        gaps.append(
            Gap(
                id=f"certification:{fold(name)}",
                dimension="certification",
                label=f"Certification not held: {name}",
                evidence=(
                    f"{len(matched)} of {total} postings name it; it appears in neither the "
                    "certifications nor the courses section of the profile"
                ),
                closing_action={
                    "kind": "certification",
                    "action": f"Sit the {name} exam",
                    "detail": (
                        "Book the exam date first and work back from it; the credential is a "
                        "screening filter in this corpus, so a planned date is already worth "
                        "stating in the application."
                    ),
                },
                effort=_effort(months, "typical study time for a working professional"),
                decisive_opportunities=_rank_links(links),
                severity=_severity(share),
                demand={"postings": len(matched), "corpus": total, "share": round(share, 3)},
                basis="requirement_named",
            )
        )
    return gaps


def _language_gaps(inputs: GapInputs) -> list[Gap]:
    postings = inputs.postings
    if not postings:
        return []
    total = len(postings)
    demand: dict[str, list[tuple[Posting, str]]] = {}
    for posting in postings:
        code = (posting.language or "").strip().lower()[:2]
        if code and code not in inputs.held_languages and code in LANGUAGE_NAMES:
            demand.setdefault(code, []).append((posting, "the posting itself is written in it"))
        for match in _LANGUAGE_REQUIREMENT_RE.finditer(posting.haystack):
            named = fold(match.group(2))
            for language_code, phrases in _LANGUAGE_PHRASES.items():
                if named in phrases and language_code not in inputs.held_languages:
                    demand.setdefault(language_code, []).append(
                        (posting, f"the posting asks for '{match.group(0).strip()}'")
                    )

    gaps: list[Gap] = []
    for code, matched in sorted(demand.items(), key=lambda kv: -len(kv[1])):
        unique: dict[str, tuple[Posting, str]] = {}
        for posting, why in matched:
            unique.setdefault(posting.id, (posting, why))
        if len(unique) < MIN_POSTINGS_FOR_DEMAND:
            continue
        name = LANGUAGE_NAMES[code]
        share = len(unique) / total
        gaps.append(
            Gap(
                id=f"language:{code}",
                dimension="language",
                label=f"Working language not listed: {name}",
                evidence=(
                    f"{len(unique)} of {total} postings are in {name} or ask for it, and the "
                    "profile's languages section does not list it"
                ),
                closing_action={
                    "kind": "course",
                    "action": f"Reach professional working proficiency (B2) in {name}",
                    "detail": (
                        "Two evening classes a week plus one weekly conversation hour is the "
                        "pace that gets a working professional from A2 to B2 inside a year; "
                        "add the level to the profile as soon as it is examined."
                    ),
                },
                effort=_effort(9.0, "A2 to B2 at two lessons a week"),
                decisive_opportunities=_rank_links(
                    [
                        _link(posting, 0.0, why, sub_score="eligibility")
                        for posting, why in unique.values()
                    ]
                ),
                severity=_severity(share),
                demand={"postings": len(unique), "corpus": total, "share": round(share, 3)},
                basis="requirement_named",
            )
        )
        if len(gaps) >= MAX_GAPS_PER_DIMENSION:
            break
    return gaps


def _seniority_gap(inputs: GapInputs) -> list[Gap]:
    """The experience dimension: scope the market expects above the profile's."""
    seeker_rank = inputs.seniority_rank
    links: list[DecisiveLink] = []
    for posting in inputs.postings:
        points = _seniority_points_lost(posting)
        if points <= 0.5:
            continue
        opportunity_rank = _seniority_rank(posting.seniority)
        if (
            seeker_rank is not None
            and opportunity_rank is not None
            and opportunity_rank <= seeker_rank
        ):
            continue
        links.append(
            _link(
                posting,
                points,
                f"the posting is pitched at '{posting.seniority or 'a higher band'}' and the "
                "profile's recorded seniority is below it",
            )
        )
    if len(links) < MIN_POSTINGS_FOR_DEMAND:
        return []

    wanted = _dream_seniority(inputs.dream)
    share = len(links) / max(len(inputs.postings), 1)
    return [
        Gap(
            id="experience:seniority",
            dimension="experience",
            label=(
                f"Seniority band below the target: {wanted}"
                if wanted
                else "Seniority band below what the collected postings ask for"
            ),
            evidence=(
                f"{len(links)} postings scored the profile below their seniority band; the "
                "profile's own level is "
                + (str(seeker_rank) if seeker_rank is not None else "not recorded")
            ),
            closing_action={
                "kind": "role_type",
                "action": (
                    "Take on one assignment with the missing scope inside the current role - "
                    "owning a budget, a programme or a small team - and record it as a "
                    "dated achievement"
                ),
                "detail": (
                    "The band is read from titles and responsibilities. An interim or "
                    "project-lead assignment closes it faster than a job move, and it is "
                    "evidence a CV can carry."
                ),
            },
            effort=_effort(12.0, "one full assignment cycle, evidenced by an outcome"),
            decisive_opportunities=_rank_links(links),
            severity=_severity(share),
            demand={
                "postings": len(links),
                "corpus": len(inputs.postings),
                "share": round(share, 3),
            },
            basis="score_delta",
        )
    ]


def _dream_seniority(dream: dict | None) -> str | None:
    for role in from_json((dream or {}).get("target_roles"), []) or []:
        if isinstance(role, dict) and role.get("seniority"):
            return str(role["seniority"])
    return None


def _leadership_gap(inputs: GapInputs) -> list[Gap]:
    if inputs.leadership_evidence:
        return []
    matched = [
        posting
        for posting in inputs.postings
        if any(phrase_in(phrase, posting.haystack) for phrase in LEADERSHIP_PHRASES)
    ]
    dream_wants = _dream_wants_leadership(inputs.dream)
    if len(matched) < MIN_POSTINGS_FOR_DEMAND and not dream_wants:
        return []

    links = [
        _link(
            posting,
            _seniority_points_lost(posting),
            "the posting describes leading people or owning a budget, and the profile "
            "evidences neither",
        )
        for posting in matched
    ]
    share = len(matched) / max(len(inputs.postings), 1)
    return [
        Gap(
            id="leadership:scope",
            dimension="leadership",
            label="Leadership scope not evidenced",
            evidence=(
                (
                    f"{len(matched)} of {len(inputs.postings)} postings ask for people "
                    "management, a team or budget ownership"
                    if matched
                    else "the dream-job statement describes leading people"
                )
                + "; no role title, achievement or composite statement records it"
            ),
            closing_action={
                "kind": "role_type",
                "action": (
                    "Lead one cross-team initiative end to end, with named people reporting "
                    "into it for its duration, and write the outcome up with numbers"
                ),
                "detail": (
                    "Formal line management is not the only evidence a hiring manager "
                    "accepts: a chapter lead, a squad lead or a programme with a budget "
                    "reads as scope, provided the write-up names the size and the result."
                ),
            },
            effort=_effort(12.0, "one initiative, from mandate to written-up outcome"),
            decisive_opportunities=_rank_links(links),
            severity=_severity(share, hard=bool(dream_wants and share >= 0.4)),
            demand={
                "postings": len(matched),
                "corpus": len(inputs.postings),
                "share": round(share, 3),
                "dream_job_requires": bool(dream_wants),
            },
            basis="requirement_named",
        )
    ]


def _dream_wants_leadership(dream: dict | None) -> bool:
    haystack = " ".join(
        str(item.get("activity") or item.get("value") or "")
        for key in ("responsibilities", "company_characteristics")
        for item in (from_json((dream or {}).get(key), []) or [])
        if isinstance(item, dict)
    )
    haystack += " " + str((dream or {}).get("statement") or "")
    return any(phrase_in(phrase, haystack) for phrase in LEADERSHIP_PHRASES)


def _visibility_gap(inputs: GapInputs) -> list[Gap]:
    if len(inputs.visibility_items) >= 2:
        return []
    matched = [
        posting
        for posting in inputs.postings
        if any(phrase_in(phrase, posting.haystack) for phrase in VISIBILITY_PHRASES)
    ]
    if len(matched) < MIN_POSTINGS_FOR_DEMAND:
        return []
    share = len(matched) / max(len(inputs.postings), 1)
    return [
        Gap(
            id="visibility:public_footprint",
            dimension="visibility",
            label="Public footprint too thin to be found or checked",
            evidence=(
                f"{len(matched)} of {len(inputs.postings)} postings value talks, publications, "
                "open-source or community presence; the profile records "
                f"{len(inputs.visibility_items)} public item(s)"
            ),
            closing_action={
                "kind": "publication",
                "action": (
                    "Publish one substantial written piece and give one talk on the same "
                    "subject within the next quarter, then attach both as evidence items"
                ),
                "detail": (
                    "One article that answers a question the target companies actually have, "
                    "and the same material as a meetup talk. Two artefacts are what turn "
                    "'no footprint' into 'a footprint', and they are also what a recruiter "
                    "searching for the dream job can find."
                ),
            },
            effort=_effort(3.0, "one article and one talk, reusing the same material"),
            decisive_opportunities=_rank_links(
                [
                    _link(
                        posting,
                        0.0,
                        "the posting names publications, talks, open source or community work",
                        sub_score="reachability",
                    )
                    for posting in matched
                ]
            ),
            severity=_severity(share),
            demand={
                "postings": len(matched),
                "corpus": len(inputs.postings),
                "share": round(share, 3),
            },
            basis="requirement_named",
        )
    ]


def _responsibility_gaps(inputs: GapInputs) -> list[Gap]:
    """Dream-job activities the profile does not evidence.

    This is the dimension that still works when no postings have been collected
    yet: the dream job model alone says what the target role does, and the
    composite profile says what the material shows.
    """
    if not inputs.dream:
        return []
    held_text = " ".join(
        text
        for key in ("core_competencies", "adjacent_competencies", "achievements",
                    "career_trajectory")
        for text in statement_texts(
            from_json(inputs.composite.get(key) if inputs.composite else None, [])
        )
    )
    held_tokens = tokens(held_text) | {t for skill in inputs.held_skills for t in tokens(skill)}

    gaps: list[Gap] = []
    for index, item in enumerate(from_json(inputs.dream.get("responsibilities"), []) or [], 1):
        if not isinstance(item, dict):
            item = {"activity": str(item)}
        activity = str(item.get("activity") or "").strip()
        importance = str(item.get("importance") or "strong")
        if not activity or importance == "nice":
            continue
        wanted = tokens(activity)
        if not wanted:
            continue
        coverage = len(wanted & held_tokens) / len(wanted)
        if coverage >= 0.5:
            continue
        matched = [
            p
            for p in inputs.postings
            if len(wanted & tokens(p.haystack)) >= max(1, len(wanted) // 2)
        ]
        links = [
            _link(
                posting,
                _skill_points_lost(posting) if posting.required else 0.0,
                "the posting describes this activity and the profile does not evidence it",
            )
            for posting in matched
        ]
        gaps.append(
            Gap(
                id=f"experience:responsibility:{index}",
                dimension="experience",
                label=f"Dream-job activity not evidenced: {activity[:120]}",
                evidence=(
                    f"the dream-job statement calls this a '{importance}' responsibility; the "
                    f"composite profile covers {coverage:.0%} of its vocabulary"
                ),
                closing_action={
                    "kind": "project",
                    "action": (
                        f"Take one piece of work that is genuinely about {activity[:80]} and "
                        "see it through to a result worth writing down"
                    ),
                    "detail": (
                        "Choose it inside the current role where possible - a side project "
                        "that nobody depends on reads as a hobby, and a delivered piece of "
                        "work reads as experience."
                    ),
                },
                effort=_effort(6.0 if importance == "must" else 4.0, "one delivered piece of work"),
                decisive_opportunities=_rank_links(links),
                severity="blocking" if importance == "must" else "significant",
                demand={
                    "postings": len(matched),
                    "corpus": len(inputs.postings),
                    "share": round(len(matched) / max(len(inputs.postings), 1), 3),
                    "dream_job_importance": importance,
                },
                basis="dream_job_model",
            )
        )
        if len(gaps) >= MAX_GAPS_PER_DIMENSION:
            break
    return gaps


# ---------------------------------------------------------------------------
# LLM refinement (optional, NFR-104, NFR-205, CR-405)
# ---------------------------------------------------------------------------


def _refine(gaps: list[Gap], inputs: GapInputs, llm: LLMClient) -> tuple[list[Gap], str, bool]:
    """Let the model sharpen wording and name concrete courses or credentials.

    The gap set, the dimensions and every number stay as computed; only
    ``closing_action`` text, ``effort.detail`` and the summary can change, and
    an unparseable or budget-exhausted call leaves the deterministic report
    untouched (NFR-104).
    """
    template = load_prompt(PROMPT_NAME)
    system, user = template.render(language="en")
    payload = {
        "gaps": [
            {
                "id": g.id,
                "dimension": g.dimension,
                "label": g.label,
                "evidence": g.evidence,
                "closing_action": g.closing_action,
                "effort_months": g.effort.get("months"),
                "decisive_opportunities": len(g.decisive_opportunities),
            }
            for g in gaps
        ],
        "dream_job": {
            "statement": (inputs.dream or {}).get("statement"),
            "target_roles": from_json((inputs.dream or {}).get("target_roles"), []),
        },
        "profile": {
            "seniority": from_json((inputs.composite or {}).get("seniority"), None),
            "core_competencies": statement_texts(
                from_json((inputs.composite or {}).get("core_competencies"), [])
            )[:15],
        },
    }
    try:
        data = llm.complete_json(
            template.task or "analysis.gap",
            system=system,
            user=user,
            untrusted={"analysis": json.dumps(payload, ensure_ascii=False)[:14000]},
            entity_type="gap_analysis",
            entity_id=(inputs.campaign or {}).get("id"),
            prompt_template=template.name,
            prompt_version=template.version,
            # The gaps, their dimensions and their numbers are already computed;
            # what is left is rewriting. The reasoning model spent its whole
            # output budget deliberating over that and returned nothing usable,
            # so the chat model is the right instrument here.
            prefer_strong=False,
            temperature=0.3,
            max_tokens=2500,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("Gap analysis stays deterministic: %s", exc)
        return gaps, "", False
    if not isinstance(data, dict):
        return gaps, "", False

    by_id = {g.id: g for g in gaps}
    for item in data.get("gaps") or []:
        if not isinstance(item, dict):
            continue
        gap = by_id.get(str(item.get("id") or ""))
        if gap is None:
            continue
        action = str(item.get("action") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if action:
            gap.closing_action = {
                **gap.closing_action,
                "action": action[:400],
                "detail": (detail or gap.closing_action.get("detail", ""))[:800],
            }
            gap.source = "llm+deterministic"
        months = item.get("effort_months")
        if isinstance(months, (int, float)) and 0 < float(months) <= 60:
            gap.effort = _effort(
                float(months), str(item.get("effort_detail") or gap.effort.get("detail", ""))[:300]
            )
    return gaps, str(data.get("summary") or "").strip()[:1200], True


def _fallback_summary(gaps: list[Gap], inputs: GapInputs) -> str:
    if not gaps:
        return (
            "No gap could be computed: the profile, the dream-job model or the collected "
            "postings are still too thin to compare."
        )
    top = gaps[0]
    blocking = [g for g in gaps if g.severity == "blocking"]
    linked = sum(len(g.decisive_opportunities) for g in gaps)
    return (
        f"{len(gaps)} gaps between the profile and the dream job, across "
        f"{len({g.dimension for g in gaps})} of the six dimensions. "
        f"The largest is {top.label.lower()}, which costs points in "
        f"{len(top.decisive_opportunities)} of the {len(inputs.postings)} collected "
        f"opportunities. "
        + (f"{len(blocking)} of them are blocking. " if blocking else "")
        + f"{linked} opportunity links were computed from the stored sub-scores."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def analyse(
    job_seeker_id: str,
    campaign_id: str | None = None,
    *,
    use_llm: bool = True,
    persist: bool = True,
    llm: LLMClient | None = None,
    max_gaps: int = DEFAULT_MAX_GAPS,
) -> GapReport:
    """Produce the FR-381 gap analysis for one job seeker and campaign."""
    inputs = load_inputs(job_seeker_id, campaign_id)

    gaps: list[Gap] = []
    gaps += _skill_gaps(inputs)
    gaps += _certification_gaps(inputs)
    gaps += _language_gaps(inputs)
    gaps += _seniority_gap(inputs)
    gaps += _leadership_gap(inputs)
    gaps += _visibility_gap(inputs)
    gaps += _responsibility_gaps(inputs)

    # Severity first, then the measured cost, then how much of the market asks
    # for it: the order the seeker should read them in.
    severity_rank = {"blocking": 0, "significant": 1, "minor": 2}
    gaps.sort(
        key=lambda g: (
            severity_rank.get(g.severity, 3),
            -g.total_points_lost,
            -float(g.demand.get("share") or 0.0),
        )
    )
    gaps = gaps[:max_gaps]

    warnings: list[str] = []
    if len(gaps) < 3:
        warnings.append(
            "Fewer than three gaps could be computed. That is usually a data gap rather "
            "than a profile without gaps: check that a composite profile, a dream-job "
            "model and a scored ranked list all exist for this campaign."
        )
    if not inputs.postings:
        warnings.append(
            "No opportunities have been scored for this campaign, so no gap could be "
            "linked to the postings where it is decisive (FR-381)."
        )

    generated_by = "deterministic"
    summary = ""
    client = llm
    if use_llm and client is None:
        client = default_llm(job_seeker_id, (inputs.campaign or {}).get("id"))
    if client is not None and gaps:
        gaps, summary, refined = _refine(gaps, inputs, client)
        if refined:
            generated_by = "llm+deterministic"
    if not summary:
        summary = _fallback_summary(gaps, inputs)

    discretion = bool(getattr(inputs.directives, "discretion_mode", False))
    report = GapReport(
        job_seeker_id=job_seeker_id,
        campaign_id=(inputs.campaign or {}).get("id"),
        gaps=gaps,
        market={
            "opportunities": len(inputs.postings),
            "postings_with_required_skills": sum(1 for p in inputs.postings if p.required),
            "skills_held": len(inputs.held_skills),
            "languages_held": sorted(inputs.held_languages),
            "public_items": len(inputs.visibility_items),
        },
        summary=summary,
        generated_by=generated_by,
        dream_job_model_id=(inputs.dream or {}).get("id"),
        composite_profile_id=(inputs.composite or {}).get("id"),
        discretion_mode=discretion,
        warnings=warnings,
    )

    if persist:
        repo.save_gap_analysis(
            job_seeker_id,
            {
                "campaign_id": report.campaign_id,
                "dream_job_model_id": report.dream_job_model_id,
                "composite_profile_id": report.composite_profile_id,
                "gaps": [g.as_dict() for g in gaps],
                "summary": summary,
                "generated_by": generated_by,
            },
        )
    return report


def stored(job_seeker_id: str, campaign_id: str | None = None) -> dict[str, Any] | None:
    """The last computed analysis, as stored (FR-381)."""
    return repo.get_gap_analysis(job_seeker_id, campaign_id)
