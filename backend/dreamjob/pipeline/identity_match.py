"""Identity matching for online findings (FR-123, FR-124, RK-02).

RK-02 is the risk this module exists to hold down: a homonym's blog post
becoming a line on the job seeker's CV.  The defence is that a name match on
its own can never reach ``confirmed``.  Every finding is scored on several
independent signals, each one recorded separately in
``enrichment_finding.identity_signals`` so the job seeker sees *why* a page was
attributed to them and can overrule it (FR-124).

Signals and weights
-------------------

===================== ======  ==================================================
signal                weight  what it measures
===================== ======  ==================================================
name_match             0.30   the page names the job seeker, in some variant
employer_overlap       0.22   employers from the profile appear on the page
cross_link             0.16   the page *is* or *links to* a URL the job seeker
                              declared themselves
location_match         0.12   cities/countries from the profile appear
timeline_consistency   0.10   years on the page fall inside the career span
photo_similarity       0.10   perceptual hash distance to the profile photo
===================== ======  ==================================================

``conflicting_evidence`` is a negative signal: it fires when the page carries
several profile-shaped anchors (employers, places, years) and *none* of them
overlap with the job seeker.  That is what a homonym looks like.

Classification thresholds
-------------------------

``confirmed``  score >= 0.72 **and** name_match >= 0.5 **and** at least two
               corroborating non-name signals scoring >= 0.4 each **and** no
               conflicting evidence.  A page hosted on a domain the job seeker
               listed themselves is confirmed outright.
``probable``   score >= 0.40 and name_match >= 0.5.
``doubtful``   everything else, including any page where the name itself is
               only a weak match.

Only ``confirmed`` is merged automatically (FR-124); the rest is queued for the
job seeker with the evidence attached.
"""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

CONFIRMED = "confirmed"
PROBABLE = "probable"
DOUBTFUL = "doubtful"

SIGNAL_WEIGHTS: dict[str, float] = {
    "name_match": 0.30,
    "employer_overlap": 0.22,
    "cross_link": 0.16,
    "location_match": 0.12,
    "timeline_consistency": 0.10,
    "photo_similarity": 0.10,
}

CONFIRMED_SCORE = 0.72
PROBABLE_SCORE = 0.40
MIN_NAME_SCORE = 0.50
MIN_CORROBORATING = 2
CORROBORATION_FLOOR = 0.40
CONFLICT_PENALTY = 0.18

# Dutch/French/German/Spanish name particles.  They are part of the surname but
# are dropped or moved around by half the sites on the web, so a variant set has
# to cover both spellings.
_PARTICLES = {
    "van", "der", "den", "de", "ter", "te", "het", "op", "aan", "vd",
    "von", "zu", "du", "des", "la", "le", "el", "di", "da", "dos", "del", "mac", "mc",
}
_LEGAL_SUFFIXES = {
    "nv", "sa", "bv", "bvba", "srl", "sprl", "cvba", "vzw", "asbl", "ltd", "limited",
    "plc", "inc", "incorporated", "llc", "gmbh", "ag", "ab", "oy", "as", "kk",
    "corp", "corporation", "company", "co", "group", "holding", "holdings", "sas",
}
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Multi-tenant hosts.  A declared link to github.com/<handle> says the job
# seeker owns that *account*, not the whole of github.com - treating the domain
# as theirs would confirm a stranger's profile page (RK-02).
SHARED_PLATFORMS = frozenset(
    {
        "github.com", "github.io", "gitlab.com", "bitbucket.org", "substack.com",
        "medium.com", "dev.to", "hashnode.com", "wordpress.com", "blogspot.com",
        "notion.site", "wixsite.com", "squarespace.com", "sites.google.com",
        "about.me", "linktr.ee", "youtube.com", "vimeo.com", "speakerdeck.com",
        "slideshare.net", "researchgate.net", "orcid.org", "stackoverflow.com",
        "behance.net", "dribbble.com", "gitbook.io", "netlify.app", "vercel.app",
        "pages.dev", "readthedocs.io",
    }
)


