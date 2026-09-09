"""Greenhouse job-board adapter (FR-181, FR-261, IR-102).

Endpoint (public, keyless, documented as the "Job Board API"):

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

The response is ``{"jobs": [...], "meta": {"total": n}}`` and returns the whole
board in one call, so there is no pagination.  ``content`` holds the advert as
HTML that has itself been HTML-escaped, which is why it is unescaped once
before the tags are stripped.
"""

from __future__ import annotations

import json
from typing import Any

from dreamjob.adapters.ats.common import ATSAdapter
from dreamjob.adapters.base import PlanItem, RawRecord, register_adapter
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    fte_percentage,
    html_to_text,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    split_skills,
    title_matches,
    unescape_entities,
)

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


@register_adapter
class GreenhouseAdapter(ATSAdapter):
    key = "ats.greenhouse"
    display_name = "Greenhouse"
    vendor = "greenhouse"
    legal_notes = "Public job board API, no key required; read-only."

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        records: list[RawRecord] = []
        for slug in self.slugs_of(item):
            url = API.format(slug=slug)
            result = await self._get(url)
            if result is None:
                continue
            records.append(
                RawRecord(
                    url=url,
                    content=result.text,
                    content_type="application/json",
                    raw_document_id=result.raw_document_id,
                    meta=self.board_meta(item, slug),
                )
            )
        return self.settle(records, nothing_to_fetch=self.no_board_named())

    def parse(self, raw: RawRecord) -> list[dict]:
        payload = json.loads(raw.content)
        jobs = payload.get("jobs") or []
        keywords = raw.meta.get("keywords") or []
        filtering = bool(raw.meta.get("title_filter"))
        out: list[dict] = []
        for job in jobs[: raw.meta.get("max_records") or len(jobs)]:
            title = str(job.get("title") or "").strip()
            if filtering and not title_matches(title, keywords):
                continue
            out.append(self._one(job, raw))
        return out

    def _one(self, job: dict, raw: RawRecord) -> dict[str, Any]:
        description = html_to_text(unescape_entities(job.get("content")))
        location = str((job.get("location") or {}).get("name") or "")
        offices = ", ".join(
            str(o.get("location") or o.get("name") or "") for o in job.get("offices") or []
        )
        departments = [str(d.get("name") or "") for d in job.get("departments") or []]
        metadata = " ".join(
            f"{m.get('name')}: {m.get('value')}" for m in job.get("metadata") or []
        )
        apply_url = job.get("absolute_url") or ""
        channel, target = application_route(apply_url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(description[:6000])
        return {
            "company_id": raw.meta.get("company_id"),
            "company_name_raw": job.get("company_name") or raw.meta.get("company_name"),
            "title": job.get("title"),
            "function_family": departments[0] if departments else None,
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or offices or None,
            "country": country_from_location(location, offices),
            "work_arrangement": normalise_work_arrangement(location, metadata, description[:2000]),
            "contract_type": normalise_contract_type(metadata, description[:2000]),
            "fte_percentage": fte_percentage(metadata, description[:1500]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(job.get("first_published") or job.get("updated_at")),
            "application_channel": channel,
            "application_target": target,
            "source_url": apply_url,
            "language": normalise_language(job.get("language"), description),
        }
