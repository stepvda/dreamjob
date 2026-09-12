"""Finding and inferring professional e-mail addresses (FR-303, CR-402, RK-08).

FR-303 names four methods and asks for the one that produced each address to be
recorded.  They are tried in that order because it is also the order of
decreasing certainty and increasing intrusiveness:

``website``
    Addresses printed on the company's own contact, careers and legal pages.
    Published by the company for exactly this purpose, so both the strongest
    evidence and the least intrusive collection (CR-402).
``press``
    Addresses on press, newsroom and media pages.  Same standing, usually a
    role mailbox.
``pattern_inference``
    No address for the person, but the domain's convention can be read off the
    addresses that *are* published.  :func:`infer_pattern` scores the
    convention against the named addresses it has seen; a guess derived from a
    weak convention stays a guess, which is why the confidence travels with the
    address and FR-304 validation still has to pass before it is used.
``lookup_service``
    A permitted third-party enrichment service.  OQ-05 has not settled which
    services are acceptable in terms of cost and data protection, so the client
    is complete but the feature is **off by default** and refuses to run until
    an administrator names a provider and switches it on.

Only professional addresses are collected, and nothing beyond the address, the
method and the page it came from is kept (FR-306, RK-08).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.website import crawler
from dreamjob.db.repositories import contacts as repo
from dreamjob.db.repositories import knowledge as kb
from dreamjob.egress.client import EgressClient, RobotsDisallowed

log = logging.getLogger(__name__)

# --- FR-303 method labels, stored verbatim in ``contact.email_source_method``
METHOD_WEBSITE = "website"
METHOD_PRESS = "press"
METHOD_PATTERN = "pattern_inference"
METHOD_LOOKUP = "lookup_service"
METHOD_MANUAL = "manual"
METHOD_VACANCY = "vacancy"
METHOD_SEARCH = "search"
METHOD_SECURITY_TXT = "security_txt"
METHOD_SITEMAP = "sitemap"
METHOD_JSONLD = "json_ld"
#: The backup sources.  ``stored_document`` is the address printed in a posting
#: the corpus already holds; ``ats_board`` is the address printed on the
#: employer's applicant-tracking board's own pages.  Both were published by the
#: employer, so neither is composed and neither is uncertain.
METHOD_ATS_BOARD = "ats_board"
METHOD_STORED_DOCUMENT = "stored_document"

#: Confidence attached to an address purely because of how it was obtained.
METHOD_CONFIDENCE = {
    METHOD_MANUAL: 0.95,
    METHOD_WEBSITE: 0.85,
    METHOD_VACANCY: 0.85,
    METHOD_PRESS: 0.8,
    METHOD_SECURITY_TXT: 0.75,
    METHOD_JSONLD: 0.8,
    METHOD_ATS_BOARD: 0.6,
    METHOD_STORED_DOCUMENT: 0.45,
    METHOD_LOOKUP: 0.7,
    METHOD_SEARCH: 0.6,
    METHOD_SITEMAP: 0.75,
    METHOD_PATTERN: 0.45,
}

#: OQ-05 - the lookup service stays off until an administrator answers it.
SETTING_LOOKUP_ENABLED = "contacts.lookup_service_enabled"
SETTING_LOOKUP_URL = "contacts.lookup_service_url"
SETTING_LOOKUP_KEY = "contacts.lookup_service_api_key"
SETTING_LOOKUP_NAME = "contacts.lookup_service_name"

#: The search provider.  A search engine has already crawled the sites that
#: block us (F5/TSPD, Cloudflare, Incapsula all answer our fetches with a JS
#: challenge), so results and snippets are the only way to read them.  It is an
#: API rather than a scraped results page: the engines disallow the HTML search
#: endpoint in robots.txt, and IR-101 means we do not work around that.  Off
#: until an administrator names a provider and a key (same posture as OQ-05).
SETTING_SEARCH_ENABLED = "contacts.search_enabled"
SETTING_SEARCH_PROVIDER = "contacts.search_provider"   # brave | bing | google_cse
SETTING_SEARCH_KEY = "contacts.search_api_key"
SETTING_SEARCH_URL = "contacts.search_api_url"

# An address in running text.  Deliberately stricter than RFC 5322: the input is
# scraped HTML, and a permissive pattern turns CSS and JSON into "addresses".
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"([A-Za-z0-9](?:[A-Za-z0-9._%+\-]{0,62}[A-Za-z0-9])?)"
    r"@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})",
)

_MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.IGNORECASE)

# "name (at) example (dot) com" and friends, the usual anti-harvesting spelling.
#
# The two runs are **bounded**, and it is not cosmetic.  ``at`` is not anchored
# to a word boundary - it cannot be, because the spelling being caught is
# "jan(at)acme.be" - so every occurrence of those two letters anywhere in the
# document is a place this pattern starts trying.  With an unbounded ``+`` in
# front of it, each attempt first swallows the whole surrounding run of
# local-part characters and then gives it back one character at a time, which is
# quadratic in the length of that run: a 40 kB base64 data: URI - one image
# inlined in a page - took 66 seconds, and a 400 kB one would have taken close
# to two hours.  It froze a contacts pass mid-crawl, and because
# :func:`collect_from_site` runs inside the event loop it froze every other
# company with it.
#
# 64 and 255 are the RFC 5321 maxima for a local part and a domain, which is the
# same bound :data:`EMAIL_RE` above already applies, so no address that could
# exist is lost - only the backtracking is.
_OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]{1,64})\s*(?:\(|\[|&#40;)?\s*(?:at|apenstaartje|arobase)\s*"
    r"(?:\)|\]|&#41;)?\s*([A-Za-z0-9.\-]{1,255})\s*(?:\(|\[)?\s*(?:dot|punt|point)\s*"
    r"(?:\)|\])?\s*([A-Za-z]{2,24})",
    re.IGNORECASE,
)

#: File extensions that look like addresses once an "@" sneaks into a filename.
_NOT_A_DOMAIN = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|json|xml|pdf|woff2?)$", re.I)

#: Pages worth reading for an address, in the order FR-303 lists them.  The
#: list is wider than the contact pages alone: on corporate sites the address
#: usually sits on the imprint, the privacy notice, the team page or the
#: careers page, not behind a contact form.  Language variants cover the four
#: languages of the market (EN/NL/FR/DE).
CONTACT_PATHS: tuple[str, ...] = (
    "/contact",
    "/contact-us",
    "/contacts",
    "/contacteer-ons",
    "/contacteer",
    "/contactez-nous",
    "/nous-contacter",
    "/kontakt",
    "/about/contact",
    "/company/contact",
    "/contact.html",
    "/about",
    "/about-us",
    "/over-ons",
    "/wie-zijn-wij",
    "/overons",
    "/a-propos",
    "/a-propos-de-nous",
    "/uber-uns",
    "/team",
    "/our-team",
    "/nos-equipes",
    "/unser-team",
    "/meet-the-team",
    "/people",
    "/leadership",
    "/management",
    "/medewerkers",
    "/equipe",
    "/imprint",
    "/impressum",
    "/legal",
    "/legal/impressum",
    "/legal-notice",
    "/mentions-legales",
    "/disclaimer",
    "/privacy",
    "/privacy-policy",
    "/privacybeleid",
    "/privacyverklaring",
    "/datenschutz",
    "/confidentialite",
    "/conditions-generales",
    "/terms",
    "/press",
    "/pers",
    "/presse",
    "/press-releases",
    "/newsroom",
    "/news",
    "/nieuws",
    "/actualites",
    "/media",
    "/about/press",
    "/jobs",
    "/careers",
    "/career",
    "/vacatures",
    "/vacature",
    "/vacancies",
    "/werken-bij",
    "/jobs/contact",
    "/carriere",
    "/carrieres",
    "/karriere",
    "/bewerbung",
    "/emploi",
    "/recrutement",
    "/offres-emploi",
    "/nous-rejoindre",
    "/solliciteren",
    "/join-us",
    "/join",
    # Locale-prefixed guesses, because the four-language market serves the same
    # page as ``/nl/contact`` and ``/fr/a-propos`` as often as at the root.
    *(
        f"/{locale}{path}"
        for locale in ("nl", "fr", "en", "de")
        for path in (
            "/contact",
            "/contact-us",
            "/over-ons",
            "/a-propos",
            "/uber-uns",
            "/kontakt",
            "/careers",
            "/jobs",
        )
    ),
)

#: Local parts that reach the hiring function - the FR-301 generic fallback.
CAREERS_LOCAL_PARTS: tuple[str, ...] = (
    "jobs",
    "careers",
    "career",
    "recruitment",
    "recruiting",
    "hr",
    "hrm",
    "talent",
    "vacatures",
    "vacature",
    "werkenbij",
    "sollicitatie",
    "emploi",
    "recrutement",
    "personal",
    "personeel",
    "bewerbung",
    "karriere",
    "apply",
    "people",
)

#: Anchor and URL tokens that mark a page worth reading for an address.  The
#: team and leadership words matter because an address in context there names a
#: person, which is both a better contact and the evidence the pattern
#: inference in FR-303 needs.
CONTACT_LINK_TOKENS: tuple[str, ...] = (
    "contact", "contacteer", "kontakt", "imprint", "impressum", "legal-notice",
    "mentions-legales", "colophon", "press", "pers", "presse", "newsroom", "media",
    "jobs", "careers", "vacature", "werken-bij", "recruit", "emploi", "about",
    "team", "people", "leadership", "management", "medewerkers", "equipe",
    "talent", "hr", "human-resources", "privacy",
)

#: Local parts that reach a company but not the hiring function.
FALLBACK_LOCAL_PARTS: tuple[str, ...] = ("info", "contact", "hello", "office", "mail")

#: Signatures of the bot-protection interstitials that large corporate sites
#: answer every fetch with.  A challenge page is not a page: it has no links
#: and no addresses, so treating its empty harvest as "this company has no
#: address" is the silent failure this constant exists to name.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "bobcmn",                 # F5 BIG-IP ASM / TSPD
    "tspd_101",
    "cf-chl-",                # Cloudflare
    "cf_chl_",
    "just a moment",          # Cloudflare "Just a moment..."
    "attention required",     # Cloudflare "Attention Required!"
    "enable javascript and cookies",   # Cloudflare
    "_incap_ses",             # Imperva / Incapsula
    "incapsula incident",
    "visid_incap",
    "distil_r_captcha",       # Distil / Imperva
    "px-captcha",             # PerimeterX / HUMAN
    "perimeterx",
    "datadome",
    "akamai bot manager",
    "/_fs-ch-",               # F5 shape
    "are you a robot",
    "unusual traffic",
    "please verify you are a human",
    "checking your browser",
)


def looks_like_challenge(html: str) -> bool:
    """Whether a fetched page is a bot-protection interstitial, not content.

    Cheap and local: the markers are literal substrings the vendors ship in the
    challenge document.  A false positive costs one source for one company; a
    false negative is what makes a blocked company look contactless, so the
    check errs towards flagging.
    """
    if not html:
        return False
    haystack = html[:20_000].lower()
    return any(marker in haystack for marker in CHALLENGE_MARKERS)


# ---------------------------------------------------------------------------
# Name and local-part normalisation
# ---------------------------------------------------------------------------

#: Dutch, French and German name particles.  "Stephane van der Aa" is filed
#: under "vanderaa" by some employers and under "aa" by others, so both are
#: generated and the observed addresses decide which the domain uses.
NAME_PARTICLES = frozenset(
    {
        "van", "de", "der", "den", "ter", "ten", "het", "'t", "op", "in",
        "vande", "vander", "vanden", "du", "des", "le", "la", "les", "di",
        "da", "dos", "del", "della", "von", "zu", "af", "af.", "mac", "mc",
    }
)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def slug(text: str) -> str:
    """A name part as it can appear in a local part: ascii letters and digits."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text or "").lower())