def is_shared_platform(domain: str) -> bool:
    return any(domain == p or domain.endswith("." + p) for p in SHARED_PLATFORMS)


def canonical_url(url: str) -> str:
    """Scheme-less, www-less, slash-trimmed form, for prefix comparison."""
    text = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.startswith("www."):
        text = text[4:]
    return text.rstrip("/")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def fold(text: str) -> str:
    """Lowercase, strip diacritics, collapse whitespace.

    'Stéphane van der Aa' and 'STEPHANE VAN DER AA' have to compare equal;
    accent handling is not optional for Belgian and French names.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.lower()).strip()


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(fold(text))


def name_variants(full_name: str) -> set[str]:
    """Spellings of one person's name that a web page might plausibly use."""
    parts = [p for p in _tokens(full_name) if p]
    if not parts:
        return set()

    given = [p for p in parts if p not in _PARTICLES]
    if not given:
        given = parts
    first = given[0]
    surname_parts = parts[parts.index(first) + 1 :] if first in parts else parts[1:]
    surname_core = [p for p in surname_parts if p not in _PARTICLES] or surname_parts
    surname_full = " ".join(surname_parts)
    surname_bare = " ".join(surname_core)

    out: set[str] = {" ".join(parts)}
    if surname_full:
        out.add(f"{first} {surname_full}")
        out.add(f"{surname_full}, {first}")
        out.add(f"{surname_full} {first}")
        out.add(f"{first[0]}. {surname_full}")
        out.add(f"{first[0]} {surname_full}")
    if len(surname_bare) >= 3 and surname_bare != surname_full:
        out.add(f"{first} {surname_bare}")
        out.add(f"{surname_bare}, {first}")
        out.add(f"{first[0]}. {surname_bare}")
    if len(given) > 2:  # middle names are dropped as often as they are kept
        out.add(f"{given[0]} {given[-1]}")
    return {v for v in out if len(v) > 3}


def handle_candidates(full_name: str) -> list[str]:
    """Usernames a person with this name is likely to have picked.

    Used both to recognise a handle on a page and to probe likely profile URLs
    when web search is unavailable.
    """
    all_parts = _tokens(full_name)
    parts = [p for p in all_parts if p and p not in _PARTICLES]
    if not parts:
        return []
    first, last = parts[0], parts[-1]
    surname_parts = all_parts[all_parts.index(first) + 1 :] if first in all_parts else all_parts[1:]
    out = [
        f"{first}{last}",
        f"{first}.{last}",
        f"{first}-{last}",
        f"{first[0]}{last}",
        f"{first}{last[0]}",
        f"{first[:4]}{last[:3]}",
    ]
    if surname_parts:
        # "Stephane van der Aa" -> "stepvda": a compound surname is routinely
        # abbreviated to its initials in a handle.
        initials = "".join(p[0] for p in surname_parts)
        out += [f"{first[:4]}{initials}", f"{first[0]}{initials}", initials]
    seen: set[str] = set()
    return [h for h in out if len(h) >= 4 and not (h in seen or seen.add(h))]


def normalise_employer(name: str) -> str:
    toks = [t for t in _tokens(name) if t not in _LEGAL_SUFFIXES]
    return " ".join(toks)


def registrable_domain(url: str) -> str:
    host = (urlparse(url).netloc or url).lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


# ---------------------------------------------------------------------------
# Profile anchors - the identity the findings are matched against
# ---------------------------------------------------------------------------


