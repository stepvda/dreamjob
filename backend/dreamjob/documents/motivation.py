"""Motivation and fit PDF (FR-330, FR-331, FR-128, CR-405).

The second document written for the job seeker alone.  FR-330 fixes its five
parts and this module keeps them in that order: why the job seeker wants this
job - linked explicitly to the dream job model (FR-128) and to their career
trajectory; why they fit the job, with evidence from the profile mapped to
*each* stated or inferred requirement; why they fit the company, drawn from the
company's values and culture, its stage, sector and working style; the
objections to expect and how to answer them; and a short set of talking points
to rehearse.

The mapping in "why I fit the job" is the part that has to be honest.  Every
requirement gets a row whether or not the profile supports it, and one that the
profile does not support is marked ``gap`` and says what is closest instead
(CR-405).  A document that quietly drops the requirements the job seeker
cannot meet is worse than useless in an interview room.

Without an LLM the whole document is still produced: requirements are matched
to profile skills and positions by term overlap, the dream-job links come from
the stored model, and the objections come from the gaps the mapping found.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dreamjob.db.connection import utcnow
from dreamjob.documents._llm import complete_json
from dreamjob.documents.consistency import fold
from dreamjob.documents.pdf_builder import (
    PdfBuilder,
    cover_page,
    label,
    meta_from_inputs,
    normalise_language,
)
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.enrichment import load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "motivation_fit"

#: FR-331: configurable templates, expressed as an ordered section list.
TEMPLATES: dict[str, tuple[str, ...]] = {
    "full": ("why_job", "fit_job", "fit_company", "objections", "talking_points"),
    "compact": ("why_job", "fit_job", "talking_points"),
    "interview_drill": ("fit_job", "objections", "talking_points"),
}
DEFAULT_TEMPLATE = "full"

STRENGTH_ORDER = {"strong": 0, "partial": 1, "gap": 2}
_STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "in", "with", "for", "to", "en", "de", "het",
    "een", "van", "met", "voor", "et", "des", "les", "und", "mit", "der", "die", "das",
    "ervaring", "experience", "erfahrung", "expérience", "kennis", "knowledge", "years",
    "jaar", "jahre", "ans", "sterke", "strong", "goede", "good",
}


@dataclass
class MotivationResult:
    path: str
    template: str
    language: str
    generated_at: str
    used_llm: bool
    content: dict[str, Any]
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "motivation_pdf_path": self.path,
            "motivation_template": self.template,
            "language": self.language,
            "generated_at": self.generated_at,
            "motivation_used_llm": self.used_llm,
            "notes": self.notes,
            "gaps": [
                row.get("requirement")
                for row in self.content.get("why_fit_job") or []
                if row.get("strength") == "gap"
            ],
        }

    def plain_text(self) -> str:
        """The document as text, for the NFR-206 leak scan."""
        chunks: list[str] = []
        for row in self.content.get("why_this_job") or []:
            chunks += [str(row.get("text") or ""), str(row.get("link") or "")]
        for row in self.content.get("why_fit_job") or []:
            chunks += [
                str(row.get("requirement") or ""), str(row.get("evidence") or ""),
                str(row.get("talking_point") or ""),
            ]
        for row in self.content.get("why_fit_company") or []:
            chunks += [str(row.get("text") or ""), str(row.get("evidence") or "")]
        for row in self.content.get("objections") or []:
            chunks += [
                str(row.get("objection") or ""), str(row.get("answer") or ""),
                str(row.get("evidence") or ""),
            ]
        chunks += [str(t) for t in self.content.get("talking_points") or []]
        return "\n".join(c for c in chunks if c)


# ---------------------------------------------------------------------------
# Requirements: stated, then inferred
# ---------------------------------------------------------------------------


def requirements(inputs: dict[str, Any]) -> list[str]:
    """Every stated or inferred requirement of the opening (FR-330)."""
    opportunity = inputs.get("opportunity") or {}
    vacancy = inputs.get("vacancy") or {}
    out: list[str] = []
    for source in (
        opportunity.get("required_skills"),
        vacancy.get("required_skills"),
        opportunity.get("desirable_skills"),
        vacancy.get("desirable_skills"),
    ):
        for item in source or []:
            text = str(item).strip()
            if text and text.lower() not in {o.lower() for o in out}:
                out.append(text)
    if not out:
        # A speculative opening has no posting to read requirements from; its
        # own rationale is the nearest thing, and it is text the pipeline
        # already produced rather than something invented here (CR-405).
        out = _inferred_requirements(
            str(opportunity.get("description") or vacancy.get("description") or "")
        ) or _inferred_requirements(str(opportunity.get("speculative_rationale") or ""))
    return out[:18]


def _inferred_requirements(description: str) -> list[str]:
    """Bullet lines of a posting are its requirements, near enough, when no
    structured extraction ran.  Nothing is invented: these are the posting's
    own sentences."""
    lines = [
        re.sub(r"^\s*[-*•–—▪·]\s*", "", line).strip()
        for line in description.splitlines()
        if line.strip()
    ]
    candidates = [
        line for line in lines if 12 <= len(line) <= 180 and not line.endswith(":")
    ]
    return candidates[:10]


# ---------------------------------------------------------------------------
# Deterministic content
# ---------------------------------------------------------------------------


def _terms(text: str) -> set[str]:
    return {w for w in fold(text).split() if len(w) > 2 and w not in _STOPWORDS}


def derive_content(inputs: dict[str, Any], lang: str) -> dict[str, Any]:
    """The document without a model: matching, not writing (CR-405)."""
    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    dream = inputs.get("dream_job") or {}
    composite = inputs.get("composite") or {}
    version = inputs.get("profile_version") or {}
    sections = version.get("sections") or {}
    skills = inputs.get("skills") or []

    # -- why this job: the dream job model and the trajectory, as stored -----
    why_job: list[dict[str, str]] = []
    if dream.get("statement"):
        why_job.append(
            {"text": str(dream["statement"])[:600], "link": "dream_job_model.statement"}
        )
    for key in ("target_roles", "responsibilities", "company_characteristics", "culture_values"):
        for item in _statements(dream.get(key))[:2]:
            why_job.append({"text": item, "link": f"dream_job_model.{key}"})
    for item in _statements(composite.get("career_trajectory"))[-2:]:
        why_job.append({"text": item, "link": "composite_profile.career_trajectory"})

    # -- why I fit the job: requirement -> evidence -------------------------
    evidence_pool = _evidence_pool(sections, skills, composite, inputs.get("evidence") or [])
    fit_job: list[dict[str, str]] = []
    for requirement in requirements(inputs):
        best, score = _best_evidence(requirement, evidence_pool)
        strength = "strong" if score >= 0.6 else "partial" if score >= 0.25 else "gap"
        fit_job.append(
            {
                "requirement": requirement,
                "evidence": best or label(lang, "no_evidence"),
                "strength": strength,
                "talking_point": best or "",
            }
        )

    # -- why I fit the company ----------------------------------------------
    fit_company: list[dict[str, str]] = []
    for value in _statements(company.get("values_culture"))[:4]:
        fit_company.append({"text": value, "evidence": "company.values_culture"})
    for key, label_key, source in (
        ("stage", "stage", company.get("stage")),
        ("sector_codes", "sector", ", ".join(_statements(company.get("sector_codes"))[:3])),
        ("trajectory", "trajectory", company.get("trajectory")),
        ("size_band", "size", company.get("size_band")),
        ("ownership", "ownership", company.get("ownership")),
    ):
        if source:
            fit_company.append(
                {"text": f"{label(lang, label_key)}: {source}", "evidence": f"company.{key}"}
            )

    # -- objections: the gaps the mapping found -----------------------------
    objections: list[dict[str, str]] = []
    for row in fit_job:
        if row["strength"] != "gap":
            continue
        objections.append(
            {
                "objection": label(lang, "gap_objection").format(requirement=row["requirement"]),
                "answer": "",
                "evidence": label(lang, "no_evidence"),
            }
        )
    for deal_breaker in _statements(dream.get("deal_breakers"))[:2]:
        objections.append(
            {
                "objection": deal_breaker,
                "answer": "",
                "evidence": "dream_job_model.deal_breakers",
            }
        )

    talking_points = [
        row["talking_point"] for row in fit_job if row["strength"] == "strong"
    ][:6] or _statements(composite.get("achievements"))[:6]

    return {
        "why_this_job": why_job[:6],
        "why_fit_job": fit_job,
        "why_fit_company": fit_company[:8],
        "objections": objections[:6],
        "talking_points": talking_points,
        "opportunity_title": opportunity.get("title"),
    }


def _statements(value: Any) -> list[str]:
    """Composite and dream-job blocks are lists of ``{"id", "text"}`` statements."""
    out: list[str] = []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        text = value.get("text") or value.get("name") or value.get("statement")
        return [str(text)] if text else []
    for item in value or []:
        if isinstance(item, dict):
            text = (
                item.get("text")
                or item.get("name")
                or item.get("title")
                or item.get("description")
            )
            if text:
                out.append(str(text))
        elif str(item).strip():
            out.append(str(item))
    return out


def _evidence_pool(
    sections: dict, skills: list[dict], composite: dict, evidence: list[dict]
) -> list[tuple[set[str], str]]:
    pool: list[tuple[set[str], str]] = []
    for row in skills:
        text = str(row.get("normalised_label") or row.get("raw_label") or "")
        if not text:
            continue
        detail = text
        if row.get("years_experience"):
            detail = f"{text} ({row['years_experience']} yr)"
        pool.append((_terms(text), detail))
    for label_text in sections.get("top_skills") or []:
        pool.append((_terms(str(label_text)), str(label_text)))
    for entry in sections.get("experience") or []:
        text = " ".join(
            str(entry.get(k) or "") for k in ("title", "company", "description")
        )
        summary = f"{entry.get('title', '')} - {entry.get('company', '')}".strip(" -")
        pool.append((_terms(text), summary or text[:160]))
    for statement in _statements(composite.get("achievements")) + _statements(
        composite.get("core_competencies")
    ):
        pool.append((_terms(statement), statement))
    for item in evidence:
        text = f"{item.get('title', '')} {item.get('description', '')}"
        pool.append((_terms(text), str(item.get("title") or "")))
    return [entry for entry in pool if entry[0]]


def _best_evidence(
    requirement: str, pool: list[tuple[set[str], str]]
) -> tuple[str | None, float]:
    wanted = _terms(requirement)
    if not wanted:
        return None, 0.0
    best_text, best_score = None, 0.0
    for terms, text in pool:
        overlap = len(wanted & terms)
        if not overlap:
            continue
        score = overlap / len(wanted)
        if score > best_score:
            best_text, best_score = text, score
    return best_text, best_score


# ---------------------------------------------------------------------------
# LLM content
# ---------------------------------------------------------------------------


def _llm_content(
    inputs: dict[str, Any], lang: str, llm: LLMClient, instructions: str | None
) -> dict[str, Any]:
    import json  # noqa: PLC0415 - only on the LLM path

    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    composite = inputs.get("composite") or {}
    version = inputs.get("profile_version") or {}
    dream = inputs.get("dream_job") or {}

    prompt = load_prompt(PROMPT_NAME)
    profile_payload = {
        "summary": (version.get("sections") or {}).get("summary"),
        "experience": (version.get("sections") or {}).get("experience"),
        "education": (version.get("sections") or {}).get("education"),
        "skills": [s.get("normalised_label") for s in inputs.get("skills") or []],
        "composite": {
            key: composite.get(key)
            for key in ("narrative", "career_trajectory", "core_competencies",
                        "achievements", "domains", "seniority", "constraints")
        },
        "dream_job_model": {
            key: dream.get(key)
            for key in ("statement", "target_roles", "responsibilities",
                        "company_characteristics", "culture_values", "deal_breakers")
        },
    }
    system, user = prompt.render(
        language=lang,
        role_title=opportunity.get("title") or "",
        company_name=company.get("name") or "",
        speculative_note=(
            ", a speculative opening that has not been advertised"
            if str(opportunity.get("kind")) == "speculative"
            else ""
        ),
        profile_json=json.dumps(profile_payload, ensure_ascii=False, default=str)[:20_000],
        requirements_json=json.dumps(requirements(inputs), ensure_ascii=False),
    )
    if instructions:
        user += f"\n\nThe job seeker asked for this revision:\n{str(instructions)[:2000]}"

    return complete_json(
        llm,
        "generate.motivation",
        system,
        user,
        untrusted={
            "company": json.dumps(company, ensure_ascii=False, default=str)[:20_000],
            "opening": json.dumps(
                {"opportunity": opportunity, "vacancy": inputs.get("vacancy") or {}},
                ensure_ascii=False,
                default=str,
            )[:15_000],
        },
        entity_type="opportunity",
        entity_id=opportunity.get("id"),
        prompt_template=prompt.name,
        prompt_version=prompt.version,
        schema_hint=(
            '{"why_this_job": [{"text": str, "link": str}], '
            '"why_fit_job": [{"requirement": str, "evidence": str, "strength": str, '
            '"talking_point": str}], '
            '"why_fit_company": [{"text": str, "evidence": str}], '
            '"objections": [{"objection": str, "answer": str, "evidence": str}], '
            '"talking_points": [str]}'
        ),
    )


def _merge(derived: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    """Keep the model's prose, but never lose a requirement it skipped."""
    out = dict(derived)
    for key in ("why_this_job", "why_fit_company", "objections", "talking_points"):
        if generated.get(key):
            out[key] = generated[key]

    rows = [r for r in generated.get("why_fit_job") or [] if isinstance(r, dict)]
    covered = {fold(r.get("requirement")) for r in rows}
    for row in derived["why_fit_job"]:
        if fold(row["requirement"]) not in covered:
            rows.append(row)
    for row in rows:
        if row.get("strength") not in STRENGTH_ORDER:
            row["strength"] = "partial"
    out["why_fit_job"] = sorted(rows, key=lambda r: STRENGTH_ORDER[r["strength"]])
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def generate_motivation(
    inputs: dict[str, Any],
    *,
    output_dir: Path | str,
    language: str | None = None,
    template: str | None = None,
    llm: LLMClient | None = None,
    instructions: str | None = None,
    basename: str = "motivation",
) -> MotivationResult:
    opportunity = inputs.get("opportunity") or {}
    seeker = inputs.get("seeker") or {}
    company = inputs.get("company") or {}
    lang = normalise_language(language or opportunity.get("language") or seeker.get("locale"))
    sections = TEMPLATES.get(template or DEFAULT_TEMPLATE, TEMPLATES[DEFAULT_TEMPLATE])

    content = derive_content(inputs, lang)
    notes: list[str] = []
    used_llm = False
    if llm is not None:
        try:
            content = _merge(content, _llm_content(inputs, lang, llm, instructions) or {})
            used_llm = True
        except BudgetExhausted:
            notes.append("Token budget exhausted; the document is the derived mapping.")
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            log.warning("Motivation generation failed: %s", exc)
            notes.append(f"Written without the model ({exc.__class__.__name__}).")
    else:
        notes.append("No LLM configured; requirements matched to the profile mechanically.")

    meta = meta_from_inputs(
        inputs,
        title=label(lang, "motivation_title"),
        subtitle=str(opportunity.get("title") or ""),
        language=lang,
        generated_at=utcnow(),
        seeker_only=True,
    )
    builder = PdfBuilder(meta)
    cover_page(
        builder,
        heading=label(lang, "motivation_title"),
        subheading=f"{opportunity.get('title', '')} · {company.get('name', '')}".strip(" ·"),
        facts=[(label(lang, "motivation_subtitle"), company.get("name"))],
    )

    renderers = {
        "why_job": _why_job,
        "fit_job": _fit_job,
        "fit_company": _fit_company,
        "objections": _objections,
        "talking_points": _talking_points,
    }
    for name in sections:
        renderers[name](builder, content, lang)

    path = builder.build(Path(output_dir) / f"{basename}.pdf")
    return MotivationResult(
        path=str(path),
        template=template or DEFAULT_TEMPLATE,
        language=lang,
        generated_at=meta.generated_at,
        used_llm=used_llm,
        content=content,
        notes=notes,
    )


