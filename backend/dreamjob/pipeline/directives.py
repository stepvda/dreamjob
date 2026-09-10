"""Structured job-search directives (FR-141..149, FR-385, NFR-501).

Directives bound a campaign, and every source adapter has to be able to turn
them into a native query (FR-162).  That only works if they are *structured*:
drop-downs, multi-select chips, auto-complete fields and sliders, with exactly
one free-text field - ``notes_to_ai`` (FR-141).  This module is the single
definition of that structure:

* the Pydantic models for the five directive groups, persisted as JSON in the
  ``directive_set`` columns of the same name;
* the vocabulary behind the controls, in English, Dutch and French (NFR-501),
  and the bundled title/synonym catalogue behind the auto-complete (FR-142);
* the pre-fill proposal from the composite profile and the pre-launch estimate
  of sources and pages (FR-147);
* the discretion-mode exclusion rules (FR-385), which the planner, discovery,
  scoring, contacts and generation slices all call through ``is_excluded`` and
  ``is_excluded_contact``.

Compensation directives (FR-146) are for filtering and scoring only.  They
carry an explicit ``disclose_in_content`` flag, defaulting to ``False``, and
every generator must check it before a salary expectation can appear in a
letter, a CV or an e-mail.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from dreamjob.db.connection import from_json
from dreamjob.pipeline.geocode import commute_minutes, geocode, haversine_km, max_radius_km

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
LOCALES = ("en", "nl", "fr")          # NFR-501
DEFAULT_LOCALE = "en"


# ---------------------------------------------------------------------------
# Vocabulary (FR-142..146).  Values are the strings persisted in JSON.
# ---------------------------------------------------------------------------


class SizeBand(StrEnum):
    """FR-143 headcount bands."""

    LT10 = "lt10"
    B10_50 = "b10_50"
    B50_250 = "b50_250"
    B250_1000 = "b250_1000"
    GT1000 = "gt1000"


SIZE_BAND_RANGES: dict[SizeBand, tuple[int, int | None]] = {
    SizeBand.LT10: (0, 9),
    SizeBand.B10_50: (10, 50),
    SizeBand.B50_250: (51, 250),
    SizeBand.B250_1000: (251, 1000),
    SizeBand.GT1000: (1001, None),
}


class Stage(StrEnum):
    STARTUP = "startup"
    SCALEUP = "scaleup"
    ESTABLISHED = "established"
    LISTED = "listed"
    PUBLIC_SECTOR = "public_sector"
    NONPROFIT = "nonprofit"


class Trajectory(StrEnum):
    GROWING = "growing"
    STABLE = "stable"
    DECLINING = "declining"
    RESTRUCTURING = "restructuring"


class Ownership(StrEnum):
    FOUNDER_LED = "founder_led"
    FAMILY_OWNED = "family_owned"
    PE_BACKED = "pe_backed"
    VC_BACKED = "vc_backed"
    SUBSIDIARY = "subsidiary"
    COOPERATIVE = "cooperative"
    STATE_OWNED = "state_owned"


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MEDIOR = "medior"
    SENIOR = "senior"
    LEAD = "lead"
    PRINCIPAL = "principal"
    MANAGER = "manager"
    DIRECTOR = "director"
    VP = "vp"
    C_LEVEL = "c_level"
    BOARD = "board"


# Individual-contributor and management tracks share ranks where they are
# genuinely comparable, so a "senior to director" range means what it says.
SENIORITY_RANK: dict[Seniority, int] = {
    Seniority.INTERN: 0,
    Seniority.JUNIOR: 1,
    Seniority.MEDIOR: 2,
    Seniority.SENIOR: 3,
    Seniority.LEAD: 4,
    Seniority.PRINCIPAL: 5,
    Seniority.MANAGER: 5,
    Seniority.DIRECTOR: 6,
    Seniority.VP: 7,
    Seniority.C_LEVEL: 8,
    Seniority.BOARD: 9,
}


class ManagementScope(StrEnum):
    INDIVIDUAL_CONTRIBUTOR = "individual_contributor"
    TEAM_LEAD = "team_lead"
    MANAGER_OF_MANAGERS = "manager_of_managers"
    DEPARTMENT_HEAD = "department_head"
    EXECUTIVE = "executive"


class WorkArrangement(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"


class ContractType(StrEnum):
    PERMANENT = "permanent"
    FIXED_TERM = "fixed_term"
    FREELANCE = "freelance"
    INTERIM = "interim"


class TravelTolerance(StrEnum):
    NONE = "none"
    OCCASIONAL = "occasional"
    REGULAR = "regular"
    FREQUENT = "frequent"
    EXTENSIVE = "extensive"


TRAVEL_MAX_PERCENT: dict[TravelTolerance, int] = {
    TravelTolerance.NONE: 0,
    TravelTolerance.OCCASIONAL: 10,
    TravelTolerance.REGULAR: 25,
    TravelTolerance.FREQUENT: 50,
    TravelTolerance.EXTENSIVE: 100,
}


class CommuteMode(StrEnum):
    WALK = "walk"
    BIKE = "bike"
    EBIKE = "ebike"
    PUBLIC_TRANSPORT = "public_transport"
    CAR = "car"
    MOTORCYCLE = "motorcycle"


class PayComponentTreatment(StrEnum):
    """How equity and variable pay count towards the minimum package (FR-146)."""

    IGNORE = "ignore"
    PARTIAL = "partial"
    FULL = "full"
    REQUIRED = "required"


class CompensationPeriod(StrEnum):
    ANNUAL = "annual"
    MONTHLY = "monthly"
    DAILY = "daily"


class CompanyExclusionReason(StrEnum):
    CURRENT_EMPLOYER = "current_employer"
    GROUP_ENTITY = "group_entity"
    FLAGGED_SENSITIVE = "flagged_sensitive"
    FORMER_EMPLOYER = "former_employer"
    USER_EXCLUDED = "user_excluded"
    OTHER = "other"


class ContactExclusionReason(StrEnum):
    CURRENT_COLLEAGUE = "current_colleague"
    EMPLOYER_RECRUITER = "employer_recruiter"
    EXCLUDED_COMPANY = "excluded_company"
    FLAGGED_SENSITIVE = "flagged_sensitive"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Directive groups
# ---------------------------------------------------------------------------


class _Group(BaseModel):
    model_config = ConfigDict(extra="ignore")


class JobContentDirectives(_Group):
    """FR-142: what the job itself has to be about."""

    target_titles: list[str] = Field(default_factory=list)
    # Synonyms accepted by the job seeker from the auto-complete, kept apart so
    # the planner can widen a query without inventing titles (FR-162).
    title_synonyms: list[str] = Field(default_factory=list)
    function_families: list[str] = Field(default_factory=list)
    seniority_min: Seniority | None = None
    seniority_max: Seniority | None = None
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    industries_include: list[str] = Field(default_factory=list)
    industries_exclude: list[str] = Field(default_factory=list)
    management_scope: ManagementScope | None = None
    min_direct_reports: int | None = Field(default=None, ge=0, le=10_000)
    keywords_to_avoid: list[str] = Field(default_factory=list)

    def all_titles(self) -> list[str]:
        """Target titles plus accepted synonyms, de-duplicated, order kept."""
        return list(dict.fromkeys([*self.target_titles, *self.title_synonyms]))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def titles(self) -> list[str]:
        """The query set: what a source should actually be searched for (FR-162).

        Serialised alongside ``target_titles`` under the conventional key that
        generic directive readers look for, so a source adapter or the planner
        finds the searchable titles without knowing this module.
        """
        return self.all_titles()

    def seniority_range(self) -> tuple[int, int]:
        low = SENIORITY_RANK[self.seniority_min] if self.seniority_min else 0
        high = SENIORITY_RANK[self.seniority_max] if self.seniority_max else 9
        return (low, high) if low <= high else (high, low)


class CompanyReference(_Group):
    """A company named in a directive list, identified as loosely as the user knows it."""

    name: str
    domain: str | None = None
    legal_id: str | None = None
    note: str | None = None


class ExcludedCompany(CompanyReference):
    """FR-385: a company the campaign must never touch."""

    reason: CompanyExclusionReason = CompanyExclusionReason.FLAGGED_SENSITIVE
    # Group entities are caught by name-prefix and shared-domain relations; a
    # user can switch that off for a company whose name is too generic.
    match_group_entities: bool = True


class ExcludedContact(_Group):
    """FR-385: a person whose involvement would expose the search."""

    full_name: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None
    reason: ContactExclusionReason = ContactExclusionReason.OTHER
    note: str | None = None


class CompanyTypeDirectives(_Group):
    """FR-143: what kind of employer."""

    size_bands: list[SizeBand] = Field(default_factory=list)
    stages: list[Stage] = Field(default_factory=list)
    trajectories: list[Trajectory] = Field(default_factory=list)
    ownerships: list[Ownership] = Field(default_factory=list)
    include_companies: list[CompanyReference] = Field(default_factory=list)
    exclude_companies: list[CompanyReference] = Field(default_factory=list)


class LocationArea(_Group):
    """A point with a radius (FR-144).  Coordinates come from the geocoder."""

    label: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float = Field(default=25.0, ge=0, le=2000)
    country_code: str | None = None
    geocoded_at: str | None = None
    place_type: str | None = None

    @property
    def is_geocoded(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class LocationDirectives(_Group):
    """FR-144: where the job may be."""

    areas: list[LocationArea] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)      # ISO-3166-1 alpha-2
    regions: list[str] = Field(default_factory=list)        # free labels, e.g. "Flanders"
    willing_to_relocate: bool = False
    relocation_countries: list[str] = Field(default_factory=list)
    home_location: LocationArea | None = None
    max_commute_minutes: int | None = Field(default=None, ge=0, le=600)
    commute_mode: CommuteMode = CommuteMode.CAR

    def effective_radius_km(self) -> float | None:
        """Radius implied by the commute tolerance, for sources with radius search."""
        if self.max_commute_minutes is None:
            return None
        return max_radius_km(self.max_commute_minutes, self.commute_mode.value)


class WorkArrangementDirectives(_Group):
    """FR-145: how the work is organised."""

    arrangements: list[WorkArrangement] = Field(default_factory=list)
    min_remote_days: int | None = Field(default=None, ge=0, le=7)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    fte_percentage_min: int | None = Field(default=None, ge=1, le=100)
    fte_percentage_max: int | None = Field(default=None, ge=1, le=100)
    contract_types: list[ContractType] = Field(default_factory=list)
    travel_tolerance: TravelTolerance = TravelTolerance.OCCASIONAL
    max_travel_percent: int | None = Field(default=None, ge=0, le=100)

    def travel_ceiling_percent(self) -> int:
        if self.max_travel_percent is not None:
            return self.max_travel_percent
        return TRAVEL_MAX_PERCENT[self.travel_tolerance]


class CompensationDirectives(_Group):
    """FR-146: filtering and scoring only - never disclosed unless opted in.

    ``disclose_in_content`` is the opt-in.  Content generators must read it and
    leave every figure out of CVs, letters and e-mails while it is ``False``.
    """

    minimum_package: float | None = Field(default=None, ge=0)
    currency: str = "EUR"
    period: CompensationPeriod = CompensationPeriod.ANNUAL
    equity_treatment: PayComponentTreatment = PayComponentTreatment.IGNORE
    variable_treatment: PayComponentTreatment = PayComponentTreatment.PARTIAL
    variable_share_counted: float = Field(default=0.5, ge=0, le=1)
    benefits_must_have: list[str] = Field(default_factory=list)
    disclose_in_content: bool = False


class DirectiveSetPayload(_Group):
    """Everything a job seeker can set (FR-141).  The write model of the API."""

    name: str = Field(default="Untitled directives", min_length=1, max_length=120)
    persona_id: str | None = None
    job_content: JobContentDirectives = Field(default_factory=JobContentDirectives)
    company_type: CompanyTypeDirectives = Field(default_factory=CompanyTypeDirectives)
    location: LocationDirectives = Field(default_factory=LocationDirectives)
    work_arrangement: WorkArrangementDirectives = Field(
        default_factory=WorkArrangementDirectives
    )
    compensation: CompensationDirectives = Field(default_factory=CompensationDirectives)
    # FR-141: the one and only free-text field.
    notes_to_ai: str | None = Field(default=None, max_length=4000)
    spontaneous_only: bool = False          # FR-149
    discretion_mode: bool = False           # FR-385
    discretion_excluded_companies: list[ExcludedCompany] = Field(default_factory=list)
    discretion_excluded_contacts: list[ExcludedContact] = Field(default_factory=list)


class DirectiveSet(DirectiveSetPayload):
    """A stored, named, versioned directive set (FR-148)."""

    id: str | None = None
    job_seeker_id: str | None = None
    version: int = 1
    created_at: str | None = None


# What every cross-slice helper accepts: a model, a plain dict, or a row.
DirectiveSetLike = DirectiveSet | DirectiveSetPayload | Mapping[str, Any]

_JSON_COLUMNS = (
    "job_content",
    "company_type",
    "location",
    "work_arrangement",
    "compensation",
    "discretion_excluded_companies",
    "discretion_excluded_contacts",
)


def coerce_directive_set(value: DirectiveSetLike) -> DirectiveSet:
    """Accept a model, a plain dict, or a ``directive_set`` database row.

    Repository rows keep the five groups as JSON text and the flags as
    integers, so every cross-slice helper starts by normalising through here
    and callers never have to care which shape they hold.
    """
    if isinstance(value, DirectiveSet):
        return value
    if isinstance(value, DirectiveSetPayload):
        return DirectiveSet.model_validate(value.model_dump())
    if isinstance(value, Mapping):
        data = dict(value)
        for column in _JSON_COLUMNS:
            if column in data:
                default: Any = [] if column.startswith("discretion_") else {}
                data[column] = from_json(data[column], default) or default
        for flag in ("spontaneous_only", "discretion_mode"):
            if flag in data:
                data[flag] = bool(data[flag])
        return DirectiveSet.model_validate(data)
    raise TypeError(f"Cannot read directives from {type(value).__name__}")


def to_columns(payload: DirectiveSetPayload) -> dict[str, Any]:
    """Directive models -> ``directive_set`` column values (JSON-ready)."""
    data = payload.model_dump(mode="json", exclude_none=False)
    return {
        "name": data["name"],
        "persona_id": data.get("persona_id"),
        "job_content": data["job_content"],
        "company_type": data["company_type"],
        "location": data["location"],
        "work_arrangement": data["work_arrangement"],
        "compensation": data["compensation"],
        "notes_to_ai": data.get("notes_to_ai"),
        "spontaneous_only": 1 if data.get("spontaneous_only") else 0,
        "discretion_mode": 1 if data.get("discretion_mode") else 0,
        "discretion_excluded_companies": data.get("discretion_excluded_companies") or [],
        "discretion_excluded_contacts": data.get("discretion_excluded_contacts") or [],
    }


# ---------------------------------------------------------------------------
# Bundled catalogues: titles and drop-down labels (FR-142, FR-141, NFR-501)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def title_catalogue() -> dict[str, Any]:
    """The bundled title/synonym catalogue behind the auto-complete (FR-142)."""
    return json.loads((DATA_DIR / "job_titles.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def label_catalogue() -> dict[str, Any]:
    """EN/NL/FR labels for every drop-down (FR-141, NFR-501)."""
    return json.loads((DATA_DIR / "directive_labels.json").read_text(encoding="utf-8"))


def _label(labels: Mapping[str, str], locale: str) -> str:
    return labels.get(locale) or labels.get(DEFAULT_LOCALE) or next(iter(labels.values()), "")


# FR-141: the numeric controls are sliders, and their bounds are the field
# constraints declared above.  They are published with the vocabulary so the
# editor cannot offer a value the model would reject.
SLIDER_RANGES: dict[str, dict[str, float | int]] = {
    "radius_km": {"min": 0, "max": 2000, "step": 5, "default": 25},
    "max_commute_minutes": {"min": 0, "max": 600, "step": 5, "default": 45},
    "min_remote_days": {"min": 0, "max": 7, "step": 1, "default": 2},
    "fte_percentage": {"min": 1, "max": 100, "step": 5, "default": 100},
    "min_direct_reports": {"min": 0, "max": 10000, "step": 1, "default": 0},
    "max_travel_percent": {"min": 0, "max": 100, "step": 5, "default": 10},
    "variable_share_counted": {"min": 0, "max": 1, "step": 0.05, "default": 0.5},
}


def vocabulary(locale: str = DEFAULT_LOCALE) -> dict[str, Any]:
    """Every control's options, labelled in ``locale`` (FR-141, NFR-501)."""
    locale = locale if locale in LOCALES else DEFAULT_LOCALE
    labels = label_catalogue()
    groups = {
        group: [{"key": o["key"], "label": _label(o["labels"], locale)} for o in options]
        for group, options in labels["groups"].items()
    }
    groups["function_family"] = [
        {"key": f["key"], "label": _label(f["labels"], locale)}
        for f in title_catalogue()["function_families"]
    ]
    return {
        "locale": locale,
        "locales": list(LOCALES),
        "groups": groups,
        "seniority_rank": {s.value: rank for s, rank in SENIORITY_RANK.items()},
        "size_band_fte": {
            band.value: {"min": low, "max": high} for band, (low, high) in SIZE_BAND_RANGES.items()
        },
        "commute_modes": [m.value for m in CommuteMode],
        "ranges": SLIDER_RANGES,
        "title_catalogue_version": title_catalogue()["version"],
    }


