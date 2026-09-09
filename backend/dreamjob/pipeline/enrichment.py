"""Online enrichment of the job seeker's profile (FR-122, FR-126, FR-127).

The pipeline is four steps:

1. **Anchors** - name variants, employers, locations, schools, declared URLs and
   the career date span are read out of the profile (``identity_match``).
2. **Search** - the open web is queried for the job seeker through DuckDuckGo's
   HTML endpoint, which needs no API key.  Queries are built from the anchors,
   not from a fixed template, so a profile with two employers produces two
   employer queries and one with ten produces ten.
3. **Fetch and extract** - every candidate page goes through ``EgressClient``
   (robots, rate limits, raw-document capture) and then through the LLM, with
   the page body passed as an ``untrusted`` block: a scraped page is hostile
   input (NFR-205).
4. **Score and store** - each finding is scored by ``identity_match`` and
   written to ``enrichment_finding`` with its per-signal evidence.  Only
   ``confirmed`` findings will later be merged automatically (FR-124).

Two things this module deliberately does *not* do:

* **FR-127** - special-category data is stripped before anything is persisted.
  Findings are filtered through ``strip_special_categories`` on the way to the
  database, and people-search aggregators - the places that publish exactly
  this kind of data about a person - are excluded from the candidate set
  outright.
* **FR-126** - when the job seeker has switched enrichment off, no request
  leaves the machine.  ``run_enrichment`` returns an empty, explicitly disabled
  report and the composite builder falls back to user-supplied data only.

Search availability is treated as a degradation, not an error.  DuckDuckGo
serves a bot-check page to datacentre IP ranges; when that happens the search
step reports itself unavailable and enrichment continues against the anchor
URLs the profile already declares plus the handle-derived candidates, which is
where the highest-value findings (personal site, repositories, newsletter) live
anyway.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from selectolax.parser import HTMLParser

import dreamjob.llm as llm_package
from dreamjob.config import get_settings
from dreamjob.db.repositories import enrichment as repo
from dreamjob.egress.client import EgressClient, RobotsDisallowed, RobotsUnavailable
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.identity_match import (
    CONFIRMED,
    ProfileAnchors,
    assess,
    build_anchors,
    hash_image_file,
    perceptual_hash,
    registrable_domain,
)

log = logging.getLogger(__name__)

DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"

# Excluded for two different reasons, both worth keeping straight:
# people-search aggregators publish exactly the special categories FR-127 bans
# and are a homonym factory (RK-02); the social platforms need the browser
# slice and its terms-of-service acknowledgement (IR-101), not a scraper.
EXCLUDED_DOMAINS = frozenset(
    {
        "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
        "tiktok.com", "pinterest.com", "quora.com",
        "spokeo.com", "whitepages.com", "radaris.com", "peoplefinders.com",
        "beenverified.com", "truepeoplesearch.com", "192.com", "rocketreach.co",
        "zoominfo.com", "signalhire.com", "lusha.com", "apollo.io",
    }
)

MAX_PAGE_CHARS = 20_000


# ---------------------------------------------------------------------------
# Versioned prompt templates (NFR-602)
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(llm_package.__file__).parent / "prompts"

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SECTION_RE = re.compile(r"^#\s*(system|user)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class PromptTemplate:
    """One ``llm/prompts/*.md`` file: metadata header plus system/user bodies."""

    name: str
    version: str
    task: str
    meta: dict[str, str]
    system: str
    user: str

    def render(self, **values: Any) -> tuple[str, str]:
        """Substitute ``{{placeholders}}``.

        Mustache-style braces rather than ``str.format``: the templates contain
        JSON examples, and single braces would have to be escaped everywhere.
        """

        def fill(text: str) -> str:
            for key, value in values.items():
                text = text.replace("{{" + key + "}}", str(value))
            return text

        return fill(self.system), fill(self.user)


def _parse_front_matter(block: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    key: str | None = None
    for line in block.splitlines():
        if line.startswith((" ", "\t")) and key:
            meta[key] = (meta[key] + " " + line.strip()).strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            meta[key] = "" if value in (">", "|") else value
    return meta


def load_prompt(name: str) -> PromptTemplate:
    """Load a versioned prompt template by file stem (NFR-602).

    The version travels with every LLM call into ``llm_call.prompt_version``,
    so a regression in generated output can be tied to the prompt revision that
    caused it.
    """
    path = PROMPTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")

    match = _FRONT_MATTER_RE.match(text)
    if not match:
        raise ValueError(f"Prompt {name!r} has no version header; NFR-602 requires one")
    meta = _parse_front_matter(match.group(1))
    body = text[match.end() :]

    parts = _SECTION_RE.split(body)
    sections: dict[str, str] = {}
    for label, chunk in zip(parts[1::2], parts[2::2], strict=False):
        sections[label.lower()] = chunk.strip()
    if "system" not in sections or "user" not in sections:
        raise ValueError(f"Prompt {name!r} must contain '# system' and '# user' sections")

    return PromptTemplate(
        name=meta.get("id", name),
        version=meta.get("version", "0"),
        task=meta.get("task", ""),
        meta=meta,
        system=sections["system"],
        user=sections["user"],
    )


# ---------------------------------------------------------------------------
# FR-127: special-category data never reaches storage
# ---------------------------------------------------------------------------

# FR-127 forbids storing special categories of personal data *about the job
# seeker*.  It does not forbid the vocabulary: a career in healthcare, medical
# devices, public health, political science or human rights is ordinary
# professional history, and a naive keyword sweep deletes exactly the people
# whose CV needs it most.
#
# So two lists.  ``_OCCUPATIONAL_RE`` recognises the professional senses and
# wins outright; ``_PERSONAL_RE`` matches only phrasings that assert something
# about a person's own health, beliefs, membership or orientation.
_OCCUPATIONAL_RE = re.compile(
    r"\b("
    r"health\s?(care|tech|system|service|insurance|data|economics|informatics)"
    r"|public health|digital health|e-?health|mental health (service|care|platform|charity)"
    r"|medical (device|technology|imaging|software|publisher|school|centre|center)"
    r"|med-?tech|life science|biotech|pharmaceutic|clinical (trial|software|system)"
    r"|political (science|economy|studies|analyst|journalism|risk)"
    r"|race condition|race track|ethnic marketing|ethnographic"
    r"|trade union (lawyer|software|representative role)"
    r"|human rights|civil liberties|privacy|neuro-?rights|cognitive liberty"
    r"|biometric (authentication|security|sensor|system|technology)"
    r"|diversity (and|&) inclusion|equal opportunit"
    r")",
    re.IGNORECASE,
)

# Personal assertions: the category is predicated of someone. This is what
# separates "diagnosed with X" from "works in healthcare".
_PERSONAL_RE = re.compile(
    r"\b("
    r"(is|was|has been|identifies as)\s+(a\s+)?(gay|lesbian|bisexual|transgender|queer|heterosexual)"
    r"|sexual orientation"
    r"|(his|her|their|my)\s+(religion|faith|church|mosque|synagogue|health|illness|disability|diagnosis)"
    r"|(diagnosed|treated|hospitalis|hospitaliz|suffers?|suffering|recovering)\s+(with|for|from)"
    r"|(board )?member(ship)? of (a|the|an)\s+[\w\s-]{0,24}?(political party|party|trade union|union|church|congregation)\b"
    r"|(votes?|voted|voting)\s+(for|labour|conservative|green|socialist)"
    r"|political (opinion|affiliation|belief|party|activis|candidate|campaign)"
    r"|religious (belief|conviction|observance|affiliation|community)"
    r"|trade[- ]union memb"
    r"|(practising|practicing|devout|observant)\s+(catholic|protestant|muslim|jewish|hindu|buddhist)"
    r"|(chronic|mental) (illness|health condition)"
    r"|(disability|impairment) (status|benefit|pension)"
    r"|ethnic (origin|background|minority status)"
    r"|racial (origin|background)"
    r")",
    re.IGNORECASE,
)

# Bare category nouns. On their own these are health, belief or origin terms
# and FR-127 says not to store them "even if encountered"; with an occupational
# qualifier they are ordinary technical or sector vocabulary, and
# ``_OCCUPATIONAL_RE`` has already claimed those.
_BARE_CATEGORY_RE = re.compile(
    r"\b("
    r"diagnosis|diagnoses|illness|ailment|comorbidit"
    r"|disabilit|impairment"
    r"|religion|religious|faith community"
    r"|ethnicity|ethnic|racial"
    r"|sexual orientation|sexuality"
    r"|trade union|labour union|labor union"
    r"|political opinion|political belief"
    r")\b",
    re.IGNORECASE,
)

# ...unless the sentence is plainly technical. "Fault diagnosis", "diagnosis
# and recovery" in a systems sense, "accessibility" work - all legitimate.
_TECHNICAL_QUALIFIER_RE = re.compile(
    r"\b(fault|error|system|network|engine|database|machine|model|code|"
    r"software|hardware|incident|root[- ]cause|automated|predictive)\b",
    re.IGNORECASE,
)


def _is_special_category(text: str) -> bool:
    """Whether a string carries special-category data about a person (FR-127).

    Three tiers, checked in order:

    1. An occupational sense wins outright - a healthcare career is not a
       health record.
    2. An assertion about a person is removed.
    3. A bare category noun is removed unless the sentence is plainly
       technical, because FR-127 says not to store these "even if encountered"
       and an ambiguous health word is not worth the risk.
    """
    if not text:
        return False
    if _OCCUPATIONAL_RE.search(text):
        return False
    if _PERSONAL_RE.search(text):
        return True
    if _BARE_CATEGORY_RE.search(text):
        return not _TECHNICAL_QUALIFIER_RE.search(text)
    return False


# A field *named* for a special category is a different matter: a key called
# "religion", "health_notes" or "political_views" is a declared attribute,
# whatever its value.
_SPECIAL_KEY_TOKENS = frozenset(
    {
        "health", "medical", "diagnosis", "illness", "disability", "disabilities",
        "religion", "religious", "faith", "political", "politics", "party",
        "union", "ethnicity", "ethnic", "race", "racial", "biometric",
        "orientation", "sexuality",
    }
)


def _is_special_key(key: str) -> bool:
    tokens = {t for t in re.split(r"[^a-z]+", str(key).lower()) if t}
    return bool(tokens & _SPECIAL_KEY_TOKENS)


# Keys that carry the substance of a fact.  When one of them is censored the
# surrounding object is dropped rather than left as a hollow fragment.
_CONTENT_KEYS = frozenset(
    {"statement", "text", "quote", "value", "description", "title", "summary",
     "fact", "preference", "cue", "activity", "constraint"}
)


def strip_special_categories(payload: Any) -> tuple[Any, list[str]]:
    """Remove special-category material from a finding (FR-127, CR-410).

    ``llm.client.redact`` already drops dictionary *keys* that name a special
    category.  A web finding needs more than that, because the offending
    material usually arrives as a free-text sentence.

    What it must not do is delete a career.  An earlier version matched the
    bare words - health, medical, political, race, ethnic - and so removed
    "healthcare", "medical devices", "political science", "human rights" and
    "race condition" from perfectly ordinary professional histories.  The test
    of a string is now whether it asserts a special category *of a person*
    (``_is_special_category``); occupational senses are recognised and kept.

    Returns the cleaned payload and the list of removals, which the caller
    records for the audit trail.
    """
    removed: list[str] = []

    def clean(node: Any, path: str = "") -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            lost_content = False
            for key, value in node.items():
                key_path = f"{path}.{key}".strip(".")
                if _is_special_key(key):
                    removed.append(key_path)
                    lost_content = lost_content or key in _CONTENT_KEYS
                    continue
                cleaned = clean(value, key_path)
                if cleaned is None and value is not None:
                    lost_content = lost_content or key in _CONTENT_KEYS
                    continue
                if cleaned is not None:
                    out[key] = cleaned
            return None if lost_content else out
        if isinstance(node, list):
            kept = []
            for index, item in enumerate(node):
                cleaned = clean(item, f"{path}[{index}]")
                if cleaned is not None:
                    kept.append(cleaned)
            return kept
        if isinstance(node, str) and _is_special_category(node):
            removed.append(path or "<value>")
            return None
        return node

    return clean(payload), removed


# ---------------------------------------------------------------------------
# Query material (FR-122)
# ---------------------------------------------------------------------------


def build_queries(anchors: ProfileAnchors, limit: int = 10) -> list[str]:
    """Turn profile anchors into web queries.

    The quoted full name is in every query: it is the one anchor that makes the
    result set about a person rather than about a topic.  Everything after it is
    a corroborating anchor, which is also what ``identity_match`` will score the
    result on.
    """
    name = anchors.display_name.strip()
    if not name:
        return []
    quoted = f'"{name}"'

    queries: list[str] = [quoted]
    for employer in anchors.employers[:3]:
        queries.append(f"{quoted} {employer}")
    for location in anchors.locations[:2]:
        queries.append(f"{quoted} {location}")
    queries += [
        f"{quoted} (github OR gitlab OR repository)",
        f"{quoted} (blog OR substack OR newsletter)",
        f"{quoted} (book OR publication OR author OR paper)",
        f"{quoted} (talk OR conference OR speaker OR podcast)",
        f"{quoted} (interview OR press OR profile)",
    ]
    for handle in anchors.handles[:2]:
        queries.append(f'"{handle}"')

    seen: set[str] = set()
    unique = [q for q in queries if not (q in seen or seen.add(q))]
    return unique[:limit]


def candidate_urls(anchors: ProfileAnchors, limit: int = 12) -> list[str]:
    """URLs worth fetching without a search engine.

    The profile's own declared links first - those are self-asserted and score
    a cross-link signal immediately - then the handle-shaped guesses for the
    platforms where a professional's public work actually lives.

    Only handles that came from a declared URL (``anchors.real_handles``) are
    probed on third-party platforms.  A handle guessed from the name ("stephaneaa")
    probes a fabricated address almost every time, so an enrichment run was
    spending its whole budget on 404s and SSL failures; it is used only when the
    profile declared no handle at all.  This is what keeps a run on the right
    URLs instead of a table of guesses.
    """
    out: list[str] = []
    for url in anchors.declared_urls:
        if url.startswith("http") and registrable_domain(url) not in EXCLUDED_DOMAINS:
            out.append(url)

    guess_limit = 3 if not anchors.real_handles else 0
    probe_handles = list(anchors.real_handles)
    # Cap the probe set: a person rarely has more than a couple of platforms.
    probe_handles = probe_handles[: max(1, limit - len(out) // 4)]
    if not probe_handles:
        probe_handles = anchors.handles[:guess_limit]

    for handle in probe_handles:
        for platform in ("github.com", "substack.com", "gitlab.com", "github.io"):
            out.append(_platform_url(platform, handle))

    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))][:limit]


def _platform_url(platform: str, handle: str) -> str:
    """Build a profile URL for a declared handle on a known platform."""
    if platform == "substack.com":
        return f"https://{handle}.substack.com"
    if platform == "github.io":
        return f"https://{handle}.github.io"
    if platform == "github.com":
        return f"https://github.com/{handle}"
    if platform == "gitlab.com":
        return f"https://gitlab.com/{handle}"
    return f"https://{platform}/{handle}"


# ---------------------------------------------------------------------------
# Search (FR-122) - DuckDuckGo HTML endpoint, no API key
# ---------------------------------------------------------------------------


def enrichment_allowed(job_seeker_id: str) -> bool:
    """FR-126: is online enrichment switched on for this job seeker?

    Resolved through the auth slice so there is one consent story in the
    application, with a direct read of ``consent_record`` as the fallback.
    """
    try:
        from dreamjob.security.auth_service import has_consent  # noqa: PLC0415
    except ImportError:
        return repo.enrichment_enabled(job_seeker_id)
    return bool(has_consent(job_seeker_id, "enrichment"))


class SearchUnavailable(RuntimeError):
    """The search endpoint served a bot check or was blocked by robots.txt."""


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str
    query: str
    rank: int


def _decode_ddg_href(href: str) -> str:
    """DuckDuckGo wraps results in ``/l/?uddg=<encoded target>``."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or href
    return href


def parse_ddg_results(html: str, query: str = "") -> list[SearchHit]:
    """Parse the DuckDuckGo HTML result list.

    Raises ``SearchUnavailable`` on the bot-check page, which returns HTTP 200
    with a challenge modal instead of results - a silent empty list would look
    like "this person has no online presence", which is a very different fact.
    """
    tree = HTMLParser(html)
    if tree.css_first(".anomaly-modal__title") or "bots use DuckDuckGo too" in html:
        raise SearchUnavailable("DuckDuckGo served a bot-check page")

    hits: list[SearchHit] = []
    for rank, node in enumerate(tree.css(".result"), start=1):
        classes = node.attributes.get("class", "") or ""
        if "result--ad" in classes:
            continue
        link = node.css_first("a.result__a")
        if link is None:
            continue
        url = _decode_ddg_href(link.attributes.get("href", "") or "")
        if not url.startswith("http"):
            continue
        snippet_node = node.css_first(".result__snippet")
        hits.append(
            SearchHit(
                url=url,
                title=link.text(strip=True),
                snippet=snippet_node.text(strip=True) if snippet_node else "",
                query=query,
                rank=rank,
            )
        )
    if not hits and not tree.css_first("#links"):
        raise SearchUnavailable("no result container in the DuckDuckGo response")
    return hits


async def duckduckgo_search(
    egress: EgressClient, query: str, *, max_results: int = 10
) -> list[SearchHit]:
    """One query against the HTML endpoint, robots-checked by ``EgressClient``."""
    url = f"{DDG_HTML_ENDPOINT}?q={quote_plus(query)}"
    try:
        result = await egress.fetch(url, use_cache=True)
    except RobotsUnavailable as exc:
        # Not "the endpoint said no" - "we never managed to ask" (FR-182).
        raise SearchUnavailable(str(exc)) from exc
    except RobotsDisallowed as exc:
        raise SearchUnavailable(f"robots.txt disallows the search endpoint: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surfaced as a degradation, not a crash
        raise SearchUnavailable(f"search request failed: {exc}") from exc
    if not result.ok:
        raise SearchUnavailable(f"search endpoint returned HTTP {result.status_code}")
    return parse_ddg_results(result.text, query)[:max_results]


# ---------------------------------------------------------------------------
# Page handling
# ---------------------------------------------------------------------------


def page_text(html: str, limit: int = MAX_PAGE_CHARS) -> str:
    tree = HTMLParser(html)
    tree.strip_tags(["script", "style", "noscript", "svg", "template"])
    body = tree.body or tree.root
    text = body.text(separator=" ", strip=True) if body else ""
    return re.sub(r"\s+", " ", text)[:limit]


def page_title(html: str) -> str:
    node = HTMLParser(html).css_first("title")
    return node.text(strip=True)[:300] if node else ""


def portrait_url(html: str, base_url: str) -> str | None:
    """The one image on a page most likely to be a photo of its subject."""
    tree = HTMLParser(html)
    meta = tree.css_first('meta[property="og:image"]') or tree.css_first('meta[name="og:image"]')
    if meta and meta.attributes.get("content"):
        return _absolutise(meta.attributes["content"], base_url)
    for node in tree.css("img"):
        haystack = " ".join(
            str(node.attributes.get(a, "") or "") for a in ("class", "id", "alt", "src")
        ).lower()
        if any(w in haystack for w in ("avatar", "portrait", "profile", "headshot", "author")):
            src = node.attributes.get("src")
            if src:
                return _absolutise(src, base_url)
    return None


def _absolutise(url: str, base_url: str) -> str:
    if url.startswith("http"):
        return url
    parsed = urlparse(base_url)
    if url.startswith("//"):
        return f"{parsed.scheme}:{url}"
    if url.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{url}"
    return f"{parsed.scheme}://{parsed.netloc}/{url.lstrip('./')}"


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

_FINDING_SCHEMA = (
    '{"page_kind": "personal_site|repository|blog|publication|talk|press|'
    'directory|other", "about_person": true, "facts": [{"statement": str, '
    '"category": "role|employer|project|publication|talk|skill|education|'
    'location|other", "quote": str}], "identity_clues": {"names": [str], '
    '"employers": [str], "locations": [str], "years": [str], "links": [str]}}'
)


def extract_finding(
    llm: LLMClient | None,
    *,
    url: str,
    title: str,
    text: str,
    anchors: ProfileAnchors,
) -> dict[str, Any]:
    """Pull structured facts out of one page.

    The page body is passed as an ``untrusted`` block (NFR-205): it is scraped
    content and may contain instructions aimed at the model.  When no LLM is
    available - budget exhausted, no key, an offline run - a heuristic
    fallback still yields the title and a text excerpt, which is enough for
    identity scoring even though it carries no extracted facts.
    """
    fallback = {
        "page_kind": "other",
        "about_person": None,
        "facts": [],
        "identity_clues": {},
        "excerpt": text[:600],
        "extraction": "heuristic",
    }
    if llm is None:
        return fallback

    prompt = load_prompt("enrichment_finding")
    system, user = prompt.render(
        person=anchors.display_name,
        employers=", ".join(anchors.employers[:8]) or "unknown",
        locations=", ".join(anchors.locations[:5]) or "unknown",
        url=url,
    )
    try:
        data = llm.complete_json(
            "summarise.page",
            system=system,
            user=user,
            untrusted={"page": f"URL: {url}\nTITLE: {title}\n\n{text}"},
            schema_hint=_FINDING_SCHEMA,
            entity_type="enrichment_finding",
            entity_id=url[:200],
            prompt_template=prompt.name,
            prompt_version=prompt.version,
            max_tokens=1200,
        )
    except (LLMError, BudgetExhausted) as exc:
        log.info("Falling back to heuristic extraction for %s: %s", url, exc)
        fallback["extraction_error"] = str(exc)[:300]
        return fallback

    if not isinstance(data, dict):
        return fallback
    data.setdefault("facts", [])
    data.setdefault("identity_clues", {})
    data["extraction"] = "llm"
    return data


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class EnrichmentReport:
    """What one enrichment run did, in a form the UI and the job log can show."""

    job_seeker_id: str
    enabled: bool = True
    queries: list[str] = field(default_factory=list)
    search_available: bool = True
    degradation_reason: str | None = None
    urls_considered: int = 0
    pages_fetched: int = 0
    findings: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    special_category_removals: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_seeker_id": self.job_seeker_id,
            "enabled": self.enabled,
            "queries": self.queries,
            "search_available": self.search_available,
            "degradation_reason": self.degradation_reason,
            "urls_considered": self.urls_considered,
            "pages_fetched": self.pages_fetched,
            "findings": self.findings,
            "counts": self.counts,
            "special_category_removals": self.special_category_removals,
            "errors": self.errors,
        }


def anchors_for_seeker(
    job_seeker_id: str, profile_version: dict | None = None, persona_id: str | None = None
) -> ProfileAnchors:
    """Build anchors from the job seeker's newest profile version."""
    version = profile_version or repo.latest_profile_version(job_seeker_id, persona_id)
    if version is None:
        return ProfileAnchors()

    anchors = build_anchors(
        version.get("sections") or {},
        display_name=str(version.get("display_name") or ""),
        photo_path=version.get("photo_path"),
    )
    if not anchors.display_name:
        # profile_version carries no name of its own; the account name is the
        # fallback anchor.  The read lives in the repository (CR-408).
        name = repo.seeker_display_name(job_seeker_id)
        if name:
            anchors.display_name = name
            named = build_anchors({}, name)
            anchors.variants = named.variants
            # Never *replace* the handles already harvested from the profile's
            # declared URLs - those are the real ones (e.g. github.com/stepvda
            # -> "stepvda").  Name-derived guesses are only appended to the
            # tail, so a guessed "stephaneaa" cannot displace a real "stepvda"
            # and cause the run to probe a dozen non-existent addresses.
            for handle in named.handles:
                if handle not in anchors.handles:
                    anchors.handles.append(handle)

    if anchors.photo_path:
        path = Path(anchors.photo_path)
        if not path.is_absolute():
            path = get_settings().abs_data_dir / path
        anchors.photo_hash = hash_image_file(str(path))
    return anchors


def _eligible(url: str, seen: set[str], rejected: set[str]) -> bool:
    if not url.startswith("http") or url in seen or url in rejected:
        return False
    domain = registrable_domain(url)
    return not any(domain == d or domain.endswith("." + d) for d in EXCLUDED_DOMAINS)


async def run_enrichment(
    job_seeker_id: str,
    *,
    profile_version: dict | None = None,
    persona_id: str | None = None,
    campaign_id: str | None = None,
    llm: LLMClient | None = None,
    max_queries: int = 6,
    max_pages: int = 12,
    force: bool = False,
) -> EnrichmentReport:
    """Search, fetch, score and store online findings (FR-122..FR-124, FR-127)."""
    report = EnrichmentReport(job_seeker_id=job_seeker_id)

    if not force and not enrichment_allowed(job_seeker_id):
        # FR-126: nothing leaves the machine when enrichment is switched off.
        report.enabled = False
        report.degradation_reason = "online enrichment is disabled for this job seeker"
        return report

    anchors = anchors_for_seeker(job_seeker_id, profile_version, persona_id)
    if not anchors.display_name:
        report.errors.append("no profile name available; cannot search for the job seeker")
        return report

    report.queries = build_queries(anchors, limit=max_queries)
    rejected = repo.rejected_urls(job_seeker_id)
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []  # (url, why)

    if llm is None:
        llm = default_llm(job_seeker_id, campaign_id)

    async with EgressClient() as egress:
        for query in report.queries:
            try:
                hits = await duckduckgo_search(egress, query, max_results=8)
            except SearchUnavailable as exc:
                report.search_available = False
                report.degradation_reason = str(exc)
                log.info("Web search unavailable (%s); falling back to anchor URLs", exc)
                break
            for hit in hits:
                if _eligible(hit.url, seen, rejected):
                    seen.add(hit.url)
                    ordered.append((hit.url, f"search: {query}"))

        for url in candidate_urls(anchors):
            if _eligible(url, seen, rejected):
                seen.add(url)
                ordered.append((url, "profile anchor"))

        ordered = ordered[:max_pages]
        report.urls_considered = len(ordered)

        for url, why in ordered:
            try:
                fetched = await egress.fetch(url)
            except RobotsUnavailable as exc:
                report.errors.append(f"{url}: {exc}")
                continue
            except RobotsDisallowed:
                report.errors.append(f"robots.txt disallows {url}")
                continue
            except Exception as exc:  # noqa: BLE001 - one dead link is not a failed run
                report.errors.append(f"{url}: {exc}")
                continue
            if not fetched.ok:
                report.errors.append(f"{url}: HTTP {fetched.status_code}")
                continue

            report.pages_fetched += 1
            html = fetched.text
            text = page_text(html)
            title = page_title(html) or url

            candidate_hash = None
            if anchors.photo_hash:
                candidate_hash = await _portrait_hash(egress, html, url)

            verdict = assess(
                url=url, title=title, text=text, anchors=anchors,
                candidate_photo_hash=candidate_hash,
            )
            facts = extract_finding(llm, url=url, title=title, text=text, anchors=anchors)
            facts["discovered_via"] = why
            facts["raw_document_id"] = fetched.raw_document_id

            clean_facts, removed = strip_special_categories(facts)
            if clean_facts is None:
                # Every extracted key carried special-category material; the
                # page is still recorded, its content is not (FR-127).
                clean_facts = {"page_kind": "other", "facts": [], "extraction": "redacted"}
            report.special_category_removals += len(removed)
            if removed:
                clean_facts["special_category_removals"] = len(removed)

            signals = verdict.to_signals_json()
            signals["discovered_via"] = why

            finding_id = repo.save_finding(
                job_seeker_id,
                url=url,
                title=title,
                extracted_facts=clean_facts,
                identity_signals=signals,
                identity_score=verdict.score,
                classification=verdict.classification,
            )
            if finding_id is None:  # permanently rejected earlier (FR-124)
                continue

            report.findings.append(
                {
                    "id": finding_id,
                    "url": url,
                    "title": title,
                    "classification": verdict.classification,
                    "identity_score": verdict.score,
                    "auto_merge": verdict.auto_merge,
                    "rule": verdict.rule,
                }
            )
            report.counts[verdict.classification] = (
                report.counts.get(verdict.classification, 0) + 1
            )

    report.counts.setdefault(CONFIRMED, 0)
    return report


async def _portrait_hash(egress: EgressClient, html: str, url: str) -> int | None:
    image_url = portrait_url(html, url)
    if not image_url:
        return None
    try:
        image = await egress.fetch(image_url)
    except Exception:  # noqa: BLE001 - a missing image is simply no signal
        return None
    return perceptual_hash(image.content) if image.ok else None


def default_llm(job_seeker_id: str, campaign_id: str | None = None) -> LLMClient | None:
    """An LLM client, or ``None`` when no provider is configured.

    Returning ``None`` rather than raising is what lets every caller in this
    package degrade to its non-LLM path instead of failing the run (NFR-104).
    """
    settings = get_settings()
    if not settings.deepseek_api_key and not settings.local_llm_base_url:
        return None
    return LLMClient(campaign_id=campaign_id, job_seeker_id=job_seeker_id)


def run_enrichment_sync(job_seeker_id: str, **kwargs: Any) -> EnrichmentReport:
    """Blocking wrapper for FastAPI's threadpool routes and CLI use."""
    return asyncio.run(run_enrichment(job_seeker_id, **kwargs))


# ---------------------------------------------------------------------------
# Regression guard: handle derivation from declared URLs (FR-122)
# ---------------------------------------------------------------------------
