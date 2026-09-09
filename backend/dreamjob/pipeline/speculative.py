"""Speculative openings and the never-claim-a-vacancy rule (FR-262, FR-263, NFR-104).

Most of the value of a search like this sits in roles nobody has advertised: a
company with money, a gap in its department map and a signal that the gap is
about to hurt.  FR-262 asks for exactly that - for every interesting company
with no matching vacancy, the roles it is likely to need or be able to create
in the next 6-12 months, each with a plausibility score and a rationale, drawn
from the company profile, hiring signals, financial capacity, department map,
competitors' hiring and the job seeker's composite profile.

FR-263 is the harder half, and it is why this module owns labelling as well as
generation.  A speculative opening must be visibly distinguishable from a real
vacancy *in every view and in all generated material* - and an introduction
e-mail must never claim a vacancy exists when it does not.  That cannot be
enforced by convention, so this module exports three things every downstream
generator is expected to use:

``kind_label`` / ``disclosure_note``
    the words to put in front of a reader, in the content language.
``false_vacancy_claims``
    a scan of generated text for phrasings that assert an advertised vacancy.
``check_generated_material``
    the same scan, raising, for a generator that wants a hard stop before a
    draft is ever shown or sent.

NFR-104 names this module explicitly: when the campaign's token budget is
nearly exhausted, speculative generation is skipped **for low-ranked companies
first**.  Companies are therefore ordered by a deterministic attractiveness
prior before any call is made, and the budget is re-checked between companies
so degradation starts the moment the threshold is crossed rather than at the
next run.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import dreamjob.llm as llm_package
from dreamjob.db.connection import from_json
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import opportunities as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError, redact
from dreamjob.pipeline import directives as dir_mod
from dreamjob.pipeline import scoring
from dreamjob.pipeline import signals as signals_mod
from dreamjob.pipeline.enrichment import load_prompt
from dreamjob.pipeline.opportunities import (
    KIND_SPECULATIVE,
    KIND_VACANCY,
    rejection_reason,
)

log = logging.getLogger(__name__)

PROMPT_NAME = "speculative_openings"
PROMPT_PATH = Path(llm_package.__file__).parent / "prompts" / f"{PROMPT_NAME}.md"

DEFAULT_MAX_OPENINGS = 4
MIN_PLAUSIBILITY = 0.2

#: NFR-104: under budget pressure only clearly attractive companies are worth a
#: generation call, and only the best few of those.
DEGRADED_MIN_ATTRACTIVENESS = 0.55
DEGRADED_MAX_COMPANIES = 10

MAX_EVIDENCE_CHARS = 6000


class ConsentRequired(RuntimeError):
    """CR-410: profile data may not reach the provider without recorded consent."""


class SpeculativeClaimError(ValueError):
    """FR-263: generated material asserts a vacancy that does not exist."""


# ---------------------------------------------------------------------------
# FR-263: labelling, in every view and in all generated material
# ---------------------------------------------------------------------------

KIND_LABELS: dict[str, dict[str, str]] = {
    KIND_VACANCY: {
        "en": "Advertised vacancy",
        "nl": "Gepubliceerde vacature",
        "fr": "Offre d'emploi publiée",
    },
    KIND_SPECULATIVE: {
        "en": "Speculative opening - not an advertised vacancy",
        "nl": "Speculatieve opening - geen gepubliceerde vacature",
        "fr": "Ouverture spéculative - pas une offre publiée",
    },
}

DISCLOSURE_NOTES: dict[str, dict[str, str]] = {
    KIND_VACANCY: {
        "en": "This role is advertised by the company.",
        "nl": "Deze functie is door het bedrijf gepubliceerd.",
        "fr": "Ce poste est publié par l'entreprise.",
    },
    KIND_SPECULATIVE: {
        "en": (
            "This role is not advertised. It is a speculative approach: a role the "
            "company may need, proposed on the basis of public information."
        ),
        "nl": (
            "Deze functie is niet gepubliceerd. Het gaat om een spontane sollicitatie "
            "voor een rol die het bedrijf mogelijk nodig heeft, op basis van publieke "
            "informatie."
        ),
        "fr": (
            "Ce poste n'est pas publié. Il s'agit d'une candidature spontanée pour un "
            "rôle dont l'entreprise pourrait avoir besoin, proposé sur la base "
            "d'informations publiques."
        ),
    },
}


def kind_label(kind: str, language: str = "en") -> str:
    """The badge every view must show next to an opportunity (FR-263)."""
    labels = KIND_LABELS.get(kind or KIND_VACANCY, KIND_LABELS[KIND_VACANCY])
    return labels.get((language or "en")[:2].lower(), labels["en"])


def disclosure_note(kind: str, language: str = "en") -> str:
    """The sentence generated material must carry for this kind (FR-263, CR-405)."""
    notes = DISCLOSURE_NOTES.get(kind or KIND_VACANCY, DISCLOSURE_NOTES[KIND_VACANCY])
    return notes.get((language or "en")[:2].lower(), notes["en"])


def is_speculative(opportunity: dict) -> bool:
    return (opportunity or {}).get("kind") == KIND_SPECULATIVE


# Phrasings that assert an advertised vacancy.  Written narrowly on purpose: a
# false positive costs a regeneration, a false negative costs the job seeker
# their credibility with a hiring manager (CR-405).
_FALSE_VACANCY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(the|this|your|that)\s+(current\s+|open\s+|advertised\s+|posted\s+)?"
        r"(vacancy|job (posting|advert(isement)?)|opening you (posted|advertised))\b",
        r"\byour (recent|current|new)\s+(posting|advertisement|job ad)\b",
        r"\b(i (am|'m) (writing|applying) (to apply )?(for|in response to)"
        r"|i would like to apply for)\s+(the|this|your)\b",
        r"\bin response to (the|your)\s+(vacancy|posting|advert)",
        r"\b(as advertised|as posted|the position you (are )?advertis)",
        r"\bapplication for the (position|role|vacancy) of\b",
        r"\breference number\b",
        r"\b(uw|de|deze|je)\s+(vacature|vacatures)\b",
        r"\bsolliciteer(en)?\s+(op|voor)\s+(de|uw|deze)\s+vacature\b",
        r"\bnaar aanleiding van (uw|de|jullie)\s+vacature\b",
        r"\b(votre|cette|l[ae])\s+(offre d'emploi|annonce)\b",
        r"\bsuite à (votre|l')\s*(offre|annonce)\b",
        r"\bje me permets de postuler (à|au|pour) (votre|l')\b",
    )
)


def false_vacancy_claims(text: str) -> list[str]:
    """Phrasings in ``text`` that assert an advertised vacancy (FR-263).

    Callers pass generated e-mail bodies, motivation letters and briefings for
    speculative opportunities.  An empty list means the text is safe to show.
    """
    if not text:
        return []
    found: list[str] = []
    for pattern in _FALSE_VACANCY_PATTERNS:
        for match in pattern.finditer(text):
            phrase = match.group(0).strip()
            if phrase and phrase not in found:
                found.append(phrase)
    return found


def check_generated_material(text: str, kind: str, *, strict: bool = True) -> list[str]:
    """FR-263 gate for generated material.

    For a real vacancy nothing is checked - claiming a vacancy exists is then
    simply true.  For a speculative opening every claim is a
    misrepresentation (CR-405), so ``strict`` callers get an exception and the
    rest get the list of offending phrases to regenerate against.
    """
    if kind != KIND_SPECULATIVE:
        return []
    claims = false_vacancy_claims(text)
    if claims and strict:
        raise SpeculativeClaimError(
            "Generated material for a speculative opening claims an advertised vacancy "
            f"(FR-263): {', '.join(repr(c) for c in claims[:5])}"
        )
    return claims


def presentation(opportunity: dict, language: str | None = None) -> dict[str, Any]:
    """The FR-263 fields every API response and every view must carry."""
    kind = opportunity.get("kind") or KIND_VACANCY
    lang = language or opportunity.get("language") or "en"
    return {
        "kind": kind,
        "is_speculative": kind == KIND_SPECULATIVE,
        "kind_label": kind_label(kind, lang),
        "disclosure_note": disclosure_note(kind, lang),
    }


# ---------------------------------------------------------------------------
# Evidence assembly (FR-262 inputs)
# ---------------------------------------------------------------------------


def _trim(value: Any, limit: int = MAX_EVIDENCE_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


def company_evidence(company_id: str) -> dict[str, Any]:
    """The six FR-262 inputs, as far as the knowledge base has them."""
    company = repo.get_company(company_id) or {}
    profile = {
        "name": company.get("name"),
        "country": company.get("country"),
        "business_summary": company.get("business_summary"),
        "products_services": from_json(company.get("products_services"), None),
        "markets": from_json(company.get("markets"), None),
        "sector_codes": from_json(company.get("sector_codes"), None),
        "size_fte": company.get("size_fte"),
        "size_band": company.get("size_band"),
        "stage": company.get("stage"),
        "ownership": company.get("ownership"),
        "trajectory": company.get("trajectory"),
        "locations": from_json(company.get("locations"), None),
        "department_map": from_json(company.get("structure"), None),
        "key_people": from_json(company.get("key_people"), None),
        "tech_stack": from_json(company.get("tech_stack"), None),
        "values_culture": from_json(company.get("values_culture"), None),
        "news": from_json(company.get("news"), None),
    }
    signals = [
        {
            "type": s.get("signal_type"),
            "description": s.get("description"),
            "occurred_at": s.get("occurred_at"),
            "strength": s.get("strength"),
        }
        for s in repo.company_signals(company_id, limit=25)
    ]
    analysis = repo.company_analysis(company_id) or {}
    financials = {
        "trajectory": analysis.get("trajectory"),
        "trajectory_confidence": analysis.get("trajectory_confidence"),
        "ability_to_pay": analysis.get("ability_to_pay"),
        "ability_to_pay_rationale": analysis.get("ability_to_pay_rationale"),
        "investment_capacity": analysis.get("investment_capacity"),
        "investment_capacity_rationale": analysis.get("investment_capacity_rationale"),
        "revenue_cagr": analysis.get("revenue_cagr"),
        "headcount_cagr": analysis.get("headcount_cagr"),
        "personnel_cost_per_fte": analysis.get("personnel_cost_per_fte"),
        "is_estimated": analysis.get("is_estimated"),
    }
    competitors = [
        {
            "peer": c.get("peer_company_name") or c.get("peer_name"),
            "title": c.get("title"),
            "function_family": c.get("function_family"),
            "seniority": c.get("seniority"),
            "posted_at": c.get("posted_at"),
        }
        for c in repo.competitor_hiring(company_id, limit=20)
    ]
    return {
        "company": company,
        "profile": profile,
        "signals": signals,
        "financials": financials,
        "competitors": competitors,
        "timing": signals_mod.timing_summary(company_id),
    }


def seeker_evidence(campaign: dict) -> dict[str, Any]:
    """Composite profile and dream job model, redacted before egress (CR-410)."""
    inputs = campaign_repo.load_planning_inputs(campaign)
    blocked = set(inputs.get("do_not_disclose") or set())
    composite = inputs.get("composite_profile") or {}
    dream = inputs.get("dream_job_model") or {}

    seeker = {
        key: from_json(composite.get(key), None)
        for key in (
            "career_trajectory", "core_competencies", "adjacent_competencies",
            "domains", "achievements", "inferred_preferences", "constraints",
        )
    }
    seeker["narrative"] = composite.get("narrative")
    seeker["seniority"] = from_json(composite.get("seniority"), composite.get("seniority"))

    dream_model = {
        key: from_json(dream.get(key), None)
        for key in (
            "target_roles", "role_families", "responsibilities",
            "company_characteristics", "culture_values", "deal_breakers",
            "implicit_preferences",
        )
    }
    dream_model["statement"] = dream.get("statement")

    return {
        "seeker": redact(seeker, blocked),
        "dream_job": redact(dream_model, blocked),
        "directives": inputs.get("directives"),
    }


# ---------------------------------------------------------------------------
# Generation (FR-262)
# ---------------------------------------------------------------------------


def _consent_ok(job_seeker_id: str) -> bool:
    try:
        from dreamjob.security.auth_service import has_consent  # noqa: PLC0415
    except ImportError:
        return campaign_repo.has_consent(job_seeker_id, "llm_transfer")
    return bool(has_consent(job_seeker_id, "llm_transfer"))


def _coerce_opening(raw: Any, company: dict, language: str) -> dict[str, Any] | None:
    """Validate one model-proposed opening before it is allowed near the database."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title or len(title) > 160:
        return None
    try:
        plausibility = float(raw.get("plausibility"))
    except (TypeError, ValueError):
        plausibility = 0.0
    plausibility = max(0.0, min(1.0, plausibility))
    if plausibility < MIN_PLAUSIBILITY:
        return None

    evidence = [str(e)[:300] for e in (raw.get("evidence") or []) if str(e).strip()][:8]
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        # FR-262 requires a rationale; an opening without one is not usable.
        return None

    locations = from_json(company.get("locations"), None)
    location = None
    if isinstance(locations, list) and locations:
        first = locations[0]
        location = first if isinstance(first, str) else (
            first.get("city") or first.get("label") or first.get("name")
            if isinstance(first, dict) else None
        )

    arrangement = str(raw.get("work_arrangement") or "").strip().lower() or None
    if arrangement not in (None, "onsite", "hybrid", "remote"):
        arrangement = None

    return {
        "kind": KIND_SPECULATIVE,
        "company_id": company.get("id"),
        "vacancy_id": None,
        "title": title,
        "function_family": str(raw.get("function_family") or "").strip() or None,
        "seniority": str(raw.get("seniority") or "").strip().lower() or None,
        "description": str(raw.get("description") or "").strip()[:4000] or None,
        "speculative_rationale": json.dumps(
            {
                "rationale": rationale[:2000],
                "evidence": evidence,
                "basis": str(raw.get("plausibility_basis") or "").strip() or None,
                "timing": str(raw.get("timing") or "").strip()[:600] or None,
                "risks": [str(r)[:200] for r in (raw.get("risks") or [])][:5],
                "likely_department": str(raw.get("likely_department") or "").strip() or None,
                "likely_decision_maker_role": str(
                    raw.get("likely_decision_maker_role") or ""
                ).strip() or None,
            },
            ensure_ascii=False,
        ),
        "plausibility": round(plausibility, 3),
        "required_skills": [],
        "desirable_skills": [],
        "location": location,
        "country": company.get("country"),
        "latitude": None,
        "longitude": None,
        "work_arrangement": arrangement,
        "remote_days": None,
        "contract_type": None,
        "fte_percentage": None,
        "posted_at": None,
        # Not a channel onto an application form: a speculative opening is
        # approached directly, and the contact slice decides how (FR-301).
        "application_channel": "speculative",
        "application_target": None,
        "source_url": company.get("careers_url"),
        "source_adapter": PROMPT_NAME,
        "comp_min": None,
        "comp_max": None,
        "comp_currency": None,
        "comp_is_stated": 0,
        "language": language,
    }


