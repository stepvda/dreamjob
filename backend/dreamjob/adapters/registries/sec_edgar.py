"""SEC EDGAR - US filings (FR-241, FR-242, FR-246, DR-101, DR-103).

EDGAR is the easiest of the registries and the richest: ``companyfacts``
returns every XBRL fact a registrant has ever tagged, keyed by us-gaap or
ifrs-full concept, in one JSON document.  Five financial years of revenue,
operating income, net income, equity, assets and cash therefore need one
request and no parsing heuristics at all - which is why
:func:`dreamjob.pipeline.filing_extract.extract_from_companyfacts` does the
whole extraction.

No key is needed.  A descriptive User-Agent with a contact address is
mandatory under the SEC's fair-access policy; the egress layer already sends
exactly that one for every request in the application (FR-182), which is why
this adapter sets no headers of its own.

FR-246: ``EX-21`` (subsidiaries of the registrant) is listed in the submissions
index, so the group relation is recorded when the exhibit is present.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from dreamjob.adapters.base import AccessMethod, AdapterCapabilities, SourceType, register_adapter
from dreamjob.adapters.registries.common import (
    DEFAULT_YEARS,
    RegistryAdapter,
    RegistryResult,
    SubsidiaryLink,
    identity_record,
)
from dreamjob.pipeline import filing_extract as fx

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _fold(name: str) -> str:
    return _NON_ALNUM.sub(" ", (name or "").lower()).strip()


@register_adapter
class SECEdgarAdapter(RegistryAdapter):
    """US-listed registrants: identity from submissions, figures from companyfacts."""

    key = "registry.sec_edgar"
    display_name = "SEC EDGAR (US)"
    source_type = SourceType.REGISTRY
    access_method = AccessMethod.API
    coverage_countries = ["US"]
    jurisdictions = ["US"]
    key_required = False
    reporting_standard = "US-GAAP"
    default_currency = "USD"
    rate_limit_rps = 1.0
    capabilities = AdapterCapabilities(
        keyword_search=True,
        company_lookup=True,
        pagination=False,
        max_results_per_query=10,
    )
    legal_notes = (
        "Public data, no key. The SEC fair-access policy requires a descriptive "
        "User-Agent with a contact address, which the egress layer sends."
    )

    #: Ticker/name index, fetched once per process.
    _ticker_index: dict[str, str] | None = None

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
        cik = await self.resolve_cik(company, egress=egress)
        if not cik:
            result.estimated = True
            result.note("No SEC CIK could be resolved; the company is probably not a registrant")
            return result

        submissions = await self._json(SUBMISSIONS_URL.format(cik=cik), egress=egress)
        if isinstance(submissions, dict):
            result.identity = self.to_identity(submissions, cik, company)
            result.subsidiaries.extend(self.group_links(submissions))

        facts_payload, document_id = await self._fetch_json(
            COMPANYFACTS_URL.format(cik=cik), egress=egress
        )
        if not isinstance(facts_payload, dict):
            result.estimated = True
            result.note(f"companyfacts unavailable for CIK {cik}")
            return result

        result.facts = fx.extract_from_companyfacts(facts_payload, years=years)
        for record in result.facts:
            record.source = self.key
            # companyfacts is one document covering every year in it, so all
            # five years point back to the same stored filing (FR-241, DR-102).
            record.filing_document_id = document_id
        if not result.facts:
            result.estimated = True
            result.note("The registrant has tagged no annual figures EDGAR could return")
        return result

    # -- identity -----------------------------------------------------------
    async def resolve_cik(self, company: dict, *, egress: Any) -> str | None:
        """CIK from the stored legal id, then from the ticker/name index (DR-101)."""
        raw = company.get("legal_id")
        if raw and str(company.get("legal_id_type") or "").lower() in ("cik", "sec_cik"):
            return str(raw).zfill(10)[-10:]
        digits = re.sub(r"\D", "", str(raw or ""))
        if digits and len(digits) <= 10 and str(company.get("legal_id_type") or "") == "":
            return digits.zfill(10)

        index = await self.ticker_index(egress=egress)
        for candidate in (company.get("ticker"), company.get("name")):
            if not candidate:
                continue
            hit = index.get(_fold(str(candidate)))
            if hit:
                return hit
        return None

    async def ticker_index(self, *, egress: Any) -> dict[str, str]:
        """``ticker`` and folded company name -> zero-padded CIK."""
        if type(self)._ticker_index is not None:
            return type(self)._ticker_index
        payload = await self._json(TICKERS_URL, egress=egress)
        index: dict[str, str] = {}
        if isinstance(payload, dict):
            for entry in payload.values():
                if not isinstance(entry, dict):
                    continue
                cik = str(entry.get("cik_str") or "").zfill(10)
                if not cik.strip("0"):
                    continue
                if entry.get("ticker"):
                    index[_fold(str(entry["ticker"]))] = cik
                if entry.get("title"):
                    index[_fold(str(entry["title"]))] = cik
        type(self)._ticker_index = index
        return index

    def to_identity(self, submissions: dict, cik: str, company: dict) -> dict[str, Any]:
        address = (submissions.get("addresses") or {}).get("business") or {}
        # ``stateOrCountry`` is a US *state* code for a domestic registrant, so it
        # belongs in the location, never in the country column (DR-101).
        return identity_record(
            company,
            name=str(submissions.get("name") or ""),
            legal_id=cik,
            legal_id_type="sec_cik",
            country="US",
            source=SUBMISSIONS_URL.format(cik=cik),
            domain=submissions.get("website") or None,
            sector_codes=[str(submissions.get("sic"))] if submissions.get("sic") else None,
            business_summary=(
                f"SIC {submissions.get('sic')} {submissions.get('sicDescription') or ''}".strip()
            ),
            stage="listed" if submissions.get("tickers") else None,
            locations=[{"kind": "business", "address": address}] if address else None,
        )

    # -- FR-246 -------------------------------------------------------------
    @staticmethod
    def group_links(submissions: dict) -> list[SubsidiaryLink]:
        """The registrant's former names are the only relation the index states outright."""
        out: list[SubsidiaryLink] = []
        for entry in submissions.get("formerNames") or []:
            if isinstance(entry, dict) and entry.get("name"):
                out.append(SubsidiaryLink(name=str(entry["name"]), relation="former_name"))
        return out

    # -- helpers ------------------------------------------------------------
    async def _fetch_json(self, url: str, *, egress: Any) -> tuple[Any, str | None]:
        """The parsed body and the id of the ``raw_document`` it was stored as (DR-102)."""
        try:
            response = await egress.fetch(url)
        except Exception as exc:  # noqa: BLE001 - EDGAR outages degrade
            log.info("[%s] %s unavailable (%s)", self.key, url, exc)
            return None, None
        if not response.ok:
            log.info("[%s] %s returned %s", self.key, url, response.status_code)
            return None, None
        try:
            return json.loads(response.text), response.raw_document_id
        except ValueError:
            return None, response.raw_document_id

    async def _json(self, url: str, *, egress: Any) -> Any:
        payload, _document_id = await self._fetch_json(url, egress=egress)
        return payload
