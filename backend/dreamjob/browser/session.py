"""Attaching to the browser the user launched and logged into (FR-201, FR-202, FR-208).

The whole design of this slice follows from one rule: Dream Job never holds a
third-party site credential.  The *user* starts a browser with a remote
debugging port and a dedicated profile directory, logs in to LinkedIn or
Glassdoor with their own hands, and the system attaches to that live session
over the DevTools Protocol (FR-201, FR-202).  Nothing here asks for a
password, reads a cookie jar, exports a storage state or writes a session
token anywhere (NFR-203) - :func:`sanitise_capture` and
:func:`strip_credential_fields` exist precisely so a stored page cannot smuggle
one in either.

A dedicated profile directory (``settings.browser_profile_dir``) is not a
detail: it keeps the user's everyday browser profile untouched, which is part
of the RK-01 mitigation, and it means the automation window is visibly
separate from the one they browse with (FR-206).

FR-208: Chromium-family browsers are driven over CDP; Firefox is offered as an
alternative through a Playwright-managed persistent context, since Firefox
speaks its own remote protocol rather than CDP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from dreamjob.config import REPO_ROOT, get_settings
from dreamjob.egress.client import EgressClient

try:  # selectolax is a declared dependency; the guard keeps imports safe
    from selectolax.parser import HTMLParser
except ImportError:  # pragma: no cover
    HTMLParser = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

DEFAULT_NAVIGATION_TIMEOUT_MS = 45_000
MAX_CAPTURE_CHARS = 400_000
#: How long to let a client-rendered page draw one of its login markers before
#: giving up and judging the shell as it stands.  Only the ambiguous case ever
#: waits this long: the wait ends the moment a marker appears.
#:
#: Deliberately short.  This budget is spent in full whenever no marker matches,
#: and the likeliest reason for that is a marker that has gone stale (RK-04) -
#: so a generous timeout would mostly serve to make a wrong answer slower.
LOGIN_MARKER_TIMEOUT_MS = 4_000


class BrowserUnavailable(RuntimeError):
    """No live browser session to attach to.

    Raised where the user has not launched their browser with the debugging
    port, or Playwright is not installed.  Call sites turn this into launch
    instructions rather than a stack trace (FR-201).
    """


# ---------------------------------------------------------------------------
# The dedicated profile (FR-201, RK-01)
# ---------------------------------------------------------------------------


def profile_dir() -> Path:
    """Absolute path of the dedicated automation profile.  Never the default one."""
    configured = get_settings().browser_profile_dir
    return configured if configured.is_absolute() else REPO_ROOT / configured


def cdp_url() -> str:
    return get_settings().cdp_url.rstrip("/")


def _port() -> int:
    match = re.search(r":(\d+)", cdp_url())
    return int(match.group(1)) if match else 9222


# ---------------------------------------------------------------------------
# Launch instructions, as data the UI renders (FR-201)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchRecipe:
    """One concrete way to start a browser with the debugging port open."""

    key: str
    display_name: str
    family: str          # chromium | firefox
    os_name: str         # macos | windows | linux
    binary: str
    note: str = ""

    def command(self, port: int, profile: Path) -> str:
        if self.family == "firefox":
            # FR-208: Firefox does not answer CDP, so there is no port to open.
            # The user signs in once in the dedicated profile and Dream Job
            # re-opens that same profile through Playwright.
            return f'"{self.binary}" --profile "{profile}" --no-remote'
        return (
            f'"{self.binary}" '
            f"--remote-debugging-port={port} "
            f'--user-data-dir="{profile}" '
            "--no-first-run --no-default-browser-check"
        )

    def as_dict(self, port: int, profile: Path) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "family": self.family,
            "os": self.os_name,
            "command": self.command(port, profile),
            "note": self.note,
        }


RECIPES: tuple[LaunchRecipe, ...] = (
    LaunchRecipe(
        "chrome-macos", "Google Chrome", "chromium", "macos",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "Run it in Terminal. Chrome may already be open: the dedicated profile "
        "starts a second, separate window.",
    ),
    LaunchRecipe(
        "edge-macos", "Microsoft Edge", "chromium", "macos",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ),
    LaunchRecipe(
        "brave-macos", "Brave", "chromium", "macos",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ),
    LaunchRecipe(
        "chromium-macos", "Chromium", "chromium", "macos",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    LaunchRecipe(
        "firefox-macos", "Firefox (alternative driver)", "firefox", "macos",
        "/Applications/Firefox.app/Contents/MacOS/firefox",
        "FR-208: Firefox is driven through a Playwright persistent context on "
        "the same dedicated profile, not over CDP. Sign in once with this "
        "command, then close the window: Dream Job re-opens the profile itself "
        "when you start a run with the Firefox driver.",
    ),
    LaunchRecipe(
        "chrome-windows", "Google Chrome", "chromium", "windows",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "Run it in PowerShell, prefixed with & (the call operator).",
    ),
    LaunchRecipe(
        "edge-windows", "Microsoft Edge", "chromium", "windows",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ),
    LaunchRecipe(
        "firefox-windows", "Firefox (alternative driver)", "firefox", "windows",
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
    ),
    LaunchRecipe(
        "chrome-linux", "Google Chrome", "chromium", "linux", "google-chrome",
    ),
    LaunchRecipe(
        "chromium-linux", "Chromium", "chromium", "linux", "chromium",
    ),
    LaunchRecipe(
        "firefox-linux", "Firefox (alternative driver)", "firefox", "linux", "firefox",
    ),
)


def host_os() -> str:
    return {"darwin": "macos", "windows": "windows"}.get(platform.system().lower(), "linux")


def launch_instructions(site: str | None = None, os_name: str | None = None) -> dict[str, Any]:
    """The step-by-step launch procedure, as data for the UI (FR-201).

    Returned rather than printed: the frontend renders the steps, the copyable
    command per browser, and the warnings that belong to the chosen site.
    """
    os_name = os_name or host_os()
    port = _port()
    profile = profile_dir()
    site_profile = SITES.get(site or "")
    steps = [
        {
            "n": 1,
            "title": "Close nothing",
            "detail": (
                "Your normal browser can stay open. The automation uses a separate profile "
                f"directory ({profile}), so your everyday profile, history and logins are "
                "untouched."
            ),
        },
        {
            "n": 2,
            "title": "Start the browser with the debugging port",
            "detail": "Copy the command for your browser and run it in a terminal.",
            "commands": [r.as_dict(port, profile) for r in RECIPES if r.os_name == os_name],
        },
        {
            "n": 3,
            "title": "Log in yourself",
            "detail": (
                f"In that window, go to {site_profile.home_url if site_profile else 'the site'} "
                "and sign in by hand, solving any verification the site asks for. Dream Job "
                "never sees your password and never stores cookies or session tokens "
                "(FR-202, NFR-203)."
            ),
        },
        {
            "n": 4,
            "title": "Come back and connect",
            "detail": (
                "Press 'Check connection'. Dream Job attaches to that window over the "
                f"DevTools Protocol at {cdp_url()}."
            ),
        },
        {
            "n": 5,
            "title": "Keep watching",
            "detail": (
                "Leave the window visible: you can watch every page it opens, pause the run "
                "at any moment and skip individual targets (FR-206)."
            ),
        },
    ]
    return {
        "os": os_name,
        "cdp_url": cdp_url(),
        "port": port,
        "profile_dir": str(profile),
        "site": site,
        "warnings": [site_profile.terms_warning] if site_profile else [],
        "steps": steps,
        "recipes": [r.as_dict(port, profile) for r in RECIPES],
    }


# ---------------------------------------------------------------------------
# Site knowledge: what is allowed, and what "logged in" looks like
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteProfile:
    key: str
    display_name: str
    home_url: str
    login_url: str
    allowed_hosts: frozenset[str]
    logged_in_markers: tuple[str, ...]
    logged_out_markers: tuple[str, ...]
    terms_warning: str
    consent_kind: str | None = None
    #: The registrable name this site serves under, before any country suffix.
    #: Glassdoor answers on a per-country domain - glassdoor.be, glassdoor.nl,
    #: glassdoor.co.uk, glassdoor.de - and an enumerated host set silently
    #: refuses every one it was not told about, both as a detected tab and as a
    #: target.  Empty means "match the enumerated hosts and nothing else".
    domain: str = ""

    def allows_host(self, host: str) -> bool:
        """Is ``host`` this site?  Enumerated hosts first, then the domain.

        ``glassdoor.be`` and ``nl.glassdoor.be`` are Glassdoor just as much as
        ``www.glassdoor.com`` is.  The suffix test is anchored at the end of the
        host, so ``glassdoor.com.example.net`` is not Glassdoor - the label has
        to be followed by a public suffix and then nothing.
        """
        host = (host or "").strip().strip(".").lower()
        if not host:
            return False
        if host in self.allowed_hosts:
            return True
        return bool(self.domain) and bool(_domain_re(self.domain).search(host))


SITES: dict[str, SiteProfile] = {
    "linkedin": SiteProfile(
        key="linkedin",
        display_name="LinkedIn",
        home_url="https://www.linkedin.com/feed/",
        login_url="https://www.linkedin.com/login",
        allowed_hosts=frozenset({"www.linkedin.com", "linkedin.com"}),
        logged_in_markers=(
            # The current frontend, checked against a live signed-in feed on
            # 2026-09-10: the authenticated nav, the member feed itself, and
            # the share box that carries the member's own avatar.  None of the
            # markers below it survive on that page, which is why a signed-in
            # session used to read as "could not tell" (RK-04).
            'data-testid="primary-nav"',
            'data-testid="mainFeed"',
            "primaryNavLinksComponentRef",
            "shareboxProfilePictureComponentRef",
            # The previous frontend.  Kept because an OR costs nothing and a
            # session still served the old UI would otherwise go unrecognised.
            "global-nav__me",
            "feed-identity-module",
            'data-control-name="identity_welcome_message"',
            "artdeco-globalnav",
        ),
        logged_out_markers=("authwall", "sign-in-form", "join-form", "/uas/login"),
        # CR-401, recorded through the auth slice as consent 'linkedin_automation'.
        terms_warning=(
            "LinkedIn's user agreement prohibits automated access and scraping of its site, "
            "including through a session you are logged into yourself. This design reduces "
            "technical friction but not the contractual restriction: your account may be "
            "restricted or lost. Dream Job will only visit the targets on the plan's list, "
            "at human pace, and will stop at the first challenge page."
        ),
        consent_kind="linkedin_automation",
    ),
    "glassdoor": SiteProfile(
        key="glassdoor",
        display_name="Glassdoor",
        home_url="https://www.glassdoor.com/member/home/index.htm",
        login_url="https://www.glassdoor.com/profile/login_input.htm",
        allowed_hosts=frozenset(
            {"www.glassdoor.com", "glassdoor.com", "www.glassdoor.be", "www.glassdoor.nl",
             "www.glassdoor.co.uk", "www.glassdoor.fr"}
        ),
        # Glassdoor answers on a domain per country; enumerating them missed
        # glassdoor.be without the www, every other country domain, and the
        # language subdomains - as a detected tab and as a refused target.
        domain="glassdoor",
        logged_in_markers=('data-test="site-header-profile"', "member-home", "userProfileMenu"),
        logged_out_markers=('data-test="sign-in"', "hardsellOverlay", "contentWall"),
        terms_warning=(
            "Glassdoor's terms restrict automated access. Compensation and review data "
            "collected here is advisory only: it is opinion data, kept with the campaign "
            "that collected it and never presented as fact."
        ),
    ),
}


def site_for_url(url: str) -> SiteProfile | None:
    host = _host(url)
    for profile in SITES.values():
        if profile.allows_host(host):
            return profile
    return None


@lru_cache(maxsize=16)
def _domain_re(domain: str) -> re.Pattern[str]:
    """``glassdoor`` -> a pattern matching glassdoor.be, www.glassdoor.co.uk, ...

    Anchored at both ends: the label must be a whole host label, and it must be
    followed by a public suffix and then the end of the host.  A one- or
    two-label suffix covers ``.com`` and ``.co.uk`` alike.
    """
    return re.compile(
        rf"^(?:[a-z0-9-]+\.)*{re.escape(domain)}\.[a-z]{{2,4}}(?:\.[a-z]{{2}})?$"
    )


def _host(url: str) -> str:
    match = re.match(r"https?://([^/?#]+)", url or "", re.IGNORECASE)
    return match.group(1).lower() if match else ""


def login_state(site: str, url: str, html: str) -> bool | None:
    """Is this site logged in, judged from one page?  ``None`` means unknown.

    A pure function on purpose: the markers are the fragile part, so they are
    testable against a saved page without a browser (RK-04).
    """
    profile = SITES.get(site)
    if profile is None:
        return None
    haystack = f"{url}\n{html or ''}"
    if any(marker in haystack for marker in profile.logged_out_markers):
        return False
    if any(marker in haystack for marker in profile.logged_in_markers):
        return True
    return None


#: ``data-control-name="identity_welcome_message"`` and friends: a marker that
#: already names an attribute is one, and becomes an attribute selector.
_ATTR_MARKER_RE = re.compile(r'^([a-zA-Z][\w:-]*)\s*=\s*"([^"]*)"$')
_TOKEN_MARKER_RE = re.compile(r"^[\w-]+$")


def marker_selector(marker: str) -> str | None:
    """The CSS selector that finds ``marker`` in a rendered page, or ``None``.

    ``None`` where the marker is a URL fragment (``/uas/login``): those are
    judged from the address, which needs no DOM at all.

    Derived from :attr:`SiteProfile.logged_in_markers` rather than maintained
    beside them on purpose.  The markers are the fragile part (RK-04) and one
    fragile list is better than two that can drift apart; substring matching is
    preserved as ``[class*=]`` / ``[id*=]`` so a selector never claims to be
    stricter than the :func:`login_state` check it stands in for.
    """
    marker = marker.strip()
    if not marker or marker.startswith("/"):
        return None
    attribute = _ATTR_MARKER_RE.match(marker)
    if attribute is not None:
        name, value = attribute.groups()
        return f'[{name}="{value}"]'
    if not _TOKEN_MARKER_RE.match(marker):
        return None
    return f'[class*="{marker}"],[id*="{marker}"]'


def login_selectors(site: str) -> str:
    """One CSS selector matching any login marker of ``site``; "" if there are none.

    Both directions are included: the wait ends as soon as the page has drawn
    *an* answer, signed in or signed out, and :func:`login_state` reads which.
    """
    profile = SITES.get(site)
    if profile is None:
        return ""
    markers = (*profile.logged_in_markers, *profile.logged_out_markers)
    return ",".join(dict.fromkeys(s for s in map(marker_selector, markers) if s))


# ---------------------------------------------------------------------------
# NFR-203: nothing that looks like a credential is ever written down
# ---------------------------------------------------------------------------

_STRIP_TAGS_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_BARE_TAG_RE = re.compile(r"<(script|style|noscript|template)\b[^>]*/?>", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(csrf[-_]?token|xsrf[-_]?token|authenticity[-_]?token|jsessionid|li_at|li_rm"
    r"|session[-_]?id|sessionid|access[-_]?token|id[-_]?token|refresh[-_]?token|auth[-_]?token"
    r"|bearer|set-cookie|cookie|password|passwd)\b"
    r"\s*[:=]\s*[\"']?[^\"'&;,\s<>]{4,}"
)
#: A logged-in page also carries its session identifiers as markup, not only as
#: script: ``<meta name="csrf-token" content="...">`` and the hidden
#: ``authenticity_token`` input are the two every framework writes.  The whole
#: element goes, since nothing this slice extracts is read from meta or input
#: tags (NFR-203).
_SECRET_ELEMENT_RE = re.compile(
    r"""(?is)<(?:meta|input)\b[^>]*\b(?:name|id|property)\s*=\s*["']?[^"'>]*"""
    r"""(?:csrf|xsrf|authenticity|token|session|auth|password|passwd|secret|api[-_]?key)"""
    r"""[^"'>]*["']?[^>]*>"""
)
_CREDENTIAL_KEY_RE = re.compile(
    r"(?i)cookie|token|password|passwd|secret|credential|session|authorization|csrf|api[-_]?key"
)

