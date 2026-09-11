"""Companies House - UK company register and accounts (FR-241, FR-242, DR-101).

Companies House publishes both halves of what FR-241 needs through one free
REST API: the company profile (registered number, name, SIC codes, status,
accounts reference date) and the filing history, from which the ``accounts``
category gives the last five annual returns.  Each accounts filing has a
document in the separate document API, served as inline XBRL - the richest
machine-readable form the UK offers - with the PDF as a fall-back for older
filings.

Authentication is an API key used as the HTTP Basic username with an empty
password.  The key is free but per-deployment, so it is read from the
environment and its absence degrades the source rather than breaking it
(FR-245).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from dreamjob.adapters.base import AccessMethod, AdapterCapabilities, SourceType, register_adapter
from dreamjob.adapters.registries.common import (
    DEFAULT_YEARS,
    RegistryAdapter,
    RegistryResult,
    SubsidiaryLink,
    identity_record,
    stated_none,
)
from dreamjob.pipeline import filing_extract as fx

log = logging.getLogger(__name__)

API_BASE = "https://api.company-information.service.gov.uk"
DOCUMENT_BASE = "https://document-api.company-information.service.gov.uk"

_SIZE_BAND = {
    "full": None,
    "small": "small",
    "medium": "medium",
    "micro-entity": "micro",
    "dormant": "dormant",
    "group": None,
}


@register_adapter
class CompaniesHouseAdapter(RegistryAdapter):
    """UK register: identity from the company profile, figures from iXBRL accounts."""

    key = "registry.companies_house"
    display_name = "Companies House (UK)"
    source_type = SourceType.REGISTRY
    access_method = AccessMethod.API
    coverage_countries = ["GB", "UK"]
    jurisdictions = ["GB", "UK"]
    secret_name = "COMPANIES_HOUSE_API_KEY"
    key_required = True
    reporting_standard = "UK-GAAP"
    default_currency = "GBP"
    rate_limit_rps = 1.0
    capabilities = AdapterCapabilities(
        keyword_search=True,
        company_lookup=True,
        pagination=True,
        max_results_per_query=50,
    )
    legal_notes = (
        "Free API key required; the register and the filed accounts are open data "
        "under the Open Government Licence."
    )

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        token = base64.b64encode(f"{self.api_key()}:".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": accept}

    @staticmethod
    def company_number(company: dict) -> str | None:
        raw = company.get("legal_id") or company.get("company_number")
        if not raw:
            return None
        cleaned = "".join(ch for ch in str(raw).upper() if ch.isalnum())
        return cleaned or None

    # -- registry contract --------------------------------------------------
    async def collect(
        self,
        company: dict,
        *,
        years: int = DEFAULT_YEARS,
        egress: Any = None,
        llm: Any = None,
    ) -> RegistryResult:
        result = RegistryResult(adapter_key=self.key)
        if not self.available():
            result.estimated = True
            result.note(self.unavailable_reason())
            return result

        async with self.session(egress) as client:
            number = self.company_number(company)
            if not number and company.get("name"):
                number = await self.search(str(company["name"]), egress=client)
            if not number:
                result.estimated = True
                result.note("No Companies House number could be resolved")
                return result

            profile = await self.profile(number, egress=client)
            if profile:
                result.identity = self.to_identity(profile, company)
                result.subsidiaries.extend(self.group_links(profile))
            filings = await self.accounts_filings(number, egress=client, limit=years * 2)
            if not filings:
                result.estimated = True
                result.note(f"No accounts filings listed for company {number}")

            seen: set[int] = set()
            for filing in filings:
                if len(seen) >= years:
                    break
                facts = await self.read_filing(
                    filing, egress=client, llm=llm, company_name=company.get("name")
                )
                for record in facts:
                    if record.fiscal_year and record.fiscal_year not in seen:
                        seen.add(record.fiscal_year)
                        result.facts.append(record)
        result.facts.sort(key=lambda r: r.fiscal_year)
        if not result.facts:
            result.estimated = True
        return result

    # -- API calls ----------------------------------------------------------
    async def _json(self, url: str, *, egress: Any) -> Any:
        try:
            response = await egress.fetch(url, headers=self._headers())
        except Exception as exc:  # noqa: BLE001 - registry outages degrade
            log.info("[%s] %s unavailable (%s)", self.key, url, exc)
            return None
        if not response.ok:
            log.info("[%s] %s returned %s", self.key, url, response.status_code)
            return None
        try:
            return json.loads(response.text)
        except ValueError:
            return None

    async def search(self, name: str, *, egress: Any) -> str | None:
        payload = await self._json(
            f"{API_BASE}/search/companies?q={name}&items_per_page=5", egress=egress
        )
        if not isinstance(payload, dict):
            # An outage, a non-2xx and an unparseable body all arrive as
            # ``None``; none of them is an answer (FR-181, NFR-403).
            return None
        for item in payload.get("items") or []:
            if item.get("company_number"):
                return str(item["company_number"])
        if stated_none(payload, listing="items", total="total_results"):
            # ``total_results`` is the register's own count, and zero is it
            # saying it holds no company of this name.  Read from the count
            # rather than from an empty ``items``, which a renamed field
            # produces just as readily (FR-181, NFR-403).
            self.record_stated_empty()
        return None

    async def profile(self, number: str, *, egress: Any) -> dict | None:
        payload = await self._json(f"{API_BASE}/company/{number}", egress=egress)
        return payload if isinstance(payload, dict) else None

    def to_identity(self, profile: dict, company: dict) -> dict[str, Any]:
        address = profile.get("registered_office_address") or {}
        accounts = (profile.get("accounts") or {}).get("last_accounts") or {}
        return identity_record(
            company,
            name=str(profile.get("company_name") or ""),
            legal_id=str(profile.get("company_number") or ""),
            legal_id_type="companies_house",
            country="GB",
            source=f"{API_BASE}/company/{profile.get('company_number')}",
            sector_codes=list(profile.get("sic_codes") or []) or None,
            locations=[{"kind": "registered_office", "address": address}] if address else None,
            size_band=_SIZE_BAND.get(str(accounts.get("type") or "").lower()),
            business_summary=(
                f"Register status: {profile.get('company_status')}; "
                f"type: {profile.get('type')}; "
                f"incorporated {profile.get('date_of_creation')}"
            ),
            stage="listed" if profile.get("type") == "plc" else None,
        )

    async def accounts_filings(self, number: str, *, egress: Any, limit: int = 10) -> list[dict]:
        payload = await self._json(
            f"{API_BASE}/company/{number}/filing-history?category=accounts&items_per_page={limit}",
            egress=egress,
        )
        out: list[dict] = []
        for item in (payload or {}).get("items") or []:
            links = item.get("links") or {}
            document = links.get("document_metadata")
            description = item.get("description_values") or {}
            period_end = description.get("made_up_date") or item.get("action_date")
            out.append(
                {
                    "transaction_id": item.get("transaction_id"),
                    "period_end": str(period_end)[:10] if period_end else None,
                    "fiscal_year": fx._fiscal_year(str(period_end)[:10] if period_end else None),
                    "document_metadata": document,
                    "description": item.get("description"),
                }
            )
        out.sort(key=lambda d: d["fiscal_year"] or 0, reverse=True)
        return out

    async def read_filing(
        self,
        filing: dict,
        *,
        egress: Any,
        llm: Any = None,
        company_name: str | None = None,
    ) -> list[fx.FilingFacts]:
        """iXBRL first, PDF second (FR-242)."""
        metadata_url = filing.get("document_metadata")
        if not metadata_url:
            return []
        document_id = str(metadata_url).rstrip("/").rsplit("/", 1)[-1]
        content_url = f"{DOCUMENT_BASE}/document/{document_id}/content"

        for accept, kind in (
            ("application/xhtml+xml", "ixbrl"),
            ("application/pdf", "pdf"),
        ):
            try:
                response = await egress.fetch(content_url, headers=self._headers(accept))
            except Exception as exc:  # noqa: BLE001
                log.info("[%s] document %s unavailable (%s)", self.key, document_id, exc)
                continue
            if not response.ok or not response.content:
                continue
            if kind == "ixbrl":
                facts = fx.extract_from_ixbrl(response.content)
            else:
                facts = [
                    fx.extract_from_pdf(
                        response.content,
                        fiscal_year=filing.get("fiscal_year"),
                        period_end=filing.get("period_end"),
                        currency="GBP",
                        reporting_standard="UK-GAAP",
                        llm=llm,
                        company_name=company_name,
                    )
                ]
            facts = [r for r in facts if r.fiscal_year]
            for record in facts:
                record.filing_document_id = response.raw_document_id
                record.currency = record.currency or "GBP"
                record.reporting_standard = "UK-GAAP"
                record.source = self.key
            if facts:
                return facts
        return []

    # -- FR-246 -------------------------------------------------------------
    @staticmethod
    def group_links(profile: dict) -> list[SubsidiaryLink]:
        """Companies House exposes no ownership graph; only an explicit parent name."""
        parent = (profile or {}).get("parent_company_name")
        return [SubsidiaryLink(name=str(parent), relation="parent")] if parent else []
