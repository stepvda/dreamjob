"""Common source-adapter interface (NFR-601, FR-161, FR-181, IR-101).

Every external source - job board, ATS, company directory, financial
registry, company website, compensation site, news feed, event feed - is
implemented as a subclass of ``SourceAdapter`` with the same four-step shape:

    plan()      -> turn directives + composite profile into native queries
    fetch()     -> pull raw material through the egress layer
    parse()     -> extract structured records from raw material
    normalise() -> map records onto the shared knowledge-base schema

Because the pipeline only ever calls those four methods, adding a new board
or registry needs no change anywhere else (NFR-601).  Each adapter also
declares its coverage, access method and terms-of-service status, which the
source catalogue stores and the planner uses to select sources (FR-161,
FR-164, IR-101).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dreamjob.db.connection import query_one, upsert_row, utcnow
from dreamjob.egress.client import EgressClient

log = logging.getLogger(__name__)


class SourceType(StrEnum):
    JOB_BOARD = "job_board"
    ATS = "ats"
    DIRECTORY = "directory"
    REGISTRY = "registry"
    WEBSITE = "website"
    LINKEDIN = "linkedin"
    COMPENSATION = "compensation"
    NEWS = "news"
    EVENTS = "events"


class AccessMethod(StrEnum):
    API = "api"
    HTTP = "http"
    BROWSER = "browser"


class ToSStatus(StrEnum):
    """IR-101: adapters marked ``PROHIBITED`` are disabled until acknowledged."""

    PERMITTED = "permitted"
    RESTRICTED = "restricted"
    PROHIBITED = "prohibited"


@dataclass
class AdapterCapabilities:
    """What the planner needs to know to choose and query this source (FR-161)."""

    keyword_search: bool = True
    location_filter: bool = False
    radius_filter: bool = False
    contract_type_filter: bool = False
    work_arrangement_filter: bool = False
    seniority_filter: bool = False
    company_lookup: bool = False
    pagination: bool = True
    max_results_per_query: int = 100


@dataclass
class PlanItem:
    """One concrete unit of work for this adapter (FR-162)."""

    adapter_key: str
    native_query: dict[str, Any]
    rationale: str = ""
    estimated_pages: int = 1
    estimated_seconds: int = 10
    estimated_cost_eur: float = 0.0
    caps: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawRecord:
    """Raw material for one entity, before extraction."""

    url: str
    content: str
    content_type: str = "text/html"
    raw_document_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalisedRecord:
    """A record ready for the knowledge base.

    ``entity_type`` is one of ``company``, ``vacancy``, ``contact``,
    ``financial_year``, ``hiring_signal``, ``event``.  ``data`` uses the
    column names of the corresponding table.  ``confidence`` and
    ``provenance`` satisfy NFR-402.
    """

    entity_type: str
    data: dict[str, Any]
    confidence: float = 0.7
    provenance: dict[str, Any] = field(default_factory=dict)
    raw_document_id: str | None = None


class SourceAdapter(ABC):
    """Base class for all source adapters."""

    # --- catalogue metadata (FR-161) --------------------------------------
    key: str = ""
    display_name: str = ""
    source_type: SourceType = SourceType.JOB_BOARD
    access_method: AccessMethod = AccessMethod.HTTP
    coverage_countries: list[str] = []          # ISO-2 codes; [] means global
    coverage_industries: list[str] = []         # [] means all
    capabilities: AdapterCapabilities = AdapterCapabilities()
    rate_limit_rps: float = 0.5
    cost_per_call_eur: float = 0.0
    tos_status: ToSStatus = ToSStatus.PERMITTED
    legal_notes: str = ""
    requires_ack: bool = False

    def __init__(self, egress: EgressClient | None = None):
        self.egress = egress
        self._extraction_attempts = 0
        self._extraction_successes = 0

    # --- the four-step contract (NFR-601) ---------------------------------
    @abstractmethod
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        """Translate directives into this source's native query form (FR-162)."""

    @abstractmethod
    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        """Retrieve raw material for one plan item, via the egress layer."""

    @abstractmethod
    def parse(self, raw: RawRecord) -> list[dict]:
        """Extract structured fields from raw material.

        Deterministic extraction is preferred; adapters may fall back to LLM
        extraction for unstructured pages (FR-183).
        """

    @abstractmethod
    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        """Map extracted fields onto knowledge-base columns."""

    # --- shared helpers ----------------------------------------------------
    def covers_country(self, country: str | None) -> bool:
        """FR-164: source selection is constrained by coverage metadata."""
        if not self.coverage_countries or not country:
            return True
        return country.upper() in {c.upper() for c in self.coverage_countries}

    def is_enabled(self) -> bool:
        """IR-101: prohibited sources need an explicit administrator acknowledgement."""
        row = query_one(
            "SELECT enabled, requires_ack, acknowledged_at FROM source_catalogue WHERE adapter_key = ?",
            (self.key,),
        )
        if row is None:
            return self.tos_status != ToSStatus.PROHIBITED
        if not row["enabled"]:
            return False
        if row["requires_ack"] and not row["acknowledged_at"]:
            return False
        return True

    def record_extraction(self, attempted: int, succeeded: int) -> None:
        """NFR-403: extraction-rate monitoring surfaces adapter breakage."""
        self._extraction_attempts += attempted
        self._extraction_successes += succeeded

    @property
    def extraction_rate(self) -> float | None:
        if not self._extraction_attempts:
            return None
        return self._extraction_successes / self._extraction_attempts

    async def run(self, item: PlanItem) -> list[NormalisedRecord]:
        """Convenience: fetch -> parse -> normalise for one plan item."""
        out: list[NormalisedRecord] = []
        raws = await self.fetch(item)
        for raw in raws:
            try:
                parsed_items = self.parse(raw)
            except Exception:  # noqa: BLE001 - one bad page must not stop the job
                log.exception("[%s] parse failed for %s", self.key, raw.url)
                self.record_extraction(1, 0)
                continue
            self.record_extraction(len(parsed_items) or 1, len(parsed_items))
            for parsed in parsed_items:
                try:
                    rec = self.normalise(parsed, raw)
                except Exception:  # noqa: BLE001
                    log.exception("[%s] normalise failed for %s", self.key, raw.url)
                    continue
                if rec:
                    rec.raw_document_id = rec.raw_document_id or raw.raw_document_id
                    out.append(rec)
        return out

    def register(self) -> None:
        """Write this adapter's metadata into the source catalogue (FR-161)."""
        upsert_row(
            "source_catalogue",
            {
                "adapter_key": self.key,
                "display_name": self.display_name or self.key,
                "source_type": self.source_type.value,
                "coverage_countries": self.coverage_countries,
                "coverage_industries": self.coverage_industries,
                "query_capabilities": self.capabilities.__dict__,
                "access_method": self.access_method.value,
                "rate_limit_rps": self.rate_limit_rps,
                "cost_per_call_eur": self.cost_per_call_eur,
                "tos_status": self.tos_status.value,
                "legal_notes": self.legal_notes,
                "requires_ack": 1 if self.requires_ack else 0,
                "updated_at": utcnow(),
            },
            ["adapter_key"],
        )


