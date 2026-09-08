"""Recruitee job-board adapter (FR-181, FR-261, IR-102).

Endpoint (public "Careers site offers" API, keyless):

    GET https://{slug}.recruitee.com/api/offers/

The response is ``{"offers": [...]}`` for the whole board.  Recruitee splits an
advert into ``description`` and ``requirements`` and states the work
arrangement as three booleans (``remote``, ``hybrid``, ``on_site``), which maps
directly onto the ``work_arrangement`` column.  Weekly hours give the FTE
percentage without guessing.
"""

from __future__ import annotations

import json
from typing import Any

from dreamjob.adapters.ats.common import ATSAdapter
from dreamjob.adapters.base import PlanItem, RawRecord, register_adapter
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    extract_skills,
    fte_from_hours,
    fte_percentage,
    html_to_text,
    normalise_contract_type,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    split_skills,
    title_matches,
)

API = "https://{slug}.recruitee.com/api/offers/"


@register_adapter
class RecruiteeAdapter(ATSAdapter):
    key = "ats.recruitee"
    display_name = "Recruitee"
    vendor = "recruitee"
    coverage_countries = ["NL", "BE", "DE", "FR", "GB", "PL"]
    legal_notes = "Public careers-site offers API, no key required; read-only."

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        slug = self.slug_of(item)
        url = API.format(slug=slug)
        result = await self._get(url)
        if result is None:
            return []
        return [
            RawRecord(
                url=url,
                content=result.text,
                content_type="application/json",
                raw_document_id=result.raw_document_id,
                meta={
                    "slug": slug,
                    "company_id": item.native_query.get("company_id"),
                    "company_name": item.native_query.get("company_name"),
                    "max_records": self.limit_of(item),
                    "keywords": item.native_query.get("keywords") or [],
                    "title_filter": item.native_query.get("title_filter"),
                },
            )
        ]

    def parse(self, raw: RawRecord) -> list[dict]:
        payload = json.loads(raw.content)
        offers = [o for o in payload.get("offers") or [] if o.get("status", "published") != "draft"]
        keywords = raw.meta.get("keywords") or []
        filtering = bool(raw.meta.get("title_filter"))
        out: list[dict] = []
        for offer in offers[: raw.meta.get("max_records") or len(offers)]:
            if filtering and not title_matches(str(offer.get("title") or ""), keywords):
                continue
            out.append(self._one(offer, raw))
        return out

    def _one(self, offer: dict, raw: RawRecord) -> dict[str, Any]:
        body = html_to_text(offer.get("description"))
        requirements = html_to_text(offer.get("requirements"))
        description = "\n\n".join(p for p in (body, requirements) if p)
        location = str(offer.get("location") or offer.get("city") or "")
        apply_url = offer.get("careers_apply_url") or offer.get("careers_url") or ""
        channel, target = application_route(apply_url, description)
        if not target and offer.get("mailbox_email"):
            channel, target = "email", str(offer["mailbox_email"])

        required, desirable = split_skills(description)
        if requirements:
            stated = extract_skills(requirements)
            required = stated or required
            desirable = [s for s in desirable if s not in required]

        salary = offer.get("salary") or {}
        salary_min = salary.get("min")
        salary_max = salary.get("max")
        currency = salary.get("currency")
        if salary_min is None and salary_max is None:
            salary_min, salary_max, currency = parse_salary_text(description[:6000])

        arrangement = normalise_work_arrangement(
            "hybrid" if offer.get("hybrid") else "",
            "remote" if offer.get("remote") else "",
            "onsite" if offer.get("on_site") else "",
            location,
            description[:2000],
        )
        hours = offer.get("max_hours_per_week") or offer.get("max_hours")
        return {
            "company_id": raw.meta.get("company_id"),
            "company_name_raw": offer.get("company_name") or raw.meta.get("company_name"),
            "title": offer.get("title"),
            "function_family": offer.get("department") or offer.get("category_code"),
            "seniority": offer.get("experience_code"),
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or None,
            "country": country_from_location(offer.get("country_code"), offer.get("country")),
            "work_arrangement": arrangement,
            "contract_type": normalise_contract_type(
                offer.get("employment_type_code"), description[:2000]
            ),
            "fte_percentage": fte_from_hours(hours) or fte_percentage(
                offer.get("employment_type_code"), description[:1500]
            ),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(offer.get("published_at") or offer.get("created_at")),
            "application_channel": channel,
            "application_target": target,
            "source_url": offer.get("careers_url") or apply_url,
        }
