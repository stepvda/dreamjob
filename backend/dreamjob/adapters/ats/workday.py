"""Workday job-board adapter (FR-181, FR-261, IR-102).

Workday tenants publish their career site through the ``cxs`` JSON endpoint
that the site's own single-page app calls.  Two requests, no key:

*list*  ``POST https://{host}/wday/cxs/{tenant}/{site}/jobs``
        body ``{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}``
        reply ``{"total": n, "jobPostings": [{"title", "externalPath",
        "locationsText", "postedOn", "bulletFields"}], "facets": [...]}``
        ``limit`` is capped at 20 by Workday, so ``offset`` walks the board.

*detail* ``GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}``
        reply ``{"jobPostingInfo": {"title", "jobDescription" (HTML),
        "location", "postedOn", "startDate", "timeType", "jobReqId",
        "country": {...}, "jobRequisitionLocation": {...}, "externalUrl",
        "remoteType"}, "hiringOrganization": {...}}``

``{host}`` is the tenant's Workday host (``acme.wd3.myworkdayjobs.com``) and
``{site}`` the career-site name (``External``, ``Careers``, ...); the slug this
adapter stores on ``company.ats_slug`` is ``"{host}/{site}"``, which is what
:func:`dreamjob.adapters.ats.detect.detect_ats` returns for a Workday URL.

Unlike the other ATS vendors Workday accepts a server-side ``searchText``, so
the adapter is keyword-capable and issues one plan item per keyword.
"""

from __future__ import annotations

import json
import re
from typing import Any

from dreamjob.adapters.ats.common import DEFAULT_MAX_RECORDS, ATSAdapter, company_targets
from dreamjob.adapters.base import AdapterCapabilities, PlanItem, RawRecord, register_adapter
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    fte_percentage,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
    title_matches,
)