def generate_for_company(
    company: dict,
    seeker_context: dict,
    llm: LLMClient,
    *,
    max_openings: int = DEFAULT_MAX_OPENINGS,
    language: str = "en",
) -> tuple[list[dict], dict[str, Any]]:
    """FR-262: the roles this company is likely to need in the next 6-12 months.

    Returns ``(openings, meta)``.  All company material goes in as ``untrusted``
    blocks (NFR-205): a company website is hostile input like any other.
    """
    evidence = company_evidence(company["id"])
    template = load_prompt(PROMPT_NAME)
    system, user = template.render(language=language, max_openings=max_openings)

    data = llm.complete_json(
        template.task or "company.speculative",
        system=system,
        user=user,
        untrusted={
            "company": _trim(evidence["profile"]),
            "signals": _trim({"signals": evidence["signals"], "timing": evidence["timing"]}, 3000),
            "financials": _trim(evidence["financials"], 2000),
            "competitors": _trim(evidence["competitors"], 2500),
            "seeker": _trim(seeker_context.get("seeker"), 6000),
            "dream_job": _trim(seeker_context.get("dream_job"), 4000),
        },
        entity_type="company",
        entity_id=company["id"],
        prompt_template=template.name,
        prompt_version=template.version,
        # NFR-104: the cheap route, deliberately.  Measured against the same
        # prompt, the reasoning route spends about thirty times the output
        # tokens on chain of thought for an equivalent answer, and because that
        # thought shares the answer's ceiling it truncates to an empty
        # completion under any budget a whole campaign can afford.
        prefer_strong=False,
        max_tokens=3000,
    )

    raw_openings = data.get("openings") if isinstance(data, dict) else data
    openings: list[dict] = []
    for raw in (raw_openings or [])[: max_openings * 2]:
        record = _coerce_opening(raw, company, language)
        if record is not None:
            openings.append(record)
    openings.sort(key=lambda o: -(o["plausibility"] or 0))

    meta = {
        "company_read": (data.get("company_read") if isinstance(data, dict) else None),
        "insufficient_evidence": bool(
            isinstance(data, dict) and data.get("insufficient_evidence")
        ),
        "evidence_counts": {
            "signals": len(evidence["signals"]),
            "competitor_postings": len(evidence["competitors"]),
            "has_financials": evidence["financials"].get("ability_to_pay") is not None,
            "has_department_map": bool(evidence["profile"].get("department_map")),
        },
    }
    return openings[:max_openings], meta