class TitleSuggestion(BaseModel):
    """One auto-complete row (FR-142)."""

    canonical: str
    label: str
    family: str
    seniority: str
    synonyms: list[str] = Field(default_factory=list)
    matched_on: str


def _fold(text: str) -> str:
    """Lowercase, accent-free comparison key."""
    normalised = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in normalised if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", stripped.lower()).strip()


@lru_cache(maxsize=1)
def _title_index() -> list[tuple[dict[str, Any], list[tuple[str, str]]]]:
    """(entry, [(folded variant, original variant)]) for every title."""
    index = []
    for entry in title_catalogue()["titles"]:
        variants = [entry["canonical"], *entry["synonyms"], *entry["labels"].values()]
        seen: dict[str, str] = {}
        for variant in variants:
            folded = _fold(variant)
            if folded and folded not in seen:
                seen[folded] = variant
        index.append((entry, list(seen.items())))
    return index


def suggest_titles(
    prefix: str, *, locale: str = DEFAULT_LOCALE, limit: int = 10, family: str | None = None
) -> list[TitleSuggestion]:
    """Auto-complete job titles with their synonyms (FR-142).

    Ranking is deliberately simple and deterministic: a prefix hit on the
    canonical title beats a prefix hit on a synonym, which beats a word-start
    hit, which beats a substring hit.  With an empty prefix the catalogue is
    returned in file order, which is how the UI shows "popular titles".
    """
    locale = locale if locale in LOCALES else DEFAULT_LOCALE
    needle = _fold(prefix)
    scored: list[tuple[tuple[int, int, str], TitleSuggestion]] = []

    for position, (entry, variants) in enumerate(_title_index()):
        if family and entry["family"] != family:
            continue
        best: tuple[int, str] | None = None
        for folded, original in variants:
            if not needle:
                rank = 4
            elif folded.startswith(needle):
                rank = 0 if original == entry["canonical"] else 1
            elif re.search(rf"\b{re.escape(needle)}", folded):
                rank = 2
            elif needle in folded:
                rank = 3
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, original)
        if best is None:
            continue
        scored.append(
            (
                (best[0], position, entry["canonical"]),
                TitleSuggestion(
                    canonical=entry["canonical"],
                    label=_label(entry["labels"], locale),
                    family=entry["family"],
                    seniority=entry["seniority"],
                    synonyms=list(entry["synonyms"]),
                    matched_on=best[1],
                ),
            )
        )

    scored.sort(key=lambda pair: pair[0])
    return [suggestion for _, suggestion in scored[: max(1, limit)]]


