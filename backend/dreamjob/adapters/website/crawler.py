"""Bounded crawl of a company's own website (FR-221, FR-341, IR-102).

A company site is the single richest source about a company, and also the one
with the worst signal-to-noise ratio: a 400-page site holds perhaps fifteen
pages that say anything about the business.  So the crawl is not breadth-first
over everything within *n* hops; it is a **budgeted best-first** walk driven by
:func:`score_url`, which ranks every discovered link by how likely it is to hold
what FR-221 asks for - business description, products and services, markets,
reference cases and customers, size indicators, office locations, team and
leadership, organisational structure, news and press, the careers page,
technology-stack indicators and values/culture statements.

With a 30-page budget that ordering is the whole game.  Two further rules keep
it honest:

* **per-kind quotas** - twelve press releases are worth less than one about
  page, so each page kind may consume only part of the budget;
* **hard caps on depth and count**, checked before every enqueue, so a
  calendar or a paginated blog cannot swallow the crawl.

robots.txt, rate limiting, caching and raw-document capture all belong to
:class:`~dreamjob.egress.client.EgressClient` (IR-102, FR-182, FR-183); a page
this crawler is not allowed to fetch is counted and skipped, never fetched
another way.
"""

from __future__ import annotations

import heapq
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

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
from dreamjob.adapters.query_errors import UnusableQuery
from dreamjob.egress.client import EgressClient, RobotsDisallowed
from dreamjob.pipeline import html_dom

log = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 30
DEFAULT_MAX_DEPTH = 3
#: Fewer pages than this cannot answer FR-221: the home page plus the handful
#: of pages that carry the business description, the careers page and the team.
MIN_CRAWL_PAGES = 8
MAX_TEXT_CHARS = 24_000


# ---------------------------------------------------------------------------
# What the crawl is looking for (FR-221)
# ---------------------------------------------------------------------------

#: Page kind -> (url/anchor keywords, weight, share of the page budget).
#:
#: The weights are the crawler's editorial judgement about FR-221: an about
#: page and a customers page each say more about a company than a press
#: release, and a values page is what FR-384 later reads.
PAGE_KINDS: dict[str, tuple[tuple[str, ...], float, float]] = {
    "about": (
        (
            "about", "about-us", "aboutus", "who-we-are", "wie-zijn-wij", "over-ons",
            "qui-sommes-nous", "a-propos", "ueber-uns", "uber-uns", "company",
            "our-story", "ons-verhaal", "history", "geschiedenis", "mission",
        ),
        1.00,
        0.20,
    ),
    "products": (
        (
            "product", "products", "producten", "service", "services", "diensten",
            "solution", "solutions", "oplossingen", "platform", "offering",
            "what-we-do", "wat-we-doen", "expertise", "capabilities", "portfolio",
        ),
        0.95,
        0.25,
    ),
    "customers": (
        (
            "customer", "customers", "client", "clients", "klanten", "case", "cases",
            "case-study", "case-studies", "referenc", "referenties", "success-stories",
            "portfolio", "projects", "projecten", "realisaties", "testimonial",
        ),
        0.90,
        0.20,
    ),
    "team": (
        (
            "team", "teams", "people", "leadership", "management", "directie", "bestuur",
            "board", "our-team", "ons-team", "equipe", "equipe-de-direction", "staff",
            "medewerkers", "founders", "executive", "governance", "organisation",
            "organization", "organigram", "structure", "departments", "afdelingen",
        ),
        0.85,
        0.15,
    ),
    "careers": (
        (
            "career", "careers", "job", "jobs", "vacature", "vacatures", "vacancy",
            "vacancies", "werken-bij", "work-with-us", "join-us", "join", "hiring",
            "emploi", "recrutement", "karriere", "stellenangebote", "opportunities",
        ),
        0.85,
        0.10,
    ),
    "news": (
        (
            "news", "nieuws", "press", "pers", "persbericht", "newsroom", "media",
            "blog", "insights", "actualites", "actualite", "presse", "announcement",
            "publications", "updates",
        ),
        0.70,
        0.20,
    ),
    "locations": (
        (
            "location", "locations", "locaties", "office", "offices", "kantoor",
            "kantoren", "contact", "contact-us", "contacteer", "find-us", "vestigingen",
            "sites", "adressen", "standorte", "nous-trouver",
        ),
        0.70,
        0.10,
    ),
    "values": (
        (
            "value", "values", "waarden", "culture", "cultuur", "our-culture",
            "sustainability", "duurzaamheid", "csr", "esg", "purpose", "vision",
            "visie", "ethics", "code-of-conduct", "diversity", "inclusion",
            "employer", "life-at", "why-us", "working-at", "werken-bij",
        ),
        0.80,
        0.15,
    ),
    "tech": (
        (
            "technology", "technologie", "tech", "stack", "engineering", "developers",
            "api", "docs", "documentation", "integrations", "security",
        ),
        0.55,
        0.10,
    ),
    "investors": (
        (
            "investor", "investors", "investeerders", "financial", "annual-report",
            "jaarverslag", "reports", "shareholders", "results", "governance",
        ),
        0.65,
        0.10,
    ),
}

