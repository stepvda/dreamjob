"""CV template contract and registry (FR-322).

FR-322 asks for a *configurable set* of CV templates.  What makes the set
configurable rather than hard-coded is this package: the data model below is
the whole contract between the generator and a template, and templates are
discovered from the package directory at call time.  Dropping a
``cv_<name>.py`` next to ``cv_classic.py`` that exports ``TEMPLATE_KEY`` and
the two render functions adds a third template without a line changing in
``cv_generator.py``.

The data model carries *facts already filtered* by the generator: the
do-not-disclose paths (FR-106) are applied while it is assembled, so a
template never has to know about them and cannot re-introduce a suppressed
field.  Equally, nothing here is free-form model output beyond ``summary`` and
the experience ``bullets``; employers, titles and dates are copied from the
profile, which is what the consistency check then verifies (CR-405, RK-03).
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from types import ModuleType
from typing import Any

from dreamjob.documents.pdf_builder import DocumentMeta, label, normalise_language

DEFAULT_TEMPLATE = "classic"

#: Month abbreviations per content language, so a date range reads naturally
#: in the language of the opportunity rather than as a bare number.
MONTH_ABBREVIATIONS: dict[str, tuple[str, ...]] = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "nl": ("jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"),
    "fr": ("jan", "fév", "mar", "avr", "mai", "juin", "juil", "août", "sep", "oct", "nov", "déc"),
    "de": ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"),
}


def format_period(
    start: str | None, end: str | None, current: bool, language: str = "en"
) -> str:
    """``("2019-05", None, True)`` -> ``"May 2019 - present"``."""
    lang = normalise_language(language)

    def one(token: str | None) -> str:
        if not token:
            return ""
        parts = str(token).split("-")
        year = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            month = int(parts[1])
            if 1 <= month <= 12:
                return f"{MONTH_ABBREVIATIONS[lang][month - 1]} {year}"
        return year

    left = one(start)
    right = label(lang, "cv_present") if current else one(end)
    if left and right:
        return f"{left} - {right}"
    return left or right


# ---------------------------------------------------------------------------
# The data model a template renders
# ---------------------------------------------------------------------------


@dataclass
class CvContact:
    name: str = ""
    headline: str = ""
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    websites: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [v for v in (self.location, self.email, self.phone) if v]
        if self.linkedin_url:
            out.append(self.linkedin_url)
        out.extend(self.websites)
        return out


@dataclass
class CvExperience:
    company: str
    title: str
    start: str | None = None
    end: str | None = None
    current: bool = False
    location: str | None = None
    bullets: list[str] = field(default_factory=list)

    def period(self, language: str = "en") -> str:
        return format_period(self.start, self.end, self.current, language)


@dataclass
class CvEducation:
    school: str
    degree: str | None = None
    field_of_study: str | None = None
    start_year: str | None = None
    end_year: str | None = None
    detail: str | None = None

    def period(self, language: str = "en") -> str:
        return format_period(self.start_year, self.end_year, False, language)

    def line(self) -> str:
        parts = [p for p in (self.degree, self.field_of_study) if p]
        return " - ".join(parts)


@dataclass
class CvEntry:
    """Certifications, publications, projects, honours, volunteering, courses."""

    title: str
    detail: str | None = None
    date: str | None = None


@dataclass
class CvLanguage:
    language: str
    level: str | None = None

    def line(self) -> str:
        return f"{self.language} - {self.level}" if self.level else self.language


@dataclass
class CvTarget:
    """What the CV was tailored to; used for file naming and the PDF metadata."""

    company_name: str = ""
    role_title: str = ""
    speculative: bool = False


#: Section keys in the order a template renders them, mapped to their label key.
SECTION_LABELS: dict[str, str] = {
    "summary": "cv_profile",
    "experience": "cv_experience",
    "education": "cv_education",
    "skills": "cv_skills",
    "languages": "cv_languages",
    "certifications": "cv_certifications",
    "publications": "cv_publications",
    "projects": "cv_projects",
    "honors": "cv_honors",
    "volunteering": "cv_volunteering",
    "courses": "cv_courses",
}


@dataclass
class CvDocument:
    """Everything a template may put on the page - and nothing else."""

    language: str = "en"
    contact: CvContact = field(default_factory=CvContact)
    summary: str | None = None
    experience: list[CvExperience] = field(default_factory=list)
    education: list[CvEducation] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[CvLanguage] = field(default_factory=list)
    certifications: list[CvEntry] = field(default_factory=list)
    publications: list[CvEntry] = field(default_factory=list)
    projects: list[CvEntry] = field(default_factory=list)
    honors: list[CvEntry] = field(default_factory=list)
    volunteering: list[CvEntry] = field(default_factory=list)
    courses: list[CvEntry] = field(default_factory=list)
    photo_path: str | None = None
    accent: str = "#1f5f8b"
    target: CvTarget = field(default_factory=CvTarget)
    meta: DocumentMeta = field(default_factory=lambda: DocumentMeta(title="Curriculum vitae"))

    def label(self, key: str) -> str:
        return label(self.language, key)

    def section_label(self, section: str) -> str:
        return label(self.language, SECTION_LABELS.get(section, section))

    def entry_sections(self) -> list[tuple[str, list[CvEntry]]]:
        return [
            (key, getattr(self, key))
            for key in ("certifications", "publications", "projects", "honors",
                        "volunteering", "courses")
            if getattr(self, key)
        ]

    def has_photo(self) -> bool:
        return bool(self.photo_path) and Path(self.photo_path).is_file()

    # -- consistency input (FR-322, NFR-206) --------------------------------
    def free_text(self) -> list[tuple[str, str]]:
        """``(locus, text)`` pairs of everything a model may have written."""
        out: list[tuple[str, str]] = []
        if self.summary:
            out.append(("summary", self.summary))
        for index, job in enumerate(self.experience):
            for order, bullet in enumerate(job.bullets):
                out.append((f"experience[{index}].bullets[{order}]", bullet))
        if self.contact.headline:
            out.append(("contact.headline", self.contact.headline))
        return out

    def plain_text(self) -> str:
        """The whole document as text, for the leakage scan (NFR-206)."""
        chunks: list[str] = [self.contact.name, self.contact.headline]
        chunks.extend(self.contact.lines())
        chunks.append(self.summary or "")
        for job in self.experience:
            chunks += [job.company, job.title, job.location or "", job.period(self.language)]
            chunks += job.bullets
        for study in self.education:
            chunks += [study.school, study.line(), study.detail or ""]
        chunks += self.skills
        chunks += [lang.line() for lang in self.languages]
        for _key, entries in self.entry_sections():
            for entry in entries:
                chunks += [entry.title, entry.detail or "", entry.date or ""]
        return "\n".join(c for c in chunks if c)


# ---------------------------------------------------------------------------
# Serialisation: the document is stored with the package so the consistency
# check can be re-run on exactly what was rendered (FR-322, FR-324).
# ---------------------------------------------------------------------------

_ENTRY_KEYS = ("certifications", "publications", "projects", "honors", "volunteering", "courses")


def to_dict(cv: CvDocument) -> dict[str, Any]:
    return asdict(cv)


def from_dict(data: dict[str, Any]) -> CvDocument:
    """Rebuild a stored document.  Unknown keys are ignored, missing ones default."""
    payload = dict(data or {})
    document = CvDocument(
        language=str(payload.get("language") or "en"),
        contact=CvContact(**_only(payload.get("contact"), CvContact)),
        summary=payload.get("summary"),
        experience=[
            CvExperience(**_only(e, CvExperience)) for e in payload.get("experience") or []
        ],
        education=[CvEducation(**_only(e, CvEducation)) for e in payload.get("education") or []],
        skills=[str(s) for s in payload.get("skills") or []],
        languages=[CvLanguage(**_only(e, CvLanguage)) for e in payload.get("languages") or []],
        photo_path=payload.get("photo_path"),
        accent=str(payload.get("accent") or "#1f5f8b"),
        target=CvTarget(**_only(payload.get("target"), CvTarget)),
        meta=DocumentMeta(**_only(payload.get("meta"), DocumentMeta)),
    )
    for key in _ENTRY_KEYS:
        setattr(document, key, [CvEntry(**_only(e, CvEntry)) for e in payload.get(key) or []])
    return document


def _only(value: Any, cls: type) -> dict[str, Any]:
    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in (value or {}).items() if k in allowed}


# ---------------------------------------------------------------------------
# Registry: discovered from this package, never enumerated by the generator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateInfo:
    key: str
    display_name: str
    description: str
    module: str


def _modules() -> dict[str, ModuleType]:
    found: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if not info.name.startswith("cv_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        key = getattr(module, "TEMPLATE_KEY", None)
        if key and callable(getattr(module, "render_pdf", None)):
            found[str(key)] = module
    return found


def available() -> list[TemplateInfo]:
    """Templates the job seeker may choose from (FR-322)."""
    return sorted(
        (
            TemplateInfo(
                key=key,
                display_name=getattr(module, "DISPLAY_NAME", key.title()),
                description=getattr(module, "DESCRIPTION", ""),
                module=module.__name__,
            )
            for key, module in _modules().items()
        ),
        key=lambda info: (info.key != DEFAULT_TEMPLATE, info.key),
    )


def get_template(key: str | None) -> ModuleType:
    """Resolve a template key, falling back to the default rather than failing."""
    modules = _modules()
    if key and key in modules:
        return modules[key]
    if DEFAULT_TEMPLATE in modules:
        return modules[DEFAULT_TEMPLATE]
    if modules:
        return next(iter(modules.values()))
    raise LookupError("No CV template modules found in dreamjob.documents.templates")


def render(
    cv: CvDocument, template_key: str | None, docx_path: Path, pdf_path: Path
) -> dict[str, Any]:
    """Render both formats FR-322 requires and report which template did it."""
    module = get_template(template_key)
    docx = module.render_docx(cv, docx_path)
    pdf = module.render_pdf(cv, pdf_path)
    return {
        "template": module.TEMPLATE_KEY,
        "docx_path": str(docx),
        "pdf_path": str(pdf),
    }
