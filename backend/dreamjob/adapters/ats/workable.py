"""Workable job-board adapter (FR-181, FR-261, FR-183, IR-102).

Endpoints (public careers-site API, keyless, ``apply.workable.com/robots.txt``
is ``User-agent: * / Disallow:`` - i.e. everything is permitted):

    POST https://apply.workable.com/api/v3/accounts/{slug}/jobs
    {}                                  -> first page
    {"token": "<nextPage>"}             -> the page after it
    -> {"total": n, "results": [{"shortcode", "title", "location": {...},
        "state", "isInternal", "published", "type", "workplace", "remote",
        "department": [...], "language"}], "nextPage": "<opaque token>"}

    GET  https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
    -> {"name": "<the employer>", "jobs": [{"shortcode", "description",
        "employment_type", "experience", "application_url", ...}]}

Three properties of the v3 list decide the shape of this adapter:

* **It pages ten at a time and the cursor is a body field.** ``nextPage`` comes
  back in the response and has to be sent back as ``{"token": ...}``; passing it
  as ``{"nextPage": ...}`` is answered ``400 {"nextPage": "Not allowed"}`` and
  as a query parameter it is ignored, which silently re-serves page one for
  ever.  The walk therefore stops on ``total``, not on an empty answer alone.
* **``nextPage`` is present even on the last page.**  A board of ten jobs
  answers ``total: 10`` with a token that returns nothing, so a walk that
  trusted the token alone would spend one wasted request per board - 2 s each
  at the per-domain limit, over the whole registry.
* **The list carries no advert text and no employer name.**  Both are in the
  one ``widget`` request per board, which is why ``fetch_details`` defaults to
  on: it is one extra request per *board* (not per posting) and it is what
  fills ``description``, ``company_name_raw`` and ``seniority``.  Campaign A
  (docs/Data_Gathering_Plan.md 2.4) prices Workable at ~1,000 requests; with
  details that becomes ~2,000, still 33-66 min in a bucket that runs beside the
  100-minute EURES one.  An operator who wants the cheaper shape sets
  ``fetch_details: false`` in the plan item and keeps the structured fields.

The widget lists a multi-location posting once per location (3E's "Consultor de
sistemas BESS y FV" appears three times, one per city), so descriptions are
indexed by ``shortcode`` first-wins rather than zipped positionally.

``llm_fallback`` is False: every field is already structured, so collection
costs 0 tokens (plan 6.9).
"""

from __future__ import annotations

import json
import logging
import math
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
)
from dreamjob.egress.client import FetchResult

log = logging.getLogger(__name__)

LIST_API = "https://apply.workable.com/api/v3/accounts/{slug}/jobs"
WIDGET_API = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
JOB_URL = "https://apply.workable.com/{slug}/j/{shortcode}/"
APPLY_URL = "https://apply.workable.com/{slug}/j/{shortcode}/apply/"

#: The service pages ten at a time and offers no way to ask for more
#: (``{"limit": 50}`` is refused with ``400 {"limit": "Not allowed"}``).
PAGE_SIZE = 10
#: Hard ceiling on the walk, whatever ``max_records`` says: 100 pages is a
#: 1,000-posting board, well past the largest one measured.
MAX_PAGES = 100

#: Workable's own employment codes, spelled the way the shared normalisers
#: read them ("full" alone matches nothing in ``_CONTRACT_MAP``).
_TYPE_WORDS = {
    "full": "full-time",
    "part": "part-time",
    "contract": "contractor",
    "temporary": "temporary",
    "internship": "internship",
}


