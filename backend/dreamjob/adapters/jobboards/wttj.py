"""Welcome to the Jungle adapter (FR-181, FR-183, IR-101).

Welcome to the Jungle matters for Belgium and France because it carries
scale-up vacancies with unusually rich company content.  Three things are true
about it and the adapter reflects all three:

* the public site is a JavaScript application behind a bot filter (HTTP 403 for
  non-browser clients) and emits no JSON-LD, so scraping the search page is not
  a viable route;
* ``https://api.welcometothejungle.com/api/v1/organizations/{slug}`` *is*
  reachable and keyless, and gives the company profile that the company slice
  wants (sectors, offices, headcount band);
* job search runs on a hosted Algolia index whose application id and
  search-only key are issued to the site.  This adapter implements the Algolia
  query protocol in full; the credentials and the index name come from
  ``native_query`` because they are the operator's to supply, not values to be
  harvested from someone else's page.

Modes (``native_query["mode"]``)
    ``pages``     (default) parse advert URLs given in ``start_urls``
    ``algolia``   query the hosted index with ``algolia_app_id`` / ``algolia_api_key``
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote_plus

from dreamjob.adapters.base import (
    AccessMethod,
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
    normalise_contract_type,
    normalise_work_arrangement,
    parse_datetime,
)

log = logging.getLogger(__name__)

ORGANIZATION_API = "https://api.welcometothejungle.com/api/v1/organizations/{slug}"
ALGOLIA_URL = "https://{app_id}-dsn.algolia.net/1/indexes/{index}/query"
DEFAULT_INDEX = "wttj_jobs_production_published_at_desc"

#: Algolia hit -> vacancy field.  Overridable via ``native_query["field_map"]``
#: because the index schema is the site's, not a published contract.
DEFAULT_FIELD_MAP = {
    "title": ("name", "title"),
    "company_name_raw": ("organization.name", "company_name"),
    "description": ("description", "profile"),
    "location": ("offices.0.city", "office.city", "city"),
    "country": ("offices.0.country", "office.country", "country"),
    "contract_type": ("contract_type", "contract_type_name"),
    "posted_at": ("published_at", "created_at"),
    "source_url": ("url", "websites_url"),
    "work_arrangement": ("remote", "office.remote"),
    "slug": ("slug",),
    "organization_slug": ("organization.slug", "organization_reference"),
}


def _dig(hit: dict, path: str) -> Any:
    node: Any = hit
    for part in path.split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _first_of(hit: dict, paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _dig(hit, path)
        if value not in (None, "", []):
            return value
    return None


@register_adapter
class WelcomeToTheJungleAdapter(HtmlBoardAdapter):
    key = "board.wttj"
    display_name = "Welcome to the Jungle"
    coverage_countries = ["FR", "BE", "ES", "CZ", "GB"]
    access_method = AccessMethod.API
    tos_status = ToSStatus.RESTRICTED
    requires_ack = False
    rate_limit_rps = 0.5
    legal_notes = (
        "Public site blocks automated clients and its terms restrict re-use. The adapter "
        "uses the keyless organisation API and, where the operator supplies their own "
        "hosted-search credentials, the job index; it never harvests keys from the site."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=True, company_lookup=True,
        pagination=True, max_results_per_query=100,
    )

    defaults: dict[str, Any] = {
        "mode": "pages",
        "index": DEFAULT_INDEX,
        "detail": True,
        "country": "FR",
        "selectors": {},
        "detail_selectors": {
            "title": "h1",
            "company": '[data-testid="job-organization-name"] || h2',
            "location": '[data-testid="job-metadata-block"] i + span || .location',
            "description": '[data-testid="job-section-description"] || main || article',
        },
    }

    # -- fetch --------------------------------------------------------------
    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        cfg = self.config(item)
        if str(cfg.get("mode") or "pages") == "algolia":
            return await self._fetch_algolia(cfg)
        return await super().fetch(item)

    async def _fetch_algolia(self, cfg: dict[str, Any]) -> list[RawRecord]:
        app_id = cfg.get("algolia_app_id")
        api_key = cfg.get("algolia_api_key")
        if not (app_id and api_key):
            log.warning(
                "[%s] algolia mode needs algolia_app_id and algolia_api_key in the plan "
                "item; collecting nothing for this item",
                self.key,
            )
            return []
        index = str(cfg.get("index") or DEFAULT_INDEX)
        url = ALGOLIA_URL.format(app_id=app_id, index=index)
        per_page = min(int(cfg.get("page_size") or 30), 100)
        pages = max(1, int(cfg.get("pages") or 1))
        records: list[RawRecord] = []
        for query in cfg.get("queries") or [""]:
            for page in range(pages):
                params = (
                    f"query={quote_plus(str(query))}"
                    f"&hitsPerPage={per_page}&page={page}"
                )
                if cfg.get("filters"):
                    params += f"&filters={quote_plus(str(cfg['filters']))}"
                result = await self._get(
                    url,
                    method="POST",
                    json={"params": params},
                    headers={
                        "X-Algolia-Application-Id": str(app_id),
                        "X-Algolia-API-Key": str(api_key),
                        "Content-Type": "application/json",
                    },
                )
                if result is None:
                    return records
                records.append(
                    RawRecord(
                        url=f"{url}?{params}",
                        content=result.text,
                        content_type="application/json",
                        raw_document_id=result.raw_document_id,
                        meta={"kind": "algolia", "cfg": cfg},
                    )
                )
                try:
                    if page + 1 >= int(json.loads(result.text).get("nbPages") or 1):
                        break
                except ValueError:
                    break
        return records

    # -- parse --------------------------------------------------------------
    def parse(self, raw: RawRecord) -> list[dict]:
        if raw.meta.get("kind") != "algolia":
            return super().parse(raw)
        cfg = raw.meta.get("cfg") or {}
        field_map = {**DEFAULT_FIELD_MAP, **(cfg.get("field_map") or {})}
        try:
            payload = json.loads(raw.content)
        except ValueError:
            return []
        out: list[dict] = []
        for hit in payload.get("hits") or []:
            fields = {
                name: _first_of(hit, tuple(paths)) for name, paths in field_map.items()
            }
            description = html_to_text(fields.get("description"))
            organisation = fields.pop("organization_slug", None)
            slug = fields.pop("slug", None)
            if not fields.get("source_url") and organisation and slug:
                fields["source_url"] = (
                    f"https://www.welcometothejungle.com/en/companies/{organisation}/jobs/{slug}"
                )
            fields["description"] = description
            fields["posted_at"] = parse_datetime(fields.get("posted_at"))
            fields["country"] = country_from_location(
                fields.get("country"), fields.get("location")
            )
            fields["work_arrangement"] = normalise_work_arrangement(
                fields.get("work_arrangement"), fields.get("location"), description[:2000]
            )
            fields["contract_type"] = normalise_contract_type(
                fields.get("contract_type"), description[:2000]
            )
            if fields.get("title"):
                out.append(self._finish(fields, cfg))
        return out

    @staticmethod
    def organization_url(slug: str) -> str:
        """Keyless company-profile endpoint, for the company-profiling slice."""
        return ORGANIZATION_API.format(slug=slug)