@dataclass(frozen=True)
class NameParts:
    """A person's name split the way an e-mail convention splits it."""

    first: str
    last: str
    middle: tuple[str, ...] = ()
    particles: tuple[str, ...] = ()

    @property
    def last_no_particle(self) -> str:
        return self.last

    @property
    def last_with_particle(self) -> str:
        return "".join(self.particles) + self.last

    def last_variants(self) -> tuple[str, ...]:
        variants = [self.last]
        if self.particles:
            variants.append(self.last_with_particle)
        return tuple(dict.fromkeys(v for v in variants if v))


def split_name(full_name: str) -> NameParts | None:
    """Split a display name into first, particles and last (FR-303).

    Returns ``None`` for anything that is not a person's name - a single token,
    a department label - because a pattern cannot be applied to it.
    """
    cleaned = re.sub(r"\s+", " ", strip_accents(full_name or "")).strip()
    cleaned = re.sub(r"[,;].*$", "", cleaned).strip()
    # Drop honorifics and post-nominals that would otherwise become the surname.
    tokens = [
        t
        for t in cleaned.split(" ")
        if t and t.lower().strip(".") not in {"mr", "mrs", "ms", "dr", "prof", "ir", "drs", "mgr"}
    ]
    tokens = [t for t in tokens if not re.fullmatch(r"\(.*\)", t)]
    if len(tokens) < 2:
        return None

    first = slug(tokens[0])
    rest = tokens[1:]
    particles = tuple(slug(t) for t in rest[:-1] if t.lower() in NAME_PARTICLES)
    middle = tuple(slug(t) for t in rest[:-1] if t.lower() not in NAME_PARTICLES)
    last = slug(rest[-1])
    if not first or not last:
        return None
    return NameParts(first=first, last=last, middle=middle, particles=particles)