# ---------------------------------------------------------------------------
# Campaign pass, with the NFR-104 degradation
# ---------------------------------------------------------------------------


@dataclass
class SpeculativeReport:
    campaign_id: str
    companies_considered: int = 0
    companies_generated: int = 0
    companies_skipped_budget: int = 0
    created: int = 0
    refreshed: int = 0
    rejected: int = 0
    degraded: bool = False
    errors: list[str] = field(default_factory=list)
    opportunity_ids: list[str] = field(default_factory=list)
    skipped_companies: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "companies_considered": self.companies_considered,
            "companies_generated": self.companies_generated,
            "companies_skipped_budget": self.companies_skipped_budget,
            "created": self.created,
            "refreshed": self.refreshed,
            "rejected": self.rejected,
            "degraded": self.degraded,
            "errors": self.errors,
            "opportunity_ids": self.opportunity_ids,
            "skipped_companies": self.skipped_companies,
        }


def candidate_companies(
    campaign: dict, directive_set: dir_mod.DirectiveSetLike | None = None
) -> list[dict]:
    """Interesting companies with no matching vacancy, best-ranked first (FR-262).

    "Interesting" is every company this campaign touched plus the seeker's
    watchlist, minus the ones a vacancy already covers and minus anything the
    directives exclude.  The order is the deterministic attractiveness prior, so
    the NFR-104 cut always removes the *low-ranked* companies.
    """
    campaign_id = campaign["id"]
    covered = repo.companies_with_vacancy_opportunity(
        campaign_id, job_seeker_id=campaign["job_seeker_id"]
    )
    ids = [
        cid
        for cid in dict.fromkeys(
            [*repo.campaign_company_ids(campaign_id),
             *repo.watchlist_company_ids(campaign["job_seeker_id"])]
        )
        if cid not in covered
    ]
    companies = repo.companies_by_ids(ids)
    if directive_set is not None:
        companies = [c for c in companies if not dir_mod.is_excluded(directive_set, c)]
    ranked = [(scoring.company_attractiveness(c["id"], company=c).value, c) for c in companies]
    ranked.sort(key=lambda pair: -pair[0])
    for prior, company in ranked:
        company["_attractiveness"] = round(prior, 3)
    return [company for _, company in ranked]


