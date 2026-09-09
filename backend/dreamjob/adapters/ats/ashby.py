"""Ashby job-board adapter (FR-181, FR-261, IR-102).

Endpoint (public "Job Posting API", keyless):

    GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

The response is ``{"apiVersion": "1", "jobs": [...]}`` covering the whole board.
Ashby is the only vendor here that publishes a machine-readable pay range, in
``compensation.scrapeableCompensationSalarySummary`` - it is used verbatim and
never extrapolated (FR-261 records compensation only "if stated").
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
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    split_skills,
    title_matches,
)

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


@register_adapter
class AshbyAdapter(ATSAdapter):
    key = "ats.ashby"
    display_name = "Ashby"
    vendor = "ashby"
    legal_notes = "Public job-board posting API, no key required; read-only."

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
        jobs = [j for j in payload.get("jobs") or [] if j.get("isListed", True)]
        keywords = raw.meta.get("keywords") or []
        filtering = bool(raw.meta.get("title_filter"))
        out: list[dict] = []
        for job in jobs[: raw.meta.get("max_records") or len(jobs)]:
            if filtering and not title_matches(str(job.get("title") or ""), keywords):
                continue
            out.append(self._one(job, raw))
        return out

    def _one(self, job: dict, raw: RawRecord) -> dict[str, Any]:
        description = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml"))
        location = str(job.get("location") or "")
        secondary = ", ".join(
            str(s.get("location") or "") for s in job.get("secondaryLocations") or []
        )
        postal = ((job.get("address") or {}).get("postalAddress") or {})
        apply_url = job.get("applyUrl") or job.get("jobUrl") or ""
        channel, target = application_route(apply_url, description)
        required, desirable = split_skills(description)

        compensation = job.get("compensation") or {}
        summary = (
            compensation.get("scrapeableCompensationSalarySummary")
            or compensation.get("compensationTierSummary")
            or ""
        )
        salary_min, salary_max, currency = parse_salary_text(summary)
        if salary_min is None:
            salary_min, salary_max, currency = parse_salary_text(description[:6000])

        return {
            "company_id": raw.meta.get("company_id"),
            "company_name_raw": raw.meta.get("company_name") or raw.meta.get("slug"),
            "title": job.get("title"),
            "function_family": job.get("department") or job.get("team"),
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or secondary or None,
            "country": country_from_location(postal.get("addressCountry"), location),
            "work_arrangement": normalise_work_arrangement(
                job.get("workplaceType"),
                "remote" if job.get("isRemote") else "",
                location,
                description[:2000],
            ),
            "contract_type": normalise_contract_type(
                job.get("employmentType"), description[:2000]
            ),
            "fte_percentage": fte_percentage(job.get("employmentType"), description[:1500]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(job.get("publishedAt")),
            "application_channel": channel,
            "application_target": target,
            "source_url": job.get("jobUrl") or apply_url,
        }