# ---------------------------------------------------------------------------
# The patterns themselves (FR-303)
# ---------------------------------------------------------------------------

#: Pattern id -> how the local part is built from a name.
#:
#: Ordered by how common the convention is in the target market, which is the
#: tie-breaker when the observed evidence supports several equally.
PATTERNS: dict[str, Callable[[str, str], str]] = {
    "first.last": lambda first, last: f"{first}.{last}",
    "firstlast": lambda first, last: f"{first}{last}",
    "f.last": lambda first, last: f"{first[0]}.{last}",
    "flast": lambda first, last: f"{first[0]}{last}",
    "first": lambda first, last: first,
    "first_last": lambda first, last: f"{first}_{last}",
    "first-last": lambda first, last: f"{first}-{last}",
    "last.first": lambda first, last: f"{last}.{first}",
    "lastfirst": lambda first, last: f"{last}{first}",
    "firstl": lambda first, last: f"{first}{last[0]}",
    "first.l": lambda first, last: f"{first}.{last[0]}",
    "last": lambda first, last: last,
    "lastf": lambda first, last: f"{last}{first[0]}",
}

PATTERN_ORDER: tuple[str, ...] = tuple(PATTERNS)


def render_pattern(pattern: str, name: NameParts, *, particle: bool = False) -> str | None:
    """Build the local part one pattern would give this name."""
    builder = PATTERNS.get(pattern)
    if builder is None:
        return None
    last = name.last_with_particle if particle and name.particles else name.last
    if not name.first or not last:
        return None
    return builder(name.first, last)


def address_for(pattern: str, full_name: str, domain: str, *, particle: bool = False) -> str | None:
    parts = split_name(full_name)
    if parts is None:
        return None
    local = render_pattern(pattern, parts, particle=particle)
    if not local:
        return None
    return f"{local}@{domain.lower()}"


# ---------------------------------------------------------------------------
# Harvesting addresses from page text (FR-303 website / press)
# ---------------------------------------------------------------------------


@dataclass
class FoundAddress:
    """One address as it was found, with the FR-303 method that found it."""

    email: str
    method: str
    source_url: str = ""
    context: str = ""
    full_name: str | None = None
    confidence: float = 0.5
    pattern: str | None = None

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1]

    @property
    def local_part(self) -> str:
        return self.email.rsplit("@", 1)[0]

    def as_contact_fields(self) -> dict[str, Any]:
        """The FR-306 subset: the address, how it was found and where."""
        return {
            "email": self.email,
            "email_source_method": self.method,
            "source": self.source_url or self.method,
            "confidence": round(self.confidence, 3),
        }


def _plausible(email: str) -> bool:
    local, _, domain = email.partition("@")
    if not local or not domain or _NOT_A_DOMAIN.search(domain):
        return False
    if len(email) > 254 or ".." in email:
        return False
    # Tracking pixels and Sentry DSNs are the usual false positives.
    return not (len(local) > 40 and not any(c in local for c in "._-"))


def extract_addresses(text: str, *, source_url: str = "", method: str = METHOD_WEBSITE,
                      domain: str | None = None) -> list[FoundAddress]:
    """Every address in one page, de-duplicated, optionally restricted to a domain."""
    found: dict[str, FoundAddress] = {}
    haystack = text or ""

    def add(email: str, context: str) -> None:
        email = email.strip().strip(".,;:<>()[]\"'").lower()
        if not _plausible(email):
            return
        if domain and not email.endswith("@" + domain.lower()):
            return
        found.setdefault(
            email,
            FoundAddress(
                email=email,
                method=method,
                source_url=source_url,
                context=context[:200],
                confidence=METHOD_CONFIDENCE.get(method, 0.6),
            ),
        )

    for match in _MAILTO_RE.finditer(haystack):
        add(match.group(1), _around(haystack, match.start()))
    for match in EMAIL_RE.finditer(haystack):
        add(match.group(0), _around(haystack, match.start()))
    for match in _OBFUSCATED_RE.finditer(haystack):
        add(
            f"{match.group(1)}@{match.group(2)}.{match.group(3)}",
            _around(haystack, match.start()),
        )
    return list(found.values())


#: Local parts that are machinery, not a person or the hiring function.  A
#: stored posting and an ATS board page both carry the platform's own sender
#: addresses, and returning ``noreply@`` as the employer's contact would be the
#: backup stage inventing a contact out of boilerplate.
_NOISE_LOCAL_PARTS = frozenset(
    {
        "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
        "postmaster", "sentry", "wixpress",
    }
)

