"""Arbeitnow adapter - a free public job-board API (FR-181, FR-261, IR-101).

Endpoint (keyless, documented by the site itself):

    GET https://www.arbeitnow.com/api/job-board-api?page=1
    -> {"data": [{"slug", "company_name", "title", "description" (HTML),
                  "remote", "url", "tags": [...], "job_types": [...],
                  "location", "created_at" (epoch seconds)}],
        "links": {"next": "...?page=2"},
        "meta": {"per_page": 250, "terms": "...", "info": "..."}}

The response states its own terms in ``meta.terms``:

    "This is a free public API for jobs, please do not abuse. I would
     appreciate linking back to the site. By using the API, you agree to the
     terms of service present on Arbeitnow.com"

IR-101 makes that binding, and three things in this adapter are that promise
kept rather than decoration:

* **Do not abuse.**  Only the one documented parameter (``page``) is sent, the
  walk is bounded by the plan item's page budget, and the host keeps the
  product's standard 0.5 req/s bucket.  Keyword filtering is done on the rows
  that came back, never by hammering the endpoint with query strings it does
  not document.
* **Link back.**  ``source_url`` and ``application_target`` are always the
  posting's page on arbeitnow.com, never a scraped-out employer URL, so
  anything the product shows a user carries the attribution back.
* **The terms travel with the data.**  They are recorded in ``legal_notes``,
  which is what the FR-185 source dashboard shows an administrator.

250 rows per request makes this the cheapest company-per-request source in the
catalogue - 1,632 companies over 3,300 jobs in 30 requests when it was measured
(docs/Data_Gathering_Plan.md 2.3) - and the postings are Europe-wide, not only
German: a 250-row page on 2026-09-09 resolved to 80 DE, 70 FR, 54 GB.

``llm_fallback`` is False: the payload is structured, so collection costs 0
tokens (plan 6.9).
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
    country_from_location,
    fte_percentage,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
    title_matches,
)

log = logging.getLogger(__name__)

API = "https://www.arbeitnow.com/api/job-board-api"
#: The service's own page size; it is not configurable.
PAGE_SIZE = 250
#: Ceiling on one plan item's walk, whatever the caps say. "Do not abuse" is a
#: term of service, so the bound is in the code and not only in the plan.
MAX_PAGES_PER_ITEM = 20


@register_adapter
class ArbeitnowAdapter(VacancySourceAdapter):
    key = "board.arbeitnow"
    display_name = "Arbeitnow"
    source_type = SourceType.JOB_BOARD
    access_method = AccessMethod.API
    coverage_countries: list[str] = []      # measured Europe-wide, DE/FR/GB heaviest
    tos_status = ToSStatus.PERMITTED
    rate_limit_rps = 0.5
    llm_fallback = False
    base_confidence = 0.8
    legal_notes = (
        "Free public job-board API. The service states its own terms in every "
        "response: 'This is a free public API for jobs, please do not abuse. I would "
        "appreciate linking back to the site. By using the API, you agree to the terms "
        "of service present on Arbeitnow.com.' Honoured by sending only the documented "
        "page parameter, by bounding the walk, and by keeping arbeitnow.com as the "
        "source and application URL of every stored record."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False,       # the API documents no search parameter
        location_filter=False,
        pagination=True,
        max_results_per_query=PAGE_SIZE,
    )

    # -- plan (FR-162, FR-186) ----------------------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        keywords = keywords_from(directives, composite_profile)
        pages = max(1, min(MAX_PAGES_PER_ITEM, int(caps.get("max_pages_per_source") or 2)))
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={
                    "page": page,
                    "pages": 1,
                    "results_per_page": PAGE_SIZE,
                    "keywords": keywords,
                    "title_filter": bool(caps.get("title_filter")),
                },
                rationale=(
                    "Arbeitnow publishes a free, keyless job-board API at 250 rows per "
                    f"request - the cheapest company-per-request source available. Page {page}"
                ),
                estimated_pages=1,
                estimated_seconds=4,
                caps={"max_records": PAGE_SIZE},
            )
            for page in range(1, pages + 1)
        ]

    # -- fetch (IR-102, FR-182, FR-185) -------------------------------------
    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        first = requested_page(query) or 1
        pages = max(1, min(MAX_PAGES_PER_ITEM, int(query.get("pages") or 1)))

        records: list[RawRecord] = []
        for page in range(first, first + pages):
            url = f"{API}?page={page}"
            result = await self._get(url, headers={"Accept": "application/json"})
            if result is None:
                break
            if result.text.lstrip()[:1] != "{":
                log.warning("[%s] %s did not answer JSON", self.key, url)
                self.fetch_outcome.ok -= 1
                self.fetch_outcome.failures.append((url, "not JSON"))
                break
            try:
                payload = json.loads(result.text)
            except ValueError:
                self.fetch_outcome.ok -= 1
                self.fetch_outcome.failures.append((url, "malformed JSON"))
                break
            rows = payload.get("data") or []
            if not rows:
                break
            records.append(
                RawRecord(
                    url=url,
                    content=result.text,
                    content_type="application/json",
                    raw_document_id=result.raw_document_id,
                    meta={
                        "page": page,
                        "keywords": query.get("keywords") or [],
                        "title_filter": bool(query.get("title_filter")),
                        "max_records": int(query.get("max_records") or PAGE_SIZE),
                    },
                )
            )
            if not (payload.get("links") or {}).get("next"):
                break
        return self.settle(
            records,
            nothing_to_fetch="no page was requested: the plan item names no page to read",
        )

    # -- parse (FR-183) -----------------------------------------------------
    def parse(self, raw: RawRecord) -> list[dict]:
        payload = json.loads(raw.content)
        rows = payload.get("data") or []
        keywords = raw.meta.get("keywords") or []
        filtering = bool(raw.meta.get("title_filter"))
        out: list[dict] = []
        for row in rows[: raw.meta.get("max_records") or len(rows)]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            if filtering and not title_matches(title, keywords):
                continue
            out.append(self._one(row))
        return out

    def _one(self, row: dict) -> dict[str, Any]:
        description = html_to_text(row.get("description"))
        location = str(row.get("location") or "").strip()
        job_types = [str(t) for t in (row.get("job_types") or []) if t]
        tags = [str(t) for t in (row.get("tags") or []) if t]
        # IR-101 "link back": the arbeitnow page is the source and the route to
        # apply, so attribution survives into every stored row.
        url = str(row.get("url") or "").strip()
        channel, target = application_route(url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(description[:6000])

        return {
            "company_name_raw": (row.get("company_name") or "").strip() or None,
            "title": str(row.get("title") or "").strip(),
            "function_family": tags[0] if tags else None,
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or None,
            "country": country_from_location(location),
            "work_arrangement": normalise_work_arrangement(
                "remote" if row.get("remote") else "", *job_types
            ) or normalise_work_arrangement(description[:2000]),
            # `job_types` is what the posting states; advert prose is only
            # consulted when it states nothing. Reading both at once turned a
            # "Full Time" posting into a fixed_term one because the body
            # mentioned an internship programme.
            "contract_type": normalise_contract_type(*job_types)
            or normalise_contract_type(description[:2000]),
            "fte_percentage": fte_percentage(*job_types),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(row.get("created_at")),
            "language": normalise_language(None, description),
            "application_channel": channel,
            "application_target": target,
            "source_url": url,
        }