CXS = "https://{host}/wday/cxs/{tenant}/{site}"
PUBLIC = "https://{host}/{site}{path}"
PAGE_SIZE = 20
_HOST_RE = re.compile(r"^(?P<tenant>[\w-]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)


def split_slug(slug: str) -> tuple[str, str, str]:
    """``"acme.wd3.myworkdayjobs.com/External"`` -> ``(host, tenant, site)``."""
    cleaned = slug.strip().strip("/")
    cleaned = re.sub(r"^https?://", "", cleaned)
    parts = [p for p in cleaned.split("/") if p]
    if len(parts) < 2:
        raise ValueError(
            f"Workday slug must be '<host>/<site>', got {slug!r} "
            "(e.g. 'acme.wd3.myworkdayjobs.com/External')"
        )
    host = parts[0]
    site = parts[-1]
    match = _HOST_RE.match(host)
    tenant = match.group("tenant") if match else host.split(".")[0]
    return host, tenant, site


@register_adapter
class WorkdayAdapter(ATSAdapter):
    key = "ats.workday"
    display_name = "Workday"
    vendor = "workday"
    legal_notes = (
        "Public career-site JSON endpoint used by the tenant's own front end; "
        "no key required, read-only, robots.txt is honoured by the egress layer."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=False, company_lookup=True,
        pagination=True, max_results_per_query=DEFAULT_MAX_RECORDS,
    )

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        keywords = keywords_from(directives, composite_profile)
        max_records = int(caps.get("max_records_per_source") or DEFAULT_MAX_RECORDS)
        items: list[PlanItem] = []
        for target in company_targets(directives, caps, self.vendor):
            for search in keywords or [""]:
                items.append(
                    PlanItem(
                        adapter_key=self.key,
                        native_query={
                            "slug": target["slug"],
                            "company_id": target.get("company_id"),
                            "company_name": target.get("company_name"),
                            "search_text": search,
                            "max_records": max_records,
                            "keywords": keywords,
                            "title_filter": bool(caps.get("title_filter")),
                        },
                        rationale=(
                            f"Workday career site of "
                            f"{target.get('company_name') or target['slug']}"
                            + (f", query '{search}'" if search else "")
                        ),
                        estimated_pages=max(1, max_records // PAGE_SIZE),
                        estimated_seconds=10 + max_records // PAGE_SIZE * 4,
                        caps={"max_records": max_records},
                    )
                )
        return items

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        try:
            host, tenant, site = split_slug(self.slug_of(item))
        except ValueError as exc:
            raise ValueError(f"[{self.key}] {exc}") from exc
        base = CXS.format(host=host, tenant=tenant, site=site)
        limit = self.limit_of(item)
        search_text = str(item.native_query.get("search_text") or "")
        meta_base = {
            "host": host,
            "site": site,
            "company_id": item.native_query.get("company_id"),
            "company_name": item.native_query.get("company_name"),
        }

        # Each posting keeps the id of the listing page it came from, so that a
        # posting whose detail page cannot be read still has provenance (FR-183).
        postings: list[tuple[dict, str | None]] = []
        one_page = requested_page(item.native_query)
        offset = (one_page - 1) * PAGE_SIZE if one_page else 0
        limit = min(limit, PAGE_SIZE) if one_page else limit
        while len(postings) < limit:
            listing = await self._get(
                f"{base}/jobs",
                method="POST",
                json={
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "searchText": search_text,
                },
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if listing is None:
                break
            try:
                payload = json.loads(listing.text)
            except ValueError:
                break
            page = payload.get("jobPostings") or []
            postings.extend((posting, listing.raw_document_id) for posting in page)
            offset += PAGE_SIZE
            if len(page) < PAGE_SIZE or offset >= int(payload.get("total") or 0):
                break
            if one_page:
                break

        if item.native_query.get("title_filter"):
            keywords = item.native_query.get("keywords") or []
            postings = [
                (posting, doc_id)
                for posting, doc_id in postings
                if title_matches(str(posting.get("title") or ""), keywords)
            ]

        records: list[RawRecord] = []
        for posting, listing_doc_id in postings[:limit]:
            path = str(posting.get("externalPath") or "")
            if not path:
                continue
            detail = await self._get(f"{base}{path}", headers={"Accept": "application/json"})
            public_url = PUBLIC.format(host=host, site=site, path=path)
            if detail is None:
                records.append(
                    RawRecord(
                        url=public_url,
                        content=json.dumps(posting),
                        content_type="application/json",
                        raw_document_id=listing_doc_id,
                        meta={**meta_base, "kind": "summary"},
                    )
                )
                continue
            records.append(
                RawRecord(
                    url=public_url,
                    content=detail.text,
                    content_type="application/json",
                    raw_document_id=detail.raw_document_id,
                    meta={**meta_base, "kind": "detail", "summary": posting},
                )
            )
        return records

    def parse(self, raw: RawRecord) -> list[dict]:
        payload = json.loads(raw.content)
        summary = raw.meta.get("summary") or {}
        if raw.meta.get("kind") == "summary":
            info: dict[str, Any] = {}
            summary = payload
        else:
            info = payload.get("jobPostingInfo") or {}
        organisation = payload.get("hiringOrganization") or {}
        return [self._one(info, summary, organisation, raw)]

    def _one(
        self, info: dict, summary: dict, organisation: dict, raw: RawRecord
    ) -> dict[str, Any]:
        description = html_to_text(info.get("jobDescription"))
        location = str(
            info.get("location")
            or (info.get("jobRequisitionLocation") or {}).get("descriptor")
            or summary.get("locationsText")
            or ""
        )
        country = (info.get("country") or {}).get("descriptor")
        alpha2 = ((info.get("jobRequisitionLocation") or {}).get("country") or {}).get("alpha2Code")
        time_type = str(info.get("timeType") or "")
        remote_type = str(info.get("remoteType") or "")
        apply_url = info.get("externalUrl") or raw.url
        channel, target = application_route(apply_url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(description[:6000])
        return {
            "company_id": raw.meta.get("company_id"),
            "company_name_raw": organisation.get("name") or raw.meta.get("company_name"),
            "title": info.get("title") or summary.get("title"),
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or None,
            "country": country_from_location(alpha2, country, location),
            "work_arrangement": normalise_work_arrangement(
                remote_type, location, description[:2000]
            ),
            "contract_type": normalise_contract_type(time_type, description[:2000]),
            "fte_percentage": fte_percentage(time_type, description[:1500]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(info.get("startDate"))
            or parse_datetime(info.get("postedOn") or summary.get("postedOn")),
            "application_channel": channel,
            "application_target": target,
            "source_url": apply_url,
        }
