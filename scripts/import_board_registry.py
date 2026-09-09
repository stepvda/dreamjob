#!/usr/bin/env python3
"""Build the ATS board registry from public URL indexes (N3, FR-182, IR-101).

    python3 scripts/import_board_registry.py --source commoncrawl --source hn
    python3 scripts/import_board_registry.py --source wayback --vendor lever
    python3 scripts/import_board_registry.py --from-file recruitee=slugs.json \
            --source-label commoncrawl

The registry is the route to 7,500 companies.  Nothing else in the product can
name thousands of employer boards: guessing slugs hits 16%, and walking company
websites with ``detect_ats`` costs ~30 requests per board found
(docs/Data_Gathering_Plan.md section 2.8).  Three public URL indexes name
15,922 boards across nine vendors in about 65 requests, and this script turns
them into ``backend/dreamjob/pipeline/data/board_registry.json`` - one row per
board: ``(vendor, slug, name, first_seen, last_verified, source)``.

This is an **operator-run importer, not a campaign step**.  It is run by hand,
roughly monthly, and the file it writes is committed.  Campaigns read the file;
they never run this script.


Why this script does not use ``dreamjob.egress`` (FR-182)
---------------------------------------------------------
``index.commoncrawl.org`` and ``data.commoncrawl.org`` both publish::

    User-agent: *
    Disallow: /

(measured 2026-09-09; ``index.commoncrawl.org`` additionally allows ``/$``,
``/index.html$``, ``/collinfo.json$`` and a handful of metadata files).  The
egress layer refuses a robots-disallowed URL, and that refusal is the product's
FR-182 invariant: *the application never fetches a robots-disallowed URL*.

Reading the Common Crawl **URL index** is a legitimate use of an open dataset.
Common Crawl's Terms of Use (https://commoncrawl.org/terms-of-use, "you may use
the data for research, analysis and any other lawful purpose", subject to the
attribution and no-misuse conditions) publish the corpus and its index for
reuse, and the index is a static, pre-built artefact rather than a live crawl of
anybody's site; the robots.txt above governs crawlers walking their web UI, not
consumers of the published dataset.

Reasonable people can disagree about that reading, so this script keeps the
disagreement out of the product.  The exception lives here - in an operator-run
script, outside every campaign, behind a named constant
(:data:`COMMON_CRAWL_HOSTS`), for exactly two hosts - so that the invariant an
auditor checks stays absolute and mechanically verifiable: no code path reached
by a campaign ever fetches a robots-disallowed URL, and no robots.txt is
ignored anywhere else, including here.  Every *other* host this script touches
is robots-checked before it is fetched, even though the script runs outside the
egress layer, and a disallow is a hard failure rather than a warning.

Both other sources were measured permitted on 2026-09-09: ``web.archive.org``
and ``hn.algolia.com`` return 404 for ``/robots.txt``.  The product's own
User-Agent is sent unchanged - there is no spoofing here and there never will
be (section 6.1 of the plan).


The three sources, with their measured costs
--------------------------------------------
* **Common Crawl index** - ~60 requests per crawl for nine vendor patterns;
  15,922 distinct slugs across two 2026 crawls.  Slugs seen in a 2026 crawl are
  87.5% live.  Paginated with ``showNumPages``/``page``.
* **Wayback CDX** - 3 requests, 64-195 s each, so it is paginated with
  ``showNumPages``/``page`` and given a 300 s timeout: the egress layer's 30 s
  timeout is far too short for it, which is a second, smaller reason this is a
  standalone script.  Wayback-only slugs are 27.5% live, so they are worth
  importing but not worth trusting: they are what ``source`` is for.
* **HN "Who is hiring"** - hn.algolia.com, 14 requests for a year of threads,
  416 boards.  Small, current, and biased toward companies that want to be
  found.

Liveness decays (C8 adds ``"board_registry": 30`` to the knowledge base's
staleness policy).  Nothing here verifies liveness: that is one request per
slug and belongs in the nightly refresh job, never in a campaign and never in
this importer, which must stay cheap enough to run monthly.

Requirement ids: N3 and section 6.3 of docs/Data_Gathering_Plan.md; FR-182
(robots compliance), IR-101 (platform terms), FR-183 (provenance: every row
carries the source that evidenced it), NFR-402.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import sys
import time
import urllib.robotparser
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

DEFAULT_OUT = REPO_ROOT / "backend" / "dreamjob" / "pipeline" / "data" / "board_registry.json"

#: Fallback only; the real value comes from ``DREAMJOB_USER_AGENT`` (FR-182).
USER_AGENT = "DreamJobBot/0.1 (+https://stepvda.net/dreamjob; contact stephane@stepvda.com)"

#: The two hosts whose robots.txt this script knowingly does not follow, for
#: the reasons set out in the module docstring.  Nothing may be added here
#: without the same written justification, and nothing in ``dreamjob/`` may
#: read this constant: it exists so the exception is one grep away.
COMMON_CRAWL_HOSTS = frozenset({"index.commoncrawl.org", "data.commoncrawl.org"})
COMMON_CRAWL_TERMS = "https://commoncrawl.org/terms-of-use"

CC_INDEX = "https://index.commoncrawl.org/{crawl}-index"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{item_id}"

#: Crawls measured in the plan (section 2.3).  ``--crawl`` overrides.
DEFAULT_CRAWLS = ("CC-MAIN-2026-34", "CC-MAIN-2026-25")

SOURCE_COMMONCRAWL = "commoncrawl"
SOURCE_WAYBACK = "wayback"
SOURCE_HN = "hackernews"
SOURCES = (SOURCE_COMMONCRAWL, SOURCE_WAYBACK, SOURCE_HN)

#: C8: the registry is re-verified monthly, so the file records the policy it
#: was written under and a reader can tell how stale it is allowed to be.
STALENESS_DAYS = 30

SCHEMA_VERSION = 1
REGISTRY_KIND = "dreamjob.board_registry"

log = logging.getLogger("import_board_registry")


class ImporterError(RuntimeError):
    """A source could not be read.  Always fatal, never swallowed."""


class RobotsDisallowed(ImporterError):
    """robots.txt forbids this fetch (FR-182), and this script obeys it."""


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vendor:
    """One ATS whose boards live on a predictable public URL.

    ``slug`` is exactly what ``company.ats_slug`` holds and what the matching
    adapter's ``slugs_of()`` reads, so a registry row is a plan item without
    further translation.
    """

    name: str
    url_patterns: tuple[re.Pattern[str], ...]
    cc_patterns: tuple[str, ...] = ()
    wayback_hosts: tuple[str, ...] = ()
    #: Measured share of a sampled board's jobs located in the EU (section 2.3).
    eu_job_share: float | None = None
    enabled: bool = True
    note: str = ""

    def slug_from(self, text: str) -> Iterator[str]:
        for pattern in self.url_patterns:
            for match in pattern.finditer(text):
                slug = self.assemble(match)
                if slug:
                    yield slug

    def assemble(self, match: re.Match[str]) -> str | None:
        if self.name == "workday":
            # The adapter's slug is "<host>/<site>" (ats/workday.py split_slug).
            # A bare tenant host is not fetchable, and a guessed site name is a
            # 404 that gets stored and re-probed (section 6.7), so drop it.
            if match.lastindex != 2 or not match.group(2):
                return None
            return f"{match.group(1).lower()}/{match.group(2)}"
        return match.group(1).lower()


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


VENDORS: dict[str, Vendor] = {
    v.name: v
    for v in (
        Vendor(
            name="greenhouse",
            url_patterns=(
                _rx(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w.-]+)"),
            ),
            cc_patterns=("boards.greenhouse.io/*", "job-boards.greenhouse.io/*"),
            wayback_hosts=("boards.greenhouse.io", "job-boards.greenhouse.io"),
            eu_job_share=0.08,
        ),
        Vendor(
            name="lever",
            url_patterns=(_rx(r"jobs(?:\.eu)?\.lever\.co/([\w.-]+)"),),
            cc_patterns=("jobs.lever.co/*", "jobs.eu.lever.co/*"),
            wayback_hosts=("jobs.lever.co",),
            eu_job_share=0.15,
        ),
        Vendor(
            name="ashby",
            url_patterns=(_rx(r"jobs\.ashbyhq\.com/([\w.-]+)"),),
            cc_patterns=("jobs.ashbyhq.com/*",),
            wayback_hosts=("jobs.ashbyhq.com",),
            eu_job_share=0.18,
        ),
        Vendor(
            name="recruitee",
            url_patterns=(_rx(r"https?://([\w-]+)\.recruitee\.com"),),
            cc_patterns=("*.recruitee.com",),
            eu_job_share=0.83,
        ),
        Vendor(
            name="personio",
            url_patterns=(_rx(r"https?://([\w-]+)\.jobs\.personio\.(?:de|com)"),),
            cc_patterns=("*.jobs.personio.de", "*.jobs.personio.com"),
            eu_job_share=0.55,
        ),
        Vendor(
            name="workable",
            url_patterns=(_rx(r"apply\.workable\.com/([\w.-]+)"),),
            cc_patterns=("apply.workable.com/*",),
            note="EU share unmeasured (appendix B.2)",
        ),
        Vendor(
            name="teamtailor",
            url_patterns=(_rx(r"https?://([\w-]+)\.teamtailor\.com"),),
            cc_patterns=("*.teamtailor.com",),
            note="EU share unmeasured (appendix B.2); Nordic-heavy by reputation",
        ),
        Vendor(
            name="workday",
            url_patterns=(
                _rx(r"https?://([\w-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)"),
            ),
            cc_patterns=("*.myworkdayjobs.com",),
            note="slug is '<host>/<site>'; a URL naming no site yields no row",
        ),
        Vendor(
            name="smartrecruiters",
            url_patterns=(_rx(r"(?:jobs|careers)\.smartrecruiters\.com/([\w.-]+)"),),
            cc_patterns=("jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*"),
            enabled=False,
            note=(
                "IR-101: api.smartrecruiters.com robots.txt allows LinkedInBot only "
                "(User-agent: * / Disallow: /), so the adapter is disabled (C6) and "
                "importing its slugs would only manufacture targets nothing may read"
            ),
        ),
    )
}


# ---------------------------------------------------------------------------
# Slug hygiene
# ---------------------------------------------------------------------------

#: A path segment that is part of the vendor's own site rather than a board.
#: ``careers-analytics``, ``cdn``, ``static``, ``assets`` and ``app`` are the
#: N9 additions: ``detect_ats`` returned ``careers-analytics`` for two Belgian
#: companies, which is Recruitee's analytics host and answers 403.
RESERVED_SLUGS = frozenset(
    {
        "api", "app", "apps", "assets", "blog", "boards", "career", "careers",
        "careers-analytics",
        "cdn", "companies", "company", "cookies", "css", "dashboard", "de", "docs",
        "embed", "en", "es", "fr", "help", "images", "img", "index", "it", "js",
        "job", "jobs", "legal", "login", "media", "nl", "o", "pages", "partners",
        "pl", "policy", "pricing", "privacy", "public", "resources", "search",
        "signin", "signup", "static", "status", "support", "terms", "test", "www",
    }
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_WORKDAY_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.wd\d+\.myworkdayjobs\.com/[\w-]{1,64}$")
_FILE_SUFFIXES = (
    ".xml", ".txt", ".json", ".js", ".css", ".html", ".htm", ".php", ".png",
    ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".rss", ".map",
)


def is_plausible_slug(vendor: str, slug: str) -> bool:
    """Could this string be a board name a company actually owns?

    The URL indexes are noisy in ways that cost real requests if they are not
    filtered here.  The Wayback CDX for ``jobs.ashbyhq.com`` alone yields
    ``"..."``, ``".sitemap.xml"`` and 400-character base64 blobs from tracking
    parameters; every one of those would become a plan item, a 404, a stored
    ``raw_document`` and a re-probe on the next run (section 6.7).  A slug that
    cannot be a board is dropped here, once, rather than everywhere later.
    """
    slug = (slug or "").strip().strip("/")
    if not slug:
        return False
    if vendor == "workday":
        return bool(_WORKDAY_SLUG_RE.match(slug))
    lowered = slug.lower()
    if lowered in RESERVED_SLUGS:
        return False
    if lowered.endswith(_FILE_SUFFIXES) or ".." in lowered:
        return False
    return bool(_SLUG_RE.match(lowered))


# ---------------------------------------------------------------------------
# The registry row
# ---------------------------------------------------------------------------


@dataclass
class Board:
    """One row of the registry (N3).

    ``last_verified`` stays ``None`` until something has actually fetched the
    board.  It is the field the nightly liveness job owns, and the reason this
    importer merges rather than overwrites: re-importing must never reset it,
    or every board looks unverified again and the next nightly run re-checks
    14,600 boards for nothing.
    """

    vendor: str
    slug: str
    name: str | None = None
    first_seen: str = ""
    last_verified: str | None = None
    source: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.vendor, self.slug if self.vendor == "workday" else self.slug.lower())

    @property
    def sources(self) -> set[str]:
        return {s for s in self.source.split("+") if s}

    def to_json(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "slug": self.slug,
            "name": self.name,
            "first_seen": self.first_seen,
            "last_verified": self.last_verified,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> Board:
        return cls(
            vendor=str(row.get("vendor") or "").lower(),
            slug=str(row.get("slug") or ""),
            name=row.get("name") or None,
            first_seen=str(row.get("first_seen") or ""),
            last_verified=row.get("last_verified") or None,
            source=str(row.get("source") or ""),
        )


def merge_boards(existing: Iterable[Board], incoming: Iterable[Board]) -> list[Board]:
    """Union of two registries, oldest ``first_seen`` and newest evidence kept.

    Merging, not replacing, is what makes a partial run safe: a Common Crawl
    outage can only fail to add boards, never delete the ones already known.
    """
    merged: dict[tuple[str, str], Board] = {}
    for board in list(existing) + list(incoming):
        current = merged.get(board.key)
        if current is None:
            merged[board.key] = Board(**vars(board))
            continue
        if board.first_seen and (not current.first_seen or board.first_seen < current.first_seen):
            current.first_seen = board.first_seen
        if board.last_verified and (
            not current.last_verified or board.last_verified > current.last_verified
        ):
            current.last_verified = board.last_verified
        if board.name and not current.name:
            current.name = board.name
        current.source = "+".join(sorted(current.sources | board.sources))
    return sorted(merged.values(), key=lambda b: (b.vendor, b.key[1]))


# ---------------------------------------------------------------------------
# Fetching (deliberately not dreamjob.egress - see the module docstring)
# ---------------------------------------------------------------------------


@dataclass
class Fetcher:
    """A small, polite HTTP client: one request per second per host, 3 tries.

    It checks robots.txt for every host except :data:`COMMON_CRAWL_HOSTS`, and
    it counts what it did.  ``errors`` is the number that decides whether the
    run is allowed to write the registry file: a run that could not read its
    sources must fail loudly rather than write a shorter file (section 3
    step 8 - a failure that reports success is the defect this project keeps
    finding).
    """

    client: httpx.Client
    delay: float = 1.0
    user_agent: str = USER_AGENT
    requests: int = 0
    errors: int = 0
    _last: dict[str, float] = field(default_factory=dict)
    _robots: dict[str, urllib.robotparser.RobotFileParser | None] = field(default_factory=dict)

    def allowed(self, url: str) -> bool:
        host = httpx.URL(url).host
        if host in COMMON_CRAWL_HOSTS:
            return True                     # the documented, narrow exception
        if host not in self._robots:
            self._robots[host] = self._read_robots(host)
        parser = self._robots[host]
        if parser is None:
            # RFC 9309 2.3.1.4: unreachable robots.txt means a complete
            # disallow.  The egress layer takes the same line (FR-182).
            return False
        return parser.can_fetch(self.user_agent, url)

    def _read_robots(self, host: str) -> urllib.robotparser.RobotFileParser | None:
        parser = urllib.robotparser.RobotFileParser()
        try:
            response = self.client.get(f"https://{host}/robots.txt", timeout=30.0)
        except httpx.HTTPError as exc:
            log.warning("robots.txt for %s could not be read (%s)", host, exc)
            return None
        self.requests += 1
        if response.status_code == 404:
            parser.parse([])                # no rules published: everything allowed
            return parser
        if response.status_code != 200:
            log.warning("robots.txt for %s answered %s", host, response.status_code)
            return None
        parser.parse(response.text.splitlines())
        return parser

    def get(self, url: str, *, timeout: float = 60.0, tries: int = 3) -> httpx.Response | None:
        """One GET.  ``None`` for 404; ``ImporterError`` when it never worked."""
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt forbids {url}")
        host = httpx.URL(url).host
        last_error = ""
        for attempt in range(1, tries + 1):
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self._last[host] = time.monotonic()
            self.requests += 1
            try:
                response = self.client.get(url, timeout=timeout)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return response
                if response.status_code in (404, 410):
                    return None
                last_error = f"HTTP {response.status_code}"
            log.warning("%s (attempt %d/%d) %s", last_error, attempt, tries, url)
            if attempt < tries:
                time.sleep(min(30.0, 3.0 * attempt))
        self.errors += 1
        raise ImporterError(f"{url}: {last_error}")


# ---------------------------------------------------------------------------
# Source 1: the Common Crawl URL index
# ---------------------------------------------------------------------------


def collect_common_crawl(
    fetcher: Fetcher,
    vendors: list[Vendor],
    crawls: Iterable[str],
    today: str,
    max_pages: int = 200,
) -> list[Board]:
    """~60 requests per crawl; 15,922 slugs across nine vendors in the plan.

    Only rows the crawler fetched successfully (``status == "200"``) count: a
    404 in the index is a board that was already gone when the crawl ran.
    """
    boards: list[Board] = []
    for vendor in vendors:
        found: set[str] = set()
        for crawl in crawls:
            for pattern in vendor.cc_patterns:
                base = f"{CC_INDEX.format(crawl=crawl)}?url={pattern}&output=json"
                pages = _cc_page_count(fetcher, base)
                for page in range(min(pages, max_pages)):
                    response = fetcher.get(f"{base}&fl=url,status&page={page}", timeout=120.0)
                    if response is None:
                        continue
                    for line in response.text.splitlines():
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if str(row.get("status")) != "200":
                            continue
                        for slug in vendor.slug_from(str(row.get("url") or "")):
                            if is_plausible_slug(vendor.name, slug):
                                found.add(slug)
        log.info("common crawl: %s -> %d slugs", vendor.name, len(found))
        boards.extend(
            Board(vendor=vendor.name, slug=s, first_seen=today, source=SOURCE_COMMONCRAWL)
            for s in sorted(found)
        )
    return boards


def _cc_page_count(fetcher: Fetcher, base_url: str) -> int:
    response = fetcher.get(f"{base_url}&showNumPages=true", timeout=120.0)
    if response is None:
        return 0
    try:
        return int(json.loads(response.text).get("pages", 0))
    except (ValueError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# Source 2: the Wayback CDX index
# ---------------------------------------------------------------------------


def collect_wayback(
    fetcher: Fetcher,
    vendors: list[Vendor],
    today: str,
    since_year: int = 2024,
    page_size: int = 5,
    max_pages: int = 50,
) -> list[Board]:
    """3 requests, 64-195 s each - hence ``showNumPages``/``page`` and 300 s.

    The egress layer's 30 s timeout cannot hold this query open, which is the
    second reason the importer is a standalone script.
    """
    boards: list[Board] = []
    for vendor in vendors:
        found: set[str] = set()
        for host in vendor.wayback_hosts:
            base = (
                f"{WAYBACK_CDX}?url={host}/*&fl=original&collapse=urlkey"
                f"&filter=statuscode:200&from={since_year}&pageSize={page_size}"
            )
            pages = _cdx_page_count(fetcher, base)
            for page in range(min(pages, max_pages)):
                response = fetcher.get(f"{base}&page={page}", timeout=300.0)
                if response is None:
                    continue
                for line in response.text.splitlines():
                    for slug in vendor.slug_from(line.strip()):
                        if is_plausible_slug(vendor.name, slug):
                            found.add(slug)
        log.info("wayback: %s -> %d slugs", vendor.name, len(found))
        boards.extend(
            Board(vendor=vendor.name, slug=s, first_seen=today, source=SOURCE_WAYBACK)
            for s in sorted(found)
        )
    return boards


def _cdx_page_count(fetcher: Fetcher, base_url: str) -> int:
    """The CDX ``showNumPages`` answer is a bare integer, not JSON."""
    response = fetcher.get(f"{base_url}&showNumPages=true", timeout=300.0)
    if response is None:
        return 0
    try:
        return int(response.text.strip() or 0)
    except ValueError:
        log.warning("CDX showNumPages returned %r; falling back to one page", response.text[:80])
        return 1


# ---------------------------------------------------------------------------
# Source 3: HN "Who is hiring"
# ---------------------------------------------------------------------------


def collect_hackernews(
    fetcher: Fetcher, vendors: list[Vendor], today: str, threads: int = 14
) -> list[Board]:
    """14 requests for a year of threads; 416 boards, all currently hiring."""
    response = fetcher.get(
        f"{HN_SEARCH}?tags=story,author_whoishiring&query=%22Who%20is%20hiring%22"
        f"&hitsPerPage={threads}"
    )
    if response is None:
        raise ImporterError("hn.algolia.com returned no story list")
    hits = response.json().get("hits") or []
    boards: list[Board] = []
    found: dict[str, set[str]] = {v.name: set() for v in vendors}
    for hit in hits:
        title = str(hit.get("title") or "").lower()
        if "hiring" not in title or "wants to be hired" in title or "freelancer" in title:
            continue
        item = fetcher.get(HN_ITEM.format(item_id=hit.get("objectID")))
        if item is None:
            continue
        text = " ".join(_hn_texts(item.json()))
        for vendor in vendors:
            for slug in vendor.slug_from(text):
                if is_plausible_slug(vendor.name, slug):
                    found[vendor.name].add(slug)
    for vendor in vendors:
        log.info("hacker news: %s -> %d slugs", vendor.name, len(found[vendor.name]))
        boards.extend(
            Board(vendor=vendor.name, slug=s, first_seen=today, source=SOURCE_HN)
            for s in sorted(found[vendor.name])
        )
    return boards


def _hn_texts(item: dict[str, Any]) -> Iterator[str]:
    """Every comment in the thread, however deeply nested."""
    text = item.get("text")
    if text:
        yield html.unescape(str(text))
    for child in item.get("children") or []:
        if isinstance(child, dict):
            yield from _hn_texts(child)


# ---------------------------------------------------------------------------
# Offline import (how the shipped seed registry was built)
# ---------------------------------------------------------------------------


def collect_from_file(path: Path, vendor: str, source: str, today: str) -> list[Board]:
    """Import a recorded slug list: ``["acme", ...]`` or full registry rows.

    The registry committed to the repository was produced this way from the
    probe output recorded in the plan's appendix C, so the seed is reproducible
    from evidence rather than hand-written.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("boards") or payload.get(vendor) or []
    boards: list[Board] = []
    for entry in payload:
        if isinstance(entry, str):
            board = Board(vendor=vendor, slug=entry, first_seen=today, source=source)
        elif isinstance(entry, dict):
            board = Board.from_json({"vendor": vendor, "first_seen": today, "source": source,
                                     **entry})
        else:
            continue
        if is_plausible_slug(board.vendor, board.slug):
            boards.append(board)
        else:
            log.debug("dropped implausible slug %r for %s", board.slug, board.vendor)
    return boards