#: Paths that never repay a page of the budget.
NOISE_TOKENS = (
    "privacy", "cookie", "terms", "disclaimer", "legal-notice", "gdpr", "sitemap",
    "login", "signin", "sign-in", "register", "account", "cart", "checkout", "basket",
    "search", "tag", "tags", "category", "categories", "archive", "author", "feed",
    "rss", "wp-json", "wp-admin", "wp-content", "wp-includes", "xmlrpc", "comment",
    "subscribe", "newsletter", "unsubscribe", "print", "share", "download", "cdn-cgi",
)

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff",
    ".css", ".js", ".mjs", ".json", ".xml", ".zip", ".gz", ".tar", ".rar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".woff", ".woff2", ".ttf",
    ".eot", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".dmg", ".exe",
)

#: Locale prefixes that are worth following; anything else is a translation of
#: a page we can already read, and a translation costs a page of the budget.
PREFERRED_LOCALES = frozenset({"en", "en-gb", "en-us", "nl", "nl-be", "nl-nl", "fr", "fr-be", "de"})

_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$")
_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|source$)", re.IGNORECASE)
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


def normalise_url(url: str, base: str = "") -> str | None:
    """Absolute, fragment-free, tracking-free form of a link, or None if unusable."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
        return None
    absolute = urljoin(base, url) if base else url
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    query = "&".join(
        part
        for part in parsed.query.split("&")
        if part and not _TRACKING_PARAMS.match(part.split("=")[0])
    )
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", query, ""))


def registrable_domain(host: str) -> str:
    """Good-enough eTLD+1 for deciding what counts as 'the company's own site'.

    A full public-suffix list would be more correct, but the only decision this
    makes is whether to follow a link, and the two-label heuristic plus the
    common compound suffixes covers the jurisdictions in scope (DR-101).
    """
    host = (host or "").lower().split(":")[0].removeprefix("www.")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    compound = {"co", "com", "org", "net", "gov", "ac", "edu"}
    if parts[-2] in compound and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(url: str, base_domain: str) -> bool:
    return registrable_domain(urlparse(url).netloc) == base_domain


def _path_tokens(url: str) -> list[str]:
    parsed = urlparse(url)
    raw = f"{parsed.path} {parsed.query}".lower()
    return [t for t in re.split(r"[^a-z0-9]+", raw) if t]


def _path_segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.lower().split("/") if s]


def _segment_hit(keyword: str, segments: list[str]) -> float:
    """How strongly a keyword matches a path, anchored on whole segments.

    Matching a keyword anywhere in the path made a case study
    (``/customer/cases/energy-company/back-pressure-forecasting``) an *about*
    page at full strength, because "company" appears inside a slug, and let
    podcast episodes take the products and values quotas.  A whole segment is
    a claim about the page; a word inside a long article slug is not.
    """
    best = 0.0
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        parts = [p for p in re.split(r"[^a-z0-9]+", segment) if p]
        if segment == keyword:
            hit = 1.0
        elif "-" in keyword and keyword in segment:
            hit = 0.9
        elif keyword in parts:
            hit = 1.0 if len(parts) == 1 else (0.8 if len(parts) == 2 else 0.5)
        else:
            continue
        if index == last and (len(segments) >= 3 or len(parts) >= 4):
            hit *= 0.55           # the tail of a deep path is an article slug
        best = max(best, hit)
    return best


def classify_url_kinds(url: str, anchor: str = "", floor: float = 0.0) -> list[tuple[str, float]]:
    """Every page kind a link matches, strongest first.

    One page is often two things - ``/who-we-are/careers`` is an about page and
    the careers page - and collapsing that to a single winner is why the crawl
    could fetch a careers page and still report that it had found none.
    """
    segments = _path_segments(url)
    anchor_words = set(re.split(r"[^a-z0-9]+", (anchor or "").lower())) - {""}
    scored: list[tuple[str, float]] = []
    for kind, (keywords, weight, _quota) in PAGE_KINDS.items():
        hit = 0.0
        for keyword in keywords:
            hit = max(hit, _segment_hit(keyword, segments))
            if hit < 0.7 and keyword in anchor_words:
                hit = max(hit, 0.7)
        score = hit * weight
        if score > floor:
            scored.append((kind, round(score, 4)))
    scored.sort(key=lambda entry: entry[1], reverse=True)
    return scored


def classify_url(url: str, anchor: str = "") -> tuple[str | None, float]:
    """Best matching page kind for a link, with the strength of the match."""
    matches = classify_url_kinds(url, anchor)
    return matches[0] if matches else (None, 0.0)


def score_url(url: str, *, anchor: str = "", depth: int = 1, base_domain: str = "") -> float:
    """Rank a discovered link by how likely it is to answer FR-221.

    Positive: it looks like one of the page kinds FR-221 enumerates, the anchor
    text says so, and it is close to the root.  Negative: boilerplate, deep
    pagination, non-HTML files, and translations of pages we can already read.
    """
    if base_domain and not same_site(url, base_domain):
        return -1.0

    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return -1.0

    tokens = _path_tokens(url)
    token_set = set(tokens)
    if token_set & set(NOISE_TOKENS):
        return -1.0

    kind, kind_score = classify_url(url, anchor)
    score = kind_score
    if kind is None:
        # An unclassified page is still worth something near the root: many
        # sites put the business description on a plain top-level page.
        score = 0.30 if depth <= 1 else 0.12

    if path in ("", "/"):
        score += 0.6

    score -= 0.18 * max(0, depth - 1)

    segments = [s for s in path.split("/") if s]
    score -= 0.05 * max(0, len(segments) - 2)

    if segments and _LOCALE_SEGMENT.match(segments[0]):
        score += 0.10 if segments[0] in PREFERRED_LOCALES else -0.55

    # Paginated archives repeat what page one already said.
    if re.search(r"/(page|p|pagina|seite)[/-]?\d+$", path) or re.search(r"\d{4}/\d{2}", path):
        score -= 0.35
    if urlparse(url).query:
        score -= 0.15
    if path.endswith(".pdf"):
        # Annual reports and press kits are worth reading, other PDFs are not.
        score += 0.10 if kind in ("investors", "news") else -0.60

    return round(score, 4)


# ---------------------------------------------------------------------------
# HTML -> text and links
# ---------------------------------------------------------------------------

_STRIP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe")
_CHROME_TAGS = ("nav", "header", "footer", "aside")


def extract_text(html: str, *, drop_chrome: bool = True) -> str:
    """Readable text of a page, with navigation chrome removed (lxml)."""
    if not html:
        return ""
    if not html_dom.LXML_AVAILABLE:  # pragma: no cover - lxml is a declared dependency
        text = re.sub(r"<[^>]+>", " ", html)
        return _tidy(text)
    tree = html_dom.Html(html)
    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    if drop_chrome:
        main = tree.css_first("main") or tree.css_first("article")
        if main is not None:
            return _tidy(main.text(separator="\n"))
        for tag in _CHROME_TAGS:
            for node in tree.css(tag):
                node.decompose()
    body = tree.body or tree.root
    if body is None:
        return ""
    return _tidy(body.text(separator="\n"))


def _tidy(text: str) -> str:
    text = _WS.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line))[:MAX_TEXT_CHARS]


def page_title(html: str) -> str:
    if not html_dom.LXML_AVAILABLE or not html:  # pragma: no cover
        match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
        return _tidy(match.group(1)) if match else ""
    node = html_dom.Html(html).css_first("title")
    return _tidy(node.text()) if node is not None else ""


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """``(url, anchor text)`` for every in-document link, de-duplicated."""
    if not html:
        return []
    out: dict[str, str] = {}
    if not html_dom.LXML_AVAILABLE:  # pragma: no cover
        for match in re.finditer(r'href=["\']([^"\']+)["\']', html):
            url = normalise_url(match.group(1), base_url)
            if url:
                out.setdefault(url, "")
        return list(out.items())
    for node in html_dom.Html(html).css("a"):
        href = (node.attributes or {}).get("href")
        url = normalise_url(href or "", base_url)
        if not url:
            continue
        anchor = _tidy(node.text() or "")[:120]
        if not out.get(url):
            out[url] = anchor
    return list(out.items())


def discover_feed_links(html: str, base_url: str) -> list[str]:
    """``<link rel="alternate">`` RSS/Atom feeds declared by the page."""
    if not html_dom.LXML_AVAILABLE or not html:  # pragma: no cover
        return []
    feeds: list[str] = []
    for node in html_dom.Html(html).css("link"):
        attrs = node.attributes or {}
        rel = (attrs.get("rel") or "").lower()
        mime = (attrs.get("type") or "").lower()
        if "alternate" not in rel:
            continue
        if "rss" not in mime and "atom" not in mime and "xml" not in mime:
            continue
        url = normalise_url(attrs.get("href") or "", base_url)
        if url and url not in feeds:
            feeds.append(url)
    return feeds


def meta_description(html: str) -> str:
    if not html_dom.LXML_AVAILABLE or not html:  # pragma: no cover
        return ""
    tree = html_dom.Html(html)
    for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
        node = tree.css_first(selector)
        if node is not None:
            content = (node.attributes or {}).get("content")
            if content:
                return _tidy(content)
    return ""


# ---------------------------------------------------------------------------
# Crawl results
# ---------------------------------------------------------------------------


@dataclass
class CrawledPage:
    """One fetched page, ready for the profiler."""

    url: str
    kind: str
    title: str
    text: str
    depth: int
    score: float
    raw_document_id: str | None = None
    status_code: int = 200
    from_cache: bool = False

    def as_source(self) -> dict[str, Any]:
        """The `sources` entry FR-222 requires on the standardised profile."""
        return {"url": self.url, "kind": self.kind, "title": self.title}


@dataclass
class CrawlResult:
    """Everything one bounded crawl produced (FR-221)."""

    home_url: str
    domain: str
    pages: list[CrawledPage] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    careers_url: str | None = None
    ats_vendor: str | None = None
    ats_slug: str | None = None
    jsonld: list[dict] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def by_kind(self, kind: str) -> list[CrawledPage]:
        return [p for p in self.pages if p.kind == kind]

    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for page in self.pages:
            counts[page.kind] = counts.get(page.kind, 0) + 1
        return counts

    def text_blocks(self, max_chars_per_kind: int = 12_000) -> dict[str, str]:
        """Crawled text grouped per page kind, for the untrusted LLM blocks (NFR-205)."""
        blocks: dict[str, str] = {}
        for page in self.pages:
            chunk = f"### {page.title or page.url}\nURL: {page.url}\n{page.text}\n"
            current = blocks.get(page.kind, "")
            if len(current) >= max_chars_per_kind:
                continue
            blocks[page.kind] = (current + "\n" + chunk)[:max_chars_per_kind]
        return blocks


# ---------------------------------------------------------------------------
# The crawler
# ---------------------------------------------------------------------------


class WebsiteCrawler:
    """Budgeted best-first crawl of one company site."""

    def __init__(
        self,
        egress: EgressClient,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_depth: int = DEFAULT_MAX_DEPTH,
        min_score: float = 0.0,
    ):
        self.egress = egress
        self.max_pages = max(1, int(max_pages))
        self.max_depth = max(0, int(max_depth))
        self.min_score = min_score

    def quota(self, kind: str) -> int:
        """How many pages of one kind the budget may buy."""
        share = PAGE_KINDS.get(kind, ((), 0.0, 0.15))[2]
        return max(1, round(self.max_pages * share))

    async def crawl(self, start_url: str, on_page: Any = None) -> CrawlResult:
        """Walk the site, best link first, until the page budget is spent."""
        home = normalise_url(start_url) or start_url
        domain = registrable_domain(urlparse(home).netloc)
        result = CrawlResult(home_url=home, domain=domain)
        stats = {"fetched": 0, "blocked": 0, "errors": 0, "skipped": 0, "considered": 0}

        seen: set[str] = {home}
        per_kind: dict[str, int] = {}
        queue: list[tuple[float, int, str, str, int]] = []
        order = 0
        heapq.heappush(queue, (-10.0, order, home, "", 0))

        while queue and len(result.pages) < self.max_pages:
            neg_score, _, url, anchor, depth = heapq.heappop(queue)
            kind = "home" if depth == 0 else self._kind_for(url, anchor, per_kind)
            if depth > 0 and per_kind.get(kind, 0) >= self.quota(kind):
                stats["skipped"] += 1
                continue

            try:
                fetched = await self.egress.fetch(url)
            except RobotsDisallowed:
                stats["blocked"] += 1
                log.debug("robots.txt disallows %s", url)
                continue
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop a crawl
                stats["errors"] += 1
                log.debug("Could not fetch %s: %s", url, exc)
                continue

            stats["fetched"] += 1
            if not fetched.ok or "html" not in (
                fetched.headers.get("content-type", "text/html").lower()
            ):
                continue

            html = fetched.text
            page = CrawledPage(
                url=url,
                kind=kind,
                title=page_title(html) or meta_description(html)[:120],
                text=extract_text(html),
                depth=depth,
                score=round(-neg_score, 4),
                raw_document_id=fetched.raw_document_id,
                status_code=fetched.status_code,
                from_cache=fetched.from_cache,
            )
            if not page.text and depth > 0:
                continue
            result.pages.append(page)
            per_kind[kind] = per_kind.get(kind, 0) + 1
            if on_page is not None:
                on_page(page)

            self._absorb_page_metadata(result, html, url, kind)

            if depth >= self.max_depth:
                continue
            for link, link_anchor in extract_links(html, url):
                if link in seen:
                    continue
                stats["considered"] += 1
                link_score = score_url(
                    link, anchor=link_anchor, depth=depth + 1, base_domain=domain
                )
                if link_score <= self.min_score:
                    continue
                seen.add(link)
                order += 1
                heapq.heappush(queue, (-link_score, order, link, link_anchor, depth + 1))

        result.stats = stats
        return result

    def _kind_for(self, url: str, anchor: str, per_kind: dict[str, int]) -> str:
        """File a page under the best kind that still has budget left.

        One page is often two things.  Filing ``/who-we-are/careers`` under its
        single strongest kind meant it competed for the *about* quota - and lost
        it to the actual about page, so the careers page of every site that
        nests it was never fetched at all, taking the ATS detection and the
        FR-225 postings signal with it.
        """
        matches = classify_url_kinds(url, anchor)
        if not matches:
            return "other"
        # Only kinds this page really is - a second kind that scores far below
        # the first is a coincidence (a "Join the beta" call to action on a
        # product page), and filing the page under it would spend the wrong
        # quota and name the wrong careers page.
        floor = matches[0][1] * 0.75
        for kind, score in matches:
            if score >= floor and per_kind.get(kind, 0) < self.quota(kind):
                return kind
        return matches[0][0]

    def _absorb_page_metadata(
        self, result: CrawlResult, html: str, url: str, kind: str
    ) -> None:
        """Deterministic finds that do not need an LLM: feeds, ATS, JSON-LD."""
        for feed in discover_feed_links(html, url):
            if feed not in result.feeds:
                result.feeds.append(feed)
        for block in organisation_jsonld(html):
            if block not in result.jsonld:
                result.jsonld.append(block)
        if not result.careers_url and is_careers_url(url, kind):
            result.careers_url = url
        if not result.ats_vendor:
            from dreamjob.adapters.ats.detect import detect_ats  # noqa: PLC0415

            vendor, slug = detect_ats(html, url)
            if vendor:
                result.ats_vendor, result.ats_slug = vendor, slug


#: How strongly a URL must read as a careers page before it is believed to be
#: one.  ``/who-we-are/careers`` scores 0.85 here and 1.0 as an about page.
CAREERS_FLOOR = 0.6


def is_careers_url(url: str, kind: str | None = None) -> bool:
    """Whether this page is the careers page, whatever kind it was filed under.

    ``classify_url`` returns one winning kind, so a careers page that also
    matches a stronger kind was filed elsewhere and ``careers_url`` stayed
    empty - taking with it the ``postings`` hiring signal (FR-225) and the ATS
    detection that the whole ATS family waits on.

    The URL has to carry the claim itself: anchor text alone ("Join the beta")
    is how a product page would otherwise be published as a company's careers
    page, which is worse than having none.
    """
    score = max(
        (value for found, value in classify_url_kinds(url) if found == "careers"), default=0.0
    )
    return score >= CAREERS_FLOOR or (kind == "careers" and score > 0)


# ---------------------------------------------------------------------------
# schema.org Organization - identity without an LLM (FR-222, DR-101)
# ---------------------------------------------------------------------------

_ORG_TYPES = {
    "organization", "corporation", "localbusiness", "ngo", "educationalorganization",
    "governmentorganization", "medicalorganization", "sportsorganization",
    "performinggroup", "onlinebusiness", "professionalservice",
}


def organisation_jsonld(html: str) -> list[dict]:
    """schema.org ``Organization`` blocks embedded in the page."""
    if not html_dom.LXML_AVAILABLE or not html:  # pragma: no cover
        return []
    import json  # noqa: PLC0415 - only needed on pages that carry JSON-LD

    found: list[dict] = []
    for node in html_dom.Html(html).css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        for candidate in _iter_jsonld(payload):
            types = candidate.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if any(str(t).lower() in _ORG_TYPES for t in types):
                found.append(candidate)
    return found


def _iter_jsonld(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        out = [payload]
        for key in ("@graph", "itemListElement", "mainEntity"):
            out.extend(_iter_jsonld(payload.get(key)))
        return out
    if isinstance(payload, list):
        out: list[dict] = []
        for item in payload:
            out.extend(_iter_jsonld(item))
        return out
    return []


def identity_from_jsonld(blocks: list[dict]) -> dict[str, Any]:
    """Legal identifiers, addresses and size from schema.org markup (DR-101)."""
    out: dict[str, Any] = {}
    locations: list[dict] = []
    for block in blocks:
        if not out.get("name") and block.get("name"):
            out["name"] = str(block["name"])[:200]
        if not out.get("legal_name") and block.get("legalName"):
            out["legal_name"] = str(block["legalName"])[:200]
        if not out.get("vat_number") and block.get("vatID"):
            out["vat_number"] = str(block["vatID"])[:60]
        for key, column in (("taxID", "legal_id"), ("leiCode", "legal_id"), ("duns", "legal_id")):
            if not out.get(column) and block.get(key):
                out[column] = str(block[key])[:60]
                out["legal_id_type"] = {"taxID": "tax_id", "leiCode": "lei", "duns": "duns"}[key]
        if not out.get("founded") and block.get("foundingDate"):
            out["founded"] = str(block["foundingDate"])[:10]
        employees = block.get("numberOfEmployees")
        if isinstance(employees, dict):
            employees = employees.get("value") or employees.get("maxValue")
        if employees and not out.get("size_fte"):
            try:
                out["size_fte"] = int(float(str(employees).replace(",", "")))
            except (TypeError, ValueError):
                pass
        for address in _addresses(block):
            if address and address not in locations:
                locations.append(address)
    if locations:
        out["locations"] = locations
    return out


def _addresses(block: dict) -> list[dict]:
    raw = block.get("address")
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        address = {
            "label": " ".join(
                str(item.get(k))
                for k in ("streetAddress", "postalCode", "addressLocality")
                if item.get(k)
            ).strip(),
            "city": item.get("addressLocality"),
            "region": item.get("addressRegion"),
            "country": item.get("addressCountry")
            if isinstance(item.get("addressCountry"), str)
            else (item.get("addressCountry") or {}).get("name"),
            "kind": "office",
        }
        if address["label"] or address["city"]:
            out.append({k: v for k, v in address.items() if v})
    return out


# ---------------------------------------------------------------------------
# Adapter (NFR-601)
# ---------------------------------------------------------------------------


@register_adapter
class CompanyWebsiteAdapter(SourceAdapter):
    """The company's own website as a source of company records (FR-221, FR-341)."""

    key = "website.crawl"
    display_name = "Company website"
    source_type = SourceType.WEBSITE
    access_method = AccessMethod.HTTP
    capabilities = AdapterCapabilities(
        keyword_search=False,
        company_lookup=True,
        pagination=False,
        max_results_per_query=1,
    )
    tos_status = ToSStatus.PERMITTED
    legal_notes = "Public company website, fetched within robots.txt and rate limits (FR-182)."
    rate_limit_rps = 0.5

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        """One plan item per company website named in the caps (FR-162).

        The planner supplies the sites: this adapter has nothing to search on
        its own, it deepens what other sources found.
        """
        sites = caps.get("company_sites") or caps.get("sites") or []
        max_pages = int(caps.get("max_pages_per_site") or DEFAULT_MAX_PAGES)
        items: list[PlanItem] = []
        for site in sites:
            url = site.get("url") if isinstance(site, dict) else str(site)
            if not url:
                continue
            company_id = site.get("company_id") if isinstance(site, dict) else None
            items.append(
                PlanItem(
                    adapter_key=self.key,
                    native_query={
                        "url": url,
                        "company_id": company_id,
                        "max_pages": max_pages,
                        "max_depth": int(caps.get("max_depth") or DEFAULT_MAX_DEPTH),
                    },
                    rationale=f"Crawl {url} for the standardised company profile (FR-221)",
                    estimated_pages=max_pages,
                    estimated_seconds=max_pages * 3,
                )
            )
        return items

    def crawl_targets(self, query: dict) -> list[dict[str, Any]]:
        """The sites one plan item stands for, whichever vocabulary named them.

        This adapter reads ``url``; the campaign planner writes ``crawl_seeds``
        (and leaves it empty); :meth:`plan` reads ``company_sites``.  Three
        vocabularies for one contract meant every ``website.crawl`` plan item
        fetched nothing and reported success, so all three are accepted - and
        when none of them names a site, the knowledge base is asked for the
        companies whose domain is known (FR-221, FR-341).
        """
        seen: set[str] = set()
        targets: list[dict[str, Any]] = []

        def add(url: Any, company_id: Any = None) -> None:
            text = url if isinstance(url, str) else ""
            if isinstance(url, dict):
                text = str(url.get("url") or url.get("website") or "")
                if not text and url.get("domain"):
                    text = f"https://{str(url['domain']).strip().lstrip('/')}"
                company_id = url.get("company_id") or url.get("id") or company_id
            text = text.strip()
            if text and not text.startswith(("http://", "https://")):
                text = f"https://{text}"
            normalised = normalise_url(text) if text else None
            if not normalised or normalised in seen:
                return
            seen.add(normalised)
            targets.append({"url": normalised, "company_id": company_id})

        add(query.get("url"), query.get("company_id"))
        for key in ("crawl_seeds", "company_sites", "sites", "companies", "urls"):
            entries = query.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    add(entry)
        if targets:
            return targets

        for row in self.known_company_sites(query):
            add(row)
        return targets

    @staticmethod
    def known_company_sites(query: dict, limit: int = 3) -> list[dict[str, Any]]:
        """Companies already in the knowledge base that have a website (FR-341)."""
        from dreamjob.db.repositories import companies as repo  # noqa: PLC0415 - avoids a cycle

        countries = query.get("countries") or ([query["country"]] if query.get("country") else [])
        country = str(countries[0]).upper()[:2] if countries else None
        try:
            rows = repo.companies_in_country(country, limit=200)
        except Exception:  # noqa: BLE001 - an empty knowledge base is the normal first case
            log.exception("Could not read companies for a website crawl")
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not row.get("domain"):
                continue
            out.append({"domain": row["domain"], "company_id": row["id"]})
            if len(out) >= limit:
                break
        return out

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = item.native_query or {}
        targets = self.crawl_targets(query)
        if not targets:
            raise UnusableQuery(
                self.key,
                "no company website to crawl: the plan item names none and no company in "
                "the knowledge base has a domain yet - company discovery has to run before "
                "the sites can be deepened (FR-221, FR-341)",
                expected=("url", "crawl_seeds", "company_sites"),
            )

        # FR-186: one "page" of this adapter is a whole crawl, so the campaign's
        # per-source page cap is what bounds it - otherwise a plan item charged
        # as a single page quietly issues thirty requests.  The budget is per
        # site, with a floor of MIN_CRAWL_PAGES: below that a crawl cannot reach
        # the pages FR-221 asks for, so it would spend requests for nothing.
        caps = getattr(item, "caps", None) or {}
        budget = int(query.get("max_pages") or caps.get("max_pages") or DEFAULT_MAX_PAGES)
        per_site = max(MIN_CRAWL_PAGES, min(budget, DEFAULT_MAX_PAGES))
        own_client = self.egress is None
        egress = self.egress or EgressClient()
        if own_client:
            await egress.__aenter__()
        raws: list[RawRecord] = []
        try:
            for target in targets:
                crawler = WebsiteCrawler(
                    egress,
                    max_pages=per_site,
                    max_depth=int(query.get("max_depth") or DEFAULT_MAX_DEPTH),
                )
                result = await crawler.crawl(target["url"])
                raws.extend(self._to_raw(result, target.get("company_id")))
        finally:
            if own_client:
                await egress.__aexit__(None, None, None)
        return raws

    def _to_raw(self, result: CrawlResult, company_id: Any) -> list[RawRecord]:
        return [
            RawRecord(
                url=page.url,
                content=page.text,
                content_type="text/plain",
                raw_document_id=page.raw_document_id,
                meta={
                    "kind": page.kind,
                    "title": page.title,
                    "company_id": company_id,
                    "home_url": result.home_url,
                    "domain": result.domain,
                    "careers_url": result.careers_url,
                    "ats_vendor": result.ats_vendor,
                    "ats_slug": result.ats_slug,
                    "feeds": result.feeds,
                    "jsonld": result.jsonld,
                    # One "page" of this adapter is a whole crawl: FR-186 needs
                    # the real request count to charge the campaign budget.
                    "requests_issued": result.stats.get("fetched", 0),
                },
            )
            for page in result.pages
        ]

    def parse(self, raw: RawRecord) -> list[dict]:
        """Deterministic page-level extraction; synthesis is the profiler's job."""
        meta = raw.meta or {}
        return [
            {
                "url": raw.url,
                "kind": meta.get("kind"),
                "title": meta.get("title"),
                "text": raw.content,
                **identity_from_jsonld(meta.get("jsonld") or []),
            }
        ]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        """Only the home page yields a company record; the rest is profile input."""
        meta = raw.meta or {}
        if meta.get("kind") != "home":
            return None
        domain = meta.get("domain")
        name = parsed.get("name") or parsed.get("legal_name")
        if not name and domain:
            name = domain.split(".")[0].replace("-", " ").title()
        if not name:
            return None
        data: dict[str, Any] = {
            "name": name,
            "domain": domain,
            "source": raw.url,
            "access_method": "http",
            "careers_url": meta.get("careers_url"),
            "ats_vendor": meta.get("ats_vendor"),
            "ats_slug": meta.get("ats_slug"),
        }
        for key in ("legal_id", "legal_id_type", "vat_number", "size_fte", "locations"):
            if parsed.get(key):
                data[key] = parsed[key]
        return NormalisedRecord(
            entity_type="company",
            data={k: v for k, v in data.items() if v not in (None, "", [], {})},
            confidence=0.6,
            provenance={"adapter": self.key, "url": raw.url},
            raw_document_id=raw.raw_document_id,
        )


async def crawl_site(
    url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    egress: EgressClient | None = None,
) -> CrawlResult:
    """Crawl one company site, opening an egress client when none is supplied."""
    if egress is not None:
        return await WebsiteCrawler(egress, max_pages=max_pages, max_depth=max_depth).crawl(url)
    async with EgressClient() as client:
        return await WebsiteCrawler(client, max_pages=max_pages, max_depth=max_depth).crawl(url)
