"""Shared machinery for company-registry adapters (FR-241, DR-101, IR-102).

A registry is not a job board.  It is asked about one company at a time, it
answers with an identity record and a list of annual deposits, and its value to
Dream Job is twofold: it is the DR-101 identity anchor (the legal identifier
that makes de-duplication reliable), and it is the only place the last five
financial years actually exist (FR-241).

So every registry adapter implements the four-step ``SourceAdapter`` contract -
the planner and the job runner know nothing else - but the useful work happens
in :meth:`RegistryAdapter.collect`, which the financial pipeline calls directly
with a company row and gets back a :class:`RegistryResult`.

Several registries need a key that this deployment may not have (Companies
House, KvK, OpenCorporates, the NBB Consult subscription).  None of them is
allowed to fail loudly for that reason: :meth:`RegistryAdapter.available`
returns ``False``, the plan skips the source, and the company's financial
profile is marked ``estimated`` (FR-245, RK-06).
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from dreamjob.adapters.base import (
    AccessMethod,
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
)
from dreamjob.db.repositories import financials as repo
from dreamjob.db.repositories.knowledge import get_setting
from dreamjob.pipeline import filing_extract as fx
from dreamjob.pipeline.dedup import normalise_company_name, normalise_legal_id

log = logging.getLogger(__name__)

DEFAULT_YEARS = 5
DEFAULT_MAX_COMPANIES = 25


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def registry_secret(name: str) -> str:
    """A registry API key, from the settings table first and the environment second.

    ``config.Settings`` deliberately enumerates the credentials the product
    ships with; registry keys are per-deployment and optional, so they are read
    here.  ``app_setting`` wins so that an administrator can add a key at run
    time through the administration screens (FR-363) without a restart.
    """
    stored = get_setting(f"registry.secret.{name.lower()}")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return (os.environ.get(name) or "").strip()


# ---------------------------------------------------------------------------
# What a registry gives back
# ---------------------------------------------------------------------------


@dataclass
class SubsidiaryLink:
    """FR-246: a parent/child relation the registry exposes."""

    name: str
    legal_id: str | None = None
    relation: str = "subsidiary"
    ownership_pct: float | None = None


@dataclass
class RegistryResult:
    """Everything one registry knows about one company."""

    adapter_key: str
    identity: dict[str, Any] | None = None
    facts: list[fx.FilingFacts] = field(default_factory=list)
    subsidiaries: list[SubsidiaryLink] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    estimated: bool = False
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        log.info("[%s] %s", self.adapter_key, message)
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return bool(self.identity or self.facts)


# ---------------------------------------------------------------------------
# The base adapter
# ---------------------------------------------------------------------------


class RegistryAdapter(SourceAdapter):
    """Company registry / filing office adapter."""

    source_type = SourceType.REGISTRY
    access_method = AccessMethod.API
    capabilities = AdapterCapabilities(
        keyword_search=False,
        company_lookup=True,
        pagination=False,
        max_results_per_query=DEFAULT_YEARS,
    )
    rate_limit_rps = 1.0

    #: Jurisdictions this registry actually covers, as ISO-2 codes.
    jurisdictions: list[str] = []
    #: Environment / app_setting name of the key, when the registry needs one.
    secret_name: str = ""
    #: Whether a missing key makes the adapter unusable rather than degraded.
    key_required: bool = False
    #: Reporting standard of the filings this registry serves.
    reporting_standard: str | None = None
    default_currency: str = "EUR"

    # -- credentials --------------------------------------------------------
    def api_key(self) -> str:
        return registry_secret(self.secret_name) if self.secret_name else ""

    def available(self) -> bool:
        """False when a required key is absent - the caller then degrades (FR-245)."""
        if self.key_required and not self.api_key():
            return False
        return True

    def unavailable_reason(self) -> str:
        return (
            f"{self.display_name} needs an API key; set {self.secret_name} in the environment "
            f"or store it as the app setting registry.secret.{self.secret_name.lower()}"
        )

    # -- egress -------------------------------------------------------------
    @asynccontextmanager
    async def session(self) -> Any:
        """Use the caller's egress client, or open one for the duration (IR-102)."""
        if self.egress is not None:
            yield self.egress
            return
        from dreamjob.egress.client import EgressClient  # noqa: PLC0415 - avoids a cycle

        async with EgressClient() as client:
            yield client

    # -- the registry contract ---------------------------------------------
    async def collect(
        self,
        company: dict,
        *,
        years: int = DEFAULT_YEARS,
        egress: Any = None,
        llm: Any = None,
    ) -> RegistryResult:
        """Identity plus up to ``years`` financial years for one company."""
        raise NotImplementedError

    # -- SourceAdapter (FR-162, NFR-601) -----------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        years = int(caps.get("financial_years") or DEFAULT_YEARS)
        limit = int(caps.get("max_companies") or DEFAULT_MAX_COMPANIES)
        items: list[PlanItem] = []
        if not self.available():
            log.info("[%s] skipped in planning: %s", self.key, self.unavailable_reason())
            return items
        for company in self.targets(directives, caps, limit):
            items.append(
                PlanItem(
                    adapter_key=self.key,
                    native_query={
                        "company": {
                            k: company.get(k)
                            for k in (
                                "id", "name", "legal_id", "legal_id_type", "vat_number",
                                "domain", "country", "jurisdiction",
                            )
                        },
                        "years": years,
                    },
                    rationale=(
                        f"{self.display_name}: {years} years of statutory accounts for "
                        f"{company.get('name') or company.get('legal_id')}, the only "
                        "authoritative source for its ability to pay (FR-241, FR-244)"
                    ),
                    estimated_pages=years,
                    estimated_seconds=12 + 3 * years,
                    caps={"years": years},
                )
            )
        return items

    def targets(self, directives: dict, caps: dict, limit: int) -> list[dict]:
        """Companies this registry should be asked about.

        The planner may name them outright; otherwise the knowledge base is
        asked for companies in this jurisdiction whose filings are incomplete,
        which is what keeps a second campaign from re-collecting the first
        campaign's filings (FR-342).
        """
        explicit = caps.get("companies") or directives.get("companies")
        if isinstance(explicit, list) and explicit:
            rows = [c for c in explicit if isinstance(c, dict)]
            return [c for c in rows if self.covers(c)][:limit]
        out: list[dict] = []
        for jurisdiction in self.jurisdictions or [None]:
            out.extend(
                repo.companies_missing_filings(
                    jurisdiction=jurisdiction,
                    min_years=int(caps.get("financial_years") or DEFAULT_YEARS),
                    limit=limit,
                )
            )
        return out[:limit]

    def covers(self, company: dict) -> bool:
        if not self.jurisdictions:
            return True
        code = (company.get("jurisdiction") or company.get("country") or "").upper()
        return not code or code in {j.upper() for j in self.jurisdictions}

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        company = dict(item.native_query.get("company") or {})
        years = int(item.native_query.get("years") or DEFAULT_YEARS)
        async with self.session() as egress:
            result = await self.collect(company, years=years, egress=egress)
        return self.result_to_raw(result, company)

    @staticmethod
    def result_to_raw(result: RegistryResult, company: dict) -> list[RawRecord]:
        """Carry the structured result through the generic adapter pipeline."""
        raws: list[RawRecord] = []
        if result.identity:
            raws.append(
                RawRecord(
                    url=str(result.identity.get("source") or ""),
                    content="",
                    content_type="application/json",
                    meta={"kind": "company", "data": result.identity},
                )
            )
        for facts in result.facts:
            raws.append(
                RawRecord(
                    url="",
                    content="",
                    content_type="application/json",
                    meta={
                        "kind": "financial_year",
                        "facts": facts,
                        "company_id": company.get("id"),
                        "estimated": result.estimated,
                    },
                    raw_document_id=facts.filing_document_id,
                )
            )
        return raws

    def parse(self, raw: RawRecord) -> list[dict]:
        return [raw.meta] if raw.meta else []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        kind = parsed.get("kind")
        if kind == "company":
            data = dict(parsed.get("data") or {})
            data.pop("source_url", None)
            return NormalisedRecord(
                entity_type="company",
                data=data,
                confidence=0.95,
                provenance={"adapter_key": self.key},
            )
        if kind == "financial_year":
            company_id = parsed.get("company_id")
            if not company_id:
                return None
            facts: fx.FilingFacts = parsed["facts"]
            row = fx.to_financial_year_row(str(company_id), facts, source=self.key)
            if parsed.get("estimated"):
                row["is_estimated"] = 1
            return NormalisedRecord(
                entity_type="financial_year",
                data=row,
                confidence=0.9 if not row.get("is_estimated") else 0.5,
                provenance={"adapter_key": self.key},
                raw_document_id=facts.filing_document_id,
            )
        return None


