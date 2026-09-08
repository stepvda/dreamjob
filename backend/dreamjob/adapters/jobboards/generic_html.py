"""Selector-driven job-board adapter (NFR-601, FR-183, IR-101).

Most job boards are the same shape: a search URL with a keyword and a location
in it, a list of cards, and a detail page per advert.  Nothing about that needs
code, so this adapter takes its whole behaviour from the plan item's
``native_query`` - an administrator can add a board by storing a configuration
row, which is exactly the extensibility NFR-601 asks for.

``native_query`` keys
---------------------
``url_template``      ``"https://board.tld/jobs?q={query}&l={location}&p={page}"``
``start_urls``        explicit listing URLs, used instead of a template
``queries``           keywords; ``plan()`` fills these from the directives
``locations``         locations to expand ``{location}`` with
``pages``             how many result pages to walk (default 1)
``page_start``        first page number (default 1)
``detail``            follow each card to its own page (default true)
``selectors``         listing-page CSS: ``list_item``, ``url``, ``title``,
                      ``company``, ``location``, ``posted_at``, ``description``,
                      ``contract_type``, ``salary``
``detail_selectors``  detail-page CSS: ``title``, ``description``, ``company``,
                      ``location``, ``posted_at``, ``apply_url``
``language``          force a language instead of detecting one
``country``           ISO-2 fallback for the ``vacancy.country`` column

A selector is ``"css"`` for the element's text, ``"css@attr"`` for an
attribute (``href``/``src`` are resolved against the page URL), and
alternatives may be separated by ``||``.

Extraction order is always the same and is what FR-183 prescribes: a
schema.org ``JobPosting`` block first, CSS selectors next, and the LLM only for
pages that yield neither.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote_plus, urljoin

from dreamjob.adapters.base import (
    AccessMethod,
    AdapterCapabilities,
    PlanItem,
    RawRecord,
    SourceType,
    ToSStatus,
    register_adapter,
)
from dreamjob.adapters.vacancy_source import (
    VacancySourceAdapter,
    application_route,
    country_from_location,
    html_to_text,
    jobposting_to_fields,
    jsonld_jobpostings,
    keywords_from,
    locations_from,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
)
from dreamjob.egress.client import FetchResult, RobotsDisallowed

try:  # pragma: no cover
    from selectolax.parser import HTMLParser
except ImportError:  # pragma: no cover
    HTMLParser = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

DEFAULT_MAX_RECORDS = 120

LISTING_KEYS = (
    "url", "title", "company", "location", "posted_at", "description",
    "contract_type", "work_arrangement", "salary",
)
DETAIL_KEYS = ("title", "description", "company", "location", "posted_at", "apply_url")


# ---------------------------------------------------------------------------
# Selector engine
# ---------------------------------------------------------------------------

def select(node: Any, spec: str | None, base_url: str = "") -> str | None:
    """Evaluate one ``"css@attr || fallback"`` selector against a node."""
    if not spec or node is None:
        return None
    for alternative in str(spec).split("||"):
        css, _, attribute = alternative.strip().partition("@")
        css = css.strip()
        target = node.css_first(css) if css else node
        if target is None:
            continue
        if attribute:
            value = (target.attributes or {}).get(attribute.strip())
            if not value:
                continue
            if attribute.strip() in ("href", "src", "data-href") and base_url:
                return urljoin(base_url, value)
            return value.strip()
        text = target.text(separator=" ").strip()
        if text:
            return " ".join(text.split())
    return None


class HtmlBoardAdapter(VacancySourceAdapter):
    """Base for every HTML job board; subclasses only supply defaults and a plan."""

    source_type = SourceType.JOB_BOARD
    access_method = AccessMethod.HTTP
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=True, pagination=True, max_results_per_query=100
    )
    tos_status = ToSStatus.PERMITTED
    base_confidence = 0.7

    #: Merged underneath the plan item's ``native_query``.
    defaults: dict[str, Any] = {}

    # -- configuration ------------------------------------------------------
    def config(self, item: PlanItem) -> dict[str, Any]:
        merged = dict(self.defaults)
        merged.update({k: v for k, v in (item.native_query or {}).items() if v is not None})
        selectors = dict(self.defaults.get("selectors") or {})
        selectors.update((item.native_query or {}).get("selectors") or {})
        merged["selectors"] = selectors
        details = dict(self.defaults.get("detail_selectors") or {})
        details.update((item.native_query or {}).get("detail_selectors") or {})
        merged["detail_selectors"] = details
        return merged

    def listing_urls(self, cfg: dict[str, Any]) -> list[str]:
        explicit = [str(u) for u in cfg.get("start_urls") or []]
        if explicit:
            return explicit
        template = cfg.get("url_template")
        if not template:
            return []
        queries = [str(q) for q in cfg.get("queries") or [""]]
        locations = [str(loc) for loc in cfg.get("locations") or [""]]
        one_page = requested_page(cfg)
        first = one_page or int(cfg.get("page_start", 1))
        pages = 1 if one_page else max(1, int(cfg.get("pages", 1)))
        urls: list[str] = []
        for query in queries:
            for location in locations:
                for offset in range(pages):
                    urls.append(
                        str(template).format(
                            query=quote_plus(query),
                            query_raw=query,
                            location=quote_plus(location),
                            location_raw=location,
                            page=first + offset,
                            offset=offset * int(cfg.get("page_size", 25)),
                            country=str(cfg.get("country") or ""),
                        )
                    )
        return urls

    # -- plan (FR-162) ------------------------------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        queries = keywords_from(directives, composite_profile)
        locations = locations_from(directives)
        pages = int(caps.get("max_pages_per_source") or self.defaults.get("pages") or 2)
        max_records = int(caps.get("max_records_per_source") or DEFAULT_MAX_RECORDS)
        configured = directives.get("boards") or caps.get("boards") or []
        bases: list[dict[str, Any]] = [
            b
            for b in configured
            if isinstance(b, dict) and b.get("adapter_key") in (None, self.key)
        ]
        if not bases and (self.defaults.get("url_template") or self.defaults.get("start_urls")):
            bases = [{}]
        items: list[PlanItem] = []
        for base in bases:
            native: dict[str, Any] = {
                "queries": base.get("queries") or queries,
                "locations": base.get("locations") or locations,
                "pages": base.get("pages") or pages,
                "max_records": max_records,
            }
            native.update({k: v for k, v in base.items() if k not in native})
            items.append(
                PlanItem(
                    adapter_key=self.key,
                    native_query=native,
                    rationale=f"{self.display_name}: keyword search over the public listing",
                    estimated_pages=pages * max(1, len(native["queries"] or [1])),
                    estimated_seconds=20 * pages,
                    caps={"max_records": max_records},
                )
            )
        return items

    # -- fetch (IR-102) -----------------------------------------------------
    async def _get(self, url: str, **kwargs: Any) -> FetchResult | None:
        if self.egress is None:
            raise RuntimeError(f"[{self.key}] fetch() needs an EgressClient (IR-102)")
        try:
            result = await self.egress.fetch(
                url, access_method=self.access_method.value, **kwargs
            )
        except RobotsDisallowed:
            log.warning("[%s] robots.txt disallows %s (FR-182)", self.key, url)
            return None
        except Exception as exc:  # noqa: BLE001 - a blocked board must not stop the job
            log.warning("[%s] fetch failed for %s: %s", self.key, url, exc)
            return None
        if not result.ok:
            log.info("[%s] %s returned HTTP %s", self.key, url, result.status_code)
            return None
        return result

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        cfg = self.config(item)
        max_records = int(cfg.get("max_records") or DEFAULT_MAX_RECORDS)
        follow_detail = cfg.get("detail", True) is not False
        records: list[RawRecord] = []
        collected = 0

        for url in self.listing_urls(cfg):
            if collected >= max_records:
                break
            listing = await self._get(url)
            if listing is None:
                continue
            cards = self.list_cards(listing.text, url, cfg)
            complete = [c for c in cards if c.get("_complete")]
            emit = bool(complete) or not follow_detail
            if emit:
                # Only a listing that yields records itself becomes a raw record to
                # parse; when detail pages are followed the listing would parse to
                # nothing and would depress the NFR-403 extraction rate. The page
                # itself is still stored by the egress layer (FR-183).
                records.append(
                    RawRecord(
                        url=url,
                        content=listing.text,
                        content_type="text/html",
                        raw_document_id=listing.raw_document_id,
                        meta={"kind": "list", "cfg": cfg, "emit": True},
                    )
                )
                collected += len(complete)
                continue
            for card in cards:
                if collected >= max_records:
                    break
                detail_url = card.get("source_url")
                if not detail_url:
                    continue
                page = await self._get(detail_url)
                if page is None:
                    continue
                collected += 1
                records.append(
                    RawRecord(
                        url=detail_url,
                        content=page.text,
                        content_type="text/html",
                        raw_document_id=page.raw_document_id,
                        meta={"kind": "detail", "cfg": cfg, "card": card},
                    )
                )
        return records

    # -- parse (FR-183) -----------------------------------------------------
    def list_cards(self, markup: str, url: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Cards on a listing page; a JSON-LD listing short-circuits detail fetches."""
        postings = jsonld_jobpostings(markup)
        if len(postings) > 1:
            out = []
            for posting in postings:
                fields = jobposting_to_fields(posting, str(posting.get("url") or url))
                # The marker goes on after _finish, which strips it: it tells fetch()
                # that this card needs no detail request.
                card = self._finish(fields, cfg)
                card["_complete"] = True
                out.append(card)
            return out
        if HTMLParser is None:  # pragma: no cover - selectolax is a declared dependency
            log.error("[%s] selectolax is not installed; cannot parse HTML listings", self.key)
            return []
        selectors = cfg.get("selectors") or {}
        item_selector = selectors.get("list_item")
        if not item_selector:
            return []
        tree = HTMLParser(markup)
        cards: list[dict[str, Any]] = []
        for node in tree.css(item_selector):
            card = {key: select(node, selectors.get(key), url) for key in LISTING_KEYS}
            card["source_url"] = card.pop("url", None)
            if card.get("title") or card.get("source_url"):
                cards.append(card)
        if not cards:
            # NFR-403: either the board changed its markup or it renders results
            # client-side. Both need the administrator's attention, not silence.
            log.info(
                "[%s] no cards matched %r on %s; check the stored selectors",
                self.key, item_selector, url,
            )
        return cards

    def parse(self, raw: RawRecord) -> list[dict]:
        cfg = raw.meta.get("cfg") or {}
        if raw.meta.get("kind") == "detail":
            return self._parse_detail(raw, cfg)
        if not raw.meta.get("emit"):
            return []
        return [
            self._finish(card, cfg) for card in self.list_cards(raw.content, raw.url, cfg)
            if card.get("title")
        ]

    def _parse_detail(self, raw: RawRecord, cfg: dict[str, Any]) -> list[dict]:
        card = dict(raw.meta.get("card") or {})
        postings = jsonld_jobpostings(raw.content)
        if postings:
            fields = jobposting_to_fields(postings[0], raw.url)
            merged = {**card, **{k: v for k, v in fields.items() if v not in (None, "", [])}}
            merged["source_url"] = raw.url
            return [self._finish(merged, cfg)]

        selectors = cfg.get("detail_selectors") or {}
        if HTMLParser is not None and selectors:
            tree = HTMLParser(raw.content)
            picked = {key: select(tree, selectors.get(key), raw.url) for key in DETAIL_KEYS}
            picked = {k: v for k, v in picked.items() if v}
            if picked.get("description"):
                merged = {**card, **picked}
                merged["source_url"] = raw.url
                merged["application_target"] = picked.get("apply_url") or card.get("source_url")
                merged.pop("apply_url", None)
                return [self._finish(merged, cfg)]

        # FR-183: nothing deterministic worked, so ask the model.
        text = html_to_text(raw.content)
        extracted = self.llm_extract(text, raw.url)
        if extracted:
            extracted.setdefault("company_name_raw", card.get("company"))
            extracted["source_url"] = raw.url
            return [self._finish(extracted, cfg)]
        return []

    # -- shared field completion -------------------------------------------
    def _finish(self, fields: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        row = dict(fields)
        row.pop("_complete", None)
        if row.get("company") and not row.get("company_name_raw"):
            row["company_name_raw"] = row.pop("company")
        row.pop("company", None)
        description = row.get("description") or ""
        if "<" in description:
            description = html_to_text(description)
            row["description"] = description
        if not row.get("required_skills"):
            required, desirable = split_skills(description)
            row["required_skills"] = required
            row["desirable_skills"] = desirable
        row["posted_at"] = parse_datetime(row.get("posted_at"))
        row["work_arrangement"] = row.get("work_arrangement") or normalise_work_arrangement(
            row.get("location"), row.get("title"), description[:2000]
        )
        row["contract_type"] = normalise_contract_type(
            row.get("contract_type"), row.get("title"), description[:2000]
        )
        if not row.get("salary_min"):
            low, high, currency = parse_salary_text(row.get("salary") or description[:6000])
            row["salary_min"], row["salary_max"], row["salary_currency"] = low, high, currency
        row.pop("salary", None)
        row["country"] = row.get("country") or country_from_location(
            row.get("location"), cfg.get("country")
        ) or (cfg.get("country") or None)
        if not row.get("application_channel"):
            channel, target = application_route(
                row.get("application_target") or row.get("source_url"), description
            )
            row["application_channel"] = channel
            row["application_target"] = target
        row["language"] = normalise_language(
            cfg.get("language") or row.get("language"), description or row.get("title")
        )
        return row


@register_adapter
class GenericHtmlBoardAdapter(HtmlBoardAdapter):
    """A board an administrator added by storing selectors, not by writing code."""

    key = "board.generic"
    display_name = "Generic HTML job board"
    legal_notes = (
        "Behaviour comes entirely from the stored configuration. The administrator "
        "who adds a board is responsible for checking that board's terms of use; "
        "robots.txt and rate limits are enforced by the egress layer (FR-182)."
    )