def expand_synonyms(titles: Sequence[str], *, per_title: int = 4) -> list[str]:
    """Widen a title list with catalogue synonyms, for source queries (FR-162)."""
    out: list[str] = []
    for title in titles:
        folded = _fold(title)
        if title not in out:
            out.append(title)
        for _entry, variants in _title_index():
            if any(folded == v[0] for v in variants):
                for _, original in variants[: per_title + 1]:
                    if original not in out:
                        out.append(original)
                break
    return out


def lookup_title(title: str) -> dict[str, Any] | None:
    """Catalogue entry for a title or one of its synonyms, if it is known.

    A canonical title wins over a synonym of another entry, so "Head of Data"
    resolves to itself rather than to the entry that lists it as a synonym.
    """
    folded = _fold(title)
    if not folded:
        return None
    for entry, _variants in _title_index():
        if _fold(entry["canonical"]) == folded:
            return entry
    for entry, variants in _title_index():
        if any(folded == variant for variant, _ in variants):
            return entry
    return None


def size_band_for_fte(fte: int | float | None) -> SizeBand | None:
    """Map a headcount onto an FR-143 band, for filtering collected companies."""
    if fte is None:
        return None
    try:
        count = int(fte)
    except (TypeError, ValueError):
        return None
    for band, (low, high) in SIZE_BAND_RANGES.items():
        if count >= low and (high is None or count <= high):
            return band
    return None


