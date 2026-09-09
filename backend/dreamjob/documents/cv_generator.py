"""Tailored CV generation (FR-321, FR-322, FR-106, CR-405, RK-03).

The generator runs in three steps, and the order is the grounding guarantee:

1. **Assemble the base document** from the job seeker's own profile version,
   with the FR-106 do-not-disclose paths applied.  Nothing else may enter the
   ``CvDocument`` after this point: employers, job titles, dates, schools,
   degrees and skill labels are copied from the profile and are never taken
   from a model response.
2. **Tailor it** for the opportunity.  The model receives the *already
   filtered* base document - so a suppressed field cannot reach the provider
   (CR-410) - plus the opportunity text as untrusted data (NFR-205), and
   returns a selection, an ordering and rephrased bullets.
3. **Apply the tailoring** field by field.  Only ``headline``, ``summary``,
   experience ``bullets`` and the skill *ordering* are taken from the response;
   an experience index the model invents is dropped, and a skill the profile
   does not list is discarded rather than added.

What survives step 3 is still model prose, so ``documents.consistency`` checks
every claim in it against the profile before the package may be dispatched
(FR-322, RK-03).

With no LLM configured, or with the campaign's token budget exhausted, the base
document is what ships: fewer words, the same facts, nothing invented
(NFR-104, CR-405).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dreamjob.db.connection import utcnow
from dreamjob.documents._llm import complete_json
from dreamjob.documents.pdf_builder import DocumentMeta, meta_from_inputs, normalise_language
from dreamjob.documents.templates import (
    DEFAULT_TEMPLATE,
    CvContact,
    CvDocument,
    CvEducation,
    CvEntry,
    CvExperience,
    CvLanguage,
    CvTarget,
    available,
    render,
)
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.enrichment import load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "tailored_cv"
MAX_SKILLS = 16
MAX_BULLETS = 4
MAX_BULLET_CHARS = 320
MAX_SUMMARY_CHARS = 1200
MAX_HEADLINE_CHARS = 120

_BULLET_PREFIX = re.compile(r"^\s*[-*•–—▪·]+\s*")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ0-9])")


# ---------------------------------------------------------------------------
# FR-106: do-not-disclose
# ---------------------------------------------------------------------------


class Disclosure:
    """Field paths the job seeker marked 'do not disclose' (FR-106).

    Paths are dotted paths into ``profile_version.sections``.  A blocked
    container blocks everything under it, so flagging ``contact`` removes the
    whole contact block rather than only a field literally named ``contact``.
    """

    def __init__(self, paths: set[str] | None = None):
        self.paths = {
            str(p).strip().lower().lstrip("/").replace("/", ".").removeprefix("sections.")
            for p in (paths or set())
            if str(p).strip()
        }

    def blocked(self, path: str) -> bool:
        candidate = path.lower()
        return any(
            candidate == blocked
            or candidate.startswith(f"{blocked}.")
            or candidate.startswith(f"{blocked}[")
            for blocked in self.paths
        )

    def photo_blocked(self) -> bool:
        return any(
            self.blocked(key)
            for key in ("photo", "photo_path", "contact.photo", "profile.photo", "picture")
        )

    def filter_text(self, path: str, value: Any) -> str | None:
        if value is None or self.blocked(path):
            return None
        text = str(value).strip()
        return text or None


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class CvResult:
    document: CvDocument
    template: str
    docx_path: str
    pdf_path: str
    tailored_by_llm: bool = False
    notes: list[str] = field(default_factory=list)
    dropped_positions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "cv_docx_path": self.docx_path,
            "cv_pdf_path": self.pdf_path,
            "tailored_by_llm": self.tailored_by_llm,
            "notes": self.notes,
            "dropped_positions": self.dropped_positions,
        }


def templates() -> list[dict[str, str]]:
    """The configurable template set offered in the UI (FR-322)."""
    return [
        {"key": t.key, "display_name": t.display_name, "description": t.description}
        for t in available()
    ]


# ---------------------------------------------------------------------------
# Step 1: the base document, straight from the profile
# ---------------------------------------------------------------------------


def build_base_document(
    inputs: dict[str, Any],
    *,
    language: str,
    accent: str = "#1f5f8b",
    meta: DocumentMeta | None = None,
) -> CvDocument:
    """The profile, filtered for FR-106 and shaped for a template.  No model."""
    language = normalise_language(language)
    disclosure = Disclosure(set(inputs.get("do_not_disclose") or set()))
    version = inputs.get("profile_version") or {}
    sections: dict[str, Any] = version.get("sections") or {}
    seeker = inputs.get("seeker") or {}
    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}

    document = CvDocument(
        language=language,
        contact=_contact(sections, seeker, disclosure),
        summary=_summary(sections, inputs, disclosure),
        experience=_experience(sections, disclosure, language),
        education=_education(sections, disclosure),
        skills=_skills(sections, inputs.get("skills") or [], disclosure),
        languages=_languages(sections, disclosure),
        photo_path=_photo(version, disclosure),
        accent=accent,
        target=CvTarget(
            company_name=str(company.get("name") or opportunity.get("company_name") or ""),
            role_title=str(opportunity.get("title") or ""),
            speculative=str(opportunity.get("kind") or "") == "speculative",
        ),
        meta=meta
        or meta_from_inputs(
            inputs,
            title="Curriculum vitae",
            subtitle=str(opportunity.get("title") or ""),
            language=language,
            generated_at=utcnow(),
            seeker_only=False,
        ),
    )
    for key in ("certifications", "publications", "projects", "honors", "volunteering",
                "courses"):
        setattr(document, key, _entries(sections, key, disclosure))
    return document


def _website_urls(entries: Any) -> list[str]:
    """The address of each site, as the header line prints it.

    Every producer of ``contact.websites`` writes ``{"url": ..., "label": ...}``:
    ``linkedin_pdf``, ``cv_parser`` and the ``profile_intake`` merge all read
    ``w["url"]``.  Taking ``str(w)`` of one of those puts a Python dict
    repr - ``{'url': '...', 'label': 'LinkedIn'}`` - in the contact line of the
    CV that gets attached and sent, which is what it did before this.  A bare
    string is still accepted, because an edited profile may hold one.
    """
    out: list[str] = []
    for entry in entries or []:
        url = str(entry.get("url") or "") if isinstance(entry, Mapping) else str(entry)
        url = url.strip()
        if url and url not in out:
            out.append(url)
    return out


def _contact(sections: dict, seeker: dict, disclosure: Disclosure) -> CvContact:
    block = sections.get("contact") or {}
    if disclosure.blocked("contact"):
        return CvContact(name=str(seeker.get("display_name") or ""))
    websites = (
        [] if disclosure.blocked("contact.websites") else _website_urls(block.get("websites"))
    )
    return CvContact(
        name=disclosure.filter_text("contact.name", block.get("name"))
        or str(seeker.get("display_name") or ""),
        headline=disclosure.filter_text("contact.headline", block.get("headline")) or "",
        email=disclosure.filter_text("contact.email", block.get("email"))
        or (None if disclosure.blocked("contact.email") else seeker.get("email")),
        phone=disclosure.filter_text("contact.phone", block.get("phone")),
        location=disclosure.filter_text("contact.location", block.get("location")),
        linkedin_url=disclosure.filter_text("contact.linkedin_url", block.get("linkedin_url")),
        websites=websites,
    )


def _summary(sections: dict, inputs: dict, disclosure: Disclosure) -> str | None:
    if not disclosure.blocked("summary"):
        text = str(sections.get("summary") or "").strip()
        if text:
            return text[:MAX_SUMMARY_CHARS]
    # The composite narrative is the job seeker's own, reviewed text (FR-125).
    narrative = (inputs.get("composite") or {}).get("narrative")
    return str(narrative).strip()[:MAX_SUMMARY_CHARS] if narrative else None


def split_bullets(text: str | None, limit: int = MAX_BULLETS) -> list[str]:
    """Turn a free-text position description into bullets without adding words."""
    if not text:
        return []
    lines = [
        _BULLET_PREFIX.sub("", line).strip()
        for line in str(text).splitlines()
        if line.strip()
    ]
    if len(lines) <= 1:
        lines = [s.strip() for s in _SENTENCE_SPLIT.split(" ".join(lines)) if s.strip()]
    return [line[:MAX_BULLET_CHARS] for line in lines if len(line) > 2][:limit]


def _experience(sections: dict, disclosure: Disclosure, language: str) -> list[CvExperience]:
    if disclosure.blocked("experience"):
        return []
    out: list[CvExperience] = []
    for index, entry in enumerate(sections.get("experience") or []):
        base = f"experience.{index}"
        if disclosure.blocked(base):
            continue
        company = disclosure.filter_text(f"{base}.company", entry.get("company")) or ""
        title = disclosure.filter_text(f"{base}.title", entry.get("title")) or ""
        if not company and not title:
            continue
        out.append(
            CvExperience(
                company=company,
                title=title,
                start=disclosure.filter_text(f"{base}.start", entry.get("start")),
                end=disclosure.filter_text(f"{base}.end", entry.get("end")),
                current=bool(entry.get("current")),
                location=disclosure.filter_text(f"{base}.location", entry.get("location")),
                bullets=split_bullets(
                    disclosure.filter_text(f"{base}.description", entry.get("description"))
                ),
            )
        )
    return out


def _education(sections: dict, disclosure: Disclosure) -> list[CvEducation]:
    if disclosure.blocked("education"):
        return []
    out: list[CvEducation] = []
    for index, entry in enumerate(sections.get("education") or []):
        base = f"education.{index}"
        if disclosure.blocked(base):
            continue
        school = disclosure.filter_text(f"{base}.school", entry.get("school"))
        if not school:
            continue
        out.append(
            CvEducation(
                school=school,
                degree=disclosure.filter_text(f"{base}.degree", entry.get("degree")),
                field_of_study=disclosure.filter_text(f"{base}.field", entry.get("field")),
                start_year=disclosure.filter_text(f"{base}.start_year", entry.get("start_year")),
                end_year=disclosure.filter_text(f"{base}.end_year", entry.get("end_year")),
                detail=disclosure.filter_text(f"{base}.description", entry.get("description")),
            )
        )
    return out


def _skills(sections: dict, skill_rows: list[dict], disclosure: Disclosure) -> list[str]:
    labels: list[str] = []
    if not disclosure.blocked("top_skills"):
        labels += [str(s).strip() for s in (sections.get("top_skills") or []) if str(s).strip()]
    # FR-107 normalised skills, strongest first (the repository already orders).
    labels += [
        str(row.get("normalised_label") or row.get("raw_label") or "").strip()
        for row in skill_rows
    ]
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        key = label.lower()
        if label and key not in seen:
            seen.add(key)
            out.append(label)
    return out[:MAX_SKILLS]


def _languages(sections: dict, disclosure: Disclosure) -> list[CvLanguage]:
    if disclosure.blocked("languages"):
        return []
    out: list[CvLanguage] = []
    for entry in sections.get("languages") or []:
        if isinstance(entry, str):
            out.append(CvLanguage(entry.strip()))
        elif isinstance(entry, dict):
            name = str(entry.get("language") or entry.get("name") or "").strip()
            if name:
                out.append(CvLanguage(name, str(entry.get("level") or "").strip() or None))
    return out


def _entries(sections: dict, key: str, disclosure: Disclosure) -> list[CvEntry]:
    if disclosure.blocked(key):
        return []
    out: list[CvEntry] = []
    for index, entry in enumerate(sections.get(key) or []):
        base = f"{key}.{index}"
        if disclosure.blocked(base):
            continue
        if isinstance(entry, str):
            title = entry.strip()
            detail, date = None, None
        else:
            title = str(entry.get("title") or "").strip()
            detail = disclosure.filter_text(f"{base}.detail", entry.get("detail"))
            date = entry.get("start") or entry.get("end") or entry.get("date")
        if title:
            out.append(CvEntry(title=title, detail=detail, date=str(date) if date else None))
    return out


def _photo(version: dict, disclosure: Disclosure) -> str | None:
    """FR-106: the photograph is one of the fields most often suppressed."""
    if disclosure.photo_blocked():
        return None
    path = version.get("photo_path")
    if not path:
        return None
    return str(path) if Path(str(path)).is_file() else None


# ---------------------------------------------------------------------------
# Step 2: tailoring (FR-321, NFR-205)
# ---------------------------------------------------------------------------


def _payload(document: CvDocument) -> dict[str, Any]:
    """What the model is allowed to see: the base document, nothing more."""
    return {
        "headline": document.contact.headline,
        "summary": document.summary,
        "experience": [
            {
                "index": index,
                "company": job.company,
                "title": job.title,
                "period": job.period(document.language),
                "location": job.location,
                "description": job.bullets,
            }
            for index, job in enumerate(document.experience)
        ],
        "education": [
            {"school": e.school, "degree": e.line(), "period": e.period(document.language)}
            for e in document.education
        ],
        "skills": document.skills,
        "languages": [entry.line() for entry in document.languages],
        "certifications": [e.title for e in document.certifications],
        "publications": [e.title for e in document.publications],
        "projects": [e.title for e in document.projects],
    }


def tailor(
    document: CvDocument,
    inputs: dict[str, Any],
    llm: LLMClient,
    *,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Ask the model for a selection and a rephrasing.  Raises on LLM failure."""
    import json  # noqa: PLC0415 - only needed on the LLM path

    opportunity = inputs.get("opportunity") or {}
    vacancy = inputs.get("vacancy") or {}
    company = inputs.get("company") or {}
    speculative = str(opportunity.get("kind") or "") == "speculative"

    prompt = load_prompt(PROMPT_NAME)
    system, user = prompt.render(
        language=document.language,
        role_title=opportunity.get("title") or "",
        company_name=company.get("name") or opportunity.get("company_name") or "",
        speculative_note=(
            ", a speculative opening that has not been advertised" if speculative else ""
        ),
        profile_json=json.dumps(_payload(document), ensure_ascii=False, indent=1),
    )
    if instructions:
        user += (
            "\n\nThe job seeker asked for this revision, and it takes precedence over "
            "style guidance above but never over the grounding rules:\n"
            + str(instructions)[:2000]
        )

    opening_text = "\n\n".join(
        str(part)
        for part in (
            opportunity.get("title"),
            opportunity.get("description") or vacancy.get("description"),
            opportunity.get("speculative_rationale"),
            _join(vacancy.get("required_skills")),
            _join(vacancy.get("desirable_skills")),
        )
        if part
    )
    return complete_json(
        llm,
        "generate.cv",
        system,
        user,
        untrusted={"opportunity": opening_text},
        entity_type="opportunity",
        entity_id=opportunity.get("id"),
        prompt_template=prompt.name,
        prompt_version=prompt.version,
        schema_hint=(
            '{"headline": str, "summary": str, "experience": '
            '[{"index": int, "include": bool, "bullets": [str]}], '
            '"skills": [str], "notes": [str]}'
        ),
    )


