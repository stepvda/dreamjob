"""Salary negotiation brief (FR-444, NFR-305).

FR-444 names four inputs and one output.  The inputs are the company's
ability-to-pay score and personnel cost per FTE (FR-243/244), market
compensation data (FR-264), and the job seeker's own compensation directives
(FR-146).  The output is a suggested ask with supporting arguments and
fallback positions.

The division of labour here matters.  **Every number is computed
deterministically** - the anchor, the range, the walk-away point, the position
of the ask inside the market band - and the model is asked only to write the
case for them.  A negotiation brief whose figures were produced by a language
model would be worth nothing, and the seeker cannot check them at the table.

The brief is advisory (NFR-305).  It says what the evidence supports and where
it is thin; it never says what to accept.  A company with no filed accounts
produces a brief that says so, with market data only - which is a weaker
position and is presented as one.

Only opportunities that have reached ``interview`` or ``offer`` on the board
get a brief, which is FR-444's own condition.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, to_json, utcnow
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.documents import pdf_builder as pdf
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline import compensation as comp_mod
from dreamjob.pipeline import directives as dir_mod
from dreamjob.pipeline.enrichment import load_prompt
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

PROMPT_NAME = "negotiation_brief"

#: FR-444 applies from the interview stage onwards.
ELIGIBLE_STAGES = ("interview", "offer")

#: Where inside the market band to anchor the ask.  Not the top: an ask the
#: market does not support is refused and costs credibility for the rest of the
#: conversation.  Ability to pay moves it within these bounds.
ANCHOR_LOW, ANCHOR_HIGH = 0.55, 0.85


class NotEligible(ValueError):
    """Raised for an opportunity that has not reached interview or offer."""


@dataclass
class Figures:
    """Everything the brief is built on, and where each number came from."""

    currency: str = "EUR"
    market_min: float | None = None
    market_median: float | None = None
    market_max: float | None = None
    market_confidence: float = 0.0
    market_method: str = "none"
    market_sample: int = 0
    stated_min: float | None = None
    stated_max: float | None = None
    is_stated: bool = False
    ability_to_pay: int | None = None
    ability_to_pay_rationale: str | None = None
    personnel_cost_per_fte: float | None = None
    headcount_fte: float | None = None
    revenue_cagr: float | None = None
    headcount_cagr: float | None = None
    financial_trajectory: str | None = None
    financials_estimated: bool = False
    directive_minimum: float | None = None
    directive_currency: str = "EUR"
    directive_period: str = "annual"
    benefits_must_have: list[str] = field(default_factory=list)
    ask_min: float | None = None
    ask_max: float | None = None
    walk_away: float | None = None
    anchor_rationale: str = ""
    confidence: float = 0.0
    gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _band(estimate: comp_mod.CompensationEstimate) -> tuple[float | None, float | None, int]:
    used = [s for s in estimate.sources if s.used]
    sample = sum(int(s.sample_size or 0) for s in used)
    return estimate.comp_min, estimate.comp_max, sample


def gather_figures(
    context: dict[str, Any], directive_row: dict | None
) -> Figures:
    """The four FR-444 inputs, read once, with the gaps recorded (NFR-402)."""
    currency = (
        context.get("comp_currency")
        or comp_mod.default_currency(context.get("country") or context.get("company_country"))
    ).upper()
    figures = Figures(currency=currency)

    # --- FR-264: market compensation ---------------------------------------
    estimate = comp_mod.estimate(context, currency=currency)
    low, high, sample = _band(estimate)
    figures.market_min, figures.market_max = low, high
    figures.market_median = (
        statistics.fmean([low, high]) if low is not None and high is not None else None
    )
    figures.market_confidence = round(estimate.confidence, 3)
    figures.market_method = estimate.method
    figures.market_sample = sample
    figures.is_stated = estimate.is_stated
    if estimate.is_stated:
        figures.stated_min, figures.stated_max = estimate.comp_min, estimate.comp_max
    if estimate.method == "insufficient_data":
        figures.gaps.append(
            "No comparable market data was found; the ask rests on the seeker's own "
            "directives and on what the company can afford."
        )

    # --- FR-243 / FR-244: ability to pay -----------------------------------
    figures.ability_to_pay = (
        int(context["ability_to_pay"]) if context.get("ability_to_pay") is not None else None
    )
    figures.ability_to_pay_rationale = context.get("ability_to_pay_rationale")
    figures.personnel_cost_per_fte = context.get("personnel_cost_per_fte")
    figures.headcount_fte = context.get("size_fte")
    figures.revenue_cagr = context.get("revenue_cagr")
    figures.headcount_cagr = context.get("headcount_cagr")
    figures.financial_trajectory = context.get("financial_trajectory") or context.get(
        "company_trajectory"
    )
    figures.financials_estimated = bool(context.get("financials_estimated"))
    if figures.ability_to_pay is None:
        figures.gaps.append(
            "The company has no financial analysis on record, so ability to pay is unknown."
        )
    if figures.personnel_cost_per_fte is None:
        figures.gaps.append(
            "Personnel cost per FTE is not on record - abbreviated accounts do not "
            "always disclose it."
        )

    # --- FR-146: the seeker's directives ------------------------------------
    if directive_row:
        payload = from_json(directive_row.get("compensation"), {}) or {}
        try:
            directives = dir_mod.CompensationDirectives(**payload)
        except Exception:  # noqa: BLE001 - a malformed stored directive is not fatal
            directives = dir_mod.CompensationDirectives()
        figures.directive_minimum = directives.minimum_package
        figures.directive_currency = directives.currency
        figures.directive_period = str(directives.period)
        figures.benefits_must_have = list(directives.benefits_must_have)
        if directives.minimum_package is None:
            figures.gaps.append(
                "No minimum package is set in the directives, so the walk-away point is "
                "the seeker's to decide."
            )
    else:
        figures.gaps.append("No directive set was found; compensation preferences are unknown.")

    return figures


def suggest_ask(figures: Figures) -> Figures:
    """Anchor the ask deterministically, and say why it sits there (FR-444).

    The band is the market range where there is one and the stated range where
    the employer named one.  Ability to pay moves the anchor inside the band -
    a company scored 80 can be asked at the top of it, one scored 30 cannot -
    and the seeker's stated minimum raises the floor, because asking below it
    would be negotiating against themselves.
    """
    low = figures.stated_min if figures.is_stated else figures.market_min
    high = figures.stated_max if figures.is_stated else figures.market_max
    minimum = figures.directive_minimum

    if low is None or high is None:
        if minimum is None:
            figures.anchor_rationale = (
                "Neither market data nor a stated minimum is available, so no figure is "
                "suggested. Ask what range the role carries before naming one."
            )
            figures.confidence = 0.0
            return figures
        figures.ask_min = minimum
        figures.ask_max = round(minimum * 1.15, -2)
        figures.walk_away = minimum
        figures.anchor_rationale = (
            "No market range was found, so the ask is anchored on the stated minimum "
            "package with the usual room to negotiate down."
        )
        figures.confidence = 0.25
        return figures

    ability = figures.ability_to_pay
    if ability is None:
        position = (ANCHOR_LOW + ANCHOR_HIGH) / 2
        reason = "no ability-to-pay score on record, so the ask sits mid-band"
    else:
        position = ANCHOR_LOW + (ANCHOR_HIGH - ANCHOR_LOW) * (ability / 100)
        reason = f"ability to pay scored {ability}/100, which places the ask at "
        reason += f"{round(position * 100)}% of the band"

    anchor = low + (high - low) * position
    if minimum is not None and anchor < minimum:
        anchor = minimum
        reason += "; raised to the stated minimum package"
    figures.ask_min = round(anchor, -2)
    figures.ask_max = round(max(anchor * 1.08, min(high, anchor * 1.12)), -2)
    figures.walk_away = minimum if minimum is not None else round(low, -2)

    band = "range stated in the posting" if figures.is_stated else "market range"
    figures.anchor_rationale = (
        f"The {band} runs {pdf.format_money(low, figures.currency)} to "
        f"{pdf.format_money(high, figures.currency)}; {reason}."
    )
    figures.confidence = round(
        min(0.95, 0.35 + 0.4 * figures.market_confidence + (0.2 if ability is not None else 0)), 2
    )
    return figures


# ---------------------------------------------------------------------------
# The written case
# ---------------------------------------------------------------------------


def deterministic_arguments(
    figures: Figures, context: dict[str, Any]
) -> tuple[list[dict], list[dict], list[str]]:
    """The case the figures make on their own, used when the model is unavailable."""
    money = lambda v: pdf.format_money(v, figures.currency)  # noqa: E731 - local shorthand
    arguments: list[dict] = []

    if figures.personnel_cost_per_fte:
        arguments.append(
            {
                "point": (
                    "The company already carries this level of personnel cost per head."
                ),
                "evidence": (
                    f"Personnel cost per FTE of {money(figures.personnel_cost_per_fte)}"
                    + (" (estimated from filings)" if figures.financials_estimated else "")
                ),
                "strength": "strong",
            }
        )
    if figures.ability_to_pay is not None and figures.ability_to_pay >= 60:
        arguments.append(
            {
                "point": "The company's finances support the upper half of the range.",
                "evidence": (
                    f"Ability to pay scored {figures.ability_to_pay}/100. "
                    f"{figures.ability_to_pay_rationale or ''}"
                ).strip(),
                "strength": "strong",
            }
        )
    if figures.market_min and figures.market_max:
        arguments.append(
            {
                "point": "The ask sits inside what comparable roles pay, not above it.",
                "evidence": (
                    f"Comparable range {money(figures.market_min)} to "
                    f"{money(figures.market_max)}, from {figures.market_method.replace('_', ' ')}"
                    + (f" across {figures.market_sample} data points" if figures.market_sample
                       else "")
                ),
                "strength": "moderate" if figures.market_confidence < 0.6 else "strong",
            }
        )
    if figures.headcount_cagr and figures.headcount_cagr > 0.05:
        arguments.append(
            {
                "point": "The team is growing, so this is a hire that has been budgeted for.",
                "evidence": f"Headcount grew {figures.headcount_cagr * 100:.0f}% a year",
                "strength": "moderate",
            }
        )
    if context.get("kind") == "speculative":
        arguments.append(
            {
                "point": (
                    "This role was not advertised, so there is no posted band to be held to."
                ),
                "evidence": "The opportunity is a speculative opening (FR-263).",
                "strength": "moderate",
            }
        )

    fallbacks = [
        {
            "ask": "A review at six months with a defined target",
            "why": "Moves the decision rather than losing it, and costs nothing today.",
            "cost_to_employer": "low",
        },
        {
            "ask": "Additional leave days",
            "why": "Cheaper for the employer than base salary and worth real money.",
            "cost_to_employer": "low",
        },
        {
            "ask": "A training and conference budget, named in the contract",
            "why": "Usually sits in a different budget line than salary.",
            "cost_to_employer": "low",
        },
        {
            "ask": "A signing amount to bridge a bonus left behind",
            "why": "One-off, so it does not raise the salary baseline they have to defend.",
            "cost_to_employer": "medium",
        },
    ]
    for benefit in figures.benefits_must_have[:3]:
        fallbacks.insert(
            0,
            {
                "ask": str(benefit),
                "why": "Listed as a must-have in the compensation directives (FR-146).",
                "cost_to_employer": "medium",
            },
        )

    risks = []
    if figures.market_confidence < 0.4:
        risks.append(
            "The market range rests on thin data; naming a figure first may anchor too low "
            "or too high."
        )
    if figures.financials_estimated:
        risks.append(
            "The company figures are estimated from incomplete filings, so ability to pay "
            "may be overstated."
        )
    if figures.ability_to_pay is not None and figures.ability_to_pay < 40:
        risks.append(
            "Ability to pay scored low; base salary may genuinely not move, which makes the "
            "fallbacks the real conversation."
        )
    if not risks:
        risks.append(
            "Naming a number before they do gives away the first move; ask for their range "
            "first where the conversation allows it."
        )
    return arguments, fallbacks, risks


def write_case(
    figures: Figures,
    context: dict[str, Any],
    *,
    language: str,
    profile: dict | None,
    directive_row: dict | None,
    llm: LLMClient | None,
) -> dict[str, Any]:
    """Arguments, fallbacks and risks.  The figures are never handed to the model to change."""
    arguments, fallbacks, risks = deterministic_arguments(figures, context)
    case = {
        "arguments": arguments,
        "fallbacks": fallbacks,
        "risks": risks,
        "opening_line": "",
        "if_pushed": (
            "Ask what range the role carries before naming a figure; if pressed, give the "
            "band rather than a single number."
        ),
        "generated_by": "computed",
    }
    if llm is None:
        return case

    template = load_prompt(PROMPT_NAME)
    system, user = template.render(language=language)
    try:
        payload = llm.complete_json(
            "analysis.financial",
            system=system,
            user=user + "\n\nComputed figures (trusted):\n" + (to_json(figures.as_dict()) or "{}"),
            untrusted={
                "context": to_json(
                    {
                        "title": context.get("title"),
                        "company": context.get("company_name"),
                        "kind": context.get("kind"),
                        "seniority": context.get("seniority"),
                        "business_summary": (context.get("business_summary") or "")[:2000],
                        "financials_estimated": figures.financials_estimated,
                    }
                )
                or "{}",
                "seeker": to_json(profile or {}) or "{}",
                "directives": to_json(
                    from_json((directive_row or {}).get("compensation"), {}) or {}
                )
                or "{}",
            },
            entity_type="opportunity",
            entity_id=context.get("id"),
            prompt_template=f"{template.name}.md",
            prompt_version=template.version,
            temperature=0.3,
            max_tokens=1800,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("Negotiation arguments stayed computed: %s", exc)
        return case

    model_arguments = [
        {
            "point": str(a.get("point") or "")[:400],
            "evidence": str(a.get("evidence") or "")[:400],
            "strength": str(a.get("strength") or "moderate")[:10],
        }
        for a in (payload or {}).get("arguments", [])
        if isinstance(a, dict) and a.get("point")
    ][:8]
    model_fallbacks = [
        {
            "ask": str(f.get("ask") or "")[:200],
            "why": str(f.get("why") or "")[:400],
            "cost_to_employer": str(f.get("cost_to_employer") or "medium")[:10],
        }
        for f in (payload or {}).get("fallbacks", [])
        if isinstance(f, dict) and f.get("ask")
    ][:8]
    if not model_arguments:
        return case
    return {
        "arguments": model_arguments,
        "fallbacks": model_fallbacks or fallbacks,
        "risks": [str(r)[:400] for r in ((payload or {}).get("risks") or risks)][:6],
        "opening_line": str((payload or {}).get("opening_line") or "")[:600],
        "if_pushed": str((payload or {}).get("if_pushed") or case["if_pushed"])[:600],
        "generated_by": "llm",
    }


# ---------------------------------------------------------------------------
# The PDF (FR-444, rendered through the shared builder)
# ---------------------------------------------------------------------------

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Salary negotiation brief",
        "subtitle": "Prepared for the job seeker - not shared with the employer",
        "the_ask": "The suggested ask",
        "why": "Why it sits there",
        "inputs": "The figures behind it",
        "arguments": "Arguments",
        "fallbacks": "Fallback positions",
        "risks": "Risks and what is thin",
        "opening": "Opening the conversation",
        "if_pushed": "If they ask for a number first",
        "gaps": "What is missing from the evidence",
        "advisory": "This brief is advisory. Every figure in it is an input to a decision "
                    "the job seeker takes (NFR-305).",
    },
    "nl": {
        "title": "Onderhandelingsbriefing loon",
        "subtitle": "Voor de sollicitant - niet voor de werkgever",
        "the_ask": "De voorgestelde vraag",
        "why": "Waarom die daar ligt",
        "inputs": "De cijfers erachter",
        "arguments": "Argumenten",
        "fallbacks": "Terugvalposities",
        "risks": "Risico's en wat dun is",
        "opening": "Het gesprek openen",
        "if_pushed": "Als zij eerst een cijfer vragen",
        "gaps": "Wat ontbreekt in het bewijs",
        "advisory": "Deze briefing is adviserend. De sollicitant beslist (NFR-305).",
    },
    "fr": {
        "title": "Note de negociation salariale",
        "subtitle": "Pour le candidat - non destinee a l'employeur",
        "the_ask": "La demande suggeree",
        "why": "Pourquoi ce niveau",
        "inputs": "Les chiffres qui la fondent",
        "arguments": "Arguments",
        "fallbacks": "Positions de repli",
        "risks": "Risques et points faibles",
        "opening": "Ouvrir la conversation",
        "if_pushed": "S'ils demandent un chiffre en premier",
        "gaps": "Ce qui manque",
        "advisory": "Cette note est indicative. Le candidat decide (NFR-305).",
    },
    "de": {
        "title": "Gehaltsverhandlungs-Briefing",
        "subtitle": "Fuer die Bewerberin oder den Bewerber - nicht fuer den Arbeitgeber",
        "the_ask": "Die vorgeschlagene Forderung",
        "why": "Warum sie dort liegt",
        "inputs": "Die Zahlen dahinter",
        "arguments": "Argumente",
        "fallbacks": "Rueckfallpositionen",
        "risks": "Risiken und duenne Stellen",
        "opening": "Das Gespraech eroeffnen",
        "if_pushed": "Wenn zuerst nach einer Zahl gefragt wird",
        "gaps": "Was in der Beweislage fehlt",
        "advisory": "Dieses Briefing ist beratend. Die Entscheidung liegt bei der "
                    "Bewerberin oder dem Bewerber (NFR-305).",
    },
}


def _t(language: str, key: str) -> str:
    return _LABELS.get(language, _LABELS["en"]).get(key, _LABELS["en"][key])


def render_pdf(
    *,
    job_seeker_id: str,
    opportunity_id: str,
    context: dict[str, Any],
    figures: Figures,
    case: dict[str, Any],
    language: str,
    seeker_name: str,
) -> Path:
    """Build the brief with the shared document furniture (FR-331)."""
    language = pdf.normalise_language(language)
    money = lambda v: pdf.format_money(v, figures.currency, language=language)  # noqa: E731
    meta = pdf.DocumentMeta(
        title=_t(language, "title"),
        subtitle=f"{context.get('title') or ''} - {context.get('company_name') or ''}".strip(" -"),
        language=language,
        generated_at=utcnow(),
        prepared_for=seeker_name,
        company_snapshot_at=context.get("collected_at"),
        seeker_only=True,
    )
    builder = pdf.PdfBuilder(meta)
    pdf.cover_page(
        builder,
        heading=_t(language, "title"),
        subheading=meta.subtitle,
        facts=[
            (_t(language, "the_ask"), _ask_line(figures, money)),
            ("Ability to pay", f"{figures.ability_to_pay}/100"
             if figures.ability_to_pay is not None else pdf.label(language, "not_available")),
            ("Personnel cost / FTE", money(figures.personnel_cost_per_fte)
             if figures.personnel_cost_per_fte else pdf.label(language, "not_available")),
            ("Confidence", f"{figures.confidence:.0%}"),
        ],
        footnote=_t(language, "advisory"),
    )

    builder.h1(_t(language, "the_ask"))
    builder.para(_ask_line(figures, money))
    builder.h2(_t(language, "why"))
    builder.para(figures.anchor_rationale)

    builder.h1(_t(language, "inputs"))
    builder.key_values(
        [
            ("Market range (FR-264)",
             f"{money(figures.market_min)} - {money(figures.market_max)}"
             if figures.market_min else pdf.label(language, "not_available")),
            ("Market basis", f"{figures.market_method.replace('_', ' ')}"
             + (f", {figures.market_sample} data points" if figures.market_sample else "")),
            ("Stated by the employer",
             f"{money(figures.stated_min)} - {money(figures.stated_max)}"
             if figures.is_stated else "no"),
            ("Ability to pay (FR-244)",
             f"{figures.ability_to_pay}/100 - {figures.ability_to_pay_rationale or ''}"
             if figures.ability_to_pay is not None else pdf.label(language, "not_available")),
            ("Personnel cost per FTE (FR-243)", money(figures.personnel_cost_per_fte)
             if figures.personnel_cost_per_fte else pdf.label(language, "not_available")),
            ("Company trajectory", figures.financial_trajectory or ""),
            ("Minimum package (FR-146)", money(figures.directive_minimum)
             if figures.directive_minimum else "not set"),
            ("Walk-away point", money(figures.walk_away) if figures.walk_away else "not set"),
        ]
    )
    if figures.financials_estimated:
        builder.note(
            f"The company figures are {pdf.label(language, 'estimated')} from incomplete "
            "filings (FR-245)."
        )

    builder.h1(_t(language, "arguments"))
    if case["arguments"]:
        builder.table(
            ["#", "Argument", "Evidence", "Strength"],
            [
                [str(i), a["point"], a.get("evidence", ""), a.get("strength", "")]
                for i, a in enumerate(case["arguments"], start=1)
            ],
            col_widths=[4, 34, 46, 12],
        )

    builder.h1(_t(language, "fallbacks"))
    if case["fallbacks"]:
        builder.table(
            ["Ask", "Why here", "Cost to employer"],
            [
                [f["ask"], f.get("why", ""), f.get("cost_to_employer", "")]
                for f in case["fallbacks"]
            ],
            col_widths=[28, 54, 18],
        )

    if case.get("opening_line"):
        builder.h2(_t(language, "opening"))
        builder.para(case["opening_line"])
    if case.get("if_pushed"):
        builder.h2(_t(language, "if_pushed"))
        builder.para(case["if_pushed"])

    builder.h1(_t(language, "risks"))
    builder.bullets(case["risks"])
    if figures.gaps:
        builder.h2(_t(language, "gaps"))
        builder.bullets(figures.gaps)
    builder.note(_t(language, "advisory"))

    target = (
        get_settings().generated_dir
        / job_seeker_id
        / "negotiation"
        / f"{opportunity_id}-{datetime.now(UTC).strftime('%Y%m%d')}.pdf"
    )
    return builder.build(target)


def _ask_line(figures: Figures, money: Any) -> str:
    if figures.ask_min is None:
        return "No figure suggested - the evidence does not support one."
    if figures.ask_max and figures.ask_max != figures.ask_min:
        return f"{money(figures.ask_min)} - {money(figures.ask_max)} ({figures.currency})"
    return f"{money(figures.ask_min)} ({figures.currency})"


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def eligible(job_seeker_id: str, opportunity_id: str) -> tuple[bool, str]:
    """FR-444's own condition: interview or offer stage."""
    card = repo.card_for_opportunity(job_seeker_id, opportunity_id)
    if card is None:
        return False, "This opportunity has no pipeline card; no application has been sent."
    stage = str(card.get("stage") or "")
    if stage not in ELIGIBLE_STAGES:
        return False, (
            f"FR-444 applies from the interview stage; this application is at {stage!r}."
        )
    return True, stage


