"""EURES adapter - the European Commission's job mobility portal (FR-181, FR-183).

EURES aggregates the vacancies of the national public employment services of
the EU/EEA, which makes it the one genuinely open, pan-European vacancy source
in this package: no key, no account, public-sector data.

Endpoint shape implemented (the portal's own JSON search service):

    POST {api_base}/jv-search/search?lang=en
    {"keywords": [{"keywordType": "freetext", "keywordValue": "data engineer"}],
     "locationCodes": ["BE"], "positionScheduleCodes": [], "occupationUris": [],
     "skillUris": [], "positionOfferingCodes": [], "euresFlagCodes": [],
     "otherBenefitsCodes": [], "requiredExperienceCodes": [], "requiredLanguages": [],
     "publicationPeriod": null, "minNumberPost": null, "sortSearch": "BEST_MATCH",
     "resultsPerPage": 50, "page": 1, "sessionId": ""}

    GET  {api_base}/jv-details/{id}?lang=en

The reply is read tolerantly - the list of vacancies has appeared under
``jvs``, ``records`` and ``content`` across portal versions, and detail fields
under ``jvDetails`` or at the top level - and ``api_base`` itself lives in
``native_query`` so an administrator can follow a portal migration without a
code change (NFR-601).  When the endpoint answers with something that is not
JSON (the portal front end was rebuilt in 2025 and the search path moved), the
plan item collects nothing and logs the reason instead of failing the campaign.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dreamjob.adapters.base import (
    AccessMethod,
    AdapterCapabilities,
    PlanItem,
    RawRecord,
    SourceType,
    ToSStatus,
    register_adapter,
)
from dreamjob.adapters.vacancy_source import (
    VacancySourceAdapter,
    application_route,
    countries_from,
    country_from_location,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
)
from dreamjob.egress.client import FetchResult, RobotsDisallowed

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://europa.eu/eures/eures-apps/searchengine/page"
PUBLIC_DETAILS = "https://europa.eu/eures/portal/jv-se/jv-details/{jv_id}?lang={lang}"
PAGE_SIZE = 50


@register_adapter
class EuresAdapter(VacancySourceAdapter):
    key = "board.eures"
    display_name = "EURES (European Job Mobility Portal)"
    source_type = SourceType.JOB_BOARD
    access_method = AccessMethod.API
    coverage_countries = [
        "BE", "NL", "FR", "DE", "LU", "IE", "ES", "IT", "PT", "AT", "DK", "SE", "FI",
        "NO", "PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "EE", "LV", "LT", "GR",
        "CY", "MT", "IS", "LI",
    ]
    tos_status = ToSStatus.PERMITTED
    rate_limit_rps = 0.5
    legal_notes = (
        "Public-sector portal of the European Commission aggregating national public "
        "employment services; reuse of public-sector information is permitted."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=True, contract_type_filter=True,
        pagination=True, max_results_per_query=200,
    )
    base_confidence = 0.8

    # -- plan (FR-162, FR-164) ---------------------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        keywords = keywords_from(directives, composite_profile)
        countries = [c for c in countries_from(directives) if self.covers_country(c)]
        pages = max(1, int(caps.get("max_pages_per_source") or 2))
        items: list[PlanItem] = []
        for keyword in keywords or [""]:
            items.append(
                PlanItem(
                    adapter_key=self.key,
                    native_query={
                        "keyword": keyword,
                        "country_codes": countries or ["BE"],
                        "pages": pages,
                        "results_per_page": PAGE_SIZE,
                        "language": directives.get("language") or "en",
                        "fetch_details": True,
                    },
                    rationale=(
                        "EURES aggregates the national public employment services; open "
                        f"data, no key. Query '{keyword or 'all'}' in "
                        f"{', '.join(countries) or 'BE'}"
                    ),
                    estimated_pages=pages,
                    estimated_seconds=15 * pages,
                    caps={"max_records": pages * PAGE_SIZE},
                )
            )
        return items

    # -- fetch (IR-102) -----------------------------------------------------
    async def _request(self, url: str, **kwargs: Any) -> FetchResult | None:
        if self.egress is None:
            raise RuntimeError(f"[{self.key}] fetch() needs an EgressClient (IR-102)")
        try:
            result = await self.egress.fetch(url, access_method="api", **kwargs)
        except RobotsDisallowed:
            log.warning("[%s] robots.txt disallows %s (FR-182)", self.key, url)
            return None
        except Exception as exc:  # noqa: BLE001 - a portal migration must not fail a campaign
            log.warning("[%s] request failed for %s: %s", self.key, url, exc)
            return None
        if not result.ok:
            log.info("[%s] %s returned HTTP %s", self.key, url, result.status_code)
            return None
        if result.text.lstrip()[:1] not in ("{", "["):
            log.warning(
                "[%s] %s did not answer JSON; set native_query['api_base'] to the current "
                "search endpoint (NFR-601)", self.key, url,
            )
            return None
        return result

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = item.native_query
        api_base = str(query.get("api_base") or DEFAULT_API_BASE).rstrip("/")
        lang = str(query.get("language") or "en")
        pages = max(1, int(query.get("pages") or 1))
        per_page = int(query.get("results_per_page") or PAGE_SIZE)
        keyword = str(query.get("keyword") or "")

        one_page = requested_page(query)
        page_numbers = [one_page] if one_page else list(range(1, pages + 1))

        records: list[RawRecord] = []
        summaries: list[dict] = []
        for page in page_numbers:
            url = f"{api_base}/jv-search/search?lang={lang}"
            listing = await self._request(
                url,
                method="POST",
                json=self.search_body(keyword, query.get("country_codes") or [], page, per_page),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if listing is None:
                break
            payload = json.loads(listing.text)
            page_items = self._results(payload)
            if not page_items:
                break
            summaries.extend(page_items)
            records.append(
                RawRecord(
                    url=f"{url}&page={page}",
                    content=listing.text,
                    content_type="application/json",
                    raw_document_id=listing.raw_document_id,
                    meta={"kind": "list", "lang": lang, "emit": True},
                )
            )
            if len(page_items) < per_page:
                break

        if query.get("fetch_details") is False:
            return records

        detailed: list[str] = []
        for summary in summaries:
            jv_id = self._entry_id(summary)
            if not jv_id:
                continue
            detail_url = f"{api_base}/jv-details/{jv_id}?lang={lang}"
            detail = await self._request(detail_url, headers={"Accept": "application/json"})
            if detail is None:
                continue
            records.append(
                RawRecord(
                    url=PUBLIC_DETAILS.format(jv_id=jv_id, lang=lang),
                    content=detail.text,
                    content_type="application/json",
                    raw_document_id=detail.raw_document_id,
                    meta={"kind": "detail", "lang": lang, "summary": summary},
                )
            )
            detailed.append(str(jv_id))
        # Every listing page must skip the ids that were detailed, not just the
        # first one, or a vacancy from page two arrives twice.
        for record in records:
            if record.meta.get("kind") == "list":
                record.meta["detailed_ids"] = detailed
        return records

    @staticmethod
    def search_body(
        keyword: str, country_codes: list[str], page: int, per_page: int
    ) -> dict[str, Any]:
        return {
            "keywords": (
                [{"keywordType": "freetext", "keywordValue": keyword}] if keyword else []
            ),
            "publicationPeriod": None,
            "occupationUris": [],
            "skillUris": [],
            "positionScheduleCodes": [],
            "locationCodes": list(country_codes),
            "positionOfferingCodes": [],
            "euresFlagCodes": [],
            "otherBenefitsCodes": [],
            "requiredExperienceCodes": [],
            "requiredLanguages": [],
            "minNumberPost": None,
            "sortSearch": "BEST_MATCH",
            "resultsPerPage": per_page,
            "page": page,
            "sessionId": "",
        }

    @staticmethod
    def _entry_id(entry: dict) -> str:
        """The vacancy id, under whichever key this portal version uses."""
        for key in ("id", "jvId", "reference"):
            value = entry.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    @staticmethod
    def _results(payload: Any) -> list[dict]:
        """The vacancy list has moved between keys across portal versions."""
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("jvs", "records", "content", "results", "jobVacancies", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [v for v in value if isinstance(v, dict)]
            if isinstance(value, dict):
                return EuresAdapter._results(value)
        return []

    # -- parse (FR-183) -----------------------------------------------------
    def parse(self, raw: RawRecord) -> list[dict]:
        payload = json.loads(raw.content)
        lang = raw.meta.get("lang") or "en"
        if raw.meta.get("kind") == "detail":
            summary = raw.meta.get("summary") or {}
            details = payload.get("jvDetails") if isinstance(payload, dict) else None
            return [self._one({**summary, **(details or payload or {})}, raw, lang)]
        skip = set(raw.meta.get("detailed_ids") or [])
        return [
            self._one(entry, raw, lang)
            for entry in self._results(payload)
            if self._entry_id(entry) not in skip
        ]

    def _one(self, entry: dict, raw: RawRecord, lang: str) -> dict[str, Any]:
        description = html_to_text(
            entry.get("description")
            or entry.get("jvDescription")
            or entry.get("descriptionShort")
            or ""
        )
        employer = entry.get("employer") or {}
        employer_name = (
            employer.get("name") if isinstance(employer, dict) else str(employer or "")
        ) or entry.get("employerName")

        locations = entry.get("location") or entry.get("locationMap") or []
        if isinstance(locations, dict):
            locations = [locations]
        first_location = locations[0] if locations else {}
        city = first_location.get("cityName") or first_location.get("city") or ""
        country_code = first_location.get("countryCode") or entry.get("countryCode")

        schedule = " ".join(
            str(s) for s in (
                entry.get("positionScheduleCodes"), entry.get("positionOfferingCode"),
                entry.get("contractType"),
            ) if s
        )
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(description[:6000])
        jv_id = entry.get("id") or entry.get("jvId")
        source_url = entry.get("jvUrl") or PUBLIC_DETAILS.format(jv_id=jv_id, lang=lang)
        apply_url = entry.get("applyUrl") or entry.get("externalUrl") or source_url
        channel, target = application_route(apply_url, description)
        return {
            "company_name_raw": employer_name or None,
            "title": entry.get("title") or entry.get("jvTitle"),
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": ", ".join(p for p in (city, country_code or "") if p) or None,
            "country": country_from_location(country_code, city),
            "work_arrangement": normalise_work_arrangement(description[:2000], city),
            "contract_type": normalise_contract_type(schedule, description[:2000]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(
                entry.get("creationDate")
                or entry.get("publicationDate")
                or entry.get("lastModificationDate")
            ),
            "application_channel": channel,
            "application_target": target,
            "source_url": source_url,
            "language": normalise_language(
                entry.get("jvLanguage") or entry.get("language"), description
            ),
        }
