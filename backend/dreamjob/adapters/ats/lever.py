"""Lever job-board adapter (FR-181, FR-261, IR-102).

Endpoint (public, keyless "Postings API"):

    GET https://api.lever.co/v0/postings/{slug}?mode=json&limit=100&skip=0

The response is a bare JSON array of postings.  ``limit``/``skip`` paginate;
a short page ends the walk.  The advert is split over ``descriptionPlain``,
a list of titled ``lists`` blocks (responsibilities, requirements, benefits)
and ``additionalPlain``, so the three are reassembled into one description.
"""

from __future__ import annotations

import json
from typing import Any

from dreamjob.adapters.ats.common import ATSAdapter
from dreamjob.adapters.base import AdapterCapabilities, PlanItem, RawRecord, register_adapter
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    fte_percentage,
    html_to_text,
    normalise_contract_type,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
    title_matches,
)

API = "https://api.lever.co/v0/postings/{slug}?mode=json&limit={limit}&skip={skip}"
PAGE_SIZE = 100


@register_adapter
class LeverAdapter(ATSAdapter):
    key = "ats.lever"
    display_name = "Lever"
    vendor = "lever"
    legal_notes = "Public postings API, no key required; read-only."
    capabilities = AdapterCapabilities(
        keyword_search=False, company_lookup=True, pagination=True, max_results_per_query=500
    )

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        slug = self.slug_of(item)
        limit = self.limit_of(item)
        out: list[RawRecord] = []
        one_page = requested_page(item.native_query)
        start = (one_page - 1) * PAGE_SIZE if one_page else 0
        budget = min(limit, PAGE_SIZE) if one_page else limit
        skip = start
        while skip - start < budget:
            page_size = min(PAGE_SIZE, budget - (skip - start))
            url = API.format(slug=slug, limit=page_size, skip=skip)
            result = await self._get(url)
            if result is None:
                break
            try:
                postings = json.loads(result.text)
            except ValueError:
                break
            if not isinstance(postings, list) or not postings:
                break
            out.append(
                RawRecord(
                    url=url,
                    content=result.text,
                    content_type="application/json",
                    raw_document_id=result.raw_document_id,
                    meta={
                        "slug": slug,
                        "company_id": item.native_query.get("company_id"),
                        "company_name": item.native_query.get("company_name"),
                        "keywords": item.native_query.get("keywords") or [],
                        "title_filter": item.native_query.get("title_filter"),
                    },
                )
            )
            if len(postings) < page_size:
                break
            skip += page_size
        return out

    def parse(self, raw: RawRecord) -> list[dict]:
        postings = json.loads(raw.content)
        keywords = raw.meta.get("keywords") or []
        filtering = bool(raw.meta.get("title_filter"))
        out: list[dict] = []
        for posting in postings:
            title = str(posting.get("text") or "").strip()
            if filtering and not title_matches(title, keywords):
                continue
            out.append(self._one(posting, raw))
        return out

    @staticmethod
    def _description(posting: dict) -> str:
        parts = [posting.get("descriptionPlain") or html_to_text(posting.get("description"))]
        for block in posting.get("lists") or []:
            heading = str(block.get("text") or "").strip()
            body = html_to_text(block.get("content"))
            parts.append(f"{heading}\n{body}" if heading else body)
        parts.append(posting.get("additionalPlain") or html_to_text(posting.get("additional")))
        return "\n\n".join(p for p in parts if p).strip()

    def _one(self, posting: dict, raw: RawRecord) -> dict[str, Any]:
        categories = posting.get("categories") or {}
        description = self._description(posting)
        location = str(categories.get("location") or "")
        all_locations = ", ".join(str(x) for x in categories.get("allLocations") or [])
        commitment = str(categories.get("commitment") or "")
        workplace = str(posting.get("workplaceType") or "")
        apply_url = posting.get("applyUrl") or posting.get("hostedUrl") or ""
        channel, target = application_route(apply_url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(description[:6000])
        return {
            "company_id": raw.meta.get("company_id"),
            "company_name_raw": raw.meta.get("company_name") or raw.meta.get("slug"),
            "title": posting.get("text"),
            "function_family": categories.get("department") or categories.get("team"),
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or all_locations or None,
            "country": country_from_location(posting.get("country"), location, all_locations),
            "work_arrangement": normalise_work_arrangement(
                workplace, location, description[:2000]
            ),
            "contract_type": normalise_contract_type(commitment, description[:2000]),
            "fte_percentage": fte_percentage(commitment, description[:1500]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(posting.get("createdAt")),
            "application_channel": channel,
            "application_target": target,
            "source_url": posting.get("hostedUrl") or apply_url,
        }