REDACTED = "[redacted-NFR-203]"


def _drop_script(match: re.Match[str]) -> str:
    """Keep JSON-LD, drop every other script: RK-04 needs the structured data."""
    body = match.group(0)
    if match.group(1).lower() == "script" and "ld+json" in body[: body.find(">") + 1].lower():
        return body
    return " "


def sanitise_capture(html: str, *, max_chars: int = MAX_CAPTURE_CHARS) -> str:
    """Make a captured page safe to store (NFR-203).

    Executable script and styling go - that is where a logged-in page keeps its
    CSRF token and its session identifiers - as do the ``<meta>`` and hidden
    ``<input>`` elements that carry the same values in markup, while
    ``application/ld+json``
    blocks stay, because re-extracting from a retained raw document is the
    mitigation for RK-04.  Whatever survives still has every ``name = value``
    pair with a credential-shaped name redacted.  Pure and deterministic, so
    the guarantee is testable.
    """
    if not html:
        return ""
    cleaned = _STRIP_TAGS_RE.sub(_drop_script, html)
    cleaned = _BARE_TAG_RE.sub(_drop_script, cleaned)
    cleaned = _SECRET_ELEMENT_RE.sub(REDACTED, cleaned)
    cleaned = _SECRET_ASSIGNMENT_RE.sub(REDACTED, cleaned)
    return cleaned[:max_chars]