# ---------------------------------------------------------------------------
# Helpers shared by the concrete registries
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"\D+")


def enterprise_number(company: dict) -> str | None:
    """The 10-digit Belgian KBO/BCE number of a company row, however it is written."""
    for key in ("legal_id", "vat_number"):
        raw = company.get(key)
        if not raw:
            continue
        normalised = normalise_legal_id(str(raw), key)
        if not normalised:
            continue
        digits = _DIGITS.sub("", normalised)
        if len(digits) == 9:
            digits = "0" + digits
        if len(digits) == 10 and digits[0] in "01":
            return digits
    return None


def format_enterprise_number(number: str) -> str:
    """``0123456789`` -> ``0123.456.789``, the form the registries print."""
    digits = _DIGITS.sub("", number).zfill(10)
    return f"{digits[:4]}.{digits[4:7]}.{digits[7:]}"


def identity_record(
    company: dict,
    *,
    name: str,
    legal_id: str | None,
    legal_id_type: str | None,
    country: str,
    source: str,
    **extra: Any,
) -> dict[str, Any]:
    """A ``company`` row keyed the DR-101 way: legal identifier first."""
    record: dict[str, Any] = {
        "name": name,
        "normalised_name": normalise_company_name(name),
        "legal_id": normalise_legal_id(legal_id, legal_id_type) if legal_id else None,
        "legal_id_type": legal_id_type,
        "country": country.upper()[:2],
        "jurisdiction": country.upper()[:2],
        "source": source,
        "access_method": "api",
        "confidence": 0.95,
    }
    # No ``id``: which knowledge-base row this belongs to is decided by the
    # de-duplicator from the DR-101 keys above, never by the adapter.
    record.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return record


async def attach_fx(facts: list[fx.FilingFacts], egress: Any = None) -> list[dict[str, Any]]:
    """DR-103: pair each year with the EUR rate that applied at its period end.

    A rate that is not an ECB quotation is written onto the record as a
    reconciliation flag before it is written onto the row, so that a figure
    resting on an indicative rate - or on none at all - is never read as
    though the conversion were exact (NFR-404, RK-06).
    """
    out: list[dict[str, Any]] = []
    for record in facts:
        if (record.currency or "EUR") == "EUR":
            out.append({"facts": record, "fx_rate_to_eur": 1.0, "fx_date": record.period_end})
            continue
        quote = await fx.fx_quote(record.currency, record.period_end or "", egress=egress)
        flag = quote.flag()
        if flag is not None:
            record.reconciliation_flags.append(flag)
            # An unconvertible currency makes every EUR comparison downstream a
            # guess, so the year travels as estimated (FR-245).
            if quote.origin == "unquoted":
                record.is_estimated = True
        out.append({"facts": record, "fx_rate_to_eur": quote.rate, "fx_date": quote.on_date})
    return out