def _join(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return str(value or "")


# ---------------------------------------------------------------------------
# Step 3: apply, field by field
# ---------------------------------------------------------------------------


def apply_tailoring(
    document: CvDocument, tailoring: dict[str, Any]
) -> tuple[CvDocument, list[str], list[str]]:
    """Merge a model response into the base document.  Facts are never taken."""
    if not isinstance(tailoring, Mapping):
        # A model that answers with a bare list - the JSON is valid, the shape
        # is not - used to raise AttributeError here, which is not in the tuple
        # :func:`generate_cv` degrades on, so one bad answer failed the whole
        # package instead of producing the untailored CV (NFR-104).
        raise ValueError(
            f"the tailoring response is a {type(tailoring).__name__}, not an object"
        )
    notes = [str(n).strip() for n in (tailoring.get("notes") or []) if str(n).strip()]
    dropped: list[str] = []

    headline = str(tailoring.get("headline") or "").strip()
    if headline:
        document.contact.headline = headline[:MAX_HEADLINE_CHARS]
    summary = str(tailoring.get("summary") or "").strip()
    if summary:
        document.summary = summary[:MAX_SUMMARY_CHARS]

    by_index: dict[int, dict] = {}
    for entry in tailoring.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(document.experience):
            by_index[index] = entry
        else:
            notes.append(f"Ignored a tailored position with no profile equivalent (index {index}).")

    kept: list[CvExperience] = []
    for index, job in enumerate(document.experience):
        entry = by_index.get(index)
        if entry is None:
            kept.append(job)
            continue
        if entry.get("include") is False and index >= 3:
            dropped.append(f"{job.title} - {job.company}".strip(" -"))
            continue
        bullets = [
            _BULLET_PREFIX.sub("", str(b)).strip()[:MAX_BULLET_CHARS]
            for b in (entry.get("bullets") or [])
            if str(b).strip()
        ]
        job.bullets = bullets[:MAX_BULLETS] or job.bullets
        kept.append(job)
    document.experience = kept

    # A skill the model returns must already be in the profile (FR-107, CR-405).
    profile_skills = {s.lower(): s for s in document.skills}
    ordered: list[str] = []
    for skill in tailoring.get("skills") or []:
        match = profile_skills.get(str(skill).strip().lower())
        if match and match not in ordered:
            ordered.append(match)
        elif str(skill).strip():
            notes.append(f"Dropped '{str(skill).strip()}': not a skill the profile lists.")
    if ordered:
        document.skills = ordered[:MAX_SKILLS]

    return document, notes, dropped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_cv(
    inputs: dict[str, Any],
    *,
    output_dir: Path | str,
    language: str | None = None,
    template: str | None = None,
    llm: LLMClient | None = None,
    instructions: str | None = None,
    basename: str = "cv",
    meta: DocumentMeta | None = None,
) -> CvResult:
    """Produce the tailored CV in DOCX and PDF (FR-321(a), FR-322)."""
    opportunity = inputs.get("opportunity") or {}
    seeker = inputs.get("seeker") or {}
    content_language = normalise_language(
        language or opportunity.get("language") or seeker.get("locale")
    )
    document = build_base_document(inputs, language=content_language, meta=meta)

    notes: list[str] = []
    dropped: list[str] = []
    tailored = False
    if llm is not None:
        try:
            document, notes, dropped = apply_tailoring(
                document, tailor(document, inputs, llm, instructions=instructions)
            )
            tailored = True
        except BudgetExhausted:
            # NFR-104: the untailored CV is a worse CV, not a failed run.
            notes.append("Token budget exhausted; the CV is the profile without tailoring.")
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            log.warning("CV tailoring failed for opportunity %s: %s", opportunity.get("id"), exc)
            notes.append(f"Tailoring unavailable ({exc.__class__.__name__}); untailored CV.")

    directory = Path(output_dir)
    template_key = template or DEFAULT_TEMPLATE
    rendered = render(
        document,
        template_key,
        directory / f"{basename}.docx",
        directory / f"{basename}.pdf",
    )
    return CvResult(
        document=document,
        template=rendered["template"],
        docx_path=rendered["docx_path"],
        pdf_path=rendered["pdf_path"],
        tailored_by_llm=tailored,
        notes=notes,
        dropped_positions=dropped,
    )
