"""Personio job-board adapter (FR-181, FR-261, IR-102).

Endpoint (public XML feed, keyless):

    GET https://{slug}.jobs.personio.de/xml        (tenants on .com redirect)

The feed is ``<workzag-jobs><position>...</position></workzag-jobs>`` with the
structured attributes Personio holds - office, department, employmentType,
seniority, schedule, yearsOfExperience, keywords, occupation, createdAt - and a
``<jobDescriptions>`` block that many tenants leave empty in the feed.  When it
is empty the adapter falls back to the public job page, which carries a
schema.org ``JobPosting`` block (FR-183), rather than to the LLM.
"""

from __future__ import annotations

import logging
from typing import Any
from xml.etree import ElementTree

from dreamjob.adapters.ats.common import ATSAdapter
from dreamjob.adapters.base import PlanItem, RawRecord, register_adapter
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    fte_percentage,
    html_to_text,
    jobposting_to_fields,
    jsonld_jobpostings,
    normalise_contract_type,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    split_skills,
    title_matches,
)

log = logging.getLogger(__name__)

FEED = "https://{slug}.jobs.{domain}/xml"
JOB_PAGE = "https://{slug}.jobs.{domain}/job/{job_id}"
DEFAULT_DOMAIN = "personio.de"
MAX_FEED_BYTES = 8_000_000


@register_adapter
class PersonioAdapter(ATSAdapter):
    key = "ats.personio"
    display_name = "Personio"
    vendor = "personio"
    coverage_countries = ["DE", "AT", "CH", "NL", "BE", "ES", "GB", "IE"]
    legal_notes = "Public XML career feed, no key required; read-only."

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        slug = self.slug_of(item)
        domain = str(item.native_query.get("domain") or DEFAULT_DOMAIN)
        feed_url = FEED.format(slug=slug, domain=domain)
        feed = await self._get(feed_url)
        if feed is None:
            return []
        body = feed.text

        meta = {
            "slug": slug,
            "domain": domain,
            "company_id": item.native_query.get("company_id"),
            "company_name": item.native_query.get("company_name"),
            "keywords": item.native_query.get("keywords") or [],
            "title_filter": item.native_query.get("title_filter"),
            "max_records": self.limit_of(item),
        }
        positions = self._positions(body)[: self.limit_of(item)]
        records = [
            RawRecord(url=feed_url, content=body, content_type="application/xml",
                      raw_document_id=feed.raw_document_id, meta={**meta, "kind": "list"})
        ]
        if item.native_query.get("fetch_details", True) is False:
            return records

        thin = [p for p in positions if not p.get("description")]
        fetched: list[str] = []
        for position in thin[: int(item.native_query.get("max_detail_pages") or 60)]:
            page_url = JOB_PAGE.format(slug=slug, domain=domain, job_id=position["id"])
            page = await self._get(page_url)
            if page is None:
                continue
            fetched.append(position["id"])
            records.append(
                RawRecord(
                    url=page_url,
                    content=page.text,
                    content_type="text/html",
                    raw_document_id=page.raw_document_id,
                    meta={**meta, "kind": "detail", "position": position},
                )
            )
        # Only the positions whose page was actually retrieved are covered by a
        # detail record; the rest still come from the feed. When every position was
        # detailed, the feed record would parse to nothing and would depress the
        # NFR-403 extraction rate, so it is dropped.
        if len(fetched) >= len(positions):
            return records[1:]
        records[0].meta["detailed_ids"] = fetched
        return records

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _positions(xml_body: str) -> list[dict[str, Any]]:
        # The feed is third-party input; cap it before handing it to expat so a
        # hostile or broken tenant cannot exhaust memory (NFR-205).
        if len(xml_body) > MAX_FEED_BYTES:
            log.warning("Personio feed exceeds %d bytes; refusing to parse", MAX_FEED_BYTES)
            return []
        try:
            root = ElementTree.fromstring(xml_body)
        except ElementTree.ParseError as exc:
            log.warning("Personio feed is not well-formed XML: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for node in root.iter("position"):
            fields: dict[str, Any] = {}
            for child in node:
                if child.tag == "jobDescriptions":
                    fields["description"] = "\n\n".join(
                        f"{(d.findtext('name') or '').strip()}\n"
                        f"{html_to_text(d.findtext('value') or '')}".strip()
                        for d in child.findall("jobDescription")
                    ).strip()
                elif child.tag == "additionalOffices":
                    fields["additional_offices"] = ", ".join(
                        (o.text or "").strip() for o in child.findall("office")
                    )
                else:
                    fields[child.tag] = (child.text or "").strip()
            if fields.get("id"):
                out.append(fields)
        return out

    def parse(self, raw: RawRecord) -> list[dict]:
        keywords = raw.meta.get("keywords") or []
        filtering = bool(raw.meta.get("title_filter"))
        if raw.meta.get("kind") == "detail":
            return self._parse_detail(raw)
        skip = set(raw.meta.get("detailed_ids") or [])
        limit = int(raw.meta.get("max_records") or 0) or None
        out: list[dict] = []
        for position in self._positions(raw.content)[:limit]:
            if position["id"] in skip:
                continue
            if filtering and not title_matches(str(position.get("name") or ""), keywords):
                continue
            out.append(self._one(position, raw))
        return out

    def _parse_detail(self, raw: RawRecord) -> list[dict]:
        position = raw.meta.get("position") or {}
        postings = jsonld_jobpostings(raw.content)
        if postings:
            fields = jobposting_to_fields(postings[0], raw.url)
            merged = self._one(position, raw)
            for key, value in fields.items():
                if value not in (None, "", []):
                    merged[key] = value
            merged["source_url"] = raw.url
            merged["company_id"] = raw.meta.get("company_id")
            return [merged]
        text = html_to_text(raw.content)
        extracted = self.llm_extract(text, raw.url) if text else None
        if extracted:
            extracted["company_id"] = raw.meta.get("company_id")
            return [extracted]
        return [self._one(position, raw)] if position.get("name") else []

    def _one(self, position: dict, raw: RawRecord) -> dict[str, Any]:
        description = position.get("description") or ""
        office = position.get("office") or ""
        offices = ", ".join(p for p in (office, position.get("additional_offices") or "") if p)
        schedule = position.get("schedule") or ""
        employment = position.get("employmentType") or ""
        keywords = position.get("keywords") or ""
        required, desirable = split_skills(description)
        if keywords:
            stated = [k.strip() for k in keywords.split(",") if k.strip()]
            required = sorted({*required, *[k for k in stated if len(k) < 60]})[:40]
        salary_min, salary_max, currency = parse_salary_text(description[:6000])
        apply_url = JOB_PAGE.format(
            slug=raw.meta.get("slug"), domain=raw.meta.get("domain", DEFAULT_DOMAIN),
            job_id=position.get("id"),
        )
        channel, target = application_route(apply_url, description)
        return {
            "company_id": raw.meta.get("company_id"),
            "company_name_raw": position.get("subcompany") or raw.meta.get("company_name"),
            "title": position.get("name"),
            "function_family": position.get("department") or position.get("occupationCategory"),
            "seniority": position.get("seniority"),
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": offices or None,
            "country": country_from_location(offices),
            "work_arrangement": normalise_work_arrangement(offices, description[:2000]),
            "contract_type": normalise_contract_type(employment, schedule, description[:2000]),
            "fte_percentage": fte_percentage(schedule, description[:1500]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(position.get("createdAt")),
            "application_channel": channel,
            "application_target": target,
            "source_url": apply_url,
        }