#: Local parts that are a hash or an asset stem, not a mailbox.
_HASH_LOCAL_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


def addresses_in_text(
    text: str,
    *,
    source_url: str | None = None,
    method: str = METHOD_STORED_DOCUMENT,
    domain: str | None = None,
) -> list[FoundAddress]:
    """Addresses in raw text that was not a page (FR-303 backup sources).

    :func:`extract_addresses` reads a fetched page; this reads the text the
    corpus already holds - a vacancy description, an application target, a JSON
    payload - and the pages of an ATS board.  Same regexes, same de-duplication,
    plus the ``method`` the caller wants recorded and a filter for the image and
    machinery "addresses" that only appear in such text.
    """
    found: dict[str, FoundAddress] = {}
    for item in extract_addresses(
        text or "", source_url=source_url or "", method=method, domain=domain
    ):
        local = item.local_part
        if local in _NOISE_LOCAL_PARTS or _HASH_LOCAL_RE.fullmatch(local):
            continue
        found.setdefault(item.email, item)
    return list(found.values())


_NAME_NEAR_RE = re.compile(
    r"\b([A-Z][\w'\u2019\-]+(?:\s+(?:van|de|der|den|von|le|du)){0,3}\s+[A-Z][\w'\u2019\-]+)\b"
)


def _around(text: str, index: int, width: int = 120) -> str:
    start = max(0, index - width)
    return re.sub(r"\s+", " ", text[start : index + width]).strip()


def attach_names(found: Iterable[FoundAddress]) -> list[FoundAddress]:
    """Guess the person an address belongs to from the text around it.

    Only used as *evidence for the pattern*: a name read out of page furniture
    is never stored on the contact (FR-306), it only tells :func:`infer_pattern`
    which convention the local part fits.
    """
    out = []
    for item in found:
        if item.full_name is None and item.context:
            match = _NAME_NEAR_RE.search(item.context)
            if match:
                item.full_name = match.group(1).strip()
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Structured sources: JSON-LD, security.txt, sitemap (FR-303)
# ---------------------------------------------------------------------------
#
# The home page of a modern site is often a JavaScript shell.  Three sources
# carry an address even then, and each is a published statement rather than a
# guess: schema.org JSON-LD (`Organization.email`, `contactPoint.email`,
# `Person.email` on team pages), an RFC 9116 ``security.txt``, and the site's
# own sitemap, which names the contact and team pages a home page may not link.

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

#: The keys that carry a person's role, best first.  Used only to say what the
#: page called them; the contact row keeps the FR-306 minimum.
_ROLE_KEYS = ("jobTitle", "contactType", "description")


