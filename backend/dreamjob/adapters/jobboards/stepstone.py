"""StepStone adapter - disabled until acknowledged (IR-101, FR-181, FR-183).

StepStone's terms of use forbid automated collection.  IR-101 says such a
source must declare that, be disabled by default and require an explicit
administrator acknowledgement, so ``tos_status`` is RESTRICTED and
``requires_ack`` is true: :meth:`SourceAdapter.is_enabled` returns False until
an administrator records the acknowledgement in the source catalogue.

The implementation itself is complete and deterministic - the result list is
server-rendered (cards carry ``data-at="job-item"``) and the advert pages
carry schema.org ``JobPosting`` - so an operator who has a licence or written
permission gets a working adapter rather than a stub.
"""

from __future__ import annotations

from typing import Any

from dreamjob.adapters.base import AdapterCapabilities, ToSStatus, register_adapter
from dreamjob.adapters.jobboards.generic_html import HtmlBoardAdapter


@register_adapter
class StepStoneAdapter(HtmlBoardAdapter):
    key = "board.stepstone"
    display_name = "StepStone"
    coverage_countries = ["BE", "DE", "NL", "AT", "GB", "FR"]
    tos_status = ToSStatus.RESTRICTED
    requires_ack = True
    rate_limit_rps = 0.2
    legal_notes = (
        "StepStone's terms of use prohibit automated access and systematic copying. "
        "Disabled until an administrator acknowledges this (IR-101); enable only with "
        "a licence or written permission."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=True, radius_filter=True,
        work_arrangement_filter=True, pagination=True, max_results_per_query=100,
    )

    defaults: dict[str, Any] = {
        "url_template": (
            "https://www.stepstone.be/jobs/{query}?q={query}&where={location}&page={page}"
        ),
        "pages": 2,
        "country": "BE",
        "detail": True,
        "selectors": {
            "list_item": '[data-at="job-item"]',
            "url": 'a[href*="/jobs--"]@href || a@href',
            "title": '[data-at="job-item-title"]',
            "company": '[data-at="job-item-company-name"]',
            "location": '[data-at="job-item-location"]',
            "posted_at": '[data-at="job-item-timeago"] || time@datetime',
        },
        "detail_selectors": {
            "title": 'h1[data-at="header-job-title"] || h1',
            "company": '[data-at="header-company-name"]',
            "location": '[data-at="metadata-location"]',
            "description": '[data-at="job-ad-content"] || main || article',
            "posted_at": "time@datetime",
            "apply_url": 'a[data-at="header-apply-button"]@href || a[href^="mailto:"]@href',
        },
    }