# ---------------------------------------------------------------------------
# FR-385 discretion mode
# ---------------------------------------------------------------------------

# Legal forms and holding words carry no identity: "Acme NV" and "Acme Group"
# are the same company for exclusion purposes.
_LEGAL_SUFFIXES = {
    "nv", "sa", "bv", "bvba", "srl", "sprl", "cv", "cvba", "vzw", "asbl", "comm",
    "va", "gmbh", "ag", "kg", "ug", "ltd", "limited", "plc", "llp", "llc", "inc",
    "incorporated", "corp", "corporation", "co", "company", "sarl", "sas", "sasu",
    "spa", "srls", "ab", "as", "oy", "aps", "bhd", "pty", "holding", "holdings",
    "group", "groep", "groupe", "gruppe", "international", "worldwide", "global",
    "belgium", "belgique", "belgie", "nederland", "netherlands", "france", "europe",
    "benelux", "emea",
}
_MULTIPART_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "co.nz", "co.jp",
    "co.za", "com.br", "com.mx", "co.in",
}


def normalise_company_name(name: str | None) -> str:
    """Comparison key for company names: folded, de-punctuated, suffix-free."""
    tokens = [t for t in _fold(name or "").split() if t]
    core = [t for t in tokens if t not in _LEGAL_SUFFIXES]
    return " ".join(core or tokens)


def registrable_domain(value: str | None) -> str | None:
    """Registrable domain from a URL, an e-mail address or a bare host name."""
    if not value:
        return None
    text = str(value).strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    text = re.sub(r"^[a-z]+://", "", text).split("/")[0].split("?")[0].strip(".")
    if not text or "." not in text:
        return None
    labels = text.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTIPART_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _domain_related(left: str | None, right: str | None) -> bool:
    a, b = registrable_domain(left), registrable_domain(right)
    return bool(a and b and a == b)


def _name_related(left: str, right: str) -> bool:
    """Name-prefix relation, e.g. "Acme" vs "Acme Digital Services" (FR-385).

    The match is on whole tokens, and the shorter name must be distinctive
    enough that the relation means something - a single token of at least four
    characters, or two tokens of any length.
    """
    a, b = normalise_company_name(left), normalise_company_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    short_tokens, long_tokens = short.split(), long.split()
    if len(short_tokens) == 1 and len(short_tokens[0]) < 4:
        return False
    return long_tokens[: len(short_tokens)] == short_tokens


def _company_fields(company: Mapping[str, Any] | str | None) -> tuple[str, str | None, str | None]:
    """(name, domain, legal_id) from a company row, a dict, or a bare name."""
    if company is None:
        return "", None, None
    if isinstance(company, str):
        return company, None, None
    name = str(
        company.get("name")
        or company.get("company_name")
        or company.get("normalised_name")
        or company.get("company_name_raw")
        or ""
    )
    domain = company.get("domain") or company.get("website") or company.get("careers_url")
    return name, (str(domain) if domain else None), (
        str(company["legal_id"]) if company.get("legal_id") else None
    )


def _matches_reference(
    reference: CompanyReference, name: str, domain: str | None, legal_id: str | None
) -> bool:
    if reference.legal_id and legal_id and reference.legal_id.strip() == legal_id.strip():
        return True
    if _domain_related(reference.domain, domain):
        return True
    return normalise_company_name(reference.name) == normalise_company_name(name)


def exclusion_reason(
    directive_set: DirectiveSetLike, company: Mapping[str, Any] | str | None
) -> str | None:
    """Why this company is out of scope, or ``None`` when it is in scope.

    Two rules apply.  The FR-143 exclude list is always in force.  The FR-385
    discretion list applies while ``discretion_mode`` is on, and additionally
    catches group entities of every excluded company by name prefix and by
    shared registrable domain - a subsidiary rarely shares its parent's exact
    name, but it very often shares its domain or leads with the parent's name.

    Returns a ``CompanyExclusionReason`` value, so callers can show *why* a
    company was dropped (FR-163, FR-282).
    """
    directives = coerce_directive_set(directive_set)
    name, domain, legal_id = _company_fields(company)
    if not (name or domain or legal_id):
        return None

    for reference in directives.company_type.exclude_companies:
        if _matches_reference(reference, name, domain, legal_id):
            return CompanyExclusionReason.USER_EXCLUDED.value

    if not directives.discretion_mode:
        return None

    for excluded in directives.discretion_excluded_companies:
        same_legal_id = bool(
            excluded.legal_id and legal_id and excluded.legal_id.strip() == legal_id.strip()
        )
        same_name = bool(
            name and normalise_company_name(excluded.name) == normalise_company_name(name)
        )
        if same_legal_id or same_name:
            return excluded.reason.value
        # A different name on the same domain, or leading with the excluded
        # company's name, is a group entity rather than the company itself.
        if _domain_related(excluded.domain, domain) or (
            excluded.match_group_entities and _name_related(excluded.name, name)
        ):
            return CompanyExclusionReason.GROUP_ENTITY.value
    return None


