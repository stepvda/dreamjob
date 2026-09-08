"""Jobat.be adapter - Belgian commercial job board (FR-181, IR-101, FR-183).

Jobat publishes structured listing pages and emits schema.org ``JobPosting``
on its advert pages, so extraction is deterministic where the site answers.
It also sits behind a bot filter that returns HTTP 403 to unknown clients; the
egress layer reports that as a failed plan item rather than retrying around it,
and the adapter is marked RESTRICTED so an operator sees why coverage is thin.
"""

from __future__ import annotations

from typing import Any

from dreamjob.adapters.base import AdapterCapabilities, ToSStatus, register_adapter
from dreamjob.adapters.jobboards.generic_html import HtmlBoardAdapter


@register_adapter
class JobatAdapter(HtmlBoardAdapter):
    key = "board.jobat"
    display_name = "Jobat.be"
    coverage_countries = ["BE"]
    tos_status = ToSStatus.RESTRICTED
    requires_ack = False
    rate_limit_rps = 0.3
    legal_notes = (
        "Commercial board; terms restrict systematic re-use. Collection is limited to "
        "public advert pages at a low rate and stores only what the advert states."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=True, contract_type_filter=True,
        pagination=True, max_results_per_query=100,
    )

    defaults: dict[str, Any] = {
        "url_template": (
            "https://www.jobat.be/nl/jobs?trefwoord={query}&plaats={location}&pagina={page}"
        ),
        "pages": 2,
        "country": "BE",
        "language": "nl",
        "detail": True,
        "selectors": {
            "list_item": "article.job, .job-card, li[data-jobid]",
            "url": 'a[href*="/nl/jobs/"]@href || a@href',
            "title": "h2 || h3 || .job-title",
            "company": ".company, .job-company",
            "location": ".location, .job-location",
            "posted_at": "time@datetime || time",
        },
        "detail_selectors": {
            "title": "h1",
            "company": '[itemprop="hiringOrganization"] || .company-name',
            "location": '[itemprop="jobLocation"] || .location',
            "description": '[itemprop="description"] || .job-description || main',
            "posted_at": "time@datetime",
            "apply_url": 'a[href^="mailto:"]@href || a.apply@href',
        },
    }