# ---------------------------------------------------------------------------
# The registry file
# ---------------------------------------------------------------------------


def load_document(path: Path) -> dict[str, Any]:
    """The registry as written, or an empty document when there is none yet."""
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {"boards": payload}


def load_registry(path: Path) -> list[Board]:
    rows = load_document(path).get("boards") or []
    return [Board.from_json(r) for r in rows if isinstance(r, dict)]


def registry_document(boards: list[Board], provenance: dict[str, Any]) -> dict[str, Any]:
    by_vendor: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for board in boards:
        by_vendor[board.vendor] = by_vendor.get(board.vendor, 0) + 1
        for source in board.sources or {"unknown"}:
            by_source[source] = by_source.get(source, 0) + 1
    return {
        "kind": REGISTRY_KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": provenance.get("generated_at"),
        "staleness_days": STALENESS_DAYS,
        "_readme": (
            "ATS boards discovered from public URL indexes (N3). One entry per board: "
            "vendor matches ATSAdapter.vendor, slug is company.ats_slug (Workday: "
            "'<host>/<site>'), name is null until a source names the company, first_seen "
            "and last_verified are ISO dates (last_verified null = never fetched), source "
            "is one or more of commoncrawl/wayback/hackernews joined with '+' (counts.by_source "
            "counts a board once per source that evidenced it, so it sums above the total). Build a "
            "board URL with dreamjob.adapters.ats.detect.board_url(vendor, slug) and find "
            "the adapter with detect.adapter_key_for(vendor). Liveness decays: re-verify "
            f"every {STALENESS_DAYS} days in the nightly job, never per campaign. "
            "Regenerate with scripts/import_board_registry.py."
        ),
        "provenance": provenance,
        "counts": {"total": len(boards), "by_vendor": by_vendor, "by_source": by_source},
        "boards": [b.to_json() for b in boards],
    }


