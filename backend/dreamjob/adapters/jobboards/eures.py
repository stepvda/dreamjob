"""EURES adapter - the European Commission's job mobility portal (FR-181, FR-183).

EURES aggregates the vacancies of the national public employment services of
the EU/EEA, which makes it the one genuinely open, pan-European vacancy source
in this package: no key, no account, public-sector data.

Endpoint shape implemented (the portal's own JSON search service, read out of
the portal bundle it is served with):

    POST {api_base}/jv-search/search?lang=en
    {"keywords": [{"keyword": "data engineer", "specificSearchCode": "EVERYWHERE"}],
     "locationCodes": ["BE"], "resultsPerPage": 50, "page": 1,
     "sortSearch": "BEST_MATCH"}
    -> {"numberRecords": n, "jvs": [{"id", "title", "description",
        "creationDate" (epoch ms), "locationMap": {"BE": ["BE32B"]},
        "employer": {...}|null, ...}], "facets": [...]}

    GET  {api_base}/jv/id/{id}?requestLang=en&preferredLang=en
    -> {"id", "reference", "source", "creationDate", "preferredLanguage",
        "jvProfiles": {"<lang>": {"title", "description", "locations": [...],
        "employer": {...}, "personContacts": [...], ...}}}

The portal was rebuilt as a single-page application and the old
``/eures/eures-apps/searchengine/page`` service was withdrawn: every search
against it answered 404 with an HTML stub, which the adapter recorded as
"nothing found".  ``api_base`` lives in ``native_query`` so an administrator can
follow the next migration without a code change (NFR-601), and a search that
cannot be reached now fails its plan item instead of reporting zero vacancies
(FR-185).

Detail pages are opt-in (``fetch_details``): one listing page carries fifty
summaries and each summary already holds the full advert text, so fetching a
detail per summary spends fifty rate-limited requests to add an employer name.

Measured against the live service on 2026-09-09 with this product's own
User-Agent (docs/Data_Gathering_Plan.md C1/C2, appendix C):

* ``locationCodes: ["BE"]`` answers HTTP 200 with ``numberRecords`` 232,496 and
  fifty summaries carrying ``employer.name`` and a median 1.1-2.3 kB advert;
* ``resultsPerPage`` above fifty is refused - 100 and 200 both answer HTTP 400
  ``{"errorMessage": "Too many results per page were requested"}`` - so a plan
  item that asks for more is clamped here rather than spent on a 400;
* the partition filters the sweep in FR-186 needs are honoured:
  ``locationCodes: ["BE1"]`` (NUTS-1) with ``publicationPeriod: "LAST_WEEK"``
  narrows 232,496 to 1,593, and adding ``sectorCodes: ["N"]`` (NACE section N,
  staffing agencies) narrows it further to 482.

europa.eu's robots.txt permits ``/eures/`` and states ``Crawl-delay: 10`` for
``User-agent: *``; volume therefore comes from partitioning the query, never
from asking this host for more per request (FR-182, plan section 6.4).
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
    country_terms,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    query_terms,
    requested_page,
    split_skills,
)
from dreamjob.egress.client import FetchResult

log = logging.getLogger(__name__)

#: The current service base, read from the portal bundle
#: (``jvseBaseUrl = "/eures/api/jv-searchengine"`` + ``/public``).
DEFAULT_API_BASE = "https://europa.eu/eures/api/jv-searchengine/public"
PUBLIC_DETAILS = "https://europa.eu/eures/portal/jv-se/jv-details/{jv_id}?lang={lang}"
#: The most results one search will return.  This is the service's ceiling, not
#: a policy choice: 100 and 200 are both refused with HTTP 400 ("Too many
#: results per page were requested"), and a refused page is a page of vacancies
#: not collected, so every caller's request is clamped to it.
PAGE_SIZE = 50
#: Detail pages per plan item when they are asked for at all.
MAX_DETAIL_PAGES = 10
#: The ``publicationPeriod`` values the service accepts, measured on BE1:
#: LAST_DAY 191 rows, LAST_WEEK 1,593, LAST_MONTH 4,651.  Anything else is
#: HTTP 400 ``{"key": "invalid-json"}``, so an unrecognised value is warned
#: about and still sent - the failed plan item is the honest report, and a
#: portal that adds a period later must not need a code change (NFR-601).
PUBLICATION_PERIODS = frozenset({"LAST_DAY", "LAST_WEEK", "LAST_MONTH"})


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
        # One search answers at most PAGE_SIZE rows (200 is an HTTP 400).  The
        # figure feeds the planner's page arithmetic and FR-342 reuse accounting,
        # both of which counted twice as many rows per page as EURES returns.
        pagination=True, max_results_per_query=PAGE_SIZE,
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
                        "fetch_details": False,
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

    # -- fetch (IR-102, FR-182) ---------------------------------------------
    async def _request(self, url: str, **kwargs: Any) -> FetchResult | None:
        """One counted request that must answer JSON (IR-102, FR-185)."""
        result = await self._get(url, **kwargs)
        if result is None:
            return None
        if result.text.lstrip()[:1] not in ("{", "["):
            log.warning(
                "[%s] %s did not answer JSON; set native_query['api_base'] to the current "
                "search endpoint (NFR-601)", self.key, url,
            )
            self.fetch_outcome.ok -= 1
            self.fetch_outcome.failures.append((url, "not JSON"))
            return None
        return result

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        api_base = str(query.get("api_base") or DEFAULT_API_BASE).rstrip("/")
        lang = str(query.get("language") or "en")
        pages = max(1, int(query.get("pages") or 1))
        per_page = self.page_size(query)
        keywords = query_terms(query) or [""]
        countries = self.locations_of(query)
        period = self.publication_period(query)
        sectors = self.sector_codes(query)

        one_page = requested_page(query)
        page_numbers = [one_page] if one_page else list(range(1, pages + 1))

        records: list[RawRecord] = []
        summaries: list[dict] = []
        for page in page_numbers:
            url = f"{api_base}/jv-search/search?lang={lang}"
            listing = await self._request(
                url,
                method="POST",
                json=self.search_body(
                    keywords[0], countries, page, per_page,
                    publication_period=period, sector_codes=sectors,
                ),
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

        # FR-186: details are opt-in.  The summary already carries the advert
        # text, so a detail adds only the employer, the city and the apply URL -
        # at one rate-limited request each, fifty per listing page.
        if not query.get("fetch_details"):
            return self.settle(records, nothing_to_fetch=self._nothing_to_fetch())

        detailed: list[str] = []
        budget = int(query.get("max_detail_pages") or MAX_DETAIL_PAGES)
        for summary in summaries[:budget]:
            jv_id = self._entry_id(summary)
            if not jv_id:
                continue
            detail_url = self.detail_url(api_base, jv_id, lang)
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
        return self.settle(records, nothing_to_fetch=self._nothing_to_fetch())

    def _nothing_to_fetch(self) -> str:
        return "no search was issued: the plan item names neither a keyword nor a country"

    # -- what a plan item may ask for (FR-162, FR-186) ----------------------
    @staticmethod
    def page_size(query: dict[str, Any]) -> int:
        """Results per search, never above the service's own ceiling.

        A plan item written against ``capabilities.max_results_per_query`` used
        to ask for 200, which the service refuses with HTTP 400 - one refused
        request per page, and a plan item reported as unavailable rather than
        as fifty vacancies.
        """
        try:
            requested = int(query.get("results_per_page") or PAGE_SIZE)
        except (TypeError, ValueError):
            requested = PAGE_SIZE
        return max(1, min(requested, PAGE_SIZE))

    @staticmethod
    def locations_of(query: dict[str, Any]) -> list[str]:
        """``locationCodes``: NUTS regions when the item partitions by region.

        A NUTS code names a region inside a country (``BE1`` is Brussels), so a
        partitioned item states the regions and nothing else - sending both
        ``BE`` and ``BE1`` would ask for the country as well as the region and
        undo the partition.
        """
        regions: list[str] = []
        for key in ("nuts_codes", "nuts", "region_codes", "regions"):
            raw = query.get(key)
            if isinstance(raw, str):
                raw = [raw]
            for entry in raw or []:
                code = str(entry).strip().upper()
                if len(code) >= 3 and code[:2].isalpha() and code.isalnum():
                    if code not in regions:
                        regions.append(code)
        return regions or country_terms(query)

    @classmethod
    def publication_period(cls, query: dict[str, Any]) -> str | None:
        """``publicationPeriod``: LAST_DAY | LAST_WEEK | LAST_MONTH (or none)."""
        value = str(query.get("publication_period") or query.get("publicationPeriod") or "").strip()
        period = value.upper() or None
        if period and period not in PUBLICATION_PERIODS:
            log.warning(
                "[%s] publicationPeriod %r is not one of %s; the service answers HTTP 400 "
                "to an unknown period and the plan item will be reported as failed",
                cls.key, period, ", ".join(sorted(PUBLICATION_PERIODS)),
            )
        return period

    @staticmethod
    def sector_codes(query: dict[str, Any]) -> list[str]:
        """``sectorCodes``: NACE sections, the second axis of a partition."""
        raw = query.get("sector_codes") or query.get("sectorCodes") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(code).strip().upper() for code in raw if str(code).strip()]

    @staticmethod
    def detail_url(api_base: str, jv_id: str, lang: str) -> str:
        """The current detail route (portal bundle: ``getJvById``)."""
        return f"{api_base}/jv/id/{jv_id}?requestLang={lang}&preferredLang={lang}"

    @staticmethod
    def search_body(
        keyword: str,
        country_codes: list[str],
        page: int,
        per_page: int,
        *,
        publication_period: str | None = None,
        sector_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """The body the current search service accepts.

        The retired ``{"keywordType": "freetext", "keywordValue": ...}`` shape is
        rejected with ``400 {"key": "invalid-json"}`` by the service that
        replaced it; ``specificSearchCode`` names the field to search
        (EVERYWHERE | TITLE | DESCRIPTION | EMPLOYER | LEGAL_ID |
        JOB_VACANCY_ID).

        ``publicationPeriod`` and ``sectorCodes`` are written only when the plan
        item states them, so an unpartitioned item sends exactly the body that
        was measured answering HTTP 200.  Both are honoured by the service:
        BE1 + LAST_WEEK is 1,593 of the country's 232,496 rows, and adding
        NACE section N leaves 482.
        """
        body: dict[str, Any] = {
            "keywords": (
                [{"keyword": keyword, "specificSearchCode": "EVERYWHERE"}] if keyword else []
            ),
            "locationCodes": list(country_codes),
            "sortSearch": "BEST_MATCH",
            # The clamp is repeated here because this is a public entry point:
            # anything above PAGE_SIZE is an HTTP 400, not a bigger page.
            "resultsPerPage": max(1, min(int(per_page or PAGE_SIZE), PAGE_SIZE)),
            "page": page,
        }
        if publication_period:
            body["publicationPeriod"] = str(publication_period).upper()
        if sector_codes:
            body["sectorCodes"] = [str(code).upper() for code in sector_codes]
        return body

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
            return [self._one({**summary, **self._profile(payload, lang)}, raw, lang)]
        skip = set(raw.meta.get("detailed_ids") or [])
        return [
            self._one(entry, raw, lang)
            for entry in self._results(payload)
            if self._entry_id(entry) not in skip
        ]

    @staticmethod
    def _profile(payload: Any, lang: str) -> dict[str, Any]:
        """The language profile of a detail document.

        The real document nests everything one level down, under
        ``jvProfiles.<language>`` - reading the top level (or the ``jvDetails``
        key an older portal version used) found the id and the timestamps and
        none of the title, description, employer or locations.
        """
        if not isinstance(payload, dict):
            return {}
        profiles = payload.get("jvProfiles")
        if isinstance(profiles, dict) and profiles:
            preferred = str(payload.get("preferredLanguage") or lang)
            profile = profiles.get(preferred) or profiles.get(lang)
            if not isinstance(profile, dict):
                profile = next((p for p in profiles.values() if isinstance(p, dict)), {})
            return {
                **{k: v for k, v in payload.items() if k != "jvProfiles"},
                **profile,
            }
        details = payload.get("jvDetails")
        return dict(details) if isinstance(details, dict) else dict(payload)

    @staticmethod
    def _place(entry: dict) -> tuple[str, str | None]:
        """``(city, country_code)`` from either the summary or the detail shape.

        A search summary carries ``locationMap`` - ``{"BE": ["BE32B"]}``, a
        country-to-NUTS-region mapping, *not* an address - so reading
        ``cityName`` off it left every EURES vacancy with a NULL location and a
        NULL country.  The detail document is the one that carries an address.
        """
        locations = entry.get("locations") or entry.get("location") or []
        if isinstance(locations, dict):
            locations = [locations]
        for place in locations:
            if not isinstance(place, dict):
                continue
            city = str(place.get("cityName") or place.get("city") or "").strip()
            country = str(place.get("countryCode") or "").strip().upper() or None
            if city or country:
                return city, country
        location_map = entry.get("locationMap")
        if isinstance(location_map, dict):
            for code in location_map:
                if isinstance(code, str) and len(code) == 2:
                    return "", code.upper()
        country = entry.get("countryCode")
        return "", str(country).upper() if country else None

    @staticmethod
    def _apply_target(entry: dict) -> str | None:
        """The application URL or address a detail document states, if any."""
        for contact in entry.get("personContacts") or []:
            communications = (contact or {}).get("communications") or {}
            for profile in communications.get("webProfiles") or []:
                uri = str((profile or {}).get("uri") or "").strip()
                if uri:
                    return uri
            for email in communications.get("emails") or []:
                address = email if isinstance(email, str) else (email or {}).get("address")
                if address:
                    return f"mailto:{address}"
        return None

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

        city, country_code = self._place(entry)

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
        apply_url = (
            entry.get("applyUrl")
            or entry.get("externalUrl")
            or self._apply_target(entry)
            or source_url
        )
        channel, target = application_route(apply_url, description)
        return {
            # A national employment service often publishes without naming the
            # employer.  FR-261 records what is stated, so an absent employer
            # stays absent rather than becoming the name of the portal that
            # relayed it.
            "company_name_raw": (str(employer_name).strip() or None) if employer_name else None,
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
                entry.get("languageVersion")
                or entry.get("jvLanguage")
                or entry.get("language"),
                description,
            ),
        }