def is_excluded(directive_set: DirectiveSetLike, company: Mapping[str, Any] | str | None) -> bool:
    """True when a company must not appear anywhere in the campaign (FR-385).

    ``directive_set`` is a ``DirectiveSet``/``DirectiveSetPayload`` model, a
    plain dict, or a ``directive_set`` row straight from the repository.
    ``company`` is a ``company`` row, any mapping carrying ``name`` and
    optionally ``domain``/``legal_id``, or a bare company name.

    The planner must not query it, discovery must drop it, scoring must not
    rank it and generation must never mention it.  Use
    ``exclusion_reason(...)`` when the reason has to be shown to the user.
    """
    return exclusion_reason(directive_set, company) is not None


def is_explicitly_included(
    directive_set: DirectiveSetLike, company: Mapping[str, Any] | str | None
) -> bool:
    """True for a company on the FR-143 include list (exclusion still wins)."""
    directives = coerce_directive_set(directive_set)
    name, domain, legal_id = _company_fields(company)
    return any(
        _matches_reference(reference, name, domain, legal_id)
        for reference in directives.company_type.include_companies
    )


_RECRUITING_ROLE = re.compile(
    r"recruit|talent acquisition|talent partner|human resources|\bhr\b|people (and|&) culture"
    r"|resources humaines|werving|rekruter",
    re.IGNORECASE,
)


def contact_exclusion_reason(
    directive_set: DirectiveSetLike,
    contact: Mapping[str, Any] | None,
    company: Mapping[str, Any] | str | None = None,
) -> str | None:
    """Why this contact must not be approached, or ``None`` (FR-385).

    ``contact`` is a ``contact`` row or any mapping with ``full_name``,
    ``email``, ``linkedin_url`` and optionally ``company_name``/``role_title``.
    ``company`` is the contact's employer when the caller already resolved it;
    otherwise the employer is inferred from the contact's own fields and from
    its e-mail domain.
    """
    if not contact:
        return None
    directives = coerce_directive_set(directive_set)
    if not directives.discretion_mode:
        return None

    email = str(contact.get("email") or "").strip().lower()
    name = str(contact.get("full_name") or contact.get("name") or "")
    linkedin = str(contact.get("linkedin_url") or "").strip().lower().rstrip("/")

    for excluded in directives.discretion_excluded_contacts:
        if excluded.email and email and excluded.email.strip().lower() == email:
            return excluded.reason.value
        if excluded.linkedin_url and linkedin:
            if excluded.linkedin_url.strip().lower().rstrip("/") == linkedin:
                return excluded.reason.value
        if excluded.full_name and name and _fold(excluded.full_name) == _fold(name):
            return excluded.reason.value

    employer: Mapping[str, Any] | str | None = company
    if employer is None:
        employer = {
            "name": contact.get("company_name") or contact.get("company") or "",
            "domain": registrable_domain(email),
        }
    if is_excluded(directives, employer):
        role = f"{contact.get('role_title') or ''} {contact.get('department') or ''}"
        if _RECRUITING_ROLE.search(role):
            return ContactExclusionReason.EMPLOYER_RECRUITER.value
        return ContactExclusionReason.CURRENT_COLLEAGUE.value
    return None


def is_excluded_contact(
    directive_set: DirectiveSetLike,
    contact: Mapping[str, Any] | None,
    company: Mapping[str, Any] | str | None = None,
) -> bool:
    """True when approaching this contact would expose the search (FR-385).

    Contacts slices must call this before enriching or storing a person, and
    generation before addressing anyone.  ``company`` is optional and only
    saves a lookup - the employer is otherwise read from the contact itself.
    """
    return contact_exclusion_reason(directive_set, contact, company) is not None


# ---------------------------------------------------------------------------
# FR-149 spontaneous-application mode
# ---------------------------------------------------------------------------

VACANCY_SOURCE_TYPES = frozenset({"job_board", "ats"})
ALL_SOURCE_TYPES = frozenset(
    {
        "job_board", "ats", "directory", "registry", "website", "linkedin",
        "compensation", "news", "events",
    }
)


def skips_vacancy_sources(directive_set: DirectiveSetLike) -> bool:
    """FR-149: in spontaneous-application mode only company discovery runs."""
    return coerce_directive_set(directive_set).spontaneous_only


def allowed_source_types(directive_set: DirectiveSetLike) -> set[str]:
    """Source types the planner may select under these directives (FR-149, FR-164)."""
    if skips_vacancy_sources(directive_set):
        return set(ALL_SOURCE_TYPES - VACANCY_SOURCE_TYPES)
    return set(ALL_SOURCE_TYPES)


# ---------------------------------------------------------------------------
# Location post-filter (FR-144)
# ---------------------------------------------------------------------------


def location_matches(
    directive_set: DirectiveSetLike,
    latitude: float | None,
    longitude: float | None,
    country: str | None = None,
) -> bool:
    """Post-filter for records from sources without radius search (FR-144).

    A record passes when it is in a listed country/relocation country, inside
    one of the areas, or within the commute tolerance of the home location.
    With no location directives at all, everything passes.
    """
    directives = coerce_directive_set(directive_set).location
    countries = {c.upper() for c in directives.countries}
    if directives.willing_to_relocate:
        countries |= {c.upper() for c in directives.relocation_countries}
    if not countries and not directives.areas and directives.home_location is None:
        return True
    if country and countries and country.upper() in countries:
        return True
    if latitude is None or longitude is None:
        # Unplaceable record: accept it on the country rule alone, so that a
        # source with coarse location data is not silently dropped.
        return not countries
    for area in directives.areas:
        if area.is_geocoded:
            distance = haversine_km(area.latitude, area.longitude, latitude, longitude)
            if distance <= area.radius_km:
                return True
    home = directives.home_location
    if home is not None and home.is_geocoded and directives.max_commute_minutes is not None:
        distance = haversine_km(home.latitude, home.longitude, latitude, longitude)
        minutes = commute_minutes(distance, directives.commute_mode.value)
        if minutes <= directives.max_commute_minutes:
            return True
    return False