def strip_credential_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop any field whose name suggests a credential before persistence (NFR-203)."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if _CREDENTIAL_KEY_RE.search(str(key)):
            log.warning("Dropping field %r before storage (NFR-203)", key)
            continue
        out[key] = strip_credential_fields(value) if isinstance(value, dict) else value
    return out


def safe_url(url: str) -> str:
    """A URL without its query string - query strings carry tokens (NFR-203)."""
    return (url or "").split("?", 1)[0].split("#", 1)[0]


# ---------------------------------------------------------------------------
# Reading a capture: the shared bits every site module needs
# ---------------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_INLINE_WS = re.compile(r"[ \t\xa0]+")


def tidy(text: str) -> str:
    """Collapse whitespace without losing the line structure extraction relies on."""
    collapsed = _INLINE_WS.sub(" ", (text or "").replace("\r", "\n"))
    lines = [line.strip() for line in collapsed.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def json_ld(html: str) -> list[dict[str, Any]]:
    """Every JSON-LD object on a page, flattened out of ``@graph`` lists.

    Read from the *raw* capture, before :func:`sanitise_capture` removes the
    script tags: structured data is the most stable extraction path there is,
    which is what keeps RK-04 (layout changes) manageable.
    """
    out: list[dict[str, Any]] = []
    for match in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(match.group(1).strip())
        except ValueError:
            continue
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                out.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


def json_ld_of_type(objects: list[dict[str, Any]], *types: str) -> dict[str, Any] | None:
    wanted = {t.lower() for t in types}
    for obj in objects:
        raw = obj.get("@type")
        kinds = raw if isinstance(raw, list) else [raw]
        if any(str(k).lower() in wanted for k in kinds if k):
            return obj
    return None


def text_of(html: str, *selectors: str) -> str:
    """Text of the first selector that matches; ``""`` when none does."""
    if HTMLParser is None or not html:  # pragma: no cover - selectolax is a hard dependency
        return ""
    tree = HTMLParser(html)
    for selector in selectors:
        node = tree.css_first(selector)
        if node is not None:
            text = tidy(node.text())
            if text:
                return text
    return ""


def page_text(html: str) -> str:
    """Readable text of a captured page - what the LLM fallback is given (NFR-205)."""
    if HTMLParser is None or not html:  # pragma: no cover
        return tidy(re.sub(r"<[^>]+>", " ", html or ""))
    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript", "svg", "template"):
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    return tidy(body.text(separator="\n")) if body is not None else ""


# ---------------------------------------------------------------------------
# Detection (FR-201)
# ---------------------------------------------------------------------------


@dataclass
class CdpStatus:
    connected: bool
    cdp_url: str
    browser: str | None = None
    protocol_version: str | None = None
    open_tabs: int = 0
    hosts: list[str] = field(default_factory=list)
    sites: dict[str, bool] = field(default_factory=dict)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "cdp_url": self.cdp_url,
            "browser": self.browser,
            "protocol_version": self.protocol_version,
            "open_tabs": self.open_tabs,
            "hosts": self.hosts,
            "sites": self.sites,
            "detail": self.detail,
        }