@dataclass
class ProfileAnchors:
    """Everything known about who the job seeker is (FR-122, FR-123).

    Built by walking the profile sections by key name rather than by a fixed
    path: the LinkedIn export defines the base schema (FR-102) but the CV merge
    and manual edits both reshape it, so a tolerant walk survives changes that a
    fixed path would not.
    """

    display_name: str = ""
    variants: set[str] = field(default_factory=set)
    handles: list[str] = field(default_factory=list)
    employers: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    schools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    declared_urls: list[str] = field(default_factory=list)
    years: set[int] = field(default_factory=set)
    photo_path: str | None = None
    photo_hash: int | None = None

    @property
    def declared_domains(self) -> set[str]:
        return {registrable_domain(u) for u in self.declared_urls if u}

    @property
    def declared_prefixes(self) -> set[str]:
        return {canonical_url(u) for u in self.declared_urls if u}

    def is_self_declared(self, url: str) -> bool:
        """Is this page the job seeker's own, by their own account?

        True when the URL sits under a link the job seeker declared, or on a
        domain they declared that is not a multi-tenant platform.  The second
        clause is what makes ``stepvda.net/anything`` theirs while leaving
        ``github.com/someone-else`` a stranger's.
        """
        target = canonical_url(url)
        for prefix in self.declared_prefixes:
            if target == prefix or target.startswith(prefix + "/") or target.startswith(
                prefix + "?"
            ):
                return True
        domain = registrable_domain(url)
        return bool(domain) and domain in self.declared_domains and not is_shared_platform(domain)

    def linked_declared(self, haystack: str) -> list[str]:
        """Declared links that the page points back to."""
        return sorted(
            {p for p in self.declared_prefixes if len(p) > 6 and p in haystack}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "variants": sorted(self.variants),
            "handles": self.handles,
            "employers": self.employers,
            "locations": self.locations,
            "titles": self.titles,
            "schools": self.schools,
            "declared_urls": self.declared_urls,
            "years": sorted(self.years),
        }


_EMPLOYER_KEYS = ("company", "companyname", "employer", "organisation", "organization", "org")
_TITLE_KEYS = ("title", "position", "role", "jobtitle", "headline")
_LOCATION_KEYS = ("location", "city", "country", "region", "place", "geo")
_SCHOOL_KEYS = ("school", "institution", "university", "college")
_URL_KEYS = ("url", "website", "link", "profile", "homepage", "site", "handle", "username")
_SKILL_KEYS = ("skill", "skills", "competency", "expertise")
_DATE_KEYS = ("start", "end", "date", "year", "period", "from", "to", "graduated")
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


def _push(bucket: list[str], value: object, limit: int = 40) -> None:
    if not isinstance(value, str):
        return
    value = value.strip()
    if value and value.lower() not in {b.lower() for b in bucket} and len(bucket) < limit:
        bucket.append(value)


def build_anchors(
    sections: Any,
    display_name: str = "",
    *,
    photo_path: str | None = None,
    extra_urls: list[str] | None = None,
) -> ProfileAnchors:
    """Collect identity anchors from a profile-version ``sections`` blob."""
    anchors = ProfileAnchors(display_name=display_name, photo_path=photo_path)

    def walk(node: Any, key: str = "") -> None:
        k = key.lower().replace("_", "").replace(" ", "")
        if isinstance(node, dict):
            for sub_key, sub in node.items():
                walk(sub, str(sub_key))
            return
        if isinstance(node, list):
            for item in node:
                walk(item, key)
            return
        if not isinstance(node, str) or not node.strip():
            return

        if any(t in k for t in _EMPLOYER_KEYS):
            _push(anchors.employers, node)
        elif any(t in k for t in _TITLE_KEYS):
            _push(anchors.titles, node)
        elif any(t in k for t in _LOCATION_KEYS):
            _push(anchors.locations, node)
        elif any(t in k for t in _SCHOOL_KEYS):
            _push(anchors.schools, node)
        elif any(t in k for t in _SKILL_KEYS):
            _push(anchors.skills, node, limit=60)
        elif any(t in k for t in _URL_KEYS):
            for url in _URL_RE.findall(node) or ([node] if "." in node and " " not in node else []):
                _push(anchors.declared_urls, url)
        if any(t in k for t in _DATE_KEYS):
            anchors.years.update(int(y) for y in _YEAR_RE.findall(node))
        # URLs are worth harvesting wherever they appear, including free text.
        for url in _URL_RE.findall(node):
            _push(anchors.declared_urls, url, limit=60)

    walk(sections)

    if not display_name and isinstance(sections, dict):
        for key in ("name", "full_name", "display_name", "fullName"):
            if isinstance(sections.get(key), str):
                anchors.display_name = sections[key]
                break

    for url in extra_urls or []:
        _push(anchors.declared_urls, url, limit=60)

    anchors.variants = name_variants(anchors.display_name)
    anchors.handles = handle_candidates(anchors.display_name)
    # Handles the job seeker actually uses beat guessed ones - but only the
    # part of a declared URL that identifies the person.  On a platform that is
    # the account name in the path or the subdomain label; on a domain of their
    # own it is the domain's own label, not an arbitrary subdomain.
    for url in reversed(anchors.declared_urls):
        for candidate in _handles_from_url(url):
            if not candidate:
                continue
            # A guessed handle that turns out to be real is promoted, not
            # duplicated: the guesses are only there to fill the tail.
            if candidate in anchors.handles:
                anchors.handles.remove(candidate)
            anchors.handles.insert(0, candidate)
    return anchors


