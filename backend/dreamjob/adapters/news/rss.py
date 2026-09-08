"""Company newsrooms and RSS/Atom feeds (FR-225, FR-402, IR-102).

Press releases are where funding rounds, office openings, product launches,
reorganisations and leadership changes are announced, which makes a company's
own newsroom the highest-yield source for hiring signals and for the timing
intelligence built on them.

The feed is found rather than configured: the website crawl already reads the
``<link rel="alternate">`` declarations of every page it visits
(:func:`~dreamjob.adapters.website.crawler.discover_feed_links`), and
:func:`candidate_feed_urls` adds the handful of conventional paths that
newsroom software uses when the declaration is missing.  Everything is fetched
through the egress layer, so robots.txt, rate limiting and raw-document capture
apply here too (IR-102, FR-182, FR-183).

RSS 2.0, RSS 1.0/RDF and Atom are all parsed by the same code: they differ in
element names, not in the four things this module needs - title, link, date and
summary.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from dreamjob.adapters.base import (
    AccessMethod,
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
    ToSStatus,
    register_adapter,
)
from dreamjob.adapters.website.crawler import extract_text, normalise_url
from dreamjob.egress.client import EgressClient, RobotsDisallowed

log = logging.getLogger(__name__)

#: Conventional feed paths, tried when a site declares no feed of its own.
COMMON_FEED_PATHS = (
    "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml",
    "/news/feed", "/news/rss", "/blog/feed", "/blog/rss", "/press/feed",
    "/nieuws/feed", "/actualites/feed", "/feed/atom", "/rss/news.xml",
)

MAX_FEED_BYTES = 4_000_000
MAX_ITEMS_PER_FEED = 60

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
_RSS10 = "{http://purl.org/rss/1.0/}"

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class NewsItem:
    """One entry from a company newsroom."""

    title: str
    url: str
    published_at: str | None
    summary: str
    feed_url: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}".strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "summary": self.summary[:1200],
            "feed_url": self.feed_url,
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def candidate_feed_urls(home_url: str, declared: list[str] | None = None) -> list[str]:
    """Declared feeds first, then the conventional paths, de-duplicated."""
    out: list[str] = []
    for url in declared or []:
        normalised = normalise_url(url, home_url)
        if normalised and normalised not in out:
            out.append(normalised)
    parsed = urlparse(home_url)
    if parsed.scheme and parsed.netloc:
        root = f"{parsed.scheme}://{parsed.netloc}"
        for path in COMMON_FEED_PATHS:
            candidate = urljoin(root, path)
            if candidate not in out:
                out.append(candidate)
    return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first_text(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    for name in names:
        for child in element:
            if _local(child.tag) == name and (child.text or "").strip():
                return (child.text or "").strip()
    return ""


def _entry_link(element: ElementTree.Element, base_url: str) -> str:
    for child in element:
        if _local(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or child.text or "").strip()
        rel = (child.attrib.get("rel") or "alternate").lower()
        if href and rel == "alternate":
            return normalise_url(href, base_url) or href
    guid = _first_text(element, ("guid", "id"))
    if guid.startswith("http"):
        return normalise_url(guid, base_url) or guid
    return ""


def parse_date(value: str | None) -> str | None:
    """RFC-822 (RSS) or ISO-8601 (Atom) to the canonical timestamp format."""
    if not value:
        return None
    text = value.strip()
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        pass
    match = _ISO_DATE.search(text)
    if match:
        try:
            day = datetime.fromisoformat(match.group(0)).replace(tzinfo=UTC)
            return day.isoformat(timespec="seconds")
        except ValueError:
            return None
    return None


def parse_feed(xml: str, feed_url: str = "") -> list[NewsItem]:
    """Parse an RSS 2.0, RSS 1.0/RDF or Atom document into news items.

    The document is untrusted input, so a malformed or hostile feed yields an
    empty list rather than an exception (NFR-205, FR-183).
    """
    if not xml or len(xml) > MAX_FEED_BYTES:
        return []
    try:
        root = ElementTree.fromstring(xml.strip())
    except ElementTree.ParseError:
        return []

    entries: list[ElementTree.Element] = []
    for element in root.iter():
        if _local(element.tag) in ("item", "entry"):
            entries.append(element)
        if len(entries) >= MAX_ITEMS_PER_FEED:
            break

    items: list[NewsItem] = []
    for entry in entries:
        title = extract_text(_first_text(entry, ("title",)), drop_chrome=False)
        summary_raw = _first_text(entry, ("description", "summary", "encoded", "content"))
        summary = extract_text(summary_raw, drop_chrome=False)[:4000]
        published = parse_date(
            _first_text(entry, ("pubdate", "published", "updated", "date", "created"))
        )
        url = _entry_link(entry, feed_url)
        if not title and not summary:
            continue
        items.append(
            NewsItem(
                title=title[:300],
                url=url,
                published_at=published,
                summary=summary,
                feed_url=feed_url,
            )
        )
    return items


def looks_like_feed(text: str) -> bool:
    head = (text or "")[:600].lstrip().lower()
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head or "<rdf:rdf" in head


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def fetch_feed(egress: EgressClient, url: str) -> list[NewsItem]:
    """Fetch and parse one feed; an unreachable or non-feed URL yields nothing."""
    try:
        result = await egress.fetch(url)
    except RobotsDisallowed:
        log.debug("robots.txt disallows feed %s", url)
        return []
    except Exception as exc:  # noqa: BLE001 - a missing feed is the normal case
        log.debug("Feed %s unavailable: %s", url, exc)
        return []
    if not result.ok or not looks_like_feed(result.text):
        return []
    return parse_feed(result.text, url)


async def collect_news(
    home_url: str,
    *,
    declared_feeds: list[str] | None = None,
    egress: EgressClient | None = None,
    max_feeds: int = 4,
    max_items: int = 40,
) -> list[NewsItem]:
    """Newsroom items for one company, newest first (FR-225).

    Candidate feeds are tried in order of likelihood and the walk stops as soon
    as enough items have been collected, so a site that declares its feed costs
    one request and a site that does not costs at most ``max_feeds``.
    """
    own_client = egress is None
    client = egress or EgressClient()
    if own_client:
        await client.__aenter__()
    try:
        items: list[NewsItem] = []
        seen_urls: set[str] = set()
        tried = 0
        for candidate in candidate_feed_urls(home_url, declared_feeds):
            if tried >= max_feeds or len(items) >= max_items:
                break
            tried += 1
            for item in await fetch_feed(client, candidate):
                key = item.url or item.title
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                items.append(item)
    finally:
        if own_client:
            await client.__aexit__(None, None, None)

    items.sort(key=lambda i: i.published_at or "", reverse=True)
    return items[:max_items]


# ---------------------------------------------------------------------------
# Adapter (NFR-601)
# ---------------------------------------------------------------------------


@register_adapter
class NewsFeedAdapter(SourceAdapter):
    """Company newsrooms and RSS/Atom feeds as a hiring-signal source (FR-225)."""

    key = "news.rss"
    display_name = "Company newsroom / RSS"
    source_type = SourceType.NEWS
    access_method = AccessMethod.HTTP
    capabilities = AdapterCapabilities(
        keyword_search=False, company_lookup=True, pagination=False, max_results_per_query=60
    )
    tos_status = ToSStatus.PERMITTED
    legal_notes = "Public syndication feeds published by the company itself."

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        """One plan item per company newsroom the planner points at (FR-162)."""
        sites = caps.get("company_sites") or caps.get("sites") or []
        items: list[PlanItem] = []
        for site in sites:
            url = site.get("url") if isinstance(site, dict) else str(site)
            if not url:
                continue
            items.append(
                PlanItem(
                    adapter_key=self.key,
                    native_query={
                        "url": url,
                        "company_id": site.get("company_id") if isinstance(site, dict) else None,
                        "feeds": site.get("feeds") if isinstance(site, dict) else None,
                    },
                    rationale=f"Read the newsroom of {url} for hiring signals (FR-225)",
                    estimated_pages=3,
                    estimated_seconds=10,
                )
            )
        return items

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = item.native_query or {}
        url = str(query.get("url") or "")
        if not url:
            return []
        items = await collect_news(
            url, declared_feeds=query.get("feeds") or [], egress=self.egress
        )
        return [
            RawRecord(
                url=news.url or news.feed_url,
                content=news.text,
                content_type="text/plain",
                meta={"item": news.as_dict(), "company_id": query.get("company_id")},
            )
            for news in items
        ]

    def parse(self, raw: RawRecord) -> list[dict]:
        item = (raw.meta or {}).get("item") or {}
        return [item] if item else []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        """A news item becomes a hiring signal only when it says something (FR-225)."""
        company_id = (raw.meta or {}).get("company_id")
        if not company_id:
            return None
        from dreamjob.pipeline.signals import classify_news  # noqa: PLC0415 - avoids a cycle

        signal_type, strength = classify_news(parsed.get("title", ""), parsed.get("summary", ""))
        if not signal_type:
            return None
        return NormalisedRecord(
            entity_type="hiring_signal",
            data={
                "company_id": company_id,
                "signal_type": signal_type,
                "description": (parsed.get("title") or "")[:500],
                "occurred_at": parsed.get("published_at"),
                "source_url": parsed.get("url") or raw.url,
                "strength": strength,
            },
            confidence=strength,
            provenance={"adapter": self.key, "url": parsed.get("url") or raw.url},
        )