def _why_job(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "why_this_job"))
    rows = content.get("why_this_job") or []
    if not rows:
        builder.note(label(lang, "not_available"))
        return
    for row in rows:
        builder.para(str(row.get("text") or ""))
        if row.get("link"):
            builder.note(f"{label(lang, 'dream_job_link')}: {row['link']}")


def _fit_job(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "why_fit_job"))
    rows = content.get("why_fit_job") or []
    if not rows:
        builder.note(label(lang, "no_requirements_stated"))
        return
    builder.table(
        [label(lang, "requirement"), label(lang, "evidence"), ""],
        [
            [
                str(row.get("requirement") or ""),
                str(row.get("evidence") or ""),
                str(row.get("strength") or ""),
            ]
            for row in rows
        ],
        col_widths=[3.2, 5.4, 1.4],
    )
    points = [str(r.get("talking_point") or "") for r in rows if r.get("talking_point")]
    if points:
        builder.bullets(points[:8])


def _fit_company(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "why_fit_company"))
    rows = content.get("why_fit_company") or []
    if not rows:
        builder.note(label(lang, "not_available"))
        return
    for row in rows:
        builder.para(str(row.get("text") or ""))
        if row.get("evidence"):
            builder.note(f"{label(lang, 'evidence')}: {row['evidence']}")


def _objections(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "objections"))
    rows = content.get("objections") or []
    if not rows:
        builder.note(label(lang, "not_available"))
        return
    for row in rows:
        builder.keep_together(lambda sub, row=row: _objection_block(sub, row, lang))


def _objection_block(sub: PdfBuilder, row: dict, lang: str) -> None:
    sub.h2(f"{label(lang, 'objection')}: {row.get('objection', '')}")
    answer = str(row.get("answer") or "").strip()
    sub.para(
        f"{label(lang, 'answer')}: {answer}" if answer else label(lang, "prepare_answer")
    )
    if row.get("evidence"):
        sub.note(f"{label(lang, 'evidence')}: {row['evidence']}")


def _talking_points(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "talking_points"))
    points = [str(p) for p in content.get("talking_points") or [] if str(p).strip()]
    if not points:
        builder.note(label(lang, "not_available"))
        return
    builder.bullets(points)
