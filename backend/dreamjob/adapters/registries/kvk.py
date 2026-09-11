"""KvK - the Netherlands Chamber of Commerce register (FR-241, FR-245, DR-101).

The KvK Handelsregister API gives identity: KvK number, RSIN, statutory and
trade names, legal form, SBI activity codes and the registered establishment.
That is what DR-101 needs for a Dutch company and what the de-duplicator keys
on (FR-184).

What it does not give is figures.  Dutch annual accounts are deposited with the
KvK but are only available through a separate, paid, per-document service, so
this adapter returns identity plus an explicit note that the financial profile
must be built from secondary signals and marked estimated (FR-245, RK-06).
The API also needs a key, and a deployment without one degrades to the same
place rather than failing.
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
    stated_none,
)

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.kvk.nl/api/v2/zoeken"
PROFILE_URL = "https://api.kvk.nl/api/v1/basisprofielen/{kvk}"


@register_adapter
class KvKAdapter(RegistryAdapter):
    """Dutch trade register: identity anchor, no public figures."""

    key = "registry.kvk"
    display_name = "KvK Handelsregister (NL)"
    source_type = SourceType.REGISTRY
    access_method = AccessMethod.API
    coverage_countries = ["NL"]
    jurisdictions = ["NL"]
    secret_name = "KVK_API_KEY"
    key_required = True
    reporting_standard = "NL-GAAP"
    default_currency = "EUR"
    rate_limit_rps = 1.0
    capabilities = AdapterCapabilities(
        keyword_search=True,
        company_lookup=True,
        pagination=True,
        max_results_per_query=50,
    )
    legal_notes = (
        "Paid/keyed API. Annual accounts are deposited with the KvK but are not part of "
        "the open Handelsregister API, so financial profiles for NL companies are "
        "estimated from secondary signals (FR-245)."
    )

    def _headers(self) -> dict[str, str]:
        return {"apikey": self.api_key(), "Accept": "application/json"}

    @staticmethod
    def kvk_number(company: dict) -> str | None:
        raw = company.get("legal_id") or company.get("kvk_number")
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        return digits.zfill(8) if 6 <= len(digits) <= 8 else None

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

        async with self.session(egress) as client:
            number = self.kvk_number(company)
            if not number and company.get("name"):
                number = await self.search(str(company["name"]), egress=client)
            if not number:
                result.note("No KvK number could be resolved")
                return result

            profile = await self._json(PROFILE_URL.format(kvk=number), egress=client)
        if not isinstance(profile, dict):
            result.note(f"KvK basisprofiel unavailable for {number}")
            return result

        result.identity = self.to_identity(profile, number, company)
        result.subsidiaries.extend(self.group_links(profile))
        result.note(
            "KvK publishes no figures through the Handelsregister API; the financial "
            "profile for this company is built from secondary signals and marked estimated"
        )
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
        payload = await self._json(f"{SEARCH_URL}?naam={name}&pagina=1&resultatenPerPagina=5",
                                   egress=egress)
        if not isinstance(payload, dict):
            # ``_json`` answers ``None`` for an outage, a non-2xx and a body
            # that is not JSON alike.  None of those is the register saying it
            # holds nothing, so nothing is claimed here (FR-181, NFR-403).
            return None
        for item in payload.get("resultaten") or []:
            if item.get("kvkNummer"):
                return str(item["kvkNummer"])
        if stated_none(payload, listing="resultaten", total="totaal"):
            # ``totaal`` is the Handelsregister's own count of what it found,
            # and zero is it answering that it holds no company of this name.
            # An empty ``resultaten`` on its own would not do: it also reads
            # empty when the field the hits arrive in is renamed, and a stated
            # ``totaal`` of three with nothing readable under it is a breakage
            # that must keep saying so (FR-181, NFR-403).
            self.record_stated_empty()
        return None

    def to_identity(self, profile: dict, number: str, company: dict) -> dict[str, Any]:
        names = [profile.get("statutaireNaam"), profile.get("naam")]
        handelsnamen = ((profile.get("_embedded") or {}).get("hoofdvestiging") or {}).get(
            "handelsnamen"
        ) or profile.get("handelsnamen") or []
        for entry in handelsnamen:
            if isinstance(entry, dict) and entry.get("naam"):
                names.append(entry["naam"])
        name = next((n for n in names if n), "")

        sbi = [
            str(entry.get("sbiCode"))
            for entry in (profile.get("sbiActiviteiten") or [])
            if isinstance(entry, dict) and entry.get("sbiCode")
        ]
        address = ((profile.get("_embedded") or {}).get("hoofdvestiging") or {}).get("adressen")
        employees = profile.get("totaalWerkzamePersonen")

        return identity_record(
            company,
            name=str(name),
            legal_id=number,
            legal_id_type="kvk",
            country="NL",
            source=PROFILE_URL.format(kvk=number),
            vat_number=None,
            sector_codes=sbi or None,
            size_fte=int(employees) if isinstance(employees, (int, float)) else None,
            locations=[{"kind": "hoofdvestiging", "address": address}] if address else None,
            business_summary=(
                f"Legal form: {profile.get('rechtsvorm')}"
                if profile.get("rechtsvorm")
                else None
            ),
        )

    # -- FR-246 -------------------------------------------------------------
    @staticmethod
    def group_links(profile: dict) -> list[SubsidiaryLink]:
        """Establishments listed under the legal person, as a group relation."""
        out: list[SubsidiaryLink] = []
        for entry in (profile.get("_embedded") or {}).get("vestigingen") or []:
            if isinstance(entry, dict) and entry.get("naam"):
                out.append(
                    SubsidiaryLink(
                        name=str(entry["naam"]),
                        legal_id=str(entry.get("vestigingsnummer") or "") or None,
                        relation="branch",
                    )
                )
        return out