def build(
    job_seeker_id: str,
    opportunity_id: str,
    *,
    language: str | None = None,
    llm: LLMClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Generate the negotiation brief and its PDF (FR-444)."""
    context = repo.opportunity_context(opportunity_id, job_seeker_id)
    if context is None:
        raise LookupError(f"No opportunity {opportunity_id} for this job seeker")
    ok, stage = eligible(job_seeker_id, opportunity_id)
    if not ok and not force:
        raise NotEligible(stage)

    seeker = repo.seeker(job_seeker_id) or {}
    lang = pdf.normalise_language(
        language or context.get("language") or seeker.get("locale") or "en"
    )
    directive_row = repo.directives_for_campaign(context.get("campaign_id"), job_seeker_id)
    figures = suggest_ask(gather_figures(context, directive_row))
    profile = _profile_blocks(repo.composite_profile(job_seeker_id))
    case = write_case(
        figures, context, language=lang, profile=profile, directive_row=directive_row, llm=llm
    )

    pdf_path: str | None = None
    try:
        pdf_path = str(
            render_pdf(
                job_seeker_id=job_seeker_id,
                opportunity_id=opportunity_id,
                context=context,
                figures=figures,
                case=case,
                language=lang,
                seeker_name=str(seeker.get("display_name") or ""),
            )
        )
    except Exception:  # noqa: BLE001 - the brief's content matters more than its PDF
        log.exception("Could not render the negotiation brief PDF for %s", opportunity_id)

    brief_id = repo.save_brief(
        job_seeker_id,
        opportunity_id,
        {
            "language": lang,
            "stage": stage if ok else None,
            "currency": figures.currency,
            "suggested_ask": _ask_line(
                figures, lambda v: pdf.format_money(v, figures.currency, language=lang)
            ),
            "ask_min": figures.ask_min,
            "ask_max": figures.ask_max,
            "walk_away": figures.walk_away,
            "arguments": to_json(case["arguments"]),
            "fallbacks": to_json(case["fallbacks"]),
            "market_data": to_json(
                {
                    "min": figures.market_min,
                    "max": figures.market_max,
                    "median": figures.market_median,
                    "method": figures.market_method,
                    "confidence": figures.market_confidence,
                    "sample_size": figures.market_sample,
                }
            ),
            "inputs": to_json(figures.as_dict()),
            "confidence": figures.confidence,
            "pdf_path": pdf_path,
        },
    )
    record_audit(
        "negotiation.brief_generated",
        "negotiation_brief",
        brief_id,
        seeker_id=job_seeker_id,
        detail={"opportunity_id": opportunity_id, "generated_by": case["generated_by"]},
    )
    return {
        "brief_id": brief_id,
        "opportunity_id": opportunity_id,
        "stage": stage if ok else None,
        "figures": figures.as_dict(),
        "case": case,
        "pdf_path": pdf_path,
        "advisory": _t(lang, "advisory"),
    }


def get(job_seeker_id: str, opportunity_id: str) -> dict | None:
    return repo.get_brief(job_seeker_id, opportunity_id)


def _profile_blocks(profile: dict | None) -> dict[str, Any] | None:
    if not profile:
        return None
    return {
        "achievements": from_json(profile.get("achievements"), None),
        "core_competencies": from_json(profile.get("core_competencies"), None),
        "seniority": profile.get("seniority"),
    }
