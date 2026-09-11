"""Indeed adapter - prohibited by default (IR-101, FR-181, FR-183).

Indeed's terms of service prohibit scraping its site.  Under IR-101 that makes
this a ``PROHIBITED`` source: it is disabled until an administrator explicitly
acknowledges the restriction, and even then the sanctioned route is Indeed's
licensed Job Sync / Search API with a publisher key rather than the public
result pages.  Setting ``native_query["api_base"]`` and ``["api_key"]`` points
the adapter at that licensed endpoint; without them it falls back to the public
result pages, which the bot filter answers with HTTP 403 for anything that is
not a browser - so the honest default outcome is "no records, and here is why"
rather than a silent circumvention attempt.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from dreamjob.adapters.base import (
    AdapterCapabilities,
    PlanItem,
    RawRecord,
    ToSStatus,
    register_adapter,
)
from dreamjob.adapters.jobboards.generic_html import HtmlBoardAdapter
from dreamjob.adapters.vacancy_source import (
    country_from_location,
    html_to_text,
    parse_datetime,
)

log = logging.getLogger(__name__)


@register_adapter
class IndeedAdapter(HtmlBoardAdapter):
    key = "board.indeed"
    display_name = "Indeed"
    coverage_countries: list[str] = []          # global
    tos_status = ToSStatus.PROHIBITED
    requires_ack = True
    rate_limit_rps = 0.15
    legal_notes = (
        "Indeed's terms of service prohibit automated access and scraping. Disabled by "
        "default (IR-101); enable only with a publisher/API licence, and prefer the "
        "licensed API by setting api_base and api_key in the plan item."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=True, radius_filter=True,
        pagination=True, max_results_per_query=100,
    )

    defaults: dict[str, Any] = {
        "url_template": "https://be.indeed.com/jobs?q={query}&l={location}&start={offset}",
        "page_size": 10,
        "pages": 2,
        "country": "BE",
        "detail": True,
        "selectors": {
            "list_item": "div.job_seen_beacon, div[data-jk]",
            "url": "h2.jobTitle a@href || a.jcs-JobTitle@href",
            "title": "h2.jobTitle span@title || h2.jobTitle",
            "company": '[data-testid="company-name"] || span.companyName',
            "location": '[data-testid="text-location"] || div.companyLocation',
            "posted_at": '[data-testid="myJobsStateDate"] || span.date',
        },
        "detail_selectors": {
            "title": 'h1[data-testid="jobsearch-JobInfoHeader-title"] || h1',
            "company": '[data-testid="inlineHeader-companyName"]',
            "location": '[data-testid="inlineHeader-companyLocation"]',
            "description": "#jobDescriptionText || main",
            "apply_url": "#indeedApplyButton@href || a[href^='mailto:']@href",
        },
    }

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        cfg = self.config(item)
        if cfg.get("api_base") and cfg.get("api_key"):
            return await self._fetch_licensed_api(item, cfg)
        return await super().fetch(item)

    async def _fetch_licensed_api(
        self, item: PlanItem, cfg: dict[str, Any]
    ) -> list[RawRecord]:
        """Indeed's licensed search API, when the operator holds a publisher key."""
        records: list[RawRecord] = []
        limit = int(cfg.get("max_records") or 50)
        for query in cfg.get("queries") or [""]:
            for location in cfg.get("locations") or [""]:
                url = (
                    f"{str(cfg['api_base']).rstrip('/')}/jobs"
                    f"?q={quote(str(query), safe='')}&l={quote(str(location), safe='')}"
                    f"&limit={min(limit, 50)}"
                )
                result = await self._get(
                    url, headers={"Authorization": f"Bearer {cfg['api_key']}"}
                )
                if result is None:
                    continue
                records.append(
                    RawRecord(
                        url=url,
                        content=result.text,
                        content_type="application/json",
                        raw_document_id=result.raw_document_id,
                        meta={"kind": "api", "cfg": cfg},
                    )
                )
        # FR-185: a licence that the API refuses is a failed plan item, not an
        # empty result set.
        return self.settle(
            records,
            nothing_to_fetch="the licensed API needs api_base, api_key and a query",
        )

    def parse(self, raw: RawRecord) -> list[dict]:
        if raw.meta.get("kind") != "api":
            return super().parse(raw)
        cfg = raw.meta.get("cfg") or {}
        try:
            payload = json.loads(raw.content)
        except ValueError:
            return []
        results = payload.get("results") or payload.get("jobs") or []
        out: list[dict] = []
        for job in results:
            description = html_to_text(job.get("snippet") or job.get("description"))
            out.append(
                self._finish(
                    {
                        "title": job.get("jobtitle") or job.get("title"),
                        "company_name_raw": job.get("company"),
                        "description": description,
                        "location": job.get("formattedLocation") or job.get("location"),
                        "country": country_from_location(job.get("country")),
                        "posted_at": parse_datetime(job.get("date") or job.get("datePosted")),
                        "source_url": job.get("url") or job.get("jobkey"),
                    },
                    cfg,
                )
            )
        return out