_GENERIC_LABELS = frozenset(
    {"www", "web", "blog", "news", "docs", "doc", "app", "api", "mail", "home",
     "site", "about", "info", "one", "two", "my", "me", "cdn", "static"}
)


def _handles_from_url(url: str) -> list[str]:
    parsed = urlparse(url if "//" in url else f"https://{url}")
    domain = registrable_domain(url)
    labels = domain.split(".")
    out: list[str] = []

    if is_shared_platform(domain):
        head = parsed.path.strip("/").split("/")[0].lstrip("@").lower()
        if head and head not in _GENERIC_LABELS:
            out.append(head)
        # "stepvda.substack.com" - the subdomain is the account.
        if len(labels) > 2 and labels[0] not in _GENERIC_LABELS:
            out.append(labels[0])
    elif len(labels) >= 2:
        # "stepvda.net" -> stepvda; "one.witysk.org" -> witysk, not "one".
        out.append(labels[-2])

    return [h for h in out if 3 <= len(h) <= 30]


# ---------------------------------------------------------------------------
# Photo similarity (FR-123) - perceptual hash, Pillow only
# ---------------------------------------------------------------------------


def perceptual_hash(image_bytes: bytes) -> int | None:
    """64-bit difference hash.

    A dHash survives re-encoding, rescaling and mild cropping - exactly what
    happens to a profile photo as it is copied between sites - while staying
    cheap enough to run on every candidate image.
    """
    try:
        from PIL import Image  # noqa: PLC0415 - optional at import time
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        log.warning("Pillow unavailable; photo similarity signal disabled")
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            small = img.convert("L").resize((9, 8), Image.LANCZOS)
            pixels = list(small.getdata())
    except Exception:  # noqa: BLE001 - a broken image is simply no signal
        return None

    if max(pixels) == min(pixels):
        # A flat image - a blank placeholder avatar, a spacer - hashes to the
        # same value as every other flat image.  That is not a match, it is an
        # absence of information, so it must not corroborate an identity.
        return None

    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | int(pixels[base + col] > pixels[base + col + 1])
    return bits


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def photo_similarity(reference: int | None, candidate: int | None) -> float:
    """0..1 similarity.  Below ~0.75 (16 differing bits) it is not the same photo."""
    if reference is None or candidate is None:
        return 0.0
    return max(0.0, 1.0 - hamming_distance(reference, candidate) / 64.0)


def hash_image_file(path: str) -> int | None:
    try:
        with open(path, "rb") as fh:
            return perceptual_hash(fh.read())
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    """One corroborating (or contradicting) observation, scored on its own."""

    name: str
    score: float
    weight: float
    detail: str
    evidence: list[str] = field(default_factory=list)

    @property
    def contribution(self) -> float:
        return self.score * self.weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.name,
            "score": round(self.score, 3),
            "weight": self.weight,
            "contribution": round(self.contribution, 4),
            "detail": self.detail,
            "evidence": self.evidence[:6],
        }