def generate_campaign(
    campaign: dict,
    *,
    llm: LLMClient | None = None,
    max_companies: int = 40,
    max_openings: int = DEFAULT_MAX_OPENINGS,
    language: str = "en",
) -> SpeculativeReport:
    """FR-262 over a whole campaign, degrading as NFR-104 requires.

    The budget is consulted before every company, not once at the start: a
    campaign that crosses the threshold half-way through stops generating for
    the remaining low-ranked companies instead of finishing the list.
    """
    campaign_id = campaign["id"]
    seeker_id = campaign["job_seeker_id"]
    report = SpeculativeReport(campaign_id=campaign_id)

    llm = llm or LLMClient(campaign_id=campaign_id, job_seeker_id=seeker_id)
    if not _consent_ok(seeker_id):
        raise ConsentRequired(
            "Consent for transferring profile data to the LLM provider (CR-410) has not "
            "been recorded; speculative openings cannot be generated."
        )

    context = seeker_evidence(campaign)
    directive_row = context.get("directives")
    directive_set = dir_mod.coerce_directive_set(directive_row) if directive_row else None

    companies = candidate_companies(campaign, directive_set)[:max_companies]
    report.companies_considered = len(companies)

    for company in companies:
        prior = float(company.get("_attractiveness") or 0.0)
        if llm.budget.should_degrade():
            # NFR-104, named degradation: the low-ranked companies go first.
            report.degraded = True
            if prior < DEGRADED_MIN_ATTRACTIVENESS or (
                report.companies_generated >= DEGRADED_MAX_COMPANIES
            ):
                report.companies_skipped_budget += 1
                report.skipped_companies.append(
                    {"company_id": company["id"], "name": company.get("name"),
                     "attractiveness": prior, "reason": "budget_degradation"}
                )
                continue

        try:
            openings, meta = generate_for_company(
                company, context, llm, max_openings=max_openings, language=language
            )
        except BudgetExhausted:
            report.degraded = True
            report.companies_skipped_budget += 1
            report.skipped_companies.append(
                {"company_id": company["id"], "name": company.get("name"),
                 "attractiveness": prior, "reason": "budget_exhausted"}
            )
            continue
        except LLMError as exc:
            report.errors.append(f"{company.get('name') or company['id']}: {exc}")
            continue

        report.companies_generated += 1
        if meta.get("insufficient_evidence"):
            log.info("No usable evidence for speculative openings at %s", company.get("name"))

        for record in openings:
            reason = rejection_reason(record, company, directive_set)
            if reason:
                report.rejected += 1
                continue
            record["timing_flag"] = signals_mod.timing_flag_for(company["id"])
            existing = repo.find_speculative(
                campaign_id, company["id"], record["title"], job_seeker_id=seeker_id
            )
            opportunity_id, created = repo.upsert_synthesised(
                seeker_id, campaign_id, record, existing
            )
            report.opportunity_ids.append(opportunity_id)
            if created:
                report.created += 1
            else:
                report.refreshed += 1

    log.info(
        "Speculative openings for campaign %s: %d created, %d refreshed, "
        "%d companies skipped for budget (degraded=%s)",
        campaign_id, report.created, report.refreshed, report.companies_skipped_budget,
        report.degraded,
    )
    return report