async def resolve_locations(
    location: LocationDirectives, *, language: str = DEFAULT_LOCALE
) -> LocationDirectives:
    """Geocode areas that only carry a label (FR-144).

    Degrades to the unresolved label when the geocoder cannot be reached; the
    directive set stays usable and the area simply has no radius filter.
    """
    resolved = location.model_copy(deep=True)
    country_codes = [c.lower() for c in resolved.countries] or None
    targets = [*resolved.areas]
    if resolved.home_location is not None:
        targets.append(resolved.home_location)
    for area in targets:
        if area.is_geocoded or not area.label.strip():
            continue
        hit = await geocode(area.label, country_codes=country_codes, language=language)
        if hit is None:
            continue
        area.latitude, area.longitude = hit.latitude, hit.longitude
        area.country_code = hit.country_code
        area.place_type = hit.place_type
        area.geocoded_at = hit.geocoded_at
    implied = resolved.effective_radius_km()
    if implied:
        for area in resolved.areas:
            # A commute tolerance is a tighter statement than a default radius.
            if area.radius_km in (0, 25.0):
                area.radius_km = round(implied, 1)
    return resolved


# ---------------------------------------------------------------------------
# FR-147: pre-fill from the composite profile
# ---------------------------------------------------------------------------

_SENIORITY_HINTS: list[tuple[re.Pattern[str], Seniority]] = [
    (re.compile(r"\b(board member|non[- ]executive|administrateur)\b", re.I), Seniority.BOARD),
    (re.compile(r"\b(c-level|chief|cto|ceo|cfo|coo|cio|cmo|cdo|ciso)\b", re.I), Seniority.C_LEVEL),
    (re.compile(r"\b(vice[- ]president|vp)\b", re.I), Seniority.VP),
    (re.compile(r"\b(director|directeur|head of|hoofd)\b", re.I), Seniority.DIRECTOR),
    (re.compile(r"\b(manager|management|teamleider|responsable)\b", re.I), Seniority.MANAGER),
    (re.compile(r"\b(principal|expert|architect)\b", re.I), Seniority.PRINCIPAL),
    (re.compile(r"\b(lead|leiding)\b", re.I), Seniority.LEAD),
    (re.compile(r"\b(senior|sr\.?)\b", re.I), Seniority.SENIOR),
    (re.compile(r"\b(medior|mid[- ]level|confirm)\b", re.I), Seniority.MEDIOR),
    (re.compile(r"\b(junior|jr\.?|graduate|starter)\b", re.I), Seniority.JUNIOR),
    (re.compile(r"\b(intern|stagiair|stagiaire)\b", re.I), Seniority.INTERN),
]

_MANAGEMENT_HINTS: list[tuple[re.Pattern[str], ManagementScope]] = [
    (re.compile(r"\b(chief|c-level|executive|directie)\b", re.I), ManagementScope.EXECUTIVE),
    (re.compile(r"\b(director|head of|department)\b", re.I), ManagementScope.DEPARTMENT_HEAD),
    (
        re.compile(r"\bmanager of managers|senior manager\b", re.I),
        ManagementScope.MANAGER_OF_MANAGERS,
    ),
    (re.compile(r"\b(manager|team lead|teamleider|lead)\b", re.I), ManagementScope.TEAM_LEAD),
]

_LOCATION_KEYS = (
    "location", "locatie", "city", "town", "address", "residence", "based_in", "region",
)
_ARRANGEMENT_HINTS = {
    WorkArrangement.REMOTE: re.compile(r"\bremote|telewerk|t[ée]l[ée]travail\b", re.I),
    WorkArrangement.HYBRID: re.compile(r"\bhybrid|hybride\b", re.I),
    WorkArrangement.ONSITE: re.compile(r"\bon[- ]?site|op kantoor|sur site\b", re.I),
}


def _texts(node: Any, depth: int = 0) -> list[str]:
    """Flatten a JSON block into the strings it contains, outermost first."""
    if depth > 4 or node is None:
        return []
    if isinstance(node, str):
        return [node] if node.strip() else []
    if isinstance(node, (int, float)):
        return [str(node)]
    if isinstance(node, Mapping):
        out: list[str] = []
        for value in node.values():
            out.extend(_texts(value, depth + 1))
        return out
    if isinstance(node, Sequence) and not isinstance(node, bytes):
        out = []
        for value in node:
            out.extend(_texts(value, depth + 1))
        return out
    return []


#: Inside a structured entry, the fields that hold a role *name*.  The rest of
#: a dream-job target role is provenance - ``rationale``, ``quote``, ``source``
#: ("inferred"), the entry's own ``id`` - and flattening all of it into the
#: proposed titles is how a directive set came to carry "inferred" and
#: "career_trajectory:1" as target titles, which then became LinkedIn searches.
_ROLE_NAME_KEYS: tuple[str, ...] = ("title", "role", "name", "example_titles")


def _role_names(node: Any) -> list[str]:
    """Role names out of either shape ``target_roles`` is stored in.

    A bare list of titles (``["Head of Data"]``) is already names.  A list of
    structured entries names each role in one of :data:`_ROLE_NAME_KEYS` and
    justifies it in the rest, so only the naming fields are read.  An entry that
    names itself in no recognised field is read whole, which is what every entry
    used to be: returning nothing for an unfamiliar shape would be worse than
    returning what it says.
    """
    if node is None:
        return []
    if isinstance(node, str):
        return [node] if node.strip() else []
    if isinstance(node, Mapping):
        return _labelled_strings(node, _ROLE_NAME_KEYS) or _texts(node)
    if isinstance(node, Sequence) and not isinstance(node, bytes):
        out: list[str] = []
        for value in node:
            out.extend(_role_names(value))
        return out
    return []


def _block(row: Mapping[str, Any] | None, column: str) -> Any:
    if not row:
        return None
    return from_json(row.get(column), row.get(column))


def _labelled_strings(node: Any, keys: Sequence[str], depth: int = 0) -> list[str]:
    """Strings stored under any of ``keys``, at any depth."""
    found: list[str] = []
    if depth > 4:
        return found
    if isinstance(node, Mapping):
        for key, value in node.items():
            named = any(k in str(key).lower() for k in keys)
            if named and isinstance(value, str) and value.strip():
                found.append(value.strip())
            else:
                found.extend(_labelled_strings(value, keys, depth + 1))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for value in node:
            found.extend(_labelled_strings(value, keys, depth + 1))
    return found


def _infer_seniority(texts: Sequence[str]) -> Seniority | None:
    for text in texts:
        for pattern, level in _SENIORITY_HINTS:
            if pattern.search(text):
                return level
    return None


