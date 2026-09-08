"""OpenCorporates - cross-jurisdiction company directory (FR-181, FR-246, DR-101).

OpenCorporates aggregates 140-odd company registers behind one schema, which
makes it the right tool for the two jobs the national registries cannot do:

* **DR-101 resolution across borders.**  A company named in a vacancy or a
  press article, with no legal identifier attached, can be resolved to a
  jurisdiction code plus a company number - and that pair is a stronger key
  than any normalised name.
* **FR-246 group structure.**  ``corporate_groupings`` names the group a
  company belongs to, which is the relation the national registries do not
  expose, and it is what lets the entity-level and consolidated pictures be
  presented side by side.

It carries no financial figures, so a company known only through
OpenCorporates gets an ``estimated`` financial profile (FR-245).  The API needs
a token; without one the adapter reports itself unavailable and the planner
skips it.
"""

from __future__ import annotations

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
)

log = logging.getLogger(__name__)

API_BASE = "https://api.opencorporates.com/v0.4"

#: OpenCorporates jurisdiction code -> the country column of ``company``.
_COUNTRY_OF = {"gb": "GB", "us": "US", "be": "BE", "nl": "NL", "fr": "FR", "de": "DE"}

_LEGAL_ID_TYPE = {
    "be": "kbo_bce",
    "gb": "companies_house",
    "nl": "kvk",
    "fr": "siren",
    "de": "handelsregister",
}


@register_adapter
class OpenCorporatesAdapter(RegistryAdapter):
    """Directory lookup: identity across jurisdictions, plus the group grouping."""

    key = "directory.opencorporates"
    display_name = "OpenCorporates"
    source_type = SourceType.DIRECTORY
    access_method = AccessMethod.API
    coverage_countries: list[str] = []          # global
    jurisdictions: list[str] = []
    secret_name = "OPENCORPORATES_API_TOKEN"
    key_required = True
    rate_limit_rps = 0.5
    capabilities = AdapterCapabilities(
        keyword_search=True,
        company_lookup=True,
        pagination=True,
        max_results_per_query=100,
    )
    legal_notes = (
        "API token required. Company data is published under the Open Database Licence; "
        "officer personal data is not collected here (NFR-302)."
    )

    @staticmethod
    def jurisdiction_code(company: dict) -> str | None:
        code = (company.get("jurisdiction") or company.get("country") or "").lower()[:2]
        return code or None

    # -- registry contract --------------------------------------------------
    async def collect(
        self,
        company: dict,
        *,
        years: int = DEFAULT_YEARS,
        egress: Any = None,
        llm: Any = None,
    ) -> RegistryResult:
        result = RegistryResult(adapter_key=self.key, estimated=True)
        if not self.available():
            result.note(self.unavailable_reason())
            return result

        record = None
        jurisdiction = self.jurisdiction_code(company)
        number = company.get("legal_id")
        if jurisdiction and number:
            record = await self.company(jurisdiction, str(number), egress=egress)
        if record is None and company.get("name"):
            record = await self.search(
                str(company["name"]), jurisdiction=jurisdiction, egress=egress
            )
        if record is None:
            result.note("No OpenCorporates record matched this company")
            return result

        result.identity = self.to_identity(record, company)
        result.subsidiaries.extend(self.group_links(record))
        result.note(
            "OpenCorporates carries no annual figures; the financial profile stays "
            "estimated until a filing registry supplies them"
        )
        return result

    # -- API calls ----------------------------------------------------------
    async def _json(self, url: str, *, egress: Any) -> Any:
        separator = "&" if "?" in url else "?"
        try:
            response = await egress.fetch(f"{url}{separator}api_token={self.api_key()}")
        except Exception as exc:  # noqa: BLE001 - directory outages degrade
            log.info("[%s] %s unavailable (%s)", self.key, url, exc)
            return None
        if not response.ok:
            log.info("[%s] %s returned %s", self.key, url, response.status_code)
            return None
        try:
            return json.loads(response.text)
        except ValueError:
            return None

    async def company(self, jurisdiction: str, number: str, *, egress: Any) -> dict | None:
        payload = await self._json(
            f"{API_BASE}/companies/{jurisdiction.lower()}/{number}", egress=egress
        )
        record = ((payload or {}).get("results") or {}).get("company")
        return record if isinstance(record, dict) else None

    async def search(
        self, name: str, *, jurisdiction: str | None = None, egress: Any
    ) -> dict | None:
        url = f"{API_BASE}/companies/search?q={name}&per_page=5"
        if jurisdiction:
            url += f"&jurisdiction_code={jurisdiction.lower()}"
        payload = await self._json(url, egress=egress)
        companies = ((payload or {}).get("results") or {}).get("companies") or []
        for entry in companies:
            record = (entry or {}).get("company")
            if isinstance(record, dict) and record.get("inactive") is not True:
                return record
        return None

    def to_identity(self, record: dict, company: dict) -> dict[str, Any]:
        jurisdiction = str(record.get("jurisdiction_code") or "").lower()
        country = _COUNTRY_OF.get(jurisdiction[:2], jurisdiction[:2].upper() or "")
        codes = [
            str(entry.get("code"))
            for entry in record.get("industry_codes") or []
            if isinstance(entry, dict) and entry.get("code")
        ]
        return identity_record(
            company,
            name=str(record.get("name") or ""),
            legal_id=str(record.get("company_number") or ""),
            legal_id_type=_LEGAL_ID_TYPE.get(jurisdiction[:2], "opencorporates"),
            country=country or "",
            source=str(record.get("opencorporates_url") or API_BASE),
            sector_codes=codes or None,
            business_summary=(
                f"{record.get('company_type') or ''}; incorporated "
                f"{record.get('incorporation_date') or 'unknown'}; status "
                f"{record.get('current_status') or 'unknown'}"
            ).strip("; "),
            locations=(
                [{"kind": "registered", "address": record.get("registered_address_in_full")}]
                if record.get("registered_address_in_full")
                else None
            ),
            domain=(record.get("home_company_url") or None),
        )

    # -- FR-246 -------------------------------------------------------------
    @staticmethod
    def group_links(record: dict) -> list[SubsidiaryLink]:
        """``corporate_groupings`` is OpenCorporates' own parent/group relation."""
        out: list[SubsidiaryLink] = []
        for entry in record.get("corporate_groupings") or []:
            grouping = (entry or {}).get("corporate_grouping") or {}
            if grouping.get("name"):
                out.append(SubsidiaryLink(name=str(grouping["name"]), relation="group"))
        controlling = record.get("controlling_entity") or {}
        if isinstance(controlling, dict) and controlling.get("name"):
            out.append(
                SubsidiaryLink(
                    name=str(controlling["name"]),
                    legal_id=str(controlling.get("company_number") or "") or None,
                    relation="parent",
                )
            )
        return out
