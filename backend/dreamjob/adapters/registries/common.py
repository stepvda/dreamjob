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
from dreamjob.adapters.query_errors import UnusableQuery
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
    async def session(self, egress: Any = None) -> Any:
        """The caller's egress client, or one opened for the duration (IR-102).

        ``egress=None`` used to mean ``egress.fetch`` on ``None``: every request
        raised ``AttributeError``, every adapter caught it under its outage
        handler, and a company that is in the register was reported as absent
        from it.  A client is never optional here - it is opened rather than
        assumed.
        """
        client = egress if egress is not None else self.egress
        if client is not None:
            yield client
            return
        from dreamjob.egress.client import EgressClient  # noqa: PLC0415 - avoids a cycle

        async with EgressClient() as opened:
            yield opened

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
        """Ask the register about every company this plan item resolves to.

        A register is asked about companies, and the campaign planner does not
        name any: it writes ``{"country", "legal_ids", "sector_codes"}``.  So
        the plan item is read for whatever companies it does carry, and when it
        carries none the knowledge base is asked for the companies in this
        jurisdiction whose filings are still missing (FR-241, FR-342) - which
        is exactly what :meth:`plan` would have produced had it been called.

        A plan item that resolves to no company at all is not "nothing to
        collect": it is an unrunnable query, and it says so (FR-181, NFR-403).
        """
        query = plan_query(item)
        if not self.available():
            # RK-06 keeps the *figures* honest by marking them estimated; it does
            # not ask the source to pretend it ran.  A plan item that says which
            # key is missing is what an administrator can act on (FR-363).
            raise UnusableQuery(self.key, self.unavailable_reason())
        years = int(query.get("years") or DEFAULT_YEARS)
        limit = self._company_limit(item, query)
        companies = self.query_targets(query, years=years, limit=limit)
        if not companies:
            raise UnusableQuery(
                self.key,
                "no company to ask the register about: the plan item names none and the "
                "knowledge base holds no company in this jurisdiction with filings still "
                "missing - run company discovery before the registries (FR-241)",
                expected=("company", "companies", "legal_ids"),
            )

        raws: list[RawRecord] = []
        async with self.session() as egress:
            for company in companies:
                result = await self.collect(company, years=years, egress=egress)
                # DR-103 belongs on the collection path too, not only on the
                # per-company path in pipeline.financial: an unpriced non-EUR
                # filing is read downstream as though 1 USD were 1 EUR.
                priced = await attach_fx(result.facts, egress=egress)
                raws.extend(self.result_to_raw(result, company, priced=priced))
        return raws

    def _company_limit(self, item: PlanItem, query: dict) -> int:
        caps = getattr(item, "caps", None) or {}
        for value in (caps.get("max_companies"), query.get("max_companies")):
            try:
                if value:
                    return max(1, int(value))
            except (TypeError, ValueError):
                continue
        return DEFAULT_MAX_COMPANIES

    def query_targets(self, query: dict, *, years: int, limit: int) -> list[dict]:
        """The companies one plan item stands for, in the order they were named."""
        country = _first_country(query) or (self.jurisdictions[0] if self.jurisdictions else "")
        rows: list[dict] = []

        explicit = query.get("company")
        if isinstance(explicit, dict) and any(v for v in explicit.values()):
            rows.append(dict(explicit))
        for entry in query.get("companies") or []:
            if isinstance(entry, dict) and any(v for v in entry.values()):
                rows.append(dict(entry))
            elif isinstance(entry, str) and entry.strip():
                rows.append({"name": entry.strip(), "country": country})
        for legal_id in query.get("legal_ids") or []:
            if str(legal_id or "").strip():
                rows.append(
                    {
                        "legal_id": str(legal_id).strip(),
                        "country": country,
                        "jurisdiction": country,
                    }
                )

        named = [row for row in rows if self.covers(row)]
        if named:
            return named[:limit]
        return self.targets({}, {"financial_years": years}, limit)

    def result_to_raw(
        self,
        result: RegistryResult,
        company: dict,
        *,
        priced: list[dict[str, Any]] | None = None,
    ) -> list[RawRecord]:
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
        # The registry knows the company by its legal identifier; the knowledge
        # base knows it by a row id.  Resolving the two here - once - is what
        # lets a filing be stored on a first campaign, when the company row is
        # created by this very answer (DR-101, FR-184).
        company_id = self.resolve_company_id(company, result.identity) if result.facts else None
        rates = {id(entry["facts"]): entry for entry in (priced or [])}
        for facts in result.facts:
            entry = rates.get(id(facts), {})
            raws.append(
                RawRecord(
                    url="",
                    content="",
                    content_type="application/json",
                    meta={
                        "kind": "financial_year",
                        "facts": facts,
                        "company_id": company_id,
                        "company": {
                            k: company.get(k)
                            for k in ("name", "legal_id", "legal_id_type", "vat_number",
                                      "domain", "country")
                        },
                        "identity": result.identity,
                        "fx_rate_to_eur": entry.get("fx_rate_to_eur"),
                        "fx_date": entry.get("fx_date"),
                        "estimated": result.estimated,
                    },
                    raw_document_id=facts.filing_document_id,
                )
            )
        return raws

    def resolve_company_id(self, company: dict, identity: dict | None = None) -> str | None:
        """Which knowledge-base row these figures belong to (DR-101, FR-184).

        The caller's row id wins.  Otherwise the de-duplicator is asked on the
        legal identifier the registry just returned, and if that company is not
        in the knowledge base yet its identity is written first - a filing whose
        company row does not exist yet is dropped by ``financial_year``'s NOT
        NULL constraint, which is the registry half of the chicken-and-egg.
        """
        if company.get("id"):
            return str(company["id"])
        from dreamjob.pipeline.knowledge_base import (  # noqa: PLC0415 - avoids a cycle
            KnowledgeBaseWriter,
            resolve_company,
        )

        for candidate in (identity, company):
            if not candidate:
                continue
            try:
                found, _matched = resolve_company(dict(candidate))
            except Exception:  # noqa: BLE001 - an unreadable row must not lose the filing
                log.exception("[%s] company resolution failed", self.key)
                found = None
            if found:
                return str(found["id"])
        if not identity:
            return None
        outcome = KnowledgeBaseWriter(adapter_key=self.key).write(
            {"entity_type": "company", "data": dict(identity), "confidence": 0.95}
        )
        return outcome.entity_id if outcome else None

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
            company_id = parsed.get("company_id") or self.resolve_company_id(
                dict(parsed.get("company") or {}), parsed.get("identity")
            )
            if not company_id:
                log.warning(
                    "[%s] a filing was read but no company row could be resolved for it; "
                    "the figures are dropped (DR-101)",
                    self.key,
                )
                return None
            facts: fx.FilingFacts = parsed["facts"]
            row = fx.to_financial_year_row(
                str(company_id),
                facts,
                fx_rate_to_eur=parsed.get("fx_rate_to_eur"),
                fx_date=parsed.get("fx_date"),
                source=self.key,
            )
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


def plan_query(item: Any) -> dict[str, Any]:
    """The native query of a plan item, whether it is a ``PlanItem`` or a row."""
    query = getattr(item, "native_query", None)
    if query is None and isinstance(item, dict):
        query = item.get("native_query")
    if isinstance(query, str):
        from dreamjob.db.connection import from_json  # noqa: PLC0415 - avoids a cycle

        query = from_json(query)
    return dict(query or {})


def _first_country(query: dict) -> str:
    """The ISO-2 code a registry plan item is scoped to, however it is written."""
    for key in ("country", "jurisdiction"):
        value = query.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()[:2]
    for key in ("countries", "jurisdictions"):
        values = query.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value.strip():
                    return value.strip().upper()[:2]
    return ""


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
