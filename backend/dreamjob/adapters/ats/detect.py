"""ATS vendor detection from a careers page or company domain (FR-181, FR-222).

The company-profiling slice calls :func:`detect_ats` with the HTML of a
company's careers page (or its home page) and the URL it came from, and stores
the result in ``company.ats_vendor`` / ``company.ats_slug``.  Those two fields
are what turn a company row into a plan item for one of the ATS adapters, which
is the cheapest and freshest route to that company's open roles.

Detection is deliberately deterministic - three signals, in order of trust:

1. the URL itself, when the page *is* the board (``jobs.lever.co/acme``);
2. links in the page pointing at a board (a careers page nearly always links
   or iframes its ATS);
3. embedded board widgets (``boards.greenhouse.io/embed/job_board?for=acme``,
   ``Grnhse.Iframe``, ``ashby_embed``, Recruitee's ``careers-site`` script).

Vendors without an adapter here (Workable, Teamtailor, SuccessFactors, ...) are
still reported: knowing which ATS a company runs is useful profiling
information even when Dream Job cannot read the board.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

#: Vendors this package can actually collect from.
SUPPORTED_VENDORS = (
    "greenhouse", "lever", "smartrecruiters", "ashby", "recruitee", "personio", "workday",
)

_ADAPTER_KEYS = {vendor: f"ats.{vendor}" for vendor in SUPPORTED_VENDORS}

# (vendor, pattern) - the first capturing group is the slug.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/embed/job_board\?for=([\w.-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w.-]+)", re.I)),
    ("greenhouse", re.compile(
        r"(?:job-boards|boards|my)\.greenhouse\.io/(?!embed)([\w.-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board/js\?for=([\w.-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([\w.-]+)", re.I)),
    ("lever", re.compile(r"jobs(?:\.eu)?\.lever\.co/([\w.-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([\w.-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([\w.-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([\w.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I)),
    ("recruitee", re.compile(r"https?://([\w-]+)\.recruitee\.com", re.I)),
    ("recruitee", re.compile(r"recruitee\.com/embed/[\w/]*\?[^\"']*company=([\w-]+)", re.I)),
    ("personio", re.compile(r"https?://([\w-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("personio", re.compile(r"https?://([\w-]+)\.personio\.(?:de|com)/(?:job|recruiting)", re.I)),
    ("workable", re.compile(r"(?:apply\.workable\.com|careers-page\.com)/([\w.-]+)", re.I)),
    ("teamtailor", re.compile(r"https?://([\w-]+)\.teamtailor\.com", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([\w.-]+)", re.I)),
    ("bamboohr", re.compile(r"([\w-]+)\.bamboohr\.com/(?:jobs|careers)", re.I)),
    ("successfactors", re.compile(r"([\w-]+)\.(?:successfactors|sapsf)\.(?:com|eu)", re.I)),
    ("taleo", re.compile(r"([\w-]+)\.taleo\.net", re.I)),
    ("softgarden", re.compile(r"([\w-]+)\.softgarden\.io", re.I)),
    ("icims", re.compile(r"([\w-]+)\.icims\.com", re.I)),
    ("join", re.compile(r"join\.com/companies/([\w.-]+)", re.I)),
    ("homerun", re.compile(r"https?://([\w-]+)\.homerun\.co", re.I)),
)

# Slugs that are really path segments of the vendor's own site, never a tenant.
_NOT_A_SLUG = {
    "embed", "www", "api", "job", "jobs", "boards", "careers", "en", "nl", "fr", "de",
    "search", "static", "assets", "images", "js", "css", "o", "companies", "company",
}

_WORKDAY_RE = re.compile(
    r"https?://([\w-]+\.wd\d+\.myworkdayjobs\.com)((?:/[\w%.-]+)*)", re.IGNORECASE
)
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2})?$")
_WORKDAY_NOISE = {"job", "jobs", "wday", "cxs", "details"}


def _clean(slug: str) -> str:
    """Slugs are case-sensitive for some vendors (Workday sites), so case is kept."""
    return unquote(slug).strip().strip("/")


def _workday(text: str) -> tuple[str, str] | None:
    """``(vendor, "host/site")`` for any Workday URL shape, including ``/wday/cxs/``."""
    match = _WORKDAY_RE.search(text)
    if not match:
        return None
    host = match.group(1).lower()
    segments = [_clean(s) for s in match.group(2).split("/") if s.strip()]
    if segments[:2] == ["wday", "cxs"] and len(segments) >= 4:
        return "workday", f"{host}/{segments[3]}"
    for segment in segments:
        if _LOCALE_RE.match(segment) or segment.lower() in _WORKDAY_NOISE:
            continue
        return "workday", f"{host}/{segment}"
    return "workday", host


def _scan(text: str) -> tuple[str | None, str | None]:
    found = _workday(text)
    if found:
        return found
    for vendor, pattern in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        slug = _clean(match.group(1))
        if not slug or slug.lower() in _NOT_A_SLUG:
            continue
        return vendor, slug
    return None, None


def detect_ats(html: str | None, url: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(vendor, slug)`` for a careers page, or ``(None, None)``.

    ``html`` may be an empty string when only the URL is known; ``url`` may be a
    bare domain.  The URL is examined first because a page served *by* the board
    is unambiguous, whereas a company page may link to several vendors (an old
    board plus the current one).
    """
    if url:
        candidate = url if "://" in url else f"https://{url}"
        vendor, slug = _scan(candidate)
        if vendor:
            return vendor, slug
        query = parse_qs(urlparse(candidate).query)
        for key in ("for", "company", "board"):
            values = query.get(key)
            if values and "greenhouse" in candidate.lower():
                return "greenhouse", _clean(values[0])

    if not html:
        return None, None

    # Links and iframes first: a careers page points at its own board.
    hrefs = re.findall(r"(?:href|src|data-src|action)\s*=\s*[\"']([^\"']+)[\"']", html, re.I)
    for href in hrefs:
        vendor, slug = _scan(href if "://" in href else f"https://{href.lstrip('/')}")
        if vendor:
            return vendor, slug

    vendor, slug = _scan(html)
    if vendor:
        return vendor, slug
    return _widget_hints(html)