@dataclass
class IdentityAssessment:
    score: float
    classification: str
    rule: str
    signals: list[Signal]

    @property
    def auto_merge(self) -> bool:
        """FR-124: only 'confirmed' is merged without asking."""
        return self.classification == CONFIRMED

    def signal(self, name: str) -> Signal | None:
        return next((s for s in self.signals if s.name == name), None)

    def to_signals_json(self) -> dict[str, Any]:
        return {
            "signals": [s.to_dict() for s in self.signals],
            "score": round(self.score, 4),
            "classification": self.classification,
            "rule": self.rule,
            "thresholds": {
                "confirmed_score": CONFIRMED_SCORE,
                "probable_score": PROBABLE_SCORE,
                "min_name_score": MIN_NAME_SCORE,
                "min_corroborating_signals": MIN_CORROBORATING,
                "corroboration_floor": CORROBORATION_FLOOR,
            },
        }


def _name_signal(haystack: str, anchors: ProfileAnchors) -> Signal:
    hits: list[str] = []
    best = 0.0
    for variant in sorted(anchors.variants, key=len, reverse=True):
        if variant in haystack:
            hits.append(variant)
            # A full "first last" beats an "initial. last", which beats a surname.
            best = max(best, 1.0 if len(variant.split()) >= 2 and "." not in variant else 0.75)
    if not hits:
        for handle in anchors.handles[:6]:
            if re.search(rf"\b{re.escape(handle)}\b", haystack):
                hits.append(handle)
                best = max(best, 0.5)
    if not hits:
        parts = _tokens(anchors.display_name)
        surname = parts[-1] if parts else ""
        if surname and len(surname) > 3 and re.search(rf"\b{re.escape(surname)}\b", haystack):
            hits.append(surname)
            best = 0.25
    detail = (
        f"matched {hits[0]!r}" if hits else "the job seeker's name does not appear on the page"
    )
    return Signal("name_match", best, SIGNAL_WEIGHTS["name_match"], detail, hits)


def _employer_signal(haystack: str, anchors: ProfileAnchors) -> Signal:
    hits = []
    for employer in anchors.employers:
        norm = normalise_employer(employer)
        if len(norm) >= 4 and norm in haystack:
            hits.append(employer)
    score = 0.0 if not hits else (0.7 if len(hits) == 1 else 1.0)
    return Signal(
        "employer_overlap",
        score,
        SIGNAL_WEIGHTS["employer_overlap"],
        f"{len(hits)} employer(s) from the profile appear on the page",
        hits,
    )


def _location_signal(haystack: str, anchors: ProfileAnchors) -> Signal:
    hits = []
    for location in anchors.locations:
        for token in re.split(r"[,/;]", location):
            token = fold(token)
            if len(token) >= 4 and token in haystack and location not in hits:
                hits.append(location)
    score = 0.0 if not hits else (0.8 if len(hits) == 1 else 1.0)
    return Signal(
        "location_match",
        score,
        SIGNAL_WEIGHTS["location_match"],
        f"{len(hits)} profile location(s) appear on the page",
        hits,
    )


def _timeline_signal(text: str, anchors: ProfileAnchors) -> Signal:
    page_years = {int(y) for y in _YEAR_RE.findall(text)}
    if not page_years or not anchors.years:
        return Signal(
            "timeline_consistency", 0.0, SIGNAL_WEIGHTS["timeline_consistency"],
            "no comparable dates on the page", [],
        )
    lo, hi = min(anchors.years), max(anchors.years)
    inside = {y for y in page_years if lo - 1 <= y <= hi + 1}
    score = len(inside) / len(page_years)
    return Signal(
        "timeline_consistency",
        round(score, 3),
        SIGNAL_WEIGHTS["timeline_consistency"],
        f"{len(inside)}/{len(page_years)} dates fall inside the career span {lo}-{hi}",
        [str(y) for y in sorted(inside)],
    )


def _cross_link_signal(url: str, text: str, anchors: ProfileAnchors) -> Signal:
    if anchors.is_self_declared(url):
        return Signal(
            "cross_link", 1.0, SIGNAL_WEIGHTS["cross_link"],
            f"the page sits under {canonical_url(url)}, which the job seeker declared",
            [registrable_domain(url)],
        )
    linked = anchors.linked_declared(text)
    if linked:
        score = 0.8 if len(linked) > 1 else 0.6
        return Signal(
            "cross_link", score, SIGNAL_WEIGHTS["cross_link"],
            "the page links back to profiles the job seeker declared", linked,
        )
    return Signal("cross_link", 0.0, SIGNAL_WEIGHTS["cross_link"], "no cross-links found", [])