async def probe(url: str | None = None, *, timeout: float = 5.0) -> CdpStatus:
    """Is a debuggable browser listening, and which sites does it have open?

    Only the DevTools metadata endpoints are read, through the egress layer
    (IR-102) with caching and raw-document storage off: the reply names the
    browser build and the open tabs, and none of it is kept (NFR-203).  Open
    tab URLs are reduced to hostnames before they leave this function.
    """
    endpoint = (url or cdp_url()).rstrip("/")
    status = CdpStatus(connected=False, cdp_url=endpoint)
    try:
        async with EgressClient(respect_robots=False, store_raw=False) as eg:
            version = await asyncio.wait_for(
                eg.fetch_json(f"{endpoint}/json/version", use_cache=False), timeout
            )
            tabs = await asyncio.wait_for(
                eg.fetch_json(f"{endpoint}/json/list", use_cache=False), timeout
            )
    except Exception as exc:  # noqa: BLE001 - "not running" is the normal case
        status.detail = (
            f"No browser is listening on {endpoint}. Follow the launch instructions "
            f"and try again ({type(exc).__name__})."
        )
        return status

    if isinstance(version, dict):
        status.browser = str(version.get("Browser") or "")
        status.protocol_version = str(version.get("Protocol-Version") or "")
    pages = [t for t in tabs if isinstance(t, dict) and t.get("type") == "page"] if (
        isinstance(tabs, list)
    ) else []
    status.open_tabs = len(pages)
    status.hosts = sorted({h for h in (_host(str(p.get("url", ""))) for p in pages) if h})
    status.sites = {
        key: any(profile.allows_host(host) for host in status.hosts)
        for key, profile in SITES.items()
    }
    status.connected = True
    status.detail = f"Attached to {status.browser or 'a browser'} with {len(pages)} open tab(s)."
    return status