def _widget_hints(html: str) -> tuple[str | None, str | None]:
    """Board widgets that name their tenant in a script variable."""
    checks = (
        ("greenhouse", r"Grnhse\.Settings\s*=\s*\{[^}]*?for\s*:\s*[\"']([\w.-]+)[\"']"),
        ("greenhouse", r"grnhse_?job_?board[^\"']*[\"']([\w.-]+)[\"']"),
        ("ashby", r"ashby_embed[^{]*\{[^}]*jobBoardName\s*:\s*[\"']([\w.-]+)[\"']"),
        ("recruitee", r"recruitee[^{]*\{[^}]*company\s*:\s*[\"']([\w.-]+)[\"']"),
        ("personio", r"personio[^{]*\{[^}]*company\s*:\s*[\"']([\w.-]+)[\"']"),
    )
    for vendor, pattern in checks:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            slug = _clean(match.group(1))
            if slug and slug.lower() not in _NOT_A_SLUG:
                return vendor, slug
    return None, None


def adapter_key_for(vendor: str | None) -> str | None:
    """The registry key of the adapter that can read this vendor's boards."""
    return _ADAPTER_KEYS.get((vendor or "").lower())


def board_url(vendor: str | None, slug: str | None) -> str | None:
    """The human-facing board URL, for ``company.careers_url`` and audit trails."""
    if not vendor or not slug:
        return None
    vendor = vendor.lower()
    builders = {
        "greenhouse": lambda s: f"https://job-boards.greenhouse.io/{s}",
        "lever": lambda s: f"https://jobs.lever.co/{s}",
        "smartrecruiters": lambda s: f"https://careers.smartrecruiters.com/{s}",
        "ashby": lambda s: f"https://jobs.ashbyhq.com/{s}",
        "recruitee": lambda s: f"https://{s}.recruitee.com/",
        "personio": lambda s: f"https://{s}.jobs.personio.de/",
        "workday": lambda s: f"https://{s}",
        "teamtailor": lambda s: f"https://{s}.teamtailor.com/jobs",
        "workable": lambda s: f"https://apply.workable.com/{s}/",
        "join": lambda s: f"https://join.com/companies/{s}",
        "homerun": lambda s: f"https://{s}.homerun.co/",
    }
    builder = builders.get(vendor)
    return builder(slug) if builder else None