def _photo_signal(anchors: ProfileAnchors, candidate_hash: int | None) -> Signal:
    similarity = photo_similarity(anchors.photo_hash, candidate_hash)
    if anchors.photo_hash is None or candidate_hash is None:
        detail = "no comparable photo available"
        score = 0.0
    else:
        distance = hamming_distance(anchors.photo_hash, candidate_hash)
        detail = f"perceptual hash distance {distance}/64"
        # Below 0.75 similarity two portraits are simply two different portraits.
        score = similarity if similarity >= 0.75 else 0.0
    return Signal("photo_similarity", round(score, 3), SIGNAL_WEIGHTS["photo_similarity"], detail)


def _conflict_signal(haystack: str, positives: list[Signal]) -> Signal:
    """The homonym shape: profile-like anchors on the page, none of them ours."""
    corroborated = any(
        s.score >= CORROBORATION_FLOOR for s in positives if s.name != "name_match"
    )
    if corroborated:
        return Signal("conflicting_evidence", 0.0, 0.0, "no contradicting evidence", [])

    markers = len(_YEAR_RE.findall(haystack))
    profile_words = ("worked at", "joined", "based in", "graduated", "ceo of", "founder of")
    markers += sum(1 for w in profile_words if w in haystack)
    if markers >= 3:
        return Signal(
            "conflicting_evidence", 1.0, 0.0,
            "the page carries biographical detail but none of it matches this profile "
            "- a different person with the same name is the likelier reading",
            [],
        )
    return Signal("conflicting_evidence", 0.0, 0.0, "not enough detail to contradict", [])


def assess(
    *,
    url: str,
    title: str | None,
    text: str,
    anchors: ProfileAnchors,
    candidate_photo_hash: int | None = None,
) -> IdentityAssessment:
    """Score one finding on every signal and classify it (FR-123, RK-02)."""
    haystack = fold(f"{title or ''} {text} {url}")

    positives = [
        _name_signal(haystack, anchors),
        _employer_signal(haystack, anchors),
        _cross_link_signal(url, haystack, anchors),
        _location_signal(haystack, anchors),
        _timeline_signal(text, anchors),
        _photo_signal(anchors, candidate_photo_hash),
    ]
    conflict = _conflict_signal(haystack, positives)
    signals = [*positives, conflict]

    score = sum(s.contribution for s in positives)
    if conflict.score:
        score = max(0.0, score - CONFLICT_PENALTY)

    name = next(s for s in positives if s.name == "name_match")
    corroborating = [
        s for s in positives if s.name != "name_match" and s.score >= CORROBORATION_FLOOR
    ]
    self_hosted = anchors.is_self_declared(url)

    if self_hosted and name.score >= MIN_NAME_SCORE:
        classification = CONFIRMED
        rule = "the job seeker declared this page as their own"
    elif name.score < MIN_NAME_SCORE:
        classification = DOUBTFUL
        rule = f"name match {name.score:.2f} is below the {MIN_NAME_SCORE} floor"
    elif conflict.score:
        classification = DOUBTFUL
        rule = "contradicting biographical detail (probable homonym)"
    elif score >= CONFIRMED_SCORE and len(corroborating) >= MIN_CORROBORATING:
        classification = CONFIRMED
        rule = (
            f"score {score:.2f} >= {CONFIRMED_SCORE} with {len(corroborating)} corroborating "
            f"signals ({', '.join(s.name for s in corroborating)})"
        )
    elif score >= PROBABLE_SCORE:
        classification = PROBABLE
        rule = (
            f"score {score:.2f} >= {PROBABLE_SCORE} but only {len(corroborating)} corroborating "
            f"signal(s); a name match alone is never enough (RK-02)"
        )
    else:
        classification = DOUBTFUL
        rule = f"score {score:.2f} below {PROBABLE_SCORE}"

    return IdentityAssessment(
        score=round(min(1.0, score), 4),
        classification=classification,
        rule=rule,
        signals=signals,
    )