async def is_available(url: str | None = None) -> bool:
    return (await probe(url)).connected


DRIVERS: tuple[str, ...] = ("cdp", "persistent")
FAMILIES: tuple[str, ...] = ("chromium", "firefox")


def driver_choice(driver: str | None, family: str | None) -> tuple[str, str]:
    """Normalise a requested driver/browser pair (FR-208).

    Firefox has no CDP endpoint to attach to, so choosing it always means the
    Playwright-managed persistent context on the dedicated profile; anything
    unrecognised falls back to the Chromium-over-CDP path FR-201 describes.
    """
    family = (family or "chromium").lower()
    if family not in FAMILIES:
        family = "chromium"
    driver = (driver or "cdp").lower()
    if driver not in DRIVERS:
        driver = "cdp"
    return ("persistent" if family == "firefox" else driver, family)


# ---------------------------------------------------------------------------
# The live session (FR-201, FR-202, FR-208)
# ---------------------------------------------------------------------------


@dataclass
class PageLoad:
    """One navigation, measured - the estimator feeds on these (FR-204)."""

    url: str
    status: int | None
    seconds: float
    title: str = ""


class BrowserSession:
    """A live attachment to the user's browser.  Owns no credentials (FR-202).

    Used as an async context manager.  On exit it *disconnects*: the user's
    browser, its tabs and its logged-in state are left exactly as they were,
    and no storage state is exported.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        driver: str = "cdp",
        family: str = "chromium",
        headless: bool = False,
    ):
        self.cdp_url = (url or cdp_url()).rstrip("/")
        self.driver = driver
        self.family = family
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> BrowserSession:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self._context is not None

    async def connect(self) -> BrowserSession:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - playwright is a declared dependency
            raise BrowserUnavailable(
                "Playwright is not installed; run `pip install -r requirements.txt`."
            ) from exc

        self._playwright = await async_playwright().start()
        try:
            if self.driver == "persistent":
                # FR-208: Playwright-managed persistent context on the dedicated
                # profile, for Firefox and for users who prefer not to open a port.
                name = "firefox" if self.family == "firefox" else "chromium"
                engine = getattr(self._playwright, name)
                profile = profile_dir()
                profile.mkdir(parents=True, exist_ok=True)
                self._context = await engine.launch_persistent_context(
                    str(profile), headless=self.headless
                )
            else:
                self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
                contexts = self._browser.contexts
                self._context = contexts[0] if contexts else await self._browser.new_context()
        except Exception as exc:  # noqa: BLE001 - turned into launch instructions upstream
            await self._shutdown_playwright()
            raise BrowserUnavailable(
                f"Could not attach to a browser at {self.cdp_url}: {exc}. "
                "Start the browser with the remote debugging port and log in first "
                "(FR-201, FR-202)."
            ) from exc
        return self

    async def close(self) -> None:
        """Detach.  Never closes the user's browser and never clears their profile."""
        try:
            if self._page is not None:
                await self._page.close()
        except Exception:  # noqa: BLE001
            log.debug("The automation tab was already gone")
        self._page = None
        try:
            if self.driver == "persistent" and self._context is not None:
                await self._context.close()
            elif self._browser is not None:
                # Disconnect only: the window stays open so the user keeps their session.
                await self._browser.close()
        except Exception:  # noqa: BLE001
            log.debug("Browser detach raised; the session was already closed")
        self._browser = None
        self._context = None
        await self._shutdown_playwright()

    async def _shutdown_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                log.debug("Playwright was already stopped")
            self._playwright = None

    # -- pages --------------------------------------------------------------
    async def page(self) -> Any:
        """One reusable tab for the automation, opened next to the user's own tabs."""
        if self._context is None:
            raise BrowserUnavailable("Not attached to a browser session")
        if self._page is None:
            self._page = await self._context.new_page()
            self._page.set_default_navigation_timeout(DEFAULT_NAVIGATION_TIMEOUT_MS)
        return self._page

    async def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> PageLoad:
        """Navigate and measure.  The measurement feeds the estimator (FR-204)."""
        page = await self.page()
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await page.goto(url, wait_until=wait_until)
        seconds = loop.time() - started
        status = getattr(response, "status", None) if response is not None else None
        title = ""
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001 - a challenge page can refuse this
            log.debug("Could not read the title of %s", safe_url(url))
        # The URL the browser *ended* on, not the one asked for: a redirect to
        # an authwall, a login page or a checkpoint is the whole signal that
        # login_state and pacing.detect_challenge read off the address.
        landed = getattr(page, "url", "") or url
        return PageLoad(url=landed, status=status, seconds=seconds, title=title)

    async def content(self) -> str:
        """Raw DOM of the current page.  Sanitised only on the way to storage."""
        page = await self.page()
        try:
            return await page.content()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read page content: %s", exc)
            return ""

    async def screenshot(self) -> bytes:
        page = await self.page()
        try:
            return await page.screenshot(full_page=False)
        except Exception as exc:  # noqa: BLE001 - screenshots are evidence, not a dependency
            log.debug("Screenshot failed: %s", exc)
            return b""

    async def bring_to_front(self) -> None:
        """FR-206 / FR-328: put the window where the user can see and act on it."""
        try:
            page = await self.page()
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            log.debug("Could not raise the browser window")

    async def wait_for_login_markers(
        self, site: str, *, timeout_ms: int = LOGIN_MARKER_TIMEOUT_MS
    ) -> bool:
        """Wait for the page to draw a login marker.  ``True`` if one appeared.

        Both sites render their navigation on the client, so the DOM at
        ``domcontentloaded`` can still be a shell that carries neither a
        signed-in nor a signed-out marker.  On a warm profile the feed is
        usually complete by then and this returns almost at once; the wait is
        here for the slow load, and it is deliberately cheap in the common case.

        It is *not* a substitute for keeping the markers current: a stale
        marker never appears no matter how long anyone waits.

        Waits for either direction - the first marker to appear ends the wait,
        and :func:`login_state` reads which one it was.  ``state="attached"``
        because the markers are matched as text by :func:`login_state`, which
        does not care whether the element is on screen.
        """
        selector = login_selectors(site)
        if not selector:
            return False
        try:
            page = await self.page()
            await page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
            return True
        except Exception:  # noqa: BLE001 - an unmarked page is an answer too
            log.debug("No login marker for %s within %dms", site, timeout_ms)
            return False

    # -- login state (FR-202) ----------------------------------------------
    async def check_login(self, site: str) -> dict[str, Any]:
        """Visit the site's home page and judge whether the user is signed in.

        Reads the DOM only.  It does not touch the cookie jar, and it cannot:
        the answer is derived from what the page renders (NFR-203).
        """
        profile = SITES.get(site)
        if profile is None:
            raise ValueError(f"Unknown site {site!r}; known: {sorted(SITES)}")
        load = await self.goto(profile.home_url)
        # A redirect to the authwall or the login page has already answered the
        # question; only an ambiguous landing is worth waiting on.
        if login_state(site, load.url, "") is None:
            await self.wait_for_login_markers(site)
        html = await self.content()
        state = login_state(site, load.url, html)
        return {
            "site": site,
            "logged_in": state,
            "status": load.status,
            "title": load.title,
            "login_url": profile.login_url,
            "detail": (
                "Signed in." if state
                else "Not signed in - log in by hand in the automation window (FR-202)."
                if state is False
                else "Could not tell; open the site in the automation window and check."
            ),
        }
