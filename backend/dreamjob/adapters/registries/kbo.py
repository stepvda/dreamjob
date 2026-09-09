"""KBO/BCE - the Belgian enterprise register (FR-241, FR-181, DR-101).

Belgium is the primary jurisdiction, and the KBO/BCE enterprise number is the
identity anchor everything else hangs from: the NBB Central Balance Sheet
Office is keyed on it, the VAT number is ``BE`` plus the same ten digits, and
DR-101 names it first in the key priority order.  Getting it right once means
the de-duplicator (FR-184) never has to guess again.

The register has no free JSON API, so two public sources are used in order:

1. **KBO Public Search** - the register's own page for an enterprise number.
   It carries everything DR-101 wants: legal name, legal form, status, seat
   address and NACE activities.
2. **VIES** - the European Commission's VAT validation service, which returns
   the registered name and address as JSON for any EU VAT number.  It is the
   fall-back when the KBO page is unreachable or robots.txt disallows it, and
   it independently corroborates the name.

Name search is supported the same way the register supports it - phonetic
search on the public site - and is deliberately not the primary path: a name
match is the weakest DR-101 key.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote

from dreamjob.adapters.base import AccessMethod, AdapterCapabilities, SourceType, register_adapter
from dreamjob.adapters.registries.common import (
    DEFAULT_YEARS,
    RegistryAdapter,
    RegistryResult,
    enterprise_number,
    format_enterprise_number,
    identity_record,
)
from dreamjob.egress.client import RobotsDisallowed
from dreamjob.pipeline.dedup import company_similarity

log = logging.getLogger(__name__)

KBO_PUBLIC_SEARCH = "https://kbopub.economie.fgov.be/kbopub/toonondernemingps.html"
KBO_NAME_SEARCH = "https://kbopub.economie.fgov.be/kbopub/zoeknaamfonetischform.html"
VIES_CHECK = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{number}"

#: The public search form is a GET form with paired checkbox markers: Spring
#: answers 404 when the ``_name=on`` half of a checkbox is missing, so the whole
#: set is sent exactly as the form posts it.
NAME_SEARCH_FORM = (
    "oudeBenaming=true&_oudeBenaming=on"
    "&ondNP=true&_ondNP=on"
    "&ondRP=true&_ondRP=on"
    "&vest=true&_vest=on"
    "&filterEnkelActieve=true&_filterEnkelActieve=on"
    "&actionNPRP=Search"
)

#: A name is the weakest DR-101 key, so a result row has to look like the
#: company that was asked for before its number is believed.
NAME_MATCH_FLOOR = 0.60

_ENTERPRISE_HREF = re.compile(r"ondernemingsnummer=(\d{9,10})")

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\xa0]+")

_STATUS_MAP = {
    "actief": "active", "active": "active", "actif": "active",
    "stopgezet": "ceased", "ceased": "ceased", "arrêté": "ceased", "arrete": "ceased",
}


def _text(markup: str) -> str:
    """The page as readable lines; the register lays its facts out as label/value rows."""
    try:
        from selectolax.parser import HTMLParser  # noqa: PLC0415 - optional at import time

        tree = HTMLParser(markup)
        for node in tree.css("script, style"):
            node.decompose()
        body = tree.body.text(separator="\n") if tree.body else tree.text(separator="\n")
    except ImportError:  # pragma: no cover - selectolax is a declared dependency
        body = _TAGS.sub("\n", markup)
    lines = [_WS.sub(" ", line).strip() for line in body.replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line)


def _labelled(text: str, *labels: str) -> str | None:
    """Value of a ``Label: value`` row, tolerating the value on the next line."""
    lines = text.split("\n")
    lowered = [line.lower() for line in lines]
    for index, line in enumerate(lowered):
        for label in labels:
            if not line.startswith(label.lower()):
                continue
            remainder = lines[index][len(label):].lstrip(" :\t")
            if remainder:
                return remainder.strip()
            if index + 1 < len(lines):
                return lines[index + 1].strip()
    return None


@register_adapter
class KBOAdapter(RegistryAdapter):
    """Belgian enterprise register: the DR-101 identity anchor."""

    key = "registry.kbo"
    display_name = "KBO/BCE (Belgian enterprise register)"
    source_type = SourceType.REGISTRY
    access_method = AccessMethod.HTTP
    coverage_countries = ["BE"]
    jurisdictions = ["BE"]
    capabilities = AdapterCapabilities(
        keyword_search=True,
        company_lookup=True,
        pagination=False,
        max_results_per_query=25,
    )
    rate_limit_rps = 0.5
    reporting_standard = "BE-GAAP"
    legal_notes = (
        "Public register, free consultation. Rate-limited to one request every two "
        "seconds and robots.txt honoured through the egress layer (FR-182)."
    )

    # -- registry contract --------------------------------------------------
    async def collect(
        self,
        company: dict,
        *,
        years: int = DEFAULT_YEARS,
        egress: Any = None,
        llm: Any = None,
    ) -> RegistryResult:
        """Identity only: the KBO holds no figures - those live at the NBB (FR-241)."""
        result = RegistryResult(adapter_key=self.key)
        async with self.session(egress) as client:
            number = enterprise_number(company)
            if number is None and company.get("name"):
                number = await self.search_by_name(str(company["name"]), egress=client)
            if number is None:
                result.note("No enterprise number could be resolved for this company")
                return result

            identity = await self.lookup(number, egress=client, company=company)
            if identity is None:
                identity = await self.vies_identity(number, egress=client, company=company)
        if identity is None:
            result.note(f"Enterprise {format_enterprise_number(number)} not found in the register")
            return result
        result.identity = identity
        return result

    # -- lookups ------------------------------------------------------------
    async def lookup(
        self, number: str, *, egress: Any, company: dict | None = None
    ) -> dict[str, Any] | None:
        """The register's own page for one enterprise number (DR-101)."""
        url = f"{KBO_PUBLIC_SEARCH}?lang=en&ondernemingsnummer={number}"
        try:
            response = await egress.fetch(url)
        except RobotsDisallowed:
            log.info("[%s] robots.txt disallows the public search; falling back to VIES", self.key)
            return None
        except Exception as exc:  # noqa: BLE001 - a registry outage is not a crash
            log.info("[%s] public search unavailable (%s)", self.key, exc)
            return None
        if not response.ok:
            return None
        return self.parse_company_page(response.text, number, company or {})

    def parse_company_page(self, markup: str, number: str, company: dict) -> dict[str, Any] | None:
        text = _text(markup)
        if "not found" in text.lower() and "enterprise" in text.lower()[:400]:
            return None
        name = _labelled(text, "Name:", "Naam:", "Nom:", "Denomination:")
        if not name:
            return None
        name = re.sub(r"\s*\b(Dutch|French|German|English)\b\s*$", "", name).strip()

        status_raw = (_labelled(text, "Status:", "Toestand:", "Statut:") or "").lower()
        legal_form = _labelled(text, "Legal form:", "Rechtsvorm:", "Forme légale:")
        address = _labelled(
            text,
            "Registered seat's address:",
            "Address of the seat:",
            "Adres van de zetel:",
            "Adres van de maatschappelijke zetel:",
            "Adresse du siège:",
            "Adresse du siège social:",
        )
        start_date = _labelled(text, "Start date:", "Begindatum:", "Date de début:")
        nace = sorted({code for code in re.findall(r"\b\d{2}\.\d{2,3}\b", text)})

        return identity_record(
            company,
            name=name,
            legal_id=number,
            legal_id_type="kbo_bce",
            country="BE",
            source=f"{KBO_PUBLIC_SEARCH}?ondernemingsnummer={number}",
            vat_number=f"BE{number}",
            sector_codes=nace or None,
            locations=[{"kind": "seat", "address": address}] if address else None,
            business_summary=self._summary(legal_form, status_raw, start_date),
            stage="nonprofit" if legal_form and "asbl" in legal_form.lower() else None,
            ownership=None,
        )

    @staticmethod
    def _summary(legal_form: str | None, status: str, start_date: str | None) -> str | None:
        parts = []
        if legal_form:
            parts.append(f"Legal form: {legal_form}")
        mapped = _STATUS_MAP.get(status.strip().lower())
        if mapped:
            parts.append(f"Register status: {mapped}")
        if start_date:
            parts.append(f"Registered since {start_date}")
        return "; ".join(parts) or None

    async def vies_identity(
        self, number: str, *, egress: Any, company: dict | None = None
    ) -> dict[str, Any] | None:
        """VAT-registry fall-back: registered name and address, as JSON."""
        url = VIES_CHECK.format(country="BE", number=number)
        try:
            response = await egress.fetch(url)
            payload = json.loads(response.text)
        except Exception as exc:  # noqa: BLE001 - optional corroboration only
            log.info("[%s] VIES unavailable (%s)", self.key, exc)
            return None
        if not isinstance(payload, dict) or not payload.get("isValid"):
            return None
        name = (payload.get("name") or "").strip()
        if not name or name == "---":
            return None
        address = (payload.get("address") or "").replace("\n", ", ").strip()
        return identity_record(
            company or {},
            name=name,
            legal_id=number,
            legal_id_type="kbo_bce",
            country="BE",
            source=url,
            vat_number=f"BE{number}",
            locations=[{"kind": "seat", "address": address}] if address else None,
            confidence=0.85,
        )

    async def search_by_name(self, name: str, *, egress: Any) -> str | None:
        """Phonetic name search - the weakest DR-101 key, so it is the last resort."""
        url = f"{KBO_NAME_SEARCH}?searchWord={quote(name)}&{NAME_SEARCH_FORM}"
        try:
            response = await egress.fetch(url)
        except Exception as exc:  # noqa: BLE001
            log.info("[%s] name search unavailable (%s)", self.key, exc)
            return None
        if not response.ok:
            log.info(
                "[%s] name search for %r returned HTTP %s", self.key, name, response.status_code
            )
            return None
        return self.pick_search_result(response.text, name)

    @staticmethod
    def parse_search_results(markup: str) -> list[dict[str, Any]]:
        """The result rows of the public search, as ``{kind, number, name}``.

        The result hrefs drop the leading zero (``ondernemingsnummer=473191041``
        for 0473.191.041), so a global ten-digit regex silently skips every
        classic Belgian enterprise and matches whatever else on the page happens
        to have ten digits.  The rows are read structurally instead, keeping the
        registered-entity/establishment-unit distinction the register draws.
        """
        try:
            from selectolax.parser import HTMLParser  # noqa: PLC0415 - optional at import time
        except ImportError:  # pragma: no cover - selectolax is a declared dependency
            return []
        out: list[dict[str, Any]] = []
        for row in HTMLParser(markup).css("tr"):
            cells = row.css("td")
            if len(cells) < 5:
                continue
            href = " ".join(
                (a.attributes or {}).get("href") or "" for a in row.css("a")
            )
            match = _ENTERPRISE_HREF.search(href)
            if not match:
                continue
            kind_text = _WS.sub(" ", cells[1].text(separator=" ")).strip()
            name_node = row.css_first("td.benaming")
            out.append(
                {
                    "kind": "ENT" if kind_text.upper().startswith("ENT") else "VE",
                    "status": "active" if "actief" in kind_text.lower() else "",
                    "number": match.group(1).zfill(10),
                    "name": _WS.sub(" ", (name_node.text() if name_node else "")).strip(),
                }
            )
        return out

    @classmethod
    def pick_search_result(cls, markup: str, wanted: str) -> str | None:
        """The enterprise number of the row that is actually this company.

        The register answers a phonetic search, so the first row is frequently
        a different company with a similar-sounding name; taking it would anchor
        DR-101 on the wrong legal entity for good.
        """
        scored = [
            (row["kind"] == "ENT", company_similarity(wanted, row["name"]), row)
            for row in cls.parse_search_results(markup)
        ]
        scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        for is_entity, score, row in scored:
            if score >= NAME_MATCH_FLOOR:
                log.info(
                    "[registry.kbo] %r resolved to %s (%s, name score %.2f)",
                    wanted, format_enterprise_number(row["number"]),
                    "entity" if is_entity else "establishment unit", score,
                )
                return row["number"]
        log.info("[registry.kbo] no register row matched %r closely enough", wanted)
        return None
