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

The campaign planner writes the same three things under different names -
``keywords``, ``location``, ``countries`` - so :meth:`HtmlBoardAdapter.config`
reads both vocabularies.  It is one character of difference (``keywords`` vs
``queries``, ``location`` vs ``locations``) and it silently emptied the search
box of every board query the planner produced, which is why the live boards
were all fetching ``?trefwoord=&plaats=``.

A selector is ``"css"`` for the element's text, ``"css@attr"`` for an
attribute (``href``/``src`` are resolved against the page URL), and
alternatives may be separated by ``||``.

Extraction order is always the same and is what FR-183 prescribes: a
schema.org ``JobPosting`` block first, CSS selectors next, and the LLM only for
pages that yield neither.
"""

from __future__ import annotations

import logging
import re
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
    country_terms,
    html_to_text,
    jobposting_to_fields,
    jsonld_jobpostings,
    keywords_from,
    location_terms,
    locations_from,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    query_terms,
    requested_page,
    split_skills,
)
from dreamjob.db.connection import execute

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

#: Text a client-rendered page shows while its JavaScript loads.  A selector
#: that matches one of these has matched the application shell, not the advert.
PLACEHOLDER_RE = re.compile(
    r"toepassing laden|pagina laden|application loading|loading\.\.\.|laden\.\.\.|"
    r"chargement|wird geladen|please enable javascript|javascript is required|"
    r"you need to enable javascript",
    re.IGNORECASE,
)
#: An advert shorter than this is a fragment or a placeholder, not a description.
MIN_DESCRIPTION_CHARS = 120


def is_extracted(description: str | None, cfg: dict[str, Any] | None = None) -> bool:
    """Did the detail page actually yield an advert? (NFR-403)"""
    text = (description or "").strip()
    minimum = int((cfg or {}).get("min_description_chars") or MIN_DESCRIPTION_CHARS)
    if len(text) < minimum:
        return False
    return not PLACEHOLDER_RE.search(text[:400])


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
        native = dict(getattr(item, "native_query", None) or {})
        merged = dict(self.defaults)
        merged.update({k: v for k, v in native.items() if v is not None})
        selectors = dict(self.defaults.get("selectors") or {})
        selectors.update(native.get("selectors") or {})
        merged["selectors"] = selectors
        details = dict(self.defaults.get("detail_selectors") or {})
        details.update(native.get("detail_selectors") or {})
        merged["detail_selectors"] = details

        # FR-162: read the planner's vocabulary as well as this adapter's own.
        # A plan item that says {"keywords": ["AI Engineer"], "location":
        # "Belgium"} means exactly what {"queries": [...], "locations": [...]}
        # means; ignoring it searched every board for nothing at all.
        queries = query_terms(merged)
        if queries:
            merged["queries"] = queries
        locations = location_terms(merged)
        if locations:
            merged["locations"] = locations
        countries = country_terms(merged)
        if countries and not merged.get("country"):
            merged["country"] = countries[0]
        if merged.get("max_pages") and "pages" not in native:
            merged["pages"] = merged["max_pages"]
        return merged

    def has_route(self, cfg: dict[str, Any] | None = None) -> bool:
        """Can this board be reached with the configuration it has? (NFR-601)

        A configuration with neither a URL template nor a start URL issues no
        request at all.  Reporting that as ``done`` with zero records is what
        let ``board.generic`` sit in every campaign plan, consuming plan slots
        and page budget, without ever having made a single request.
        """
        cfg = self.defaults if cfg is None else cfg
        return bool(cfg.get("url_template") or cfg.get("start_urls"))

    def register(self) -> None:
        """FR-161: a source that cannot reach anything is not an enabled source.

        The administrator's own decision wins: once the source is acknowledged
        (IR-101) this leaves ``enabled`` alone, so configuring a board and
        switching it on is not undone by the next restart.
        """
        super().register()
        if self.has_route():
            return
        changed = execute(
            "UPDATE source_catalogue SET enabled = 0 "
            "WHERE adapter_key = ? AND acknowledged_at IS NULL AND enabled = 1",
            (self.key,),
        )
        if changed:
            log.info(
                "[%s] catalogued as disabled: it has no URL template and no start URLs, "
                "so an administrator has to store a board configuration first (NFR-601)",
                self.key,
            )

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

    # -- fetch (IR-102, FR-182) ---------------------------------------------
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
        # FR-185: "blocked by a bot filter", "the search URL 404s" and "this
        # board has no matching vacancies" are three different answers and the
        # plan item has to be able to tell them apart.
        return self.settle(
            records,
            nothing_to_fetch=(
                "no listing URL: the plan item has neither start_urls nor a url_template "
                "this adapter could fill in (NFR-601)"
            ),
        )

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
            if is_extracted(picked.get("description"), cfg):
                merged = {**card, **picked}
                merged["source_url"] = raw.url
                merged["application_target"] = picked.get("apply_url") or card.get("source_url")
                merged.pop("apply_url", None)
                return [self._finish(merged, cfg)]
            if picked.get("description"):
                # A single-page application answers 200 with its loading shell.
                # Writing "Toepassing laden..." into vacancy.description as
                # though it were the advert is worse than extracting nothing.
                log.info(
                    "[%s] %s served a placeholder rather than an advert (%r); treating it "
                    "as no extraction (NFR-403)",
                    self.key, raw.url, picked["description"][:60],
                )

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
    """A board an administrator added by storing selectors, not by writing code.

    It has no defaults of its own, so until a configuration exists it can reach
    nothing: :meth:`HtmlBoardAdapter.register` therefore catalogues it as
    disabled, and it is not selected into a plan (FR-161, FR-164).  Enabling it
    is the same act as configuring it.
    """

    key = "board.generic"
    display_name = "Generic HTML job board"
    legal_notes = (
        "Behaviour comes entirely from the stored configuration, so this source is "
        "catalogued as disabled until an administrator stores one. The administrator "
        "who adds a board is responsible for checking that board's terms of use; "
        "robots.txt and rate limits are enforced by the egress layer (FR-182)."
    )
