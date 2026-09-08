"""Company values against the dream-job statement (FR-384, FR-330, FR-385, CR-405).

FR-384 has two halves.  The first - deriving a company's values and working
style from its website, leadership statements, job-advertisement language and
employer reviews - is done by the company-profile slice and lands in
``company.values_culture``.  This module is the second half: comparing those
derived cues with the values the dream-job statement expresses, and surfacing
the mismatches **as explicit warnings**, both on the company profile and in the
motivation and fit document (FR-330).

A mismatch is stated only when the material contradicts the cue, never when it
is silent about it (CR-405).  Silence is reported as ``unknown``, which is a
question for the interview rather than a warning on a profile.

The comparison is deterministic and evidence-carrying: every warning names the
company cue it came from, the source that cue was derived from, and the words
of the dream-job statement it contradicts.  The LLM, when configured, only
rewrites the explanation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.connection import from_json
from dreamjob.db.repositories import intelligence as repo
from dreamjob.intelligence.text import fold, phrase_in, tokens
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline import directives as dir_mod
from dreamjob.pipeline.enrichment import default_llm, load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "values_match"

SEVERITY_BLOCKING = "blocking"
SEVERITY_WARNING = "warning"
SEVERITY_CAUTION = "caution"

#: What the dream-job statement asks for, and the company language that
#: contradicts it.  Both sides are phrases as they actually appear in company
#: pages and job advertisements; the pairs are what turns "we value autonomy"
#: and "close supervision by the team lead" into a stated warning rather than
#: two unrelated bullet points.
CONTRASTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("autonomy", "autonomous", "ownership", "self-directed", "freedom", "zelfstandig"),
        ("micromanagement", "micromanage", "close supervision", "command and control",
         "strict oversight", "top-down", "top down", "closely monitored"),
    ),
    (
        ("flat hierarchy", "flat structure", "non-hierarchical", "no titles"),
        ("hierarchical", "layers of management", "chain of command", "matrix reporting",
         "top down"),
    ),
    (
        ("remote", "remote-first", "hybrid", "flexible hours", "flexibility", "telewerk"),
        ("five days in the office", "5 days in the office", "on-site five days", "office-first",
         "no remote", "fully on site", "fully onsite", "presence in the office is required"),
    ),
    (
        ("work-life balance", "sustainable pace", "family-friendly", "balance"),
        ("long hours", "crunch", "always on", "hustle", "whatever it takes", "high pressure",
         "demanding pace"),
    ),
    (
        ("consensus", "collaborative decision", "collegial", "co-creation"),
        ("top-down", "top down", "founder decides", "command and control",
         "decisions are made at head office"),
    ),
    (
        ("stability", "long-term", "predictable", "steady"),
        ("constant change", "pivot", "restructuring", "reorganisation", "turnaround",
         "fast-changing priorities"),
    ),
    (
        ("fast-paced", "startup pace", "move quickly", "high tempo"),
        ("bureaucratic", "committee", "lengthy approval", "slow-moving", "risk-averse"),
    ),
    (
        ("transparency", "open communication", "open book"),
        ("need-to-know", "confidential culture", "information is closely held"),
    ),
    (
        ("diversity", "inclusive", "inclusion", "belonging"),
        ("boys club", "homogeneous team", "culture fit above all"),
    ),
    (
        ("learning", "development", "growth", "coaching", "mentoring"),
        ("no training budget", "learn on the job only", "sink or swim"),
    ),
    (
        ("innovation", "experimentation", "r&d", "research"),
        ("legacy systems", "risk-averse", "conservative", "maintenance mode"),
    ),
    (
        ("craft", "quality", "engineering excellence", "craftsmanship"),
        ("move fast and break things", "ship first", "good enough is good enough"),
    ),
    (
        ("purpose", "mission-driven", "impact", "social", "sustainability"),
        ("shareholder value above all", "profit first"),
    ),
    (
        ("international", "multicultural", "global teams"),
        ("local market only", "single-country"),
    ),
)


# ---------------------------------------------------------------------------
# Cues
# ---------------------------------------------------------------------------


@dataclass
class CompanyCue:
    """One derived cue about how the company works (FR-384)."""

    text: str
    kind: str                       # stated_value|working_style|leadership|job_ad|review
    evidence: str = ""
    source: str = ""
    sentiment: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "cue": self.text,
            "kind": self.kind,
            "evidence": self.evidence,
            "source": self.source,
            "sentiment": self.sentiment,
        }


@dataclass
class DreamCue:
    """One value the dream-job statement expresses (FR-128)."""

    cue: str
    polarity: str = "seek"          # seek | avoid
    why: str = ""
    quote: str = ""
    hard: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "cue": self.cue,
            "polarity": self.polarity,
            "why": self.why,
            "quote": self.quote,
            "hard": self.hard,
        }


@dataclass
class ValueFinding:
    """One comparison outcome, aligned or mismatched, with its evidence."""

    cue: str
    polarity: str
    status: str                     # aligned | mismatch | unknown
    severity: str | None
    explanation: str
    company_cues: list[dict[str, Any]] = field(default_factory=list)
    quote: str = ""
    source: str = "deterministic"

    def as_dict(self) -> dict[str, Any]:
        return {
            "cue": self.cue,
            "polarity": self.polarity,
            "status": self.status,
            "severity": self.severity,
            "explanation": self.explanation,
            "company_cues": self.company_cues,
            "quote": self.quote,
            "source": self.source,
        }


@dataclass
class ValuesMatch:
    """The FR-384 comparison for one company."""

    company_id: str | None
    company_name: str | None
    aligned: list[ValueFinding] = field(default_factory=list)
    warnings: list[ValueFinding] = field(default_factory=list)
    unknown: list[ValueFinding] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    confidence: float | None = None
    generated_by: str = "deterministic"
    discretion_mode: bool = False
    excluded_reason: str | None = None

    @property
    def score(self) -> float | None:
        considered = len(self.aligned) + len(self.warnings)
        if not considered:
            return None
        return round(len(self.aligned) / considered, 3)

    @property
    def has_blocking_warning(self) -> bool:
        return any(w.severity == SEVERITY_BLOCKING for w in self.warnings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "score": self.score,
            "confidence": self.confidence,
            "generated_by": self.generated_by,
            "discretion_mode": self.discretion_mode,
            "excluded_reason": self.excluded_reason,
            "has_blocking_warning": self.has_blocking_warning,
            "warnings": [w.as_dict() for w in self.warnings],
            "aligned": [a.as_dict() for a in self.aligned],
            "unknown": [u.as_dict() for u in self.unknown],
            "derived_from": self.derived_from,
            "counts": {
                "aligned": len(self.aligned),
                "warnings": len(self.warnings),
                "unknown": len(self.unknown),
            },
            "note": (
                "A value is only reported as a mismatch when the company's own material "
                "contradicts it. Silence is reported as unknown - a question for the "
                "interview, not a warning (CR-405)."
            ),
        }


# ---------------------------------------------------------------------------
# Reading both sides
# ---------------------------------------------------------------------------


def company_cues(values_culture: Any) -> list[CompanyCue]:
    """Flatten ``company.values_culture`` into comparable cues (FR-384)."""
    block = from_json(values_culture, None) if not isinstance(values_culture, dict) else (
        values_culture
    )
    if not isinstance(block, dict):
        return []
    cues: list[CompanyCue] = []

    def add(items: Any, kind: str, text_keys: tuple[str, ...]) -> None:
        for item in items or []:
            if isinstance(item, dict):
                text = next((str(item[k]) for k in text_keys if item.get(k)), "")
                evidence = str(item.get("evidence") or item.get("quote") or "")
                source = str(item.get("source") or "")
                sentiment = str(item.get("sentiment") or "")
            else:
                text, evidence, source, sentiment = str(item), "", "", ""
            if text.strip():
                cues.append(CompanyCue(text.strip(), kind, evidence[:400], source[:300], sentiment))

    add(block.get("stated_values"), "stated_value", ("value", "cue", "text"))
    add(block.get("working_style"), "working_style", ("cue", "value", "text"))
    add(block.get("leadership_statements"), "leadership", ("quote", "statement", "text"))
    add(block.get("job_ad_language"), "job_ad", ("cue", "text"))
    add(block.get("employer_review_themes"), "review", ("theme", "text"))
    return cues


def dream_cues(dream: dict[str, Any] | None) -> list[DreamCue]:
    """The values half of the dream job model (FR-128), plus culture deal-breakers."""
    if not dream:
        return []
    out: list[DreamCue] = []
    for item in from_json(dream.get("culture_values"), []) or []:
        if isinstance(item, dict):
            cue = str(item.get("cue") or item.get("value") or "").strip()
            polarity = str(item.get("polarity") or "seek").strip().lower()
            out.append(
                DreamCue(
                    cue=cue,
                    polarity="avoid" if polarity == "avoid" else "seek",
                    why=str(item.get("why") or "")[:400],
                    quote=str(item.get("quote") or "")[:300],
                )
            )
        elif str(item).strip():
            out.append(DreamCue(cue=str(item).strip()))

    for item in from_json(dream.get("deal_breakers"), []) or []:
        if not isinstance(item, dict):
            item = {"constraint": str(item)}
        constraint = str(item.get("constraint") or "").strip()
        if not constraint:
            continue
        # Only culture-shaped deal-breakers belong here; the rest (location,
        # contract type) are the directives' and the scorer's business.
        if not _is_culture_shaped(constraint):
            continue
        out.append(
            DreamCue(
                cue=constraint,
                polarity="avoid",
                why="named as a deal-breaker in the dream-job statement",
                quote=str(item.get("quote") or "")[:300],
                hard=bool(item.get("hard", True)),
            )
        )

    for item in from_json(dream.get("company_characteristics"), []) or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("attribute") or "").strip().lower() not in ("mission", "team_structure"):
            continue
        value = str(item.get("value") or "").strip()
        if value:
            out.append(
                DreamCue(
                    cue=value,
                    polarity="seek",
                    why=f"stated {item.get('attribute')} preference",
                    quote=str(item.get("quote") or "")[:300],
                )
            )
    return [c for c in out if c.cue]


_CULTURE_WORDS = frozenset(
    {
        "culture", "hierarchy", "hierarchical", "micromanagement", "politics", "bureaucracy",
        "overtime", "hours", "balance", "autonomy", "values", "management", "leadership",
        "team", "people", "atmosphere", "pace", "pressure", "consensus", "transparency",
        "diversity", "inclusion", "learning", "innovation", "mission", "purpose",
    }
)


def _is_culture_shaped(text: str) -> bool:
    return bool(tokens(text) & _CULTURE_WORDS)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def _contrast_phrases(cue: str) -> tuple[str, ...]:
    folded = fold(cue)
    for synonyms, opposites in CONTRASTS:
        if any(word in folded for word in synonyms):
            return opposites
    return ()


def _matching_cues(cue: str, cues: list[CompanyCue]) -> list[CompanyCue]:
    wanted = tokens(cue)
    if not wanted:
        return []
    hits: list[CompanyCue] = []
    for company_cue in cues:
        haystack = f"{company_cue.text} {company_cue.evidence}"
        if phrase_in(cue, haystack):
            hits.append(company_cue)
            continue
        overlap = wanted & tokens(haystack)
        if overlap and len(overlap) >= max(1, len(wanted) // 2):
            hits.append(company_cue)
    return hits


def _contradicting_cues(cue: str, cues: list[CompanyCue]) -> list[CompanyCue]:
    phrases = _contrast_phrases(cue)
    if not phrases:
        return []
    return [
        company_cue
        for company_cue in cues
        if any(phrase_in(p, f"{company_cue.text} {company_cue.evidence}") for p in phrases)
    ]


def compare(
    dream: list[DreamCue], company: list[CompanyCue], *, company_name: str | None = None
) -> ValuesMatch:
    """Compare the two sides.  Pure function - no database, no network."""
    match = ValuesMatch(company_id=None, company_name=company_name)
    for cue in dream:
        matched = _matching_cues(cue.cue, company)
        contradicted = _contradicting_cues(cue.cue, company)
        negative_reviews = [
            c for c in matched if c.kind == "review" and c.sentiment.lower() == "negative"
        ]

        if cue.polarity == "avoid" and matched:
            match.warnings.append(
                ValueFinding(
                    cue=cue.cue,
                    polarity=cue.polarity,
                    status="mismatch",
                    severity=SEVERITY_BLOCKING if cue.hard else SEVERITY_WARNING,
                    explanation=(
                        f"The dream-job statement rules out '{cue.cue}', and the company's "
                        f"own material describes it: {matched[0].text}."
                    ),
                    company_cues=[c.as_dict() for c in matched[:3]],
                    quote=cue.quote,
                )
            )
            continue
        if cue.polarity == "avoid":
            match.unknown.append(
                ValueFinding(
                    cue=cue.cue,
                    polarity=cue.polarity,
                    status="unknown",
                    severity=None,
                    explanation=(
                        f"Nothing collected about this company mentions '{cue.cue}'. Worth "
                        "asking about; not evidence either way."
                    ),
                    quote=cue.quote,
                )
            )
            continue

        if contradicted:
            match.warnings.append(
                ValueFinding(
                    cue=cue.cue,
                    polarity=cue.polarity,
                    status="mismatch",
                    severity=SEVERITY_WARNING,
                    explanation=(
                        f"The dream-job statement asks for '{cue.cue}'. The company's material "
                        f"points the other way: {contradicted[0].text}"
                        + (f" ({contradicted[0].evidence})" if contradicted[0].evidence else "")
                        + "."
                    ),
                    company_cues=[c.as_dict() for c in contradicted[:3]],
                    quote=cue.quote,
                )
            )
        elif negative_reviews:
            match.warnings.append(
                ValueFinding(
                    cue=cue.cue,
                    polarity=cue.polarity,
                    status="mismatch",
                    severity=SEVERITY_CAUTION,
                    explanation=(
                        f"'{cue.cue}' shows up in employer reviews as a negative theme: "
                        f"{negative_reviews[0].text}. Employer reviews are self-selected "
                        "and advisory only (FR-265)."
                    ),
                    company_cues=[c.as_dict() for c in negative_reviews[:3]],
                    quote=cue.quote,
                )
            )
        elif matched:
            match.aligned.append(
                ValueFinding(
                    cue=cue.cue,
                    polarity=cue.polarity,
                    status="aligned",
                    severity=None,
                    explanation=(
                        f"The company states it too: {matched[0].text}"
                        + (f" ({matched[0].source})" if matched[0].source else "")
                        + "."
                    ),
                    company_cues=[c.as_dict() for c in matched[:3]],
                    quote=cue.quote,
                )
            )
        else:
            match.unknown.append(
                ValueFinding(
                    cue=cue.cue,
                    polarity=cue.polarity,
                    status="unknown",
                    severity=None,
                    explanation=(
                        f"Nothing collected about this company speaks to '{cue.cue}'."
                    ),
                    quote=cue.quote,
                )
            )
    return match


# ---------------------------------------------------------------------------
# LLM refinement (NFR-104, NFR-205)
# ---------------------------------------------------------------------------


def _refine(match: ValuesMatch, llm: LLMClient) -> bool:
    template = load_prompt(PROMPT_NAME)
    system, user = template.render(language="en", company=match.company_name or "the company")
    payload = {
        "company": match.company_name,
        "warnings": [w.as_dict() for w in match.warnings],
        "aligned": [a.as_dict() for a in match.aligned],
        "unknown": [u.cue for u in match.unknown],
    }
    try:
        data = llm.complete_json(
            template.task or "analysis.values",
            system=system,
            user=user,
            untrusted={"comparison": json.dumps(payload, ensure_ascii=False)[:10000]},
            entity_type="company",
            entity_id=match.company_id,
            prompt_template=template.name,
            prompt_version=template.version,
            prefer_strong=False,
            max_tokens=1400,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("Values comparison stays deterministic: %s", exc)
        return False
    if not isinstance(data, dict):
        return False

    by_cue = {fold(w.cue): w for w in match.warnings}
    for item in data.get("warnings") or []:
        if not isinstance(item, dict):
            continue
        finding = by_cue.get(fold(str(item.get("cue") or "")))
        explanation = str(item.get("explanation") or "").strip()
        if finding is not None and explanation:
            finding.explanation = explanation[:600]
            finding.source = "llm+deterministic"
    return True


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def for_company(
    job_seeker_id: str,
    company_id: str,
    *,
    campaign_id: str | None = None,
    use_llm: bool = False,
    llm: LLMClient | None = None,
) -> ValuesMatch:
    """The FR-384 comparison for one company, for the profile screen."""
    company = repo.get_company(company_id)
    if company is None:
        raise LookupError(f"No company {company_id}")

    campaign = (
        repo.get_campaign(campaign_id, job_seeker_id)
        if campaign_id
        else repo.latest_campaign(job_seeker_id)
    )
    dream = repo.latest_dream_model(job_seeker_id, (campaign or {}).get("persona_id")) or (
        repo.latest_dream_model(job_seeker_id)
    )
    directive_row = repo.directive_set(job_seeker_id, (campaign or {}).get("directive_set_id"))

    cues = company_cues(company.get("values_culture"))
    match = compare(dream_cues(dream), cues, company_name=company.get("name"))
    match.company_id = company_id
    block = from_json(company.get("values_culture"), None) or {}
    match.derived_from = list(block.get("derived_from") or []) if isinstance(block, dict) else []
    match.confidence = block.get("confidence") if isinstance(block, dict) else None

    # FR-385: a company excluded by discretion mode must not be surfaced at all.
    if directive_row:
        match.discretion_mode = bool(directive_row.get("discretion_mode"))
        match.excluded_reason = dir_mod.exclusion_reason(directive_row, company)

    client = llm
    if use_llm and client is None:
        client = default_llm(job_seeker_id, (campaign or {}).get("id"))
    if client is not None and match.warnings and _refine(match, client):
        match.generated_by = "llm+deterministic"
    return match


def warnings_for_document(
    job_seeker_id: str, company_id: str, *, campaign_id: str | None = None
) -> dict[str, Any]:
    """What the motivation and fit document has to carry (FR-384 -> FR-330).

    The documents slice consumes this: ``warnings`` are the explicit mismatch
    statements, ``address_in_letter`` are the ones a motivation letter should
    answer rather than ignore, and ``alignments`` are the honest, evidenced
    reasons to want the job - which is what the letter is actually made of.
    """
    match = for_company(job_seeker_id, company_id, campaign_id=campaign_id, use_llm=False)
    return {
        "company_id": company_id,
        "company_name": match.company_name,
        "score": match.score,
        "has_blocking_warning": match.has_blocking_warning,
        "warnings": [w.as_dict() for w in match.warnings],
        "address_in_letter": [
            w.as_dict()
            for w in match.warnings
            if w.severity in (SEVERITY_BLOCKING, SEVERITY_WARNING)
        ],
        "alignments": [a.as_dict() for a in match.aligned],
        "open_questions": [u.cue for u in match.unknown],
        "discretion_mode": match.discretion_mode,
        "note": (
            "Alignments may be cited in the motivation document because each carries the "
            "company's own words; warnings are for the job seeker's eyes and for the "
            "interview, and must never be turned into flattery (CR-405)."
        ),
    }