# ---------------------------------------------------------------------------
# Registry (NFR-601): adapters self-register on import.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[SourceAdapter]] = {}


def register_adapter(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    if not cls.key:
        raise ValueError(f"{cls.__name__} must define a non-empty `key`")
    _REGISTRY[cls.key] = cls
    return cls


def get_adapter(key: str, egress: EgressClient | None = None) -> SourceAdapter:
    if key not in _REGISTRY:
        raise KeyError(f"Unknown adapter {key!r}. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[key](egress=egress)


def all_adapters() -> dict[str, type[SourceAdapter]]:
    return dict(_REGISTRY)


def sync_catalogue(egress: EgressClient | None = None) -> int:
    """Register every known adapter's metadata in the database (FR-161, FR-363).

    Registration writes what each adapter says about itself; the reconciliation
    that follows corrects the two things an adapter cannot know - the rate the
    egress layer will really apply to it, and whether a catalogued source still
    has an adapter at all (FR-185, IR-101).  See
    ``db.repositories.admin.reconcile_catalogue``.
    """
    from dreamjob.db.repositories import admin as admin_repo  # noqa: PLC0415

    for key in _REGISTRY:
        get_adapter(key, egress).register()
    admin_repo.reconcile_catalogue(_REGISTRY)
    return len(_REGISTRY)