# ---------------------------------------------------------------------------
# Stage entry point (NFR-603, FR-149, FR-262)
# ---------------------------------------------------------------------------


def is_spontaneous_campaign(campaign: dict) -> bool:
    """FR-149: does this campaign's directive set drop every vacancy source?

    Such a campaign plans no job board and no ATS, so synthesising the vacancies
    it collected can only ever produce an empty list.  Its opportunities come
    from this module or from nowhere, which is why the caller of the synthesis
    pass asks this before deciding it is finished.
    """
    try:
        inputs = campaign_repo.load_planning_inputs(campaign)
    except Exception:  # noqa: BLE001 - an unreadable directive set is not spontaneous-only
        log.exception("Could not read the directive set of campaign %s", campaign.get("id"))
        return False
    directives = inputs.get("directives")
    if not directives:
        return False
    return dir_mod.skips_vacancy_sources(directives)


def rerun(campaign_id: str, job_seeker_id: str, **options: Any) -> dict[str, Any]:
    """Generate this campaign's speculative openings (FR-262, NFR-603).

    ``dreamjob.pipeline.collection`` reserves the ``speculative`` stage for this
    module, which is what puts the spontaneous-application track on the "re-run
    a stage" surface and inside the campaign pipeline.  Until this existed the
    only route to :func:`generate_campaign` was a hand-written POST: no screen
    called it, no stage ran it, and FR-149's ``spontaneous_only`` directive -
    which drops every vacancy source - produced a campaign that could not
    yield a single opportunity.

    ``ConsentRequired`` is answered rather than raised, because a stage re-run
    reports what happened; the caller renders the answer.
    """
    campaign = campaign_repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    try:
        report = generate_campaign(
            campaign,
            max_companies=int(options.get("max_companies") or 40),
            max_openings=int(options.get("max_openings") or DEFAULT_MAX_OPENINGS),
            language=str(options.get("language") or "en"),
        )
    except ConsentRequired as exc:
        return {"error": "consent_required", "detail": str(exc)}
    return report.as_dict()