def _shift(level: Seniority, delta: int) -> Seniority:
    """Move ``delta`` rungs along the ladder, clamped at both ends.

    ``PRINCIPAL`` shares its rank with ``MANAGER`` and is kept off the ladder
    so the order is unambiguous.  A level that is not on the ladder therefore
    enters at the rung carrying the same rank - without that, an inferred
    "principal" would fall to the bottom and propose an intern-level range.
    """
    order = sorted(
        (s for s in Seniority if s is not Seniority.PRINCIPAL),
        key=lambda s: SENIORITY_RANK[s],
    )
    if level in order:
        index = order.index(level)
    else:
        index = next(
            (i for i, s in enumerate(order) if SENIORITY_RANK[s] == SENIORITY_RANK[level]), 0
        )
    return order[max(0, min(len(order) - 1, index + delta))]


class DirectiveProposal(BaseModel):
    """A pre-filled directive set with its provenance (FR-147)."""

    directives: DirectiveSetPayload
    provenance: dict[str, str] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)


def propose_directives(
    composite_profile: Mapping[str, Any] | None = None,
    dream_job_model: Mapping[str, Any] | None = None,
    profile_version: Mapping[str, Any] | None = None,
    *,
    name: str = "Proposed directives",
) -> DirectiveProposal:
    """Pre-fill directives from the composite profile (FR-147).

    Deterministic on purpose: the profile has already been through the LLM, and
    a second interpretation here would only add drift and cost.  Nothing is
    invented (CR-405) - every value traces back to a profile field, and
    ``provenance`` records which one.  The job seeker edits the result before
    it is saved.
    """
    payload = DirectiveSetPayload(name=name)
    provenance: dict[str, str] = {}
    unresolved: list[str] = []

    # --- titles and function family (FR-142) -------------------------------
    # A target role names itself in one field and justifies itself in the rest.
    roles = _role_names(_block(dream_job_model, "target_roles"))[:6]
    # career_trajectory is biography - ``{id, text}`` where the text is a
    # sentence about a job held, not the name of one.  Only a phrase the title
    # catalogue actually recognises is mined from it; the rest is prose, and
    # prose proposed as a target title searches for nothing.
    roles += [
        text
        for text in _texts(_block(composite_profile, "career_trajectory"))[:12]
        if lookup_title(text)
    ][:4]
    titles: list[str] = []
    for role in roles:
        entry = lookup_title(role)
        canonical = entry["canonical"] if entry else role.strip()
        if 2 < len(canonical) <= 80 and canonical not in titles:
            titles.append(canonical)
    payload.job_content.target_titles = titles[:6]
    if titles:
        provenance["job_content.target_titles"] = (
            "dream_job_model.target_roles + composite_profile.career_trajectory"
        )
        synonyms: list[str] = []
        for title in payload.job_content.target_titles:
            entry = lookup_title(title)
            if entry:
                synonyms.extend(s for s in entry["synonyms"] if s not in synonyms)
        payload.job_content.title_synonyms = synonyms[:12]
        families = [e["family"] for e in (lookup_title(t) for t in titles) if e]
        payload.job_content.function_families = list(dict.fromkeys(families))[:3]
        if families:
            provenance["job_content.function_families"] = "title catalogue lookup"
    else:
        unresolved.append("job_content.target_titles")

    # --- seniority range (FR-142) ------------------------------------------
    seniority_texts = [
        *_texts(_block(composite_profile, "seniority")),
        *titles,
        *_texts(_block(composite_profile, "career_trajectory"))[:3],
    ]
    level = _infer_seniority(seniority_texts)
    if level:
        payload.job_content.seniority_min = _shift(level, -1)
        payload.job_content.seniority_max = _shift(level, 1)
        provenance["job_content.seniority_min"] = "composite_profile.seniority (inferred)"
        provenance["job_content.seniority_max"] = "composite_profile.seniority (inferred)"
    else:
        unresolved.append("job_content.seniority_min")

    scope = next(
        (s for pattern, s in _MANAGEMENT_HINTS for t in seniority_texts if pattern.search(t)),
        None,
    )
    if scope:
        payload.job_content.management_scope = scope
        provenance["job_content.management_scope"] = "composite_profile.seniority (inferred)"

    # --- skills and domains (FR-142) ---------------------------------------
    core = [t for t in _texts(_block(composite_profile, "core_competencies")) if len(t) < 60]
    adjacent_block = _block(composite_profile, "adjacent_competencies")
    adjacent = [t for t in _texts(adjacent_block) if len(t) < 60]
    domains = [t for t in _texts(_block(composite_profile, "domains")) if len(t) < 60]
    payload.job_content.must_have_skills = list(dict.fromkeys(core))[:8]
    payload.job_content.nice_to_have_skills = list(dict.fromkeys(adjacent))[:8]
    payload.job_content.industries_include = list(dict.fromkeys(domains))[:6]
    if core:
        provenance["job_content.must_have_skills"] = "composite_profile.core_competencies"
    if adjacent:
        provenance["job_content.nice_to_have_skills"] = "composite_profile.adjacent_competencies"
    if domains:
        provenance["job_content.industries_include"] = "composite_profile.domains"

    # --- home location (FR-144) --------------------------------------------
    home_candidates = [
        *_labelled_strings(_block(profile_version, "sections"), _LOCATION_KEYS),
        *_labelled_strings(_block(composite_profile, "constraints"), _LOCATION_KEYS),
        *_labelled_strings(_block(composite_profile, "inferred_preferences"), _LOCATION_KEYS),
    ]
    home = next((h for h in home_candidates if 2 < len(h) < 120), None)
    if home:
        payload.location.home_location = LocationArea(label=home, radius_km=0)
        payload.location.areas = [LocationArea(label=home, radius_km=35.0)]
        payload.location.max_commute_minutes = 45
        payload.location.commute_mode = CommuteMode.CAR
        provenance["location.home_location"] = "profile_version.sections (location)"
        unresolved.append("location.areas[0].coordinates")
    else:
        unresolved.append("location.home_location")

    # --- work arrangement (FR-145) -----------------------------------------
    preference_texts = _texts(_block(composite_profile, "inferred_preferences"))
    arrangements = [
        arrangement
        for arrangement, pattern in _ARRANGEMENT_HINTS.items()
        if any(pattern.search(t) for t in preference_texts)
    ]
    payload.work_arrangement.arrangements = arrangements or [
        WorkArrangement.HYBRID,
        WorkArrangement.ONSITE,
    ]
    if WorkArrangement.HYBRID in payload.work_arrangement.arrangements:
        payload.work_arrangement.min_remote_days = 2
    payload.work_arrangement.employment_types = [EmploymentType.FULL_TIME]
    payload.work_arrangement.fte_percentage_min = 80
    payload.work_arrangement.contract_types = [ContractType.PERMANENT]
    provenance["work_arrangement.arrangements"] = (
        "composite_profile.inferred_preferences" if arrangements else "default"
    )

    # --- company type (FR-143) ---------------------------------------------
    payload.company_type.trajectories = [Trajectory.GROWING, Trajectory.STABLE]
    provenance["company_type.trajectories"] = "default"

    # FR-146 is never guessed: a wrong salary floor silently deletes
    # opportunities.  It stays empty, and undisclosed, until the user sets it.
    unresolved.append("compensation.minimum_package")

    return DirectiveProposal(directives=payload, provenance=provenance, unresolved=unresolved)