def write_registry(path: Path, document: dict[str, Any]) -> None:
    """Ordinary JSON, but one board per line, so a monthly refresh reads as a diff."""
    boards = document.get("boards") or []
    head = {k: v for k, v in document.items() if k != "boards"}
    head_text = json.dumps(head, indent=2, ensure_ascii=False)
    rows = [
        "    " + json.dumps(board, ensure_ascii=False, separators=(", ", ": "))
        for board in boards
    ]
    text = head_text[:-2].rstrip() + ',\n  "boards": [\n' + ",\n".join(rows) + "\n  ]\n}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Optional: liveness state in the database (migration 092)
# ---------------------------------------------------------------------------

_DB_COLUMNS = (
    "id, vendor, slug, name, source, first_seen, last_verified, liveness, "
    "last_status, job_count, consecutive_failures, updated_at"
)


def board_row_id(vendor: str, slug: str) -> str:
    """Deterministic id, so re-importing updates rows instead of creating them."""
    return hashlib.sha256(f"{vendor}/{slug}".encode()).hexdigest()[:32]


def load_into_db(boards: list[Board], db_path: Path | None = None) -> dict[str, int]:
    """Upsert the registry into ``board_registry`` (migration 092).

    The liveness columns belong to the nightly job, so they are read back and
    preserved: an import must never reset ``last_verified``, or the next
    nightly run re-verifies every board it verified yesterday.
    """
    from dreamjob.db.connection import query_all, upsert_row, utcnow

    existing = {
        (row["vendor"], row["slug"]): row
        for row in query_all(f"SELECT {_DB_COLUMNS} FROM board_registry", (), db_path)
    }
    now = utcnow()
    inserted = updated = 0
    for board in boards:
        prior = existing.get((board.vendor, board.slug))
        values = {
            "id": board_row_id(board.vendor, board.slug),
            "vendor": board.vendor,
            "slug": board.slug,
            "name": board.name or (prior or {}).get("name"),
            "source": board.source,
            "first_seen": min(
                [x for x in (board.first_seen, (prior or {}).get("first_seen")) if x]
                or [now[:10]]
            ),
            "last_verified": (prior or {}).get("last_verified") or board.last_verified,
            "liveness": (prior or {}).get("liveness") or "unverified",
            "last_status": (prior or {}).get("last_status"),
            "job_count": (prior or {}).get("job_count"),
            "consecutive_failures": (prior or {}).get("consecutive_failures") or 0,
            "updated_at": now,
        }
        upsert_row("board_registry", values, ["vendor", "slug"], db_path)
        if prior is None:
            inserted += 1
        else:
            updated += 1
    return {"inserted": inserted, "updated": updated}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _vendors(names: list[str] | None) -> list[Vendor]:
    if not names:
        return [v for v in VENDORS.values() if v.enabled]
    chosen: list[Vendor] = []
    for name in names:
        vendor = VENDORS.get(name.lower())
        if vendor is None:
            raise SystemExit(f"unknown vendor {name!r}; known: {', '.join(sorted(VENDORS))}")
        if not vendor.enabled:
            raise SystemExit(f"{name} is disabled: {vendor.note}")
        chosen.append(vendor)
    return chosen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", action="append", choices=SOURCES, default=None,
                        help="repeatable; default: all three")
    parser.add_argument("--vendor", action="append", default=None,
                        help="repeatable; default: every enabled vendor")
    parser.add_argument("--crawl", action="append", default=None,
                        help=f"Common Crawl ids; default {' '.join(DEFAULT_CRAWLS)}")
    parser.add_argument("--wayback-from", type=int, default=2024, metavar="YEAR")
    parser.add_argument("--hn-threads", type=int, default=14)
    parser.add_argument("--from-file", action="append", default=None,
                        metavar="VENDOR=PATH[=SOURCE]",
                        help="import a recorded slug list instead of fetching")
    parser.add_argument("--source-label", default=SOURCE_COMMONCRAWL, choices=SOURCES,
                        help="default source for --from-file lists that do not name one")
    parser.add_argument("--note", action="append", default=None,
                        help="repeatable; recorded in the file's provenance block")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--replace", action="store_true",
                        help="discard the existing registry instead of merging into it")
    parser.add_argument("--allow-partial", action="store_true",
                        help="write the registry even though a source failed")
    parser.add_argument("--load-db", action="store_true",
                        help="also upsert into board_registry (migration 092)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    try:
        from dreamjob.config import get_settings

        user_agent = get_settings().user_agent or USER_AGENT
    except Exception:  # noqa: BLE001 - the importer must run without a configured install
        user_agent = USER_AGENT

    vendors = _vendors(args.vendor)
    today = datetime.now(UTC).date().isoformat()
    sources = args.source or (list(SOURCES) if not args.from_file else [])
    crawls = tuple(args.crawl or DEFAULT_CRAWLS)

    collected: list[Board] = []
    used: list[str] = []
    failures: list[str] = []
    fetcher: Fetcher | None = None

    for spec in args.from_file or []:
        vendor_name, _, rest = spec.partition("=")
        head, sep, tail = rest.rpartition("=")
        raw_path, source_label = (head, tail) if sep and tail in SOURCES else (rest, args.source_label)
        if not raw_path:
            raise SystemExit(f"--from-file wants VENDOR=PATH[=SOURCE], got {spec!r}")
        vendor = VENDORS.get(vendor_name.lower())
        if vendor is None or not vendor.enabled:
            raise SystemExit(f"--from-file names an unknown or disabled vendor: {vendor_name!r}")
        boards = collect_from_file(Path(raw_path), vendor.name, source_label, today)
        log.info("file %s: %s -> %d slugs", raw_path, vendor.name, len(boards))
        collected.extend(boards)
        used.append(f"{source_label}:{vendor.name}:{Path(raw_path).name}")

    if sources:
        with httpx.Client(
            headers={"User-Agent": user_agent}, follow_redirects=True, timeout=60.0
        ) as client:
            fetcher = Fetcher(client=client, delay=args.delay, user_agent=user_agent)
            for source in sources:
                try:
                    if source == SOURCE_COMMONCRAWL:
                        collected += collect_common_crawl(fetcher, vendors, crawls, today)
                        used.append(f"commoncrawl:{'+'.join(crawls)}")
                    elif source == SOURCE_WAYBACK:
                        collected += collect_wayback(
                            fetcher, vendors, today, since_year=args.wayback_from
                        )
                        used.append(f"wayback:from-{args.wayback_from}")
                    elif source == SOURCE_HN:
                        collected += collect_hackernews(
                            fetcher, vendors, today, threads=args.hn_threads
                        )
                        used.append(f"hackernews:{args.hn_threads}-threads")
                except ImporterError as exc:
                    log.error("source %s failed: %s", source, exc)
                    failures.append(f"{source}: {exc}")

    prior = {} if args.replace else load_document(args.out)
    prior_provenance = prior.get("provenance") or {}
    existing = [Board.from_json(r) for r in (prior.get("boards") or []) if isinstance(r, dict)]
    boards = merge_boards(existing, collected)
    log.info(
        "collected %d rows from %d source(s); registry holds %d boards (was %d)",
        len(collected), len(used), len(boards), len(existing),
    )

    if failures and not args.allow_partial:
        # A run that could not read its sources must not quietly write a
        # shorter registry and exit 0.  That is the shape of defect this whole
        # plan exists to remove.
        for failure in failures:
            log.error("not writing %s: %s", args.out, failure)
        return 2
    if args.dry_run:
        log.info("dry run: %s not written", args.out)
        return 1 if failures else 0

    # The file is the union of every run that merged into it, so its
    # provenance must be too: dropping the previous run's sources would leave
    # 3,000 rows whose ``source`` names an import the file no longer admits to.
    all_sources = list(dict.fromkeys(list(prior_provenance.get("sources") or []) + used))
    provenance = {
        "generated_at": today,
        "generated_by": "scripts/import_board_registry.py",
        "sources": all_sources,
        "composition": list(args.note or prior_provenance.get("composition") or []),
        "partial": bool(failures),
        "failures": failures,
        # Of the last run only: the file is a merge of several, and its
        # sources list is what says how many that was.
        "requests_last_run": fetcher.requests if fetcher else 0,
        "user_agent": user_agent,
        "common_crawl_terms": COMMON_CRAWL_TERMS,
        "notes": (
            "Operator-run monthly import, never a campaign step. index.commoncrawl.org "
            "and data.commoncrawl.org are robots-disallowed and are read here, outside "
            "the egress layer, as an open dataset under Common Crawl's terms of use; "
            "every other host is robots-checked. Liveness is not verified here: "
            "last_verified is written by the nightly job, one request per slug."
        ),
    }
    write_registry(args.out, registry_document(boards, provenance))
    log.info("wrote %s (%d boards)", args.out, len(boards))

    if args.load_db:
        stats = load_into_db(boards)
        log.info("board_registry table: %(inserted)d inserted, %(updated)d updated", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
