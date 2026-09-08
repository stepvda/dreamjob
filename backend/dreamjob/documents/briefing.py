"""Company and job briefing PDF (FR-329, FR-331).

The interview-preparation document.  It is written for the job seeker and is
never sent to the employer - the assembly of dispatch attachments in
``documents.package`` is what enforces that (FR-321).

FR-329 enumerates what the document must contain, and the section list below is
that enumeration in order: the standardised company profile, the five-year
financial analysis *with charts*, hiring signals and recent news, competitors
and market position, the opening in full, the hiring contact and introduction
path, likely interview topics and questions, and the questions to ask back.
Sections whose data has not been collected say so rather than disappearing: a
briefing that silently omits the financials reads like a company with no
filings.

Only the last two sections are written by a model, and both degrade to a
deterministic version derived from the vacancy requirements, the hiring signals
and the financial trajectory when no LLM is available (NFR-104, CR-405).

The document is regenerable on demand so it can be refreshed just before an
interview (FR-329); it carries the generation date and the data versions it was
built from on every page (FR-331).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dreamjob.db.connection import utcnow
from dreamjob.documents._llm import complete_json
from dreamjob.documents.pdf_builder import (
    PdfBuilder,
    cover_page,
    format_money,
    format_number,
    label,
    meta_from_inputs,
    normalise_language,
)
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.enrichment import load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "briefing_interview_prep"

#: FR-331: the briefing is generated from a configurable template.  A template
#: is a section list; the renderer skips what a template leaves out.
TEMPLATES: dict[str, tuple[str, ...]] = {
    "full": (
        "company_profile", "financials", "signals", "competitors", "opening",
        "contact", "interview", "questions",
    ),
    "compact": ("company_profile", "financials", "opening", "interview", "questions"),
    "financial": ("company_profile", "financials", "competitors", "questions"),
}
DEFAULT_TEMPLATE = "full"


@dataclass
class BriefingResult:
    path: str
    template: str
    language: str
    generated_at: str
    used_llm: bool
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "briefing_pdf_path": self.path,
            "briefing_template": self.template,
            "language": self.language,
            "generated_at": self.generated_at,
            "briefing_used_llm": self.used_llm,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_briefing(
    inputs: dict[str, Any],
    *,
    output_dir: Path | str,
    language: str | None = None,
    template: str | None = None,
    llm: LLMClient | None = None,
    basename: str = "briefing",
) -> BriefingResult:
    opportunity = inputs.get("opportunity") or {}
    seeker = inputs.get("seeker") or {}
    company = inputs.get("company") or {}
    lang = normalise_language(language or opportunity.get("language") or seeker.get("locale"))
    sections = TEMPLATES.get(template or DEFAULT_TEMPLATE, TEMPLATES[DEFAULT_TEMPLATE])

    prep, notes, used_llm = _interview_prep(inputs, lang, llm)

    meta = meta_from_inputs(
        inputs,
        title=label(lang, "briefing_title"),
        subtitle=str(company.get("name") or opportunity.get("title") or ""),
        language=lang,
        generated_at=utcnow(),
        seeker_only=True,
    )
    builder = PdfBuilder(meta)
    cover_page(
        builder,
        heading=label(lang, "briefing_title"),
        subheading=str(company.get("name") or ""),
        facts=[
            (label(lang, "the_opening"), opportunity.get("title")),
            (
                label(lang, "company_profile"),
                _one_line(company.get("business_summary")),
            ),
        ],
        footnote=(
            label(lang, "speculative_opening")
            if str(opportunity.get("kind")) == "speculative"
            else ""
        ),
    )

    renderers = {
        "company_profile": _company_profile,
        "financials": _financials,
        "signals": _signals,
        "competitors": _competitors,
        "opening": _opening,
        "contact": _contact,
        "interview": lambda b, i, lg: _interview(b, i, lg, prep),
        "questions": lambda b, i, lg: _questions(b, i, lg, prep),
    }
    for name in sections:
        renderers[name](builder, inputs, lang)

    path = builder.build(Path(output_dir) / f"{basename}.pdf")
    return BriefingResult(
        path=str(path),
        template=template or DEFAULT_TEMPLATE,
        language=lang,
        generated_at=meta.generated_at,
        used_llm=used_llm,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# FR-329: the standardised company profile
# ---------------------------------------------------------------------------


def _company_profile(builder: PdfBuilder, inputs: dict, lang: str) -> None:
    company = inputs.get("company") or {}
    builder.h1(label(lang, "company_profile"))
    if not company:
        builder.note(label(lang, "not_available"))
        return

    builder.para(company.get("business_summary"))
    builder.key_values(
        [
            (label(lang, "size_and_locations"), _size_line(company, lang)),
            (
                f"{label(lang, 'stage')} / {label(lang, 'ownership')}",
                " · ".join(
                    str(v) for v in (company.get("stage"), company.get("ownership")) if v
                ),
            ),
            (label(lang, "trajectory"), company.get("trajectory")),
            (label(lang, "sector"), _listing(company.get("sector_codes"))),
            (label(lang, "markets"), _listing(company.get("markets"))),
            (label(lang, "domain"), company.get("domain")),
            (label(lang, "source"), _freshness(company, lang)),
        ]
    )

    if company.get("products_services"):
        builder.h2(label(lang, "products_services"))
        builder.bullets(_items(company.get("products_services")))
    if company.get("reference_customers"):
        builder.h2(label(lang, "reference_customers"))
        builder.bullets(_items(company.get("reference_customers")))
    if company.get("tech_stack"):
        builder.h2(label(lang, "tech_stack"))
        builder.para(_listing(company.get("tech_stack")))
    if company.get("values_culture"):
        builder.h2(label(lang, "values_culture"))
        builder.bullets(_items(company.get("values_culture")))
    if company.get("locations"):
        builder.h2(label(lang, "size_and_locations"))
        builder.bullets(_items(company.get("locations")))
    if company.get("structure"):
        builder.h2(label(lang, "structure"))
        builder.bullets(_items(company.get("structure")))
    if company.get("key_people"):
        builder.h2(label(lang, "key_people"))
        rows = [
            [str(p.get("name") or ""), str(p.get("role") or p.get("title") or ""),
             str(p.get("department") or p.get("note") or "")]
            for p in _dicts(company.get("key_people"))
        ]
        if rows:
            builder.table(
                [label(lang, "name"), label(lang, "role"), ""], rows, col_widths=[3, 4, 3]
            )
        else:
            builder.bullets(_items(company.get("key_people")))


# ---------------------------------------------------------------------------
# FR-329: five years of financials, with charts
# ---------------------------------------------------------------------------

_FIGURE_ROWS: tuple[tuple[str, str], ...] = (
    ("revenue", "revenue"),
    ("gross_profit", "revenue"),
    ("ebitda", "ebitda"),
    ("ebit", "ebit"),
    ("net_result", "net_result"),
    ("equity", "equity"),
    ("personnel_costs", "personnel_costs"),
)


def _financials(builder: PdfBuilder, inputs: dict, lang: str) -> None:
    years = sorted(
        inputs.get("financial_years") or [], key=lambda r: int(r.get("fiscal_year") or 0)
    )
    analysis = inputs.get("financial_analysis") or {}
    builder.h1(label(lang, "financials"))
    if not years:
        builder.note(label(lang, "not_available"))
        return

    currency = str(years[-1].get("currency") or "EUR")
    categories = [str(int(r["fiscal_year"])) for r in years]

    builder.bar_chart(
        label(lang, "revenue_and_result"),
        categories,
        {
            label(lang, "revenue"): [_num(r.get("revenue")) for r in years],
            label(lang, "ebitda"): [_num(r.get("ebitda") or r.get("ebit")) for r in years],
            label(lang, "net_result"): [_num(r.get("net_result")) for r in years],
        },
        value_scale=1_000_000,
        unit=f"M {currency}",
    )
    builder.line_chart(
        label(lang, "headcount_trend"),
        categories,
        {label(lang, "headcount"): [_num(r.get("headcount_fte")) for r in years]},
        unit="FTE",
    )

    builder.h2(label(lang, "financial_table"))
    header = [label(lang, "fiscal_year"), *categories]
    rows: list[list[str]] = []
    for key, label_key in _FIGURE_ROWS:
        if not any(r.get(key) is not None for r in years):
            continue
        rows.append(
            [
                label(lang, label_key if key != "gross_profit" else "gross_profit"),
                *[format_money(_num(r.get(key)), currency, language=lang) for r in years],
            ]
        )
    if any(r.get("headcount_fte") is not None for r in years):
        rows.append(
            [
                label(lang, "headcount"),
                *[format_number(_num(r.get("headcount_fte")), language=lang) for r in years],
            ]
        )
    rows.append(
        [
            label(lang, "estimated"),
            *["x" if r.get("is_estimated") else "-" for r in years],
        ]
    )
    builder.table(header, rows, col_widths=[3, *([2] * len(categories))], align_right_from=1)

    if analysis:
        builder.key_values(
            [
                (label(lang, "trajectory"), _trajectory_line(analysis, lang)),
                (label(lang, "revenue_cagr"), _pct(analysis.get("revenue_cagr"))),
                (label(lang, "headcount_cagr"), _pct(analysis.get("headcount_cagr"))),
                (label(lang, "margin_trend"), analysis.get("margin_trend")),
                (
                    label(lang, "personnel_cost_per_fte"),
                    format_money(_num(analysis.get("personnel_cost_per_fte")), currency,
                                 language=lang)
                    if analysis.get("personnel_cost_per_fte") is not None
                    else "",
                ),
                (
                    label(lang, "current_ratio"),
                    format_number(_num(analysis.get("current_ratio")), digits=2),
                ),
                (label(lang, "solvency_ratio"), _pct(analysis.get("solvency_ratio"))),
                (
                    label(lang, "ability_to_pay"),
                    _score_line(analysis.get("ability_to_pay"),
                                analysis.get("ability_to_pay_rationale")),
                ),
                (
                    label(lang, "investment_capacity"),
                    _score_line(analysis.get("investment_capacity"),
                                analysis.get("investment_capacity_rationale")),
                ),
            ]
        )
    if any(r.get("is_estimated") for r in years) or analysis.get("is_estimated"):
        builder.note(label(lang, "estimated_note"))
    sources = sorted({str(r.get("source")) for r in years if r.get("source")})
    if sources:
        builder.note(f"{label(lang, 'source')}: {', '.join(sources)}")


# ---------------------------------------------------------------------------
# FR-329: signals, news, competitors
# ---------------------------------------------------------------------------


def _signals(builder: PdfBuilder, inputs: dict, lang: str) -> None:
    signals = inputs.get("hiring_signals") or []
    company = inputs.get("company") or {}
    news = _dicts(company.get("news"))
    builder.h1(label(lang, "hiring_signals"))
    if not signals and not news:
        builder.note(label(lang, "not_available"))
        return
    if signals:
        builder.table(
            [label(lang, "signal"), label(lang, "description"), label(lang, "when")],
            [
                [
                    str(s.get("signal_type") or ""),
                    str(s.get("description") or ""),
                    str(s.get("occurred_at") or s.get("collected_at") or "")[:10],
                ]
                for s in signals[:15]
            ],
            col_widths=[2, 6, 2],
        )
    if news:
        builder.h2(label(lang, "news"))
        builder.bullets(
            [
                " - ".join(
                    p for p in (
                        str(n.get("date") or n.get("published_at") or "")[:10],
                        str(n.get("title") or n.get("headline") or n.get("summary") or ""),
                    ) if p
                )
                for n in news[:12]
            ]
        )
    elif company.get("news"):
        builder.h2(label(lang, "news"))
        builder.bullets(_items(company.get("news"))[:12])


def _competitors(builder: PdfBuilder, inputs: dict, lang: str) -> None:
    peers = inputs.get("competitors") or []
    builder.h1(label(lang, "competitors"))
    if not peers:
        builder.note(label(lang, "not_available"))
        return
    builder.table(
        [
            label(lang, "competitor"), label(lang, "basis"), label(lang, "size"),
            label(lang, "stage"), "",
        ],
        [
            [
                str(p.get("peer_company_name") or p.get("peer_name") or ""),
                str(p.get("basis") or ""),
                str(p.get("size_band") or ""),
                str(p.get("stage") or ""),
                _one_line(p.get("business_summary"), 120),
            ]
            for p in peers[:12]
        ],
        col_widths=[3, 2, 1.4, 1.6, 4],
    )
    company = inputs.get("company") or {}
    if company.get("size_band") or company.get("trajectory"):
        builder.note(
            f"{company.get('name', '')}: {company.get('size_band') or ''} · "
            f"{company.get('trajectory') or ''}"
        )


# ---------------------------------------------------------------------------
# FR-329: the opening in full, and the way in
# ---------------------------------------------------------------------------


def _opening(builder: PdfBuilder, inputs: dict, lang: str) -> None:
    opportunity = inputs.get("opportunity") or {}
    vacancy = inputs.get("vacancy") or {}
    speculative = str(opportunity.get("kind")) == "speculative"
    builder.h1(label(lang, "the_opening"))
    builder.h2(str(opportunity.get("title") or vacancy.get("title") or ""))
    if speculative:
        builder.note(label(lang, "speculative_opening"))

    builder.key_values(
        [
            (
                label(lang, "location"),
                opportunity.get("location") or vacancy.get("location"),
            ),
            (
                label(lang, "work_arrangement"),
                opportunity.get("work_arrangement") or vacancy.get("work_arrangement"),
            ),
            (
                label(lang, "contract"),
                opportunity.get("contract_type") or vacancy.get("contract_type"),
            ),
            (
                label(lang, "seniority"),
                opportunity.get("seniority") or vacancy.get("seniority"),
            ),
            (
                label(lang, "posted"),
                str(opportunity.get("posted_at") or vacancy.get("posted_at") or "")[:10],
            ),
            (label(lang, "compensation"), _comp_line(opportunity, lang)),
            (label(lang, "employer_rating"), _rating_line(opportunity)),
            (
                label(lang, "source"),
                opportunity.get("source_url") or vacancy.get("source_url"),
            ),
        ]
    )
    if speculative and opportunity.get("speculative_rationale"):
        builder.h2(label(lang, "why_plausible"))
        builder.para(opportunity.get("speculative_rationale"))
        if opportunity.get("plausibility") is not None:
            score = float(opportunity["plausibility"])
            builder.note(f"{label(lang, 'plausibility')} {score:.2f}")

    required = _items(opportunity.get("required_skills") or vacancy.get("required_skills"))
    desirable = _items(opportunity.get("desirable_skills") or vacancy.get("desirable_skills"))
    if required:
        builder.h2(label(lang, "requirements"))
        builder.bullets(required)
    if desirable:
        builder.h2(label(lang, "desirable"))
        builder.bullets(desirable)

    text = opportunity.get("description") or vacancy.get("description")
    if text:
        builder.h2(label(lang, "vacancy_full_text"))
        builder.para(text)


def _contact(builder: PdfBuilder, inputs: dict, lang: str) -> None:
    contacts = inputs.get("contacts") or []
    paths = inputs.get("introduction_paths") or []
    builder.h1(label(lang, "hiring_contact"))
    if not contacts and not paths:
        builder.note(label(lang, "not_available"))
        return
    if contacts:
        builder.table(
            [
                label(lang, "name"), label(lang, "role"), label(lang, "department"),
                label(lang, "email"), label(lang, "validation"),
            ],
            [
                [
                    str(c.get("full_name") or ""),
                    str(c.get("role_title") or ""),
                    str(c.get("department") or ""),
                    str(c.get("email") or ""),
                    str(c.get("email_validation") or "unknown"),
                ]
                for c in contacts[:8]
            ],
            col_widths=[2.5, 2.5, 2, 3.5, 1.5],
        )
    if paths:
        builder.h2(label(lang, "introduction_path"))
        builder.bullets(
            [
                " · ".join(
                    p for p in (
                        str(row.get("intermediary_name") or ""),
                        str(row.get("intermediary_role") or ""),
                        str(row.get("relationship") or ""),
                        f"degree {row.get('degree')}" if row.get("degree") else "",
                    ) if p
                )
                for row in paths[:6]
            ]
        )


# ---------------------------------------------------------------------------
# FR-329: interview topics and questions
# ---------------------------------------------------------------------------


def _interview(builder: PdfBuilder, inputs: dict, lang: str, prep: dict) -> None:
    builder.h1(label(lang, "interview_topics"))
    topics = prep.get("topics") or []
    if not topics:
        builder.note(label(lang, "not_available"))
        return
    for topic in topics:
        builder.keep_together(lambda sub, topic=topic: _topic_block(sub, topic))
    for warning in prep.get("watch_outs") or []:
        builder.note(f"{label(lang, 'watch_out')}: {warning}")


def _topic_block(sub: PdfBuilder, topic: dict) -> None:
    sub.h2(str(topic.get("topic") or ""))
    if topic.get("why"):
        sub.para(str(topic["why"]), style="note")
    sub.bullets([str(q) for q in (topic.get("questions") or [])])


def _questions(builder: PdfBuilder, inputs: dict, lang: str, prep: dict) -> None:
    builder.h1(label(lang, "questions_to_ask"))
    questions = prep.get("questions_to_ask") or []
    if not questions:
        builder.note(label(lang, "not_available"))
        return
    builder.bullets([str(q) for q in questions])


def _interview_prep(
    inputs: dict, lang: str, llm: LLMClient | None
) -> tuple[dict[str, Any], list[str], bool]:
    if llm is None:
        note = "No LLM configured; questions derived from the collected data."
        return _fallback_prep(inputs, lang), [note], False
    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    try:
        prompt = load_prompt(PROMPT_NAME)
        system, user = prompt.render(
            language=lang,
            role_title=opportunity.get("title") or "",
            company_name=company.get("name") or "",
            speculative_note=(
                ", a speculative opening that has not been advertised"
                if str(opportunity.get("kind")) == "speculative"
                else ""
            ),
            profile_summary=_profile_summary(inputs),
        )
        response = complete_json(
            llm,
            "generate.briefing",
            system,
            user,
            untrusted={
                "company": _company_blob(inputs),
                "opening": _opening_blob(inputs),
            },
            entity_type="opportunity",
            entity_id=opportunity.get("id"),
            prompt_template=prompt.name,
            prompt_version=prompt.version,
            schema_hint=(
                '{"topics": [{"topic": str, "why": str, "questions": [str]}], '
                '"questions_to_ask": [str], "watch_outs": [str]}'
            ),
        )
        if isinstance(response, dict) and response.get("topics"):
            return response, [], True
        note = "The model returned no topics; used the derived set."
        return _fallback_prep(inputs, lang), [note], False
    except BudgetExhausted:
        return _fallback_prep(inputs, lang), ["Token budget exhausted; derived questions."], False
    except (LLMError, ValueError, KeyError, TypeError) as exc:
        log.warning("Briefing preparation failed: %s", exc)
        note = f"Questions derived without the model ({exc.__class__.__name__})."
        return _fallback_prep(inputs, lang), [note], False


def _fallback_prep(inputs: dict, lang: str) -> dict[str, Any]:
    """Deterministic preparation: what the collected data already implies."""
    opportunity = inputs.get("opportunity") or {}
    vacancy = inputs.get("vacancy") or {}
    company = inputs.get("company") or {}
    analysis = inputs.get("financial_analysis") or {}
    signals = inputs.get("hiring_signals") or []

    topics: list[dict[str, Any]] = []
    requirements = _items(
        opportunity.get("required_skills") or vacancy.get("required_skills")
    )[:5]
    for requirement in requirements:
        topics.append(
            {
                "topic": requirement,
                "why": "Listed as a requirement of the opening.",
                "questions": [
                    f"Where have you applied {requirement} and what was the outcome?",
                    f"How would you approach {requirement} in the first ninety days here?",
                ],
            }
        )
    if analysis.get("trajectory"):
        topics.append(
            {
                "topic": f"Company trajectory: {analysis['trajectory']}",
                "why": "From the five-year financial analysis.",
                "questions": [
                    "What is driving the direction the numbers show?",
                    "What would you need this role to change about it?",
                ],
            }
        )
    for signal in signals[:2]:
        topics.append(
            {
                "topic": str(signal.get("signal_type") or "Hiring signal").replace("_", " "),
                "why": str(signal.get("description") or "Recorded hiring signal."),
                "questions": ["How does this role relate to that?"],
            }
        )

    questions = [
        "What does success in this role look like after twelve months?",
        "Who would I work with most closely, and how is the team organised today?",
        "What is the biggest constraint the person in this role will run into?",
        "How are decisions made here, and where would mine sit?",
        "What made the last person in a comparable role succeed or struggle?",
    ]
    if company.get("trajectory"):
        questions.append(
            f"The company is described as {company['trajectory']} - "
            "how does that feel from the inside?"
        )
    if str(opportunity.get("kind")) == "speculative":
        questions.append("If this need is real, what would the route to creating the role be?")
    return {"topics": topics, "questions_to_ask": questions, "watch_outs": []}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return ""
    return f"{number * 100:.1f}%" if abs(number) <= 3 else f"{number:.1f}%"


def _score_line(score: Any, rationale: Any) -> str:
    if score is None:
        return ""
    text = f"{int(score)}/100"
    return f"{text} - {rationale}" if rationale else text


def _trajectory_line(analysis: dict, lang: str) -> str:
    trajectory = str(analysis.get("trajectory") or "")
    confidence = _num(analysis.get("trajectory_confidence"))
    if trajectory and confidence is not None:
        return f"{trajectory} (confidence {confidence:.0%})"
    return trajectory


def _size_line(company: dict, lang: str) -> str:
    parts = [
        f"{int(company['size_fte'])} FTE" if company.get("size_fte") else "",
        str(company.get("size_band") or ""),
        str(company.get("country") or ""),
    ]
    return " · ".join(p for p in parts if p)


def _freshness(company: dict, lang: str) -> str:
    stamp = str(company.get("refreshed_at") or company.get("collected_at") or "")[:10]
    source = str(company.get("source") or company.get("access_method") or "")
    return " · ".join(p for p in (source, stamp) if p)


def _comp_line(opportunity: dict, lang: str = "en") -> str:
    low, high = _num(opportunity.get("comp_min")), _num(opportunity.get("comp_max"))
    if low is None and high is None:
        return ""
    currency = str(opportunity.get("comp_currency") or "EUR")
    span = " - ".join(f"{v:,.0f}" for v in (low, high) if v is not None)
    stated = "" if opportunity.get("comp_is_stated") else f" ({label(lang, 'estimated')})"
    return f"{span} {currency}{stated}"


def _rating_line(opportunity: dict) -> str:
    rating = _num(opportunity.get("employer_rating"))
    themes = _items(opportunity.get("employer_review_themes"))
    if rating is None and not themes:
        return ""
    head = f"{rating:.1f}/5" if rating is not None else ""
    return " · ".join(p for p in (head, ", ".join(themes[:4])) if p)


def _items(value: Any) -> list[str]:
    """Flatten a JSON column into display lines without inventing structure."""
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [f"{k}: {_one_line(v)}" for k, v in value.items() if v not in (None, "", [], {})]
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            out.append(
                " - ".join(
                    str(item[k])
                    for k in ("name", "title", "label", "department", "description", "summary",
                              "text", "city", "country", "value")
                    if item.get(k)
                )
                or _one_line(item)
            )
        else:
            out.append(str(item))
    return [o for o in out if o.strip()]


def _dicts(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _listing(value: Any) -> str:
    return ", ".join(_items(value)[:12])


def _one_line(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _profile_summary(inputs: dict) -> str:
    composite = inputs.get("composite") or {}
    version = inputs.get("profile_version") or {}
    sections = version.get("sections") or {}
    lines = [str(composite.get("narrative") or sections.get("summary") or "")]
    for entry in (sections.get("experience") or [])[:4]:
        lines.append(
            f"- {entry.get('title', '')} at {entry.get('company', '')} "
            f"({entry.get('start', '')}-{entry.get('end') or 'present'})"
        )
    skills = [str(s.get("normalised_label") or "") for s in (inputs.get("skills") or [])][:12]
    if skills:
        lines.append("Skills: " + ", ".join(skills))
    return "\n".join(line for line in lines if line.strip())[:3000]


def _company_blob(inputs: dict) -> str:
    import json  # noqa: PLC0415 - only on the LLM path

    company = dict(inputs.get("company") or {})
    return json.dumps(
        {
            "company": company,
            "financial_years": inputs.get("financial_years") or [],
            "financial_analysis": inputs.get("financial_analysis") or {},
            "hiring_signals": inputs.get("hiring_signals") or [],
            "competitors": inputs.get("competitors") or [],
        },
        ensure_ascii=False,
        default=str,
    )[:25_000]


def _opening_blob(inputs: dict) -> str:
    import json  # noqa: PLC0415

    return json.dumps(
        {
            "opportunity": inputs.get("opportunity") or {},
            "vacancy": inputs.get("vacancy") or {},
        },
        ensure_ascii=False,
        default=str,
    )[:15_000]