# ---------------------------------------------------------------------------
# FR-147: pre-launch collection estimate
# ---------------------------------------------------------------------------

DEFAULT_ESTIMATE_CAPS = {
    "max_titles": 8,
    "max_areas": 5,
    "max_pages_per_query": 4,
    "results_per_page": 25,
    "parse_seconds_per_page": 1.5,
}


class SourceEstimate(BaseModel):
    """Expected volume for one source under the current directives (FR-147)."""

    adapter_key: str
    display_name: str
    source_type: str
    access_method: str
    queries: int
    estimated_pages: int
    estimated_seconds: int
    estimated_cost_eur: float


class CollectionEstimate(BaseModel):
    """What a campaign under these directives would collect (FR-147, FR-163)."""

    source_count: int
    query_count: int
    total_pages: int
    total_seconds: int
    total_cost_eur: float
    sources: list[SourceEstimate] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _target_countries(directives: DirectiveSet) -> set[str]:
    countries = {c.upper() for c in directives.location.countries}
    countries |= {a.country_code.upper() for a in directives.location.areas if a.country_code}
    if directives.location.willing_to_relocate:
        countries |= {c.upper() for c in directives.location.relocation_countries}
    return countries


def estimate_collection(
    directive_set: DirectiveSetLike,
    sources: Sequence[Mapping[str, Any]],
    *,
    caps: Mapping[str, Any] | None = None,
) -> CollectionEstimate:
    """Estimate sources, queries and pages before launch (FR-147).

    ``sources`` are ``source_catalogue`` rows - the repository reads them, this
    function stays free of SQL (CR-408).  The arithmetic is deliberately plain:
    one query per title per area the source can filter on, paginated up to the
    source's own result ceiling, priced with its declared cost per call and
    paced by its declared rate limit.  It is an order-of-magnitude figure shown
    before launch, not a promise.
    """
    directives = coerce_directive_set(directive_set)
    limits = {**DEFAULT_ESTIMATE_CAPS, **(caps or {})}
    allowed = allowed_source_types(directives)
    countries = _target_countries(directives)

    titles = directives.job_content.all_titles() or ["*"]
    title_count = max(1, min(len(titles), int(limits["max_titles"])))
    area_count = max(1, min(len(directives.location.areas) or 1, int(limits["max_areas"])))

    estimates: list[SourceEstimate] = []
    skipped: list[dict[str, str]] = []
    notes: list[str] = []

    for row in sources:
        key = str(row.get("adapter_key") or "")
        source_type = str(row.get("source_type") or "")
        if not row.get("enabled", 1):
            skipped.append({"adapter_key": key, "reason": "disabled"})
            continue
        if row.get("requires_ack") and not row.get("acknowledged_at"):
            skipped.append({"adapter_key": key, "reason": "awaiting_acknowledgement"})
            continue
        if row.get("tos_status") == "prohibited" and not row.get("acknowledged_at"):
            # The planner drops these outright (IR-101); counting their pages
            # here would promise a collection that never happens.
            skipped.append({"adapter_key": key, "reason": "tos_prohibited"})
            continue
        if source_type not in allowed:
            skipped.append({"adapter_key": key, "reason": "spontaneous_only"})
            continue
        coverage = {c.upper() for c in (from_json(row.get("coverage_countries"), []) or [])}
        if coverage and countries and not (coverage & countries):
            skipped.append({"adapter_key": key, "reason": "outside_coverage"})
            continue

        capabilities = from_json(row.get("query_capabilities"), {}) or {}
        per_query_results = int(capabilities.get("max_results_per_query") or 100)
        paginates = bool(capabilities.get("pagination", True))
        location_aware = bool(capabilities.get("location_filter"))

        queries = title_count * (area_count if location_aware else 1)
        if source_type in {"registry", "directory", "compensation"}:
            # Looked up per company, not searched per title.
            queries = max(1, area_count)
        pages_per_query = 1
        if paginates:
            pages_per_query = min(
                int(limits["max_pages_per_query"]),
                max(1, math.ceil(per_query_results / int(limits["results_per_page"]))),
            )
        pages = queries * pages_per_query

        rps = float(row.get("rate_limit_rps") or 0.5)
        per_page = (1.0 / rps if rps > 0 else 2.0) + float(limits["parse_seconds_per_page"])
        seconds = pages * per_page
        cost = pages * float(row.get("cost_per_call_eur") or 0.0)

        estimates.append(
            SourceEstimate(
                adapter_key=key,
                display_name=str(row.get("display_name") or key),
                source_type=source_type,
                access_method=str(row.get("access_method") or "http"),
                queries=queries,
                estimated_pages=pages,
                estimated_seconds=int(seconds),
                estimated_cost_eur=round(cost, 4),
            )
        )

    if not sources:
        notes.append("The source catalogue is empty; no adapters are registered yet.")
    if directives.spontaneous_only:
        notes.append("Spontaneous-application mode: vacancy sources are skipped (FR-149).")
    if directives.discretion_mode:
        notes.append(
            f"Discretion mode: {len(directives.discretion_excluded_companies)} companies and "
            f"{len(directives.discretion_excluded_contacts)} contacts are excluded (FR-385)."
        )
    if not directives.job_content.target_titles:
        notes.append("No target titles set; the estimate assumes one broad query per source.")
    if any(not a.is_geocoded for a in directives.location.areas):
        notes.append("Some areas are not geocoded; radius filters will not be sent to sources.")

    return CollectionEstimate(
        source_count=len(estimates),
        query_count=sum(e.queries for e in estimates),
        total_pages=sum(e.estimated_pages for e in estimates),
        total_seconds=sum(e.estimated_seconds for e in estimates),
        total_cost_eur=round(sum(e.estimated_cost_eur for e in estimates), 4),
        sources=sorted(estimates, key=lambda e: -e.estimated_pages),
        skipped=skipped,
        notes=notes,
    )