def jsonld_payloads(html: str) -> list[Any]:
    """Every parsed JSON-LD object on a page, flattening ``@graph`` blocks.

    Tolerant by design: a malformed block (a trailing comma, an HTML entity) is
    skipped rather than allowed to abort the harvest, because the other blocks
    on the page are still evidence.
    """
    out: list[Any] = []
    for match in _JSONLD_RE.finditer(html or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "@graph" in node:
                    for child in node.get("@graph") or []:
                        walk(child)
                out.append(node)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        walk(value)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(payload)
    return out


def _jsonld_role(node: dict[str, Any]) -> str:
    for key in _ROLE_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def jsonld_contacts(html: str, *, domain: str | None = None, source_url: str = "") -> list[FoundAddress]:
    """Addresses carried by schema.org JSON-LD, with any role the page stated."""
    found: dict[str, FoundAddress] = {}
    for node in jsonld_payloads(html):
        if not isinstance(node, dict):
            continue
        contexts: list[dict[str, Any]] = [node]
        for key in ("contactPoint", "employee", "founder", "author"):
            value = node.get(key)
            if isinstance(value, dict):
                contexts.append(value)
            elif isinstance(value, list):
                contexts.extend(v for v in value if isinstance(v, dict))
        for context in contexts:
            email = context.get("email")
            if not isinstance(email, str):
                continue
            email = email.strip().lstrip("mailto:").strip()
            if domain and not email.lower().endswith("@" + domain.lower()):
                continue
            if not _plausible(email):
                continue
            name = context.get("name") if isinstance(context.get("name"), str) else None
            role = _jsonld_role(context)
            item = FoundAddress(
                email=email.lower(),
                method=METHOD_JSONLD,
                source_url=source_url,
                context=role or (name or ""),
                full_name=name,
                confidence=METHOD_CONFIDENCE[METHOD_JSONLD],
            )
            if email.lower() not in found or role:
                found[email.lower()] = item
    return list(found.values())


def security_txt_addresses(text: str, *, domain: str | None = None,
                           source_url: str = "") -> list[FoundAddress]:
    """The ``Contact:`` fields of an RFC 9116 ``security.txt``.

    Usually ``security@`` or ``abuse@`` - not the hiring mailbox - so it is a
    last-resort *domain* signal more than a contact.  It is still a published
    statement that reaches a monitored mailbox, which is why it is kept and
    labelled rather than discarded.
    """
    out: dict[str, FoundAddress] = {}
    for line in (text or "").splitlines():
        if not line.lower().startswith("contact:"):
            continue
        value = line.split(":", 1)[1].strip()
        for address in extract_addresses(value, source_url=source_url,
                                         method=METHOD_SECURITY_TXT, domain=domain):
            out.setdefault(address.email, address)
    return list(out.values())


def sitemap_locations(text: str) -> list[str]:
    """Every ``<loc>`` in a sitemap or sitemap index, in document order."""
    return [m.group(1).strip() for m in re.finditer(r"<loc>\s*(.*?)\s*</loc>", text or "",
                                                    re.IGNORECASE | re.DOTALL)]


def is_contact_url(url: str, anchor: str = "") -> bool:
    """Whether a URL is worth a fetch for an address (FR-303 page selection)."""
    haystack = f"{url} {anchor}".lower()
    return any(token in haystack for token in CONTACT_LINK_TOKENS)


async def _sitemap_page_urls(
    domain: str, client: EgressClient, *, limit: int = 20
) -> list[str]:
    """Contact-intent URLs named by the site's sitemap, cheapest first.

    Reads the ``Sitemap:`` lines in robots.txt when present (the only place a
    sitemap is *declared*), then ``/sitemap.xml``, following a sitemap index one
    level deep.  A sitemap is the site telling us where its pages are, which is
    both politer and more complete than guessing paths.
    """
    candidates: list[str] = []
    roots: list[str] = []
    robots = None
    try:
        robots = await client.fetch(f"https://{domain}/robots.txt")
    except RobotsDisallowed:
        robots = None
    except Exception:  # noqa: BLE001 - robots is optional here
        robots = None
    if robots is not None and robots.ok:
        for line in robots.text.splitlines():
            if line.lower().startswith("sitemap:"):
                roots.append(line.split(":", 1)[1].strip())
    if not roots:
        roots = [f"https://{domain}/sitemap.xml"]

    seen: set[str] = set()
    for root in roots[:3]:
        try:
            page = await client.fetch(root)
        except RobotsDisallowed:
            continue
        except Exception:  # noqa: BLE001
            continue
        if page is None or not page.ok:
            continue
        locations = sitemap_locations(page.text)
        # A sitemap index points at other sitemaps; follow one level.
        child = [u for u in locations if u.lower().endswith(".xml")][:3]
        for url in child:
            if url in seen:
                continue
            seen.add(url)
            try:
                sub = await client.fetch(url)
            except RobotsDisallowed:
                continue
            except Exception:  # noqa: BLE001
                continue
            if sub is not None and sub.ok:
                locations.extend(sitemap_locations(sub.text))
        candidates.extend(u for u in locations if not u.lower().endswith(".xml"))
    ordered = [u for u in candidates if is_contact_url(u)]
    return ordered[:limit]


# ---------------------------------------------------------------------------
# Pattern inference (FR-303)
# ---------------------------------------------------------------------------


@dataclass
class PatternInference:
    """What the observed addresses on a domain say about its convention."""

    domain: str
    pattern: str | None
    confidence: float
    sample_count: int
    supporting: int
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    local_parts: list[str] = field(default_factory=list)
    source: str = METHOD_WEBSITE

    def as_row(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern or "",
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "supporting": self.supporting,
            "alternatives": self.alternatives,
            "local_parts": self.local_parts[:50],
            "source": self.source,
        }


def _confidence_for(supporting: int, samples: int) -> float:
    """Agreement, damped for small samples.

    One matching address is weak evidence and must not produce a 1.0 guess that
    later reads as a verified address; four consistent ones are about as good as
    an inference gets.  The curve is ``share * n / (n + 1)`` capped at 0.8, so
    an inferred address never outranks one the company published (0.85).
    """
    if samples <= 0 or supporting <= 0:
        return 0.0
    share = supporting / samples
    damping = samples / (samples + 1.0)
    return round(min(0.8, share * damping), 3)


def infer_pattern(
    domain: str,
    observations: Sequence[tuple[str, str]],
    *,
    source: str = METHOD_WEBSITE,
) -> PatternInference:
    """Infer the local-part convention of one domain (FR-303).

    ``observations`` is ``(full_name, email)`` pairs seen on that domain.  Each
    pair votes for every pattern that reproduces its local part - "j.smith"
    votes for both ``f.last`` and, for a Jan Smith, nothing else - and the
    pattern with the most votes wins, ties broken by how common the convention
    is.  Role mailboxes are ignored: ``info@`` fits no naming rule and would
    otherwise drown the evidence.
    """
    domain = domain.lower()
    votes: Counter[str] = Counter()
    considered = 0
    seen_locals: list[str] = []

    for full_name, email in observations:
        if "@" not in (email or ""):
            continue
        local, _, addr_domain = email.lower().partition("@")
        if addr_domain != domain:
            continue
        seen_locals.append(local)
        parts = split_name(full_name or "")
        if parts is None:
            continue
        considered += 1
        matched = {
            pattern
            for pattern in PATTERN_ORDER
            for particle in (False, True)
            if render_pattern(pattern, parts, particle=particle) == local
        }
        for pattern in matched:
            votes[pattern] += 1

    if not votes:
        return PatternInference(
            domain=domain,
            pattern=None,
            confidence=0.0,
            sample_count=considered,
            supporting=0,
            local_parts=seen_locals,
            source=source,
        )

    best = max(votes.items(), key=lambda kv: (kv[1], -PATTERN_ORDER.index(kv[0])))
    alternatives = [
        {"pattern": p, "support": c}
        for p, c in sorted(votes.items(), key=lambda kv: -kv[1])
        if p != best[0]
    ]
    return PatternInference(
        domain=domain,
        pattern=best[0],
        confidence=_confidence_for(best[1], considered),
        sample_count=considered,
        supporting=best[1],
        alternatives=alternatives,
        local_parts=seen_locals,
        source=source,
    )


def learn_domain_pattern(
    domain: str,
    observations: Sequence[tuple[str, str]] | None = None,
    *,
    source: str = METHOD_WEBSITE,
) -> PatternInference:
    """Infer and store the pattern for a domain, reusing what is already known.

    The stored addresses of that domain are always part of the evidence, so the
    inference gets stronger as more contacts of the same employer are found.
    """
    domain = domain.lower()
    pairs: list[tuple[str, str]] = list(observations or [])
    for row in repo.known_addresses_on_domain(domain):
        if row.get("full_name") and row.get("email"):
            pairs.append((row["full_name"], row["email"]))

    # De-duplicate on the address; the same person may appear twice.
    unique: dict[str, tuple[str, str]] = {}
    for name, email in pairs:
        unique.setdefault(email.lower(), (name, email.lower()))

    inference = infer_pattern(domain, list(unique.values()), source=source)
    if inference.pattern:
        repo.save_pattern(domain, inference.as_row())
    return inference


def known_pattern(domain: str) -> PatternInference | None:
    row = repo.get_pattern(domain)
    if not row or not row.get("pattern"):
        return None
    return PatternInference(
        domain=domain.lower(),
        pattern=row["pattern"],
        confidence=float(row.get("confidence") or 0.0),
        sample_count=int(row.get("sample_count") or 0),
        supporting=int(row.get("supporting") or 0),
        alternatives=row.get("alternatives") or [],
        local_parts=row.get("local_parts") or [],
        source=row.get("source") or METHOD_WEBSITE,
    )


def candidates_for_person(
    full_name: str, domain: str, *, inference: PatternInference | None = None, limit: int = 4
) -> list[FoundAddress]:
    """Ranked guesses for one person on one domain (FR-303 pattern inference).

    The domain's own convention comes first; the common conventions follow as
    weaker guesses so FR-304 validation has something to test when the domain
    has never been seen before.
    """
    parts = split_name(full_name)
    if parts is None or not domain:
        return []
    inference = inference or known_pattern(domain)

    ordered: list[tuple[str, float]] = []
    if inference and inference.pattern:
        ordered.append((inference.pattern, max(0.3, inference.confidence)))
        for alt in inference.alternatives[:2]:
            ordered.append((str(alt.get("pattern")), max(0.2, inference.confidence * 0.5)))
    for pattern in ("first.last", "f.last", "firstlast", "first"):
        ordered.append((pattern, 0.25))

    out: dict[str, FoundAddress] = {}
    for pattern, confidence in ordered:
        for particle in (False, True):
            local = render_pattern(pattern, parts, particle=particle)
            if not local:
                continue
            email = f"{local}@{domain.lower()}"
            if email in out:
                continue
            out[email] = FoundAddress(
                email=email,
                method=METHOD_PATTERN,
                source_url="",
                full_name=full_name,
                confidence=round(confidence, 3),
                pattern=pattern,
            )
            if len(out) >= limit:
                return list(out.values())
    return list(out.values())


def generic_candidates(domain: str, *, careers_url: str | None = None) -> list[FoundAddress]:
    """The generic careers mailbox FR-301 falls back to."""
    if not domain:
        return []
    domain = domain.lower()
    source = careers_url or f"https://{domain}"
    out = []
    for index, local in enumerate(CAREERS_LOCAL_PARTS[:6]):
        out.append(
            FoundAddress(
                email=f"{local}@{domain}",
                method=METHOD_PATTERN,
                source_url=source,
                confidence=round(0.4 - index * 0.03, 3),
            )
        )
    for local in FALLBACK_LOCAL_PARTS[:2]:
        out.append(
            FoundAddress(
                email=f"{local}@{domain}",
                method=METHOD_PATTERN,
                source_url=source,
                confidence=0.2,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Collecting addresses off the company's own site (FR-303, CR-402)
# ---------------------------------------------------------------------------


def _classify_page(url: str) -> str:
    kind, _ = crawler.classify_url(url)
    if kind == "news" or any(token in url.lower() for token in ("press", "pers", "media")):
        return METHOD_PRESS
    return METHOD_WEBSITE


async def collect_from_site(
    domain: str,
    *,
    careers_url: str | None = None,
    egress: EgressClient | None = None,
    max_pages: int = 12,
    diagnostics: dict[str, Any] | None = None,
) -> list[FoundAddress]:
    """Read the pages of one company site that can carry an address (FR-303).

    Four sources feed this, in the order they are trusted: the home page and the
    pages it links to; the site's own sitemap, which names contact and team pages
    a JavaScript shell does not link; schema.org JSON-LD; and ``security.txt``.
    Robots.txt and the per-domain pacing remain the egress layer's business
    (FR-182); a disallowed page is skipped, never worked around.

    A bot-protection interstitial is not a page: F5/TSPD, Cloudflare and
    Incapsula answer large corporate sites with a challenge that has no links and
    no addresses.  Treating that empty harvest as "no address here" is what made
    the website source look broken, so when it happens the fact is recorded on
    ``diagnostics`` for the caller to report rather than swallowed.
    """
    if not domain:
        return []
    domain = domain.lower().lstrip(".")
    base = f"https://{domain}"
    diag: dict[str, Any] = diagnostics if diagnostics is not None else {}
    diag.setdefault("pages_fetched", 0)
    diag.setdefault("challenge_pages", 0)
    diag["sources"] = list(diag.get("sources") or [])

    async def _read(client: EgressClient, url: str) -> tuple[str, str] | None:
        """``(html, url)`` for one page, or ``None`` when it cannot be read."""
        try:
            result = await client.fetch(url)
        except RobotsDisallowed:
            log.info("robots.txt disallows %s; skipped (CR-402)", url)
            return None
        except Exception as exc:  # noqa: BLE001 - one dead page must not stop the rest
            log.debug("Could not fetch %s: %s", url, exc)
            return None
        if not result.ok:
            return None
        diag["pages_fetched"] += 1
        if looks_like_challenge(result.text):
            diag["challenge_pages"] += 1
            return None
        return (result.text, str(url))

    async def _run(client: EgressClient) -> list[FoundAddress]:
        collected: dict[str, FoundAddress] = {}

        def absorb(html: str, url: str) -> None:
            text = crawler.extract_text(html, drop_chrome=False)
            method = _classify_page(url)
            for item in attach_names(
                extract_addresses(f"{html}\n{text}", source_url=url, method=method, domain=domain)
            ):
                current = collected.get(item.email)
                if current is None or item.confidence > current.confidence:
                    collected[item.email] = item
            for item in jsonld_contacts(html, domain=domain, source_url=url):
                current = collected.get(item.email)
                if current is None or item.confidence > current.confidence:
                    collected[item.email] = item

        home = await _read(client, base)
        if home is not None:
            absorb(*home)
        else:
            log.info("No readable home page for %s; trying sitemap and structured sources", domain)

        # The sitemap is how a shell site advertises its contact and team pages.
        try:
            sitemap_pages = await _sitemap_page_urls(domain, client, limit=max_pages)
        except Exception as exc:  # noqa: BLE001 - optional source
            log.debug("Sitemap read failed for %s: %s", domain, exc)
            sitemap_pages = []

        # security.txt is a published Contact field, usually security@ or abuse@.
        security = await _read(client, f"{base}/.well-known/security.txt")
        if security is not None:
            for item in security_txt_addresses(security[0], domain=domain, source_url=security[1]):
                collected.setdefault(item.email, item)

        # Follow the company's own links, then the sitemap, then conventional
        # paths.  The home page decides first; the rest is fallback.
        targets: dict[str, float] = {}
        if home is not None:
            for url, anchor in crawler.extract_links(home[0], base):
                if not crawler.same_site(url, crawler.registrable_domain(domain)):
                    continue
                kind, _ = crawler.classify_url(url, anchor)
                if kind in ("locations", "careers", "news") or is_contact_url(url, anchor):
                    targets[url] = 1.0 if kind == "locations" else 0.7
        for url in sitemap_pages:
            targets.setdefault(url, 0.6)
        if careers_url:
            targets.setdefault(careers_url, 0.9)
        for path in CONTACT_PATHS:
            targets.setdefault(base + path, 0.4)

        ordered = sorted(targets, key=lambda u: (-targets[u], len(u)))[: max(0, max_pages)]
        for url in ordered:
            page = await _read(client, url)
            if page is not None:
                absorb(*page)
        diag["sources"] = sorted({a.method for a in collected.values()})
        return list(collected.values())

    if egress is not None:
        return await _run(egress)
    async with EgressClient() as client:
        return await _run(client)


def addresses_from_pages(pages: Iterable[Any], domain: str | None = None) -> list[FoundAddress]:
    """Harvest a crawl that another stage already paid for (FR-182 reuse).

    Accepts ``crawler.CrawledPage`` objects or plain ``{"url", "text"}`` dicts.
    """
    collected: dict[str, FoundAddress] = {}
    for page in pages:
        url = getattr(page, "url", None) or (page.get("url") if isinstance(page, dict) else "")
        text = getattr(page, "text", None) or (page.get("text") if isinstance(page, dict) else "")
        if not text:
            continue
        method = _classify_page(url or "")
        for item in attach_names(
            extract_addresses(text, source_url=url or "", method=method, domain=domain)
        ):
            current = collected.get(item.email)
            if current is None or item.confidence > current.confidence:
                collected[item.email] = item
    return list(collected.values())


# ---------------------------------------------------------------------------
# Third-party lookup services (FR-303, OQ-05)
# ---------------------------------------------------------------------------


class LookupServiceDisabled(RuntimeError):
    """Raised when the third-party lookup is called while OQ-05 is unresolved."""


def lookup_service_config() -> dict[str, Any]:
    """What an administrator has configured for the FR-303 lookup service."""
    return {
        "enabled": bool(kb.get_setting(SETTING_LOOKUP_ENABLED, False)),
        "url": kb.get_setting(SETTING_LOOKUP_URL, "") or "",
        "api_key": kb.get_setting(SETTING_LOOKUP_KEY, "") or "",
        "name": kb.get_setting(SETTING_LOOKUP_NAME, "") or "",
    }


def lookup_service_enabled() -> bool:
    config = lookup_service_config()
    return bool(config["enabled"] and config["url"])


async def lookup_via_service(
    full_name: str,
    domain: str,
    *,
    egress: EgressClient | None = None,
) -> list[FoundAddress]:
    """Ask the configured third-party service for an address (FR-303, OQ-05).

    The request template is the shape every provider of this kind exposes -
    ``GET <url>?domain=&first_name=&last_name=`` with a bearer key - and the
    response is read defensively because the field names differ per provider.
    Nothing is sent until an administrator has both named a provider and
    switched the feature on, because OQ-05 has not established which services
    are acceptable under the data-protection terms.
    """
    config = lookup_service_config()
    if not config["enabled"] or not config["url"]:
        raise LookupServiceDisabled(
            "Third-party contact lookup is disabled (OQ-05 unresolved). An administrator "
            f"must set {SETTING_LOOKUP_URL} and {SETTING_LOOKUP_ENABLED} before it is used."
        )
    parts = split_name(full_name)
    if parts is None or not domain:
        return []

    params = {
        "domain": domain.lower(),
        "first_name": parts.first,
        "last_name": parts.last,
        "full_name": full_name,
    }
    headers = {"Accept": "application/json"}
    if config["api_key"]:
        headers["Authorization"] = f"Bearer {config['api_key']}"

    async def _run(client: EgressClient) -> list[FoundAddress]:
        result = await client.fetch(
            config["url"], params=params, headers=headers, use_cache=False, access_method="api"
        )
        if not result.ok:
            log.info("Lookup service returned %s for %s", result.status_code, domain)
            return []
        import json  # noqa: PLC0415 - only needed on this path

        try:
            payload = json.loads(result.text)
        except ValueError:
            log.warning("Lookup service returned a non-JSON body")
            return []
        return _parse_lookup_payload(payload, domain, full_name, config["name"])

    if egress is not None:
        return await _run(egress)
    async with EgressClient() as client:
        return await _run(client)


def _parse_lookup_payload(
    payload: Any, domain: str, full_name: str, provider: str
) -> list[FoundAddress]:
    """Read an address and a score out of whatever shape the provider returns."""
    node = payload.get("data", payload) if isinstance(payload, dict) else payload
    candidates: list[dict] = []
    if isinstance(node, dict):
        if node.get("email"):
            candidates.append(node)
        for key in ("emails", "results", "candidates"):
            value = node.get(key)
            if isinstance(value, list):
                candidates.extend(v for v in value if isinstance(v, dict))
    elif isinstance(node, list):
        candidates.extend(v for v in node if isinstance(v, dict))

    out: list[FoundAddress] = []
    for candidate in candidates:
        email = str(candidate.get("email") or candidate.get("value") or "").strip().lower()
        if not _plausible(email) or not email.endswith("@" + domain.lower()):
            continue
        raw_score = candidate.get("confidence", candidate.get("score", 70))
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = 70.0
        out.append(
            FoundAddress(
                email=email,
                method=METHOD_LOOKUP,
                source_url=provider or "lookup_service",
                full_name=full_name,
                confidence=round(min(0.9, score / 100 if score > 1 else score), 3),
            )
        )
    return out


def domain_of(url_or_domain: str | None) -> str:
    """The bare registrable host of a company, from a URL or a domain."""
    if not url_or_domain:
        return ""
    value = url_or_domain.strip()
    if "://" in value:
        value = urlparse(value).netloc
    value = value.split("/")[0].split("@")[-1].lower()
    if value.startswith("www."):
        value = value[4:]
    return value


# ---------------------------------------------------------------------------
# Search-engine discovery (FR-303)
# ---------------------------------------------------------------------------
#
# A search engine has already read the sites that answer our fetches with a
# bot-protection challenge, and its index includes the pages a small crawl does
# not reach.  Querying it turns "this company is unreachable" into "here is the
# page that carries the address".  It is an API, never a scraped results page:
# Bing and Startpage disallow ``/search`` in robots.txt, and IR-101 means a
# refusal is recorded rather than worked around.  Disabled until an
# administrator names a provider and a key - the same posture as OQ-05.


class SearchDisabled(RuntimeError):
    """Raised when the search source is called while it is switched off."""


_SEARCH_HEADERS = {
    "brave": "X-Subscription-Token",
    "bing": "Ocp-Apim-Subscription-Key",
}

_SEARCH_ENDPOINTS = {
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "bing": "https://api.bing.microsoft.com/v7.0/search",
}


def search_config() -> dict[str, Any]:
    """The administrator's search-provider configuration (FR-303)."""
    return {
        "enabled": bool(kb.get_setting(SETTING_SEARCH_ENABLED, False)),
        "provider": (kb.get_setting(SETTING_SEARCH_PROVIDER, "") or "").strip().lower(),
        "key": (kb.get_setting(SETTING_SEARCH_KEY, "") or "").strip(),
        "url": (kb.get_setting(SETTING_SEARCH_URL, "") or "").strip(),
    }


def search_enabled() -> bool:
    config = search_config()
    return bool(config["enabled"] and config["provider"] and config["key"])


def search_results(payload: Any, provider: str) -> list[tuple[str, str]]:
    """``(url, snippet)`` pairs from a provider's search response.

    Read defensively and by provider: the three supported shapes differ, and a
    provider that changes its field names degrades to no results rather than an
    exception inside a contacts pass.
    """
    items: list[Any] = []
    if not isinstance(payload, dict):
        return []
    if provider == "brave":
        web = payload.get("web") or {}
        items = web.get("results") if isinstance(web, dict) else []
    elif provider == "bing":
        pages = payload.get("webPages") or {}
        items = pages.get("value") if isinstance(pages, dict) else []
    elif provider == "google_cse":
        items = payload.get("items")
    if not isinstance(items, list):
        return []
    out: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link") or ""
        snippet = " ".join(
            str(item.get(key) or "") for key in ("description", "snippet", "title", "name")
        ).strip()
        if url:
            out.append((str(url), snippet))
    return out


async def search_for_contacts(
    company_name: str,
    domain: str,
    *,
    egress: EgressClient | None = None,
    max_results: int = 8,
) -> list[FoundAddress]:
    """Find addresses via the configured search API and the pages it names.

    Two queries do the work: the address pattern on the domain, and the company
    plus the hiring vocabulary.  Snippets are read for addresses directly, and
    the pages they cite are fetched through the ordinary egress path (robots and
    pacing apply) so an address in the page body is found too.
    """
    config = search_config()
    if not config["enabled"] or not config["provider"]:
        raise SearchDisabled(
            "Web-search contact discovery is disabled. An administrator must set "
            f"{SETTING_SEARCH_PROVIDER} and {SETTING_SEARCH_KEY} and switch on "
            f"{SETTING_SEARCH_ENABLED} before it is used. Bing and Startpage disallow "
            "their HTML search endpoint in robots.txt, so this is an API only (IR-101)."
        )
    provider = config["provider"]
    endpoint = config["url"] or _SEARCH_ENDPOINTS.get(provider, "")
    if not endpoint:
        return []

    queries = []
    if domain:
        queries.append(f'"@{domain}"')
    if company_name:
        queries.append(
            f'"{company_name}" (recruitment OR careers OR "talent acquisition" OR HR) email'
        )
    if not queries:
        return []

    headers = {"Accept": "application/json"}
    header = _SEARCH_HEADERS.get(provider)
    if header and config["key"]:
        headers[header] = config["key"]

    async def _run(client: EgressClient) -> list[FoundAddress]:
        found: dict[str, FoundAddress] = {}
        page_urls: list[str] = []
        for query in queries[:2]:
            params: dict[str, Any] = {"q": query, "count": max_results}
            if provider == "google_cse":
                params = {"q": query, "num": max_results}
            try:
                result = await client.fetch(
                    endpoint, params=params, headers=headers,
                    use_cache=False, access_method="api",
                )
            except Exception as exc:  # noqa: BLE001 - a search failure must not abort a pass
                log.info("Search provider %s failed: %s", provider, exc)
                continue
            if not result.ok:
                log.info("Search provider %s returned %s", provider, result.status_code)
                continue
            try:
                payload = json.loads(result.text)
            except ValueError:
                continue
            for url, snippet in search_results(payload, provider):
                for item in extract_addresses(
                    snippet, source_url=url, method=METHOD_SEARCH, domain=domain
                ):
                    found.setdefault(item.email, item)
                if domain and is_contact_url(url):
                    page_urls.append(url)

        for url in page_urls[:max_results]:
            try:
                page = await client.fetch(url)
            except Exception:  # noqa: BLE001
                continue
            if not page.ok or looks_like_challenge(page.text):
                continue
            text = crawler.extract_text(page.text, drop_chrome=False)
            for item in extract_addresses(
                f"{page.text}\n{text}", source_url=url, method=METHOD_SEARCH, domain=domain
            ):
                found.setdefault(item.email, item)
        return list(found.values())

    if egress is not None:
        return await _run(egress)
    async with EgressClient() as client:
        return await _run(client)