@register_adapter
class WorkableAdapter(ATSAdapter):
    key = "ats.workable"
    display_name = "Workable"
    vendor = "workable"
    # Global: the registry's Workable slugs span every market, and the EU share
    # is unmeasured (plan appendix B.2), so [] - "no country filter" - is the
    # honest declaration for FR-164 rather than a guessed list.
    coverage_countries: list[str] = []
    legal_notes = (
        "Public careers-site API of apply.workable.com, no key required, read-only. "
        "robots.txt allows every path for every agent (Disallow: empty)."
    )

    # -- fetch (IR-102, FR-182, FR-185) -------------------------------------
    async def _post_page(self, url: str, token: str | None) -> FetchResult | None:
        """One counted list request that must answer JSON (FR-185)."""
        body: dict[str, Any] = {"token": token} if token else {}
        result = await self._get(
            url,
            method="POST",
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if result is None:
            return None
        if result.text.lstrip()[:1] not in ("{", "["):
            # A 200 that is not JSON is a changed endpoint, not an empty board.
            log.warning("[%s] %s did not answer JSON", self.key, url)
            self.fetch_outcome.ok -= 1
            self.fetch_outcome.failures.append((url, "not JSON"))
            return None
        return result

    async def _widget(self, slug: str) -> tuple[str | None, dict[str, dict]]:
        """The one request that carries the advert text and the employer name."""
        result = await self._get(
            WIDGET_API.format(slug=slug), headers={"Accept": "application/json"}
        )
        if result is None:
            return None, {}
        try:
            payload = json.loads(result.text)
        except ValueError:
            log.warning("[%s] widget response for %s was not JSON", self.key, slug)
            return None, {}
        if not isinstance(payload, dict):
            return None, {}
        details: dict[str, dict] = {}
        for job in payload.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            shortcode = str(job.get("shortcode") or "")
            # A posting open in several cities is listed once per city; the
            # advert text is the same, so the first one wins.
            if shortcode and shortcode not in details:
                details[shortcode] = job
        name = str(payload.get("name") or "").strip() or None
        return name, details

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        want_details = query.get("fetch_details", True) is not False
        limit = self.limit_of(item)
        page_budget = min(MAX_PAGES, max(1, math.ceil(limit / PAGE_SIZE)))

        records: list[RawRecord] = []
        for slug in self.slugs_of(item):
            url = LIST_API.format(slug=slug)
            pages: list[RawRecord] = []
            token: str | None = None
            seen = 0
            for page in range(1, page_budget + 1):
                result = await self._post_page(url, token)
                if result is None:
                    break
                try:
                    payload = json.loads(result.text)
                except ValueError:
                    self.fetch_outcome.ok -= 1
                    self.fetch_outcome.failures.append((url, "malformed JSON"))
                    break
                results = payload.get("results") or []
                if not results:
                    break
                pages.append(
                    RawRecord(
                        url=f"{url}#page={page}",
                        content=result.text,
                        content_type="application/json",
                        raw_document_id=result.raw_document_id,
                        meta={
                            **self.board_meta(item, slug),
                            "kind": "list",
                            "page": page,
                            # `max_records` is the budget for the whole board,
                            # and parse() applies it per record: without the
                            # running total a three-page walk under a 20-record
                            # cap would have emitted thirty.
                            "max_records": max(0, limit - seen),
                        },
                    )
                )
                seen += len(results)
                total = payload.get("total")
                token = payload.get("nextPage") or None
                # `nextPage` is handed out even when the board is exhausted, so
                # `total` is the authority; without this every board costs one
                # extra empty request.
                if not token or seen >= limit:
                    break
                if isinstance(total, int) and seen >= total:
                    break
            if not pages:
                continue
            account_name, details = (await self._widget(slug)) if want_details else (None, {})
            for record in pages:
                record.meta["details"] = details
                record.meta["account_name"] = account_name
            records.extend(pages)
        return self.settle(records, nothing_to_fetch=self.no_board_named())

    # -- parse (FR-183) -----------------------------------------------------
    def parse(self, raw: RawRecord) -> list[dict]:
        payload = json.loads(raw.content)
        results = payload.get("results") or []
        keywords = raw.meta.get("keywords") or []
        filtering = bool(raw.meta.get("title_filter"))
        out: list[dict] = []
        for job in results[: raw.meta.get("max_records") or len(results)]:
            if not isinstance(job, dict):
                continue
            # A draft or an internal-only posting is not a public vacancy.
            if str(job.get("state") or "published") != "published" or job.get("isInternal"):
                continue
            if filtering and not title_matches(str(job.get("title") or ""), keywords):
                continue
            out.append(self._one(job, raw))
        return out

    def _one(self, job: dict, raw: RawRecord) -> dict[str, Any]:
        slug = raw.meta.get("slug") or ""
        shortcode = str(job.get("shortcode") or "")
        detail = (raw.meta.get("details") or {}).get(shortcode) or {}

        description = html_to_text(detail.get("description"))
        place = job.get("location") if isinstance(job.get("location"), dict) else {}
        location = ", ".join(
            part
            for part in (
                str(place.get("city") or "").strip(),
                str(place.get("region") or "").strip(),
                str(place.get("country") or "").strip(),
            )
            if part
        )
        type_word = _TYPE_WORDS.get(str(job.get("type") or "").lower(), str(job.get("type") or ""))
        workplace = str(job.get("workplace") or "").replace("_", " ")
        apply_url = detail.get("application_url") or APPLY_URL.format(
            slug=slug, shortcode=shortcode
        )
        channel, target = application_route(apply_url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(description[:6000])

        return {
            "company_id": raw.meta.get("company_id"),
            # The v3 list names no employer; the widget's account name is the
            # employer, and the plan item's company name is the fallback.
            "company_name_raw": raw.meta.get("account_name") or raw.meta.get("company_name"),
            "title": job.get("title"),
            "function_family": ", ".join(
                str(d) for d in (job.get("department") or []) if d
            ) or detail.get("department"),
            "seniority": detail.get("experience"),
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or None,
            "country": country_from_location(place.get("countryCode"), place.get("country")),
            # The structured `workplace` wins: an advert that says "hybrid
            # working is common here" must not overturn a board that states
            # the posting is remote.
            "work_arrangement": normalise_work_arrangement(
                workplace, "remote" if job.get("remote") else ""
            ) or normalise_work_arrangement(description[:2000]),
            "contract_type": normalise_contract_type(type_word, detail.get("employment_type")),
            "fte_percentage": fte_percentage(type_word, detail.get("employment_type")),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(job.get("published") or detail.get("published_on")),
            "language": normalise_language(job.get("language"), description),
            "application_channel": channel,
            "application_target": target,
            "source_url": JOB_URL.format(slug=slug, shortcode=shortcode)
            if slug and shortcode
            else detail.get("url"),
        }
