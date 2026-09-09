"""Rung 2 of the employer-kind ladder: read the company's own website (FR-341).

The register answers for Belgian companies and says nothing about the other
two-thirds of the corpus.  For those, the general answer is the site itself: an
agency describes itself unmistakably, because it has to sell to two audiences
at once.  It offers *people* to companies ("voor werkgevers", "voor bedrijven",
"für Arbeitgeber", "plaats een vacature") next to jobs for candidates, and it
lists the sectors it staffs rather than a product it makes.  Measured on 55
hand-labelled sites this reads 53 correctly - agency recall 20/20, agency
precision 20/21 - at EUR 0.0014 a company on the cheap model
(``docs/Agency_Research_Design.md`` section 3.5).

Three properties hold that measurement up, and each one lives in this module
rather than in the prompt, because a prompt cannot enforce anything:

* **Every verdict carries evidence that was checked** (NFR-402).  The model
  returns verbatim quotes; :func:`verify_quote` looks each one up in the page
  text it claims to come from, and a quote that is not there is discarded.  If
  no surviving quote supports the classification, the verdict is downgraded to
  ``cannot_tell(no_verifiable_evidence)``.  A hallucinated verdict is therefore
  not storable, and a job seeker who disagrees can read the sentence that
  decided it.
* **Web pages are untrusted** (NFR-205).  Page text goes through
  ``LLMClient.complete_json(untrusted=...)``, so it is fenced and the injection
  preamble is attached; and a quote that is itself a planted instruction is
  discarded here and reported in ``anomalies`` rather than counted as evidence.
* **The refusals happen before the LLM.**  A page that cannot support a verdict
  - under 180 characters of text (100g.be renders 0, editx.eu renders
  "loading..."), a 403 bot wall, a parking page, an off-domain redirect
  (think-about-it.com now lands on hyundaiusa.com) - becomes a specific
  ``cannot_tell`` reason.  ``cannot_tell`` is a first-class state with a reason
  and a cheapest-next-rung, never a default to "employer" or to "agency"
  (``Agency_Research_Design.md`` section 7).

What this module does **not** do: confirm that the domain belongs to the
company.  That is the hardened identity gate of the registry slice
(``apply_contacts.confirm_domain`` / ``company_named_on_page``); this rung is
handed a *confirmed* domain and records what it observed about identity
(requested domain, final domain, pages read) so the gate's verdict and this
one can be compared.

The verdict is a shared knowledge-base fact: one row per company, no job seeker
on it (FR-344).  A company does not stop being an agency because a different
person is looking at it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.website import crawler
from dreamjob.config import get_settings
from dreamjob.egress.client import EgressClient, RobotsDisallowed
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.apply_contacts import MIN_PAGE_TEXT, PARKING_MARKERS, strip_accents
from dreamjob.pipeline.enrichment import load_prompt

log = logging.getLogger(__name__)

#: Routing and provenance.  ``classify.employer_kind`` is a cheap-model task
#: (measured: deepseek-chat, 96.4%, EUR 0.0014 a call); ``LLMClient.route``
#: sends any task it does not recognise to the strong model, so every call from
#: here passes ``prefer_strong=False`` explicitly rather than relying on the
#: task id having been added to ``TASK_CHEAP``.
LLM_TASK = "classify.employer_kind"
PROMPT_NAME = "employer_kind"
#: ``company_employer_kind.rung`` and ``.method`` as migration 110 spells them.
#: This is the ladder's fifth rung by cost and its second by the research note's
#: numbering; the column holds the name, not the number.
RUNG = "website"
METHOD = "website_llm"
#: What the product does with each verdict (proposal section 4).  The ``board``
#: role - a job board that appears as an employer - is decided from
#: ``AGGREGATOR_DOMAINS`` by the product slice, not from the page text.
EMPLOYER_ROLE = {"agency": "agency", "employer": "direct", "cannot_tell": "unverified"}

#: Read the home page plus at most this many employer-facing pages.
MAX_SUPPORT_PAGES = 3
#: Character budgets that produced the measured 4,459 input tokens a call.
HOME_TEXT_CHARS = 6_000
PAGE_TEXT_CHARS = 4_000

#: Call parameters as measured (section 3.3).
TEMPERATURE = 0.1
MAX_TOKENS = 900

#: A rung-2 verdict is never certain: the site is the company talking about
#: itself.  One surviving quote is weaker still.
CONFIDENCE_CAP = 0.95
SINGLE_QUOTE_CAP = 0.80
#: Below this, a decided verdict is stored as ``ambiguous_self_description``
#: with the quotes on both sides, for the job seeker to read (section 7.4).
#: The model was never wrong above 0.8 on the measured set and once wrong at it.
AMBIGUOUS_FLOOR = 0.80

#: Statuses that mean "a machine was refused", not "there is nothing here".
BOT_WALL_STATUSES = frozenset({401, 403, 429, 503})
#: Challenge pages answer 200 as often as 403.
BOT_WALL_MARKERS = (
    "attention required", "just a moment", "checking your browser",
    "you have been blocked", "please enable cookies", "enable javascript and cookies",
    "cf-browser-verification", "ddos protection by", "access denied",
)

VALID_KINDS = frozenset({"agency", "employer", "cannot_tell"})
SERVICE_MODELS = frozenset({
    "product", "services", "consultancy_or_outsourcing", "temp_agency",
    "recruitment_selection", "payrolling", "job_board", "public_body", "unknown",
})
#: The seven keys the prompt asks for (section 3.4 rule 5).
RESPONSE_KEYS = ("classification", "confidence", "service_model", "audiences",
                 "evidence", "summary", "anomalies")

#: ``cannot_tell`` reasons this rung can produce, and how long before the pass
#: tries again (section 5).  ``None`` means "never automatically": the answer
#: will not change on its own, so it waits for a person or for another rung.
RETRY_AFTER_DAYS: dict[str, int | None] = {
    "no_domain": None,               # event-driven: a domain is derived or entered
    "unreachable": 3,
    "robots": 90,
    "bot_wall": 90,
    "js_rendered_or_empty": 90,
    "parked": 90,
    "off_domain_redirect": None,
    "ambiguous_self_description": None,
    "no_verifiable_evidence": None,
    "llm_failed": 3,
}

#: The cheapest next rung, in the words the company panel shows (section 7.3).
NEXT_STEP: dict[str, str] = {
    "no_domain": "add the website",
    "unreachable": "the site did not answer; it will be tried again",
    "robots": "the site asks machines not to read it; check it in a browser",
    "bot_wall": "the site blocks machines; check it in a browser",
    "js_rendered_or_empty": "the site needs a browser to render; check it in a browser",
    "parked": "the domain is a placeholder; add the real website",
    "off_domain_redirect": "the known domain now belongs to somebody else; add the real website",
    "ambiguous_self_description": "read the quotes and tell us which it is",
    "no_verifiable_evidence": "read the site and tell us which it is",
    "llm_failed": "the reading failed; it will be tried again",
}

#: How long a decided website verdict stands before it is re-read.  A domain
#: can change hands and a company can pivot; think-about-it.com now sells cars.
DECIDED_STALENESS_DAYS = 180

#: Text that is trying to talk to the model rather than describe the business.
#: ``LLMClient.wrap_untrusted`` already annotates these inside the fenced block;
#: this list is the second half of the defence: such a sentence is never
#: evidence, however verbatim it is.
INJECTION_MARKERS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |any )?(previous|prior|above) instructions",
        r"disregard (the )?(system|previous|above)",
        r"note to (ai|assistant|llm|language model)",
        r"you are now\b",
        r"new instructions?:",
        r"classify (it|this|us|the (company|organisation|organization)) as",
        r"\bflagged-instruction-in-data\b",
        r"</?(system|assistant|instructions?)>",
    )
)

#: Path and anchor words that mark the page an agency writes for its clients.
#: ``crawler.PAGE_KINDS`` has no employer-facing kind - *werkgevers*,
#: *voor-bedrijven*, *plaats-een-vacature* all fall through to the generic
#: score - and adding one belongs to the crawler, not here, so this rung ranks
#: its own three pages with the shared scorer underneath it.
EMPLOYER_PAGE_TOKENS = (
    "werkgever", "werkgevers", "voor-werkgevers", "employer", "employers",
    "for-employers", "for-companies", "voor-bedrijven", "bedrijven", "ondernemingen",
    "opdrachtgever", "opdrachtgevers", "werknemer", "personeel", "vind-personeel",
    "medewerker", "entreprise", "entreprises", "employeur", "employeurs",
    "recruteur", "arbeitgeber", "unternehmen", "personalvermittlung", "zeitarbeit",
    "arbeitnehmerueberlassung", "plaats-een-vacature", "vacature-plaatsen", "post-a-job",
    "poster-une-offre", "uitzendarbeid", "uitzendwerk", "interim",
    "rekrutering", "recruitment", "recrutement", "selectie", "staffing", "payroll",
    "payrolling", "outsourcing", "detachering", "headhunting", "executive-search",
    "talent-solutions", "hr-solutions", "hiring-solutions", "onze-diensten", "dienstverlening",
)
#: Which pages are read, and in what order, once the home page is in hand.
PAGE_KIND_WEIGHT = {"employers": 1.00, "products": 0.80, "about": 0.70, "values": 0.45}

_WORD_RE = re.compile(r"[a-z0-9]+")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")
_WS_RE = re.compile(r"\s+")
#: A quote shorter than this cannot be checked: one word matches any page.
MIN_QUOTE_TOKENS = 2
#: Words of slack when looking a quote up, for the word or two a model drops.
#: The slack is *inside one span*, which is what stops two menu labels spliced
#: with an ellipsis - the six failures in 167 measured quotes - from verifying.
QUOTE_SLACK = 2
#: Rule 2 of the prompt: at most 30 words.  A longer quote still verifies, but
#: it is reported.
MAX_QUOTE_WORDS = 30


# ---------------------------------------------------------------------------
# What was read, and what was concluded
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageRead:
    """One page this rung actually read, with the provenance FR-183 keeps."""

    url: str
    kind: str                       # home | employers | products | about | values | other
    title: str
    text: str
    fetched_at: str
    raw_document_id: str | None = None
    status_code: int = 200
    #: The egress client's hash of the bytes.  A refresh whose pages all hash
    #: the same is renewed without an LLM call (design section 5).
    content_hash: str = ""

    def block(self) -> str:
        """The untrusted block the prompt reads: title, ``URL:`` line, text."""
        return f"### {self.title or self.url}\nURL: {self.url}\n{self.text}\n"

    @property
    def quotable(self) -> str:
        """Everything the model was shown for this page, and may quote from.

        The title is part of the block, so it is fair evidence - Adecco's
        "Vind de juiste job, werf het juiste talent aan" is the page title and
        appears nowhere in the body text.  Verifying against the body alone
        discarded a quote the model had read correctly.
        """
        return f"{self.title}\n{self.text}"


@dataclass
class PageSet:
    """The outcome of the fetch half: pages, or the reason there are none."""

    domain: str
    home_url: str = ""
    final_url: str = ""
    pages: list[PageRead] = field(default_factory=list)
    refusal: str | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.refusal is None and bool(self.pages)


@dataclass
class WebsiteVerdict:
    """One rung-2 answer about one company, with the evidence that supports it.

    ``kind`` is ``agency``, ``employer`` or ``cannot_tell``; ``cannot_tell`` is
    a first-class state, never a default, and always carries a ``reason``.
    """

    kind: str
    confidence: float = 0.0
    reason: str | None = None
    service_model: str | None = None
    audiences: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    needs_review: bool = False
    detail: str = ""
    domain: str = ""
    home_url: str = ""
    final_url: str = ""
    pages_read: list[str] = field(default_factory=list)
    #: url -> content hash of the page the verdict was read from, so a refresh
    #: that finds every page unchanged renews the row without an LLM call.
    content_hashes: dict[str, str] = field(default_factory=dict)
    method: str = METHOD
    rung: str = RUNG
    model: str | None = None
    prompt_template: str | None = None
    prompt_version: str | None = None
    established_at: str = ""
    attempts: int = 1

    def __post_init__(self) -> None:
        if not self.established_at:
            self.established_at = datetime.now(UTC).isoformat(timespec="seconds")

    @property
    def decided(self) -> bool:
        return self.kind in ("agency", "employer")

    @property
    def next_step(self) -> str:
        return NEXT_STEP.get(self.reason or "", "")

    def _plus(self, days: int | None) -> str | None:
        if days is None:
            return None
        moment = datetime.fromisoformat(self.established_at) + timedelta(days=days)
        return moment.isoformat(timespec="seconds")

    def expires_at(self) -> str | None:
        """When a *decided* verdict goes stale.  A domain can change hands.

        180 days: a website verdict is the company describing itself, and
        think-about-it.com now sells cars.  A ``cannot_tell`` does not go
        stale - it was never established - it comes back on
        :meth:`retry_after`.
        """
        return self._plus(DECIDED_STALENESS_DAYS) if self.decided else None

    def retry_after(self) -> str | None:
        """When this refusal is worth another request, or ``None`` for never.

        Never means one of two things, and both are correct: the answer cannot
        change without a person (an off-domain redirect, an ambiguity only a
        reader can settle), or the retry is event-driven - ``no_domain``
        becomes due the moment a domain is derived or entered.
        """
        if self.decided:
            return None
        return self._plus(RETRY_AFTER_DAYS.get(self.reason or ""))

    def as_row(self, company_id: str = "") -> dict[str, Any]:
        """The ``company_employer_kind`` row this verdict writes (migration 110).

        Serialisation of the JSON columns is left to the repository, which owns
        the table; this is the shape, not the SQL.  There is deliberately no
        job seeker on it (FR-344).  ``js_rendered_or_empty`` is left in the
        research note's spelling: the schema accepts both and the repository
        folds it onto ``js_rendered`` rather than opening a second bucket.
        """
        return {
            "company_id": company_id,
            "kind": self.kind,
            "employer_role": EMPLOYER_ROLE.get(self.kind, "unverified"),
            "service_model": self.service_model or "unknown",
            "confidence": round(float(self.confidence), 3),
            "method": self.method,
            "rung": self.rung,
            "reason": self.reason,
            "note": self.detail,
            "evidence": self.evidence,
            "identity_evidence": {
                "gate": "confirmed_domain",
                "requested_domain": self.domain,
                "final_url": self.final_url,
                "pages_read": self.pages_read,
                "content_hashes": self.content_hashes,
            },
            "audiences": self.audiences,
            "summary": self.summary,
            "prompt_template": self.prompt_template,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "established_at": self.established_at,
            "expires_at": self.expires_at(),
            "retry_after": self.retry_after(),
            "attempts": self.attempts,
            "anomalies": self.anomalies,
        }


def cannot_tell(reason: str, *, detail: str = "", **kw: Any) -> WebsiteVerdict:
    """A refusal with its reason - the answer, not the absence of one."""
    return WebsiteVerdict(kind="cannot_tell", confidence=0.0, reason=reason, detail=detail, **kw)


# ---------------------------------------------------------------------------
# Choosing the pages (the employer-facing kind the shared crawler lacks)
# ---------------------------------------------------------------------------


def _anchor_tokens(anchor: str) -> set[str]:
    return set(_WORD_RE.findall(strip_accents((anchor or "").lower())))


#: A path segment with more words than this is an article slug, not a section:
#: "flexi-jobs-vanaf-1-juli-2026-wat-u-als-werkgever-moet-weten" is a news item
#: that happens to contain "werkgever", and reading it proves nothing about the
#: business.  "ik-zoek-een-werknemer" is four words and is the page itself.
MAX_SEGMENT_WORDS = 4


def _employer_page_score(url: str, anchor: str) -> float:
    """How strongly a link looks like the page an agency writes for its clients."""
    path = strip_accents(urlparse(url).path.lower())
    segments = [s for s in path.split("/") if s]
    segments = [s for s in segments if len(s.split("-")) <= MAX_SEGMENT_WORDS]
    words = _anchor_tokens(anchor)
    best = 0.0
    for token in EMPLOYER_PAGE_TOKENS:
        if any(token == seg or token in seg.split("-") or ("-" in token and token in seg)
               for seg in segments):
            best = max(best, 1.0)
        elif token in words:
            best = max(best, 0.8)
    # "Voor bedrijven" as an anchor over a URL that says nothing ("/nl/ik-zoek-
    # een-werknemer" is both, but "/b2b" is only the anchor).
    if best < 0.8 and {"werkgevers", "bedrijven", "employers", "arbeitgeber"} & words:
        best = 0.8
    return best


def rank_employer_pages(
    html: str, base_url: str, *, domain: str, limit: int = MAX_SUPPORT_PAGES
) -> list[tuple[str, str, float]]:
    """``(url, kind, score)`` for the pages worth reading after the home page.

    Employer-facing first, then what the company sells, then who it says it is:
    the order the measurement used.  The shared URL scorer still decides what
    is not worth a request at all (boilerplate, other sites, binaries, deep
    pagination), so this only adds the kind it has no name for.
    """
    site = crawler.registrable_domain(domain)
    home = crawler.normalise_url(base_url) or base_url
    ranked: dict[str, tuple[str, float]] = {}
    for url, anchor in crawler.extract_links(html, base_url):
        if url == home or not crawler.same_site(url, site):
            continue
        base_score = crawler.score_url(url, anchor=anchor, depth=1, base_domain=site)
        if base_score <= 0:
            continue
        kinds = crawler.classify_url_kinds(url, anchor)
        if kinds and kinds[0][0] == "news":
            # A press release about employer obligations is not the company
            # describing its own business, whatever words are in the slug.
            continue
        employer_hit = _employer_page_score(url, anchor)
        if employer_hit:
            kind, weight, hit = "employers", PAGE_KIND_WEIGHT["employers"], employer_hit
        else:
            matches = [(k, s) for k, s in kinds if k in PAGE_KIND_WEIGHT]
            if not matches:
                continue
            kind, hit = matches[0]
            weight = PAGE_KIND_WEIGHT[kind]
        depth_penalty = 0.05 * max(0, len([s for s in urlparse(url).path.split("/") if s]) - 1)
        score = round(weight * hit + 0.1 * base_score - depth_penalty, 4)
        current = ranked.get(url)
        if current is None or score > current[1]:
            ranked[url] = (kind, score)
    out = [(url, kind, score) for url, (kind, score) in ranked.items()]
    out.sort(key=lambda row: (-row[2], len(row[0]), row[0]))
    return out[:limit]


# ---------------------------------------------------------------------------
# The fetch half: what the classifier must never see (section 3.1)
# ---------------------------------------------------------------------------


def _label(domain: str) -> str:
    """The organisation-bearing label of a registrable domain."""
    parts = crawler.registrable_domain(domain).split(".")
    return parts[0] if parts else ""


def same_organisation(requested: str, final_url: str) -> bool:
    """Is the page that answered still the organisation whose domain we asked for?

    Registrable-domain equality is the rule, with one deliberate exception: a
    market site that redirects to the group domain under the same name -
    ``adecco.be`` to ``adecco.com/nl-be`` - is the same organisation, and the
    measured set classified it from exactly that redirect.  A different name -
    ``think-about-it.com`` to ``hyundaiusa.com`` - is not, whatever the page
    then says about itself.
    """
    final_domain = crawler.registrable_domain(urlparse(final_url).netloc)
    wanted = crawler.registrable_domain(requested)
    if not final_domain or final_domain == wanted:
        return True
    return bool(_label(wanted)) and _label(wanted) == _label(final_domain)


def _bot_wall(title: str, text: str) -> bool:
    lowered = f"{title}\n{text[:2000]}".lower()
    return any(marker in lowered for marker in BOT_WALL_MARKERS)


def _parked(text: str) -> str | None:
    lowered = text.lower()
    for marker in PARKING_MARKERS:
        if marker in lowered:
            return marker
    return None


def judge_page(fetched: Any, *, requested_domain: str) -> tuple[str | None, str]:
    """``(refusal reason, detail)`` for one fetched page, or ``(None, "")``.

    Everything here is decided in code, before any LLM call and before any
    money is spent: the classifier never sees a page that cannot support a
    verdict.
    """
    status = int(getattr(fetched, "status_code", 0) or 0)
    if status in BOT_WALL_STATUSES:
        return "bot_wall", f"HTTP {status}"
    if not getattr(fetched, "ok", False):
        return "unreachable", f"HTTP {status}"
    final_url = getattr(fetched, "url", "") or ""
    if not same_organisation(requested_domain, final_url):
        return "off_domain_redirect", f"{requested_domain} now answers from {final_url}"

    html = getattr(fetched, "text", "") or ""
    title = crawler.page_title(html)
    text = crawler.extract_text(html, drop_chrome=False)
    if _bot_wall(title, text):
        return "bot_wall", f"challenge page: {title[:80]!r}"
    marker = _parked(text)
    if marker:
        return "parked", f"the page is a placeholder ({marker!r})"
    if len(text) < MIN_PAGE_TEXT:
        return "js_rendered_or_empty", f"the page renders {len(text)} characters of text"
    return None, ""


async def read_pages(
    domain: str,
    *,
    egress: Any,
    limit: int = MAX_SUPPORT_PAGES,
) -> PageSet:
    """Fetch the home page and up to ``limit`` employer-facing pages.

    The domain must already be **confirmed** - this rung does not derive or
    re-judge identity (that is the registry slice's hardened gate).  A home
    page that cannot support a verdict ends the read with a reason; a subpage
    that cannot is simply skipped, because the home page already answered.
    """
    # A caller that hands over "https://forumjobs.be/nl" means the site, not a
    # nonexistent host of that name; answering "unreachable" to that would be a
    # refusal about our own parsing rather than about the company.
    domain = _WS_RE.sub("", (domain or "").strip().lower())
    domain = _SCHEME_RE.sub("", domain).split("/")[0].split("?")[0]
    domain = domain.removeprefix("www.").strip(".")
    if not domain or "." not in domain:
        return PageSet(domain=domain, refusal="no_domain", detail="no confirmed domain")

    home_url = f"https://{domain}"
    result = PageSet(domain=domain, home_url=home_url)
    try:
        fetched = await egress.fetch(home_url)
    except RobotsDisallowed as exc:
        # CR-402: not permission to read, so not evidence.  Never worked around.
        result.refusal, result.detail = "robots", f"robots.txt: {exc}"[:200]
        return result
    except Exception as exc:  # noqa: BLE001 - one dead site must not stop a pass
        result.refusal, result.detail = "unreachable", f"{type(exc).__name__}: {exc}"[:200]
        return result

    result.final_url = getattr(fetched, "url", "") or home_url
    reason, detail = judge_page(fetched, requested_domain=domain)
    if reason:
        result.refusal, result.detail = reason, detail
        return result

    # Once the redirect is accepted as the same organisation, the organisation's
    # domain is the one that answered: adecco.be's pages are on adecco.com, and
    # ranking its links against adecco.be would discard every one of them.
    site = crawler.registrable_domain(urlparse(result.final_url).netloc) or domain

    html = fetched.text
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    result.pages.append(
        PageRead(
            url=result.final_url,
            kind="home",
            title=crawler.page_title(html),
            # The navigation chrome is the evidence on an agency's home page:
            # "Ik zoek werk | Voor bedrijven" is the whole tell.
            text=crawler.extract_text(html, drop_chrome=False)[:HOME_TEXT_CHARS],
            fetched_at=fetched_at,
            raw_document_id=getattr(fetched, "raw_document_id", None),
            status_code=fetched.status_code,
            content_hash=getattr(fetched, "content_hash", "") or "",
        )
    )

    for url, kind, _score in rank_employer_pages(html, result.final_url, domain=site, limit=limit):
        try:
            sub = await egress.fetch(url)
        except Exception as exc:  # noqa: BLE001 - a missing subpage is not a verdict
            log.debug("employer_kind: skipping %s (%s)", url, exc)
            continue
        if judge_page(sub, requested_domain=site)[0]:
            continue
        sub_html = sub.text
        text = crawler.extract_text(sub_html, drop_chrome=False)[:PAGE_TEXT_CHARS]
        result.pages.append(
            PageRead(
                url=getattr(sub, "url", url) or url,
                kind=kind,
                title=crawler.page_title(sub_html),
                text=text,
                fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
                raw_document_id=getattr(sub, "raw_document_id", None),
                status_code=sub.status_code,
                content_hash=getattr(sub, "content_hash", "") or "",
            )
        )
    return result


# ---------------------------------------------------------------------------
# Quote verification (the half of NFR-205 that is about the output)
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    """Lower-case alphanumeric words, accents folded: "Noël" matches "Noel"."""
    return _WORD_RE.findall(strip_accents((text or "").lower()))


def looks_like_injection(quote: str) -> bool:
    """Is this quote the page trying to instruct the model, rather than describe itself?"""
    return any(pattern.search(quote or "") for pattern in INJECTION_MARKERS)


def _span_contains(quote: list[str], page: list[str], starts: list[int]) -> bool:
    for start in starts:
        index, position, slack = start + 1, 1, QUOTE_SLACK
        while position < len(quote) and index < len(page):
            if page[index] == quote[position]:
                position += 1
                index += 1
            elif slack:
                slack -= 1
                index += 1
            else:
                break
        if position == len(quote):
            return True
    return False


def quote_on_page(quote: str, text: str) -> bool:
    """Is this quote verbatim on this page, give or take a word the model dropped?

    Word-level rather than character-level, so punctuation, line breaks and the
    accents a model normalises away do not matter, and with the slack confined
    to a single span, so the failure mode the measurement found - two menu
    labels spliced with an ellipsis - does not verify.
    """
    words = _tokens(quote)
    if len(words) < MIN_QUOTE_TOKENS:
        return False
    page = _tokens(text)
    if not page:
        return False
    first = words[0]
    starts = [i for i, word in enumerate(page) if word == first]
    return bool(starts) and _span_contains(words, page, starts)


def verify_quote(quote: str, pages: list[PageRead], claimed_url: str = "") -> PageRead | None:
    """The page a quote is really on, preferring the one the model named.

    A quote attributed to the wrong page is still evidence - the sentence is on
    the site - but the URL is corrected to where it was found, because NFR-402
    promises the job seeker a link that shows the sentence.
    """
    by_url = {page.url: page for page in pages}
    claimed = by_url.get(claimed_url)
    if claimed is not None and quote_on_page(quote, claimed.quotable):
        return claimed
    for page in pages:
        if page is not claimed and quote_on_page(quote, page.quotable):
            return page
    return None


# ---------------------------------------------------------------------------
# Output validation (section 3.4)
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_strings(values: Any, limit: int = 8) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return [str(v).strip()[:300] for v in values if str(v).strip()][:limit]


def validate_response(payload: Any, pages: list[PageRead], **verdict_fields: Any) -> WebsiteVerdict:
    """Turn a model answer into a verdict, or refuse it.

    The model's answer is not the verdict; the validated answer is.  In order:
    the shape is checked, quotes that are instructions are dropped, quotes that
    are not on the page are dropped, a classification left without a supporting
    quote is downgraded to ``cannot_tell(no_verifiable_evidence)``, the
    confidence is capped, and a decided verdict below the ambiguity floor is
    stored as ``cannot_tell(ambiguous_self_description)`` with the quotes on
    both sides.

    Raises :class:`LLMError` when the answer is not the shape the prompt asked
    for; the caller retries once with the strong model.
    """
    if not isinstance(payload, dict):
        raise LLMError(f"employer_kind answer is {type(payload).__name__}, not an object")
    missing = [key for key in RESPONSE_KEYS if key not in payload]
    if missing:
        raise LLMError(f"employer_kind answer is missing {', '.join(missing)}")

    classification = str(payload.get("classification") or "").strip().lower()
    if classification not in VALID_KINDS:
        raise LLMError(f"employer_kind returned classification {classification!r}")
    confidence = _as_float(payload.get("confidence"))
    if confidence is None:
        raise LLMError(f"employer_kind returned confidence {payload.get('confidence')!r}")
    confidence = max(0.0, min(1.0, confidence))

    anomalies = _clean_strings(payload.get("anomalies"))
    extra = [key for key in payload if key not in RESPONSE_KEYS]
    if extra:
        # An extra key is an answer to the question plus something; a missing
        # one is not an answer.  Worth reporting, not worth refusing.
        anomalies.append(f"unexpected keys in the answer: {', '.join(sorted(extra))}")

    service_model = str(payload.get("service_model") or "").strip().lower() or None
    if service_model and service_model not in SERVICE_MODELS:
        anomalies.append(f"unknown service_model {service_model!r}")
        service_model = "unknown"
    audiences = payload.get("audiences")
    audiences = audiences if isinstance(audiences, dict) else {}

    raw_evidence = payload.get("evidence")
    raw_evidence = raw_evidence if isinstance(raw_evidence, list) else []
    evidence: list[dict[str, Any]] = []
    for item in raw_evidence[:4]:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        supports = str(item.get("supports") or "").strip().lower()
        if not quote or supports not in ("agency", "employer"):
            continue
        if looks_like_injection(quote):
            anomalies.append(
                "a quote was text addressed to the reader of this page rather than a "
                f"description of the organisation, and was discarded: {quote[:120]!r}"
            )
            continue
        page = verify_quote(quote, pages, str(item.get("url") or ""))
        if page is None:
            anomalies.append(f"quote not found on any page read, discarded: {quote[:120]!r}")
            continue
        if len(quote.split()) > MAX_QUOTE_WORDS:
            anomalies.append(f"quote longer than {MAX_QUOTE_WORDS} words: {quote[:80]!r}")
        if str(item.get("url") or "") not in ("", page.url):
            anomalies.append(f"quote was attributed to {item.get('url')!r}; it is on {page.url}")
        evidence.append({
            # ``type`` and ``verified`` are what the storage rules test: an
            # unverified quote may not carry a verdict, and this rung is the
            # only place that can honestly set the flag, because it is the only
            # place that has the page text.
            "type": "quote",
            "verified": True,
            "signal": "website",
            "supports": supports,
            "quote": quote[:400],
            "url": page.url,
            "detail": f"the company's own {page.kind} page says so",
            "raw_document_id": page.raw_document_id,
            "fetched_at": page.fetched_at,
            "established_at": page.fetched_at,
        })

    summary = str(payload.get("summary") or "").strip()[:400]
    common = dict(
        service_model=service_model,
        audiences=audiences,
        summary=summary,
        evidence=evidence,
        anomalies=anomalies,
        needs_review=bool(anomalies),
        **verdict_fields,
    )

    if classification == "cannot_tell":
        return cannot_tell("ambiguous_self_description",
                           detail=summary or "the pages do not say what the organisation does",
                           **common)

    supporting = [item for item in evidence if item["supports"] == classification]
    if not supporting:
        return cannot_tell("no_verifiable_evidence",
                           detail="no quote supporting the classification was found on the pages",
                           **common)

    confidence = min(confidence, CONFIDENCE_CAP)
    if len(supporting) == 1:
        confidence = min(confidence, SINGLE_QUOTE_CAP)
    if confidence < AMBIGUOUS_FLOOR:
        # Both sides' quotes are kept: this is the case the job seeker reads
        # and settles, not one the product decides at 0.7.
        return cannot_tell(
            "ambiguous_self_description",
            detail=f"the site reads {classification} at only {confidence:.2f}",
            **common,
        )
    return WebsiteVerdict(kind=classification, confidence=confidence, **common)


# ---------------------------------------------------------------------------
# The LLM half
# ---------------------------------------------------------------------------


def _untrusted_blocks(pages: list[PageRead]) -> dict[str, str]:
    blocks: dict[str, str] = {}
    counts: dict[str, int] = {}
    for page in pages:
        counts[page.kind] = counts.get(page.kind, 0) + 1
        key = page.kind if counts[page.kind] == 1 else f"{page.kind}_{counts[page.kind]}"
        blocks[key] = page.block()
    return blocks


def _model_name(llm: Any, prefer_strong: bool) -> str | None:
    """Which model actually answered, for the audit trail on the verdict row."""
    try:
        return llm.route(LLM_TASK, prefer_strong)[2]
    except Exception:  # noqa: BLE001 - the model name is provenance, not the verdict
        return None


def _llm_available() -> bool:
    settings = get_settings()
    return bool(settings.deepseek_api_key or settings.local_llm_base_url)


def classify_pages(
    company_name: str,
    domain: str,
    pages: list[PageRead],
    *,
    llm: Any | None = None,
    company_id: str | None = None,
    campaign_id: str | None = None,
    job_seeker_id: str | None = None,
) -> WebsiteVerdict:
    """Ask the cheap model what the site says, and validate the answer.

    Blocking, deliberately: ``LLMClient`` is synchronous, and the caller runs
    this in a worker thread so a pass can hold several calls open at once.
    """
    fields = dict(
        domain=domain,
        home_url=pages[0].url if pages else "",
        final_url=pages[0].url if pages else "",
        pages_read=[page.url for page in pages],
        content_hashes={page.url: page.content_hash for page in pages if page.content_hash},
    )
    if not pages:
        return cannot_tell("js_rendered_or_empty", detail="no page carried readable text", **fields)
    if llm is None:
        if not _llm_available():
            return cannot_tell("llm_failed", detail="no LLM is configured", **fields)
        llm = LLMClient(campaign_id=campaign_id, job_seeker_id=job_seeker_id)

    template = load_prompt(PROMPT_NAME)
    system, user = template.render(company_name=company_name, domain=domain)
    blocks = _untrusted_blocks(pages)
    fields |= dict(prompt_template=template.name, prompt_version=template.version)

    attempts = 0
    last_error: Exception | None = None
    # Rule 5 of section 3.4: an answer that is not the shape the prompt asked
    # for is retried once on the strong model, then left cannot_tell.
    for prefer_strong in (False, True):
        attempts += 1
        try:
            payload = llm.complete_json(
                LLM_TASK,
                system=system,
                user=user,
                untrusted=blocks,
                prefer_strong=prefer_strong,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                entity_type="company",
                entity_id=company_id,
                prompt_template=template.name,
                prompt_version=template.version,
            )
            verdict = validate_response(payload, pages, **fields)
            verdict.attempts = attempts
            verdict.model = _model_name(llm, prefer_strong)
            return verdict
        except BudgetExhausted as exc:  # NFR-104: degrade, do not guess
            return cannot_tell("llm_failed", detail=f"token budget exhausted: {exc}"[:200],
                               attempts=attempts, **fields)
        except LLMError as exc:
            last_error = exc
            log.info("employer_kind: %s answered badly for %s (%s)",
                     "strong model" if prefer_strong else "cheap model", domain, exc)

    return cannot_tell("llm_failed", detail=f"{type(last_error).__name__}: {last_error}"[:200],
                       attempts=attempts, **fields)


# ---------------------------------------------------------------------------
# The rung
# ---------------------------------------------------------------------------


async def website_rung(ctx: Any) -> dict[str, Any]:
    """This rung in the ladder's protocol (``employer_resolver.RungContext``).

    The resolver looks its rungs up by name and accepts a plain dict, so this
    adapter deliberately imports nothing from it: a dict cannot go stale
    against another slice's dataclass, and a build without the resolver still
    has a working rung.  Everything the ladder reads - ``kind``, ``confidence``,
    ``rung``, ``method``, ``reason``, ``evidence``, ``anomalies`` and the prompt
    provenance - is in the row.

    Register it with ``employer_resolver.register_rung("website",
    employer_website_rung.website_rung)``, or add this module to that module's
    ``PROVIDER_PATHS``.
    """
    verdict = await classify_from_website(
        ctx.name,
        ctx.domain,
        egress=getattr(ctx, "egress", None),
        llm=getattr(ctx, "llm", None),
        company_id=ctx.company_id or None,
        campaign_id=getattr(ctx, "campaign_id", None),
    )
    return verdict.as_row(ctx.company_id)


async def classify_from_website(
    company_name: str,
    domain: str,
    *,
    egress: Any | None = None,
    llm: Any | None = None,
    company_id: str | None = None,
    campaign_id: str | None = None,
    job_seeker_id: str | None = None,
    limit: int = MAX_SUPPORT_PAGES,
) -> WebsiteVerdict:
    """Read one **confirmed** domain and say what kind of organisation it is.

    Returns a verdict in every case: ``agency``, ``employer``, or
    ``cannot_tell`` with the reason and the cheapest next rung.  The domain is
    not re-derived and not re-confirmed here (registry slice); what this rung
    records about identity is what it observed - the domain asked for, the URL
    that answered, and the pages read.
    """
    if egress is None:
        async with EgressClient() as client:
            return await classify_from_website(
                company_name, domain, egress=client, llm=llm, company_id=company_id,
                campaign_id=campaign_id, job_seeker_id=job_seeker_id, limit=limit,
            )

    pages = await read_pages(domain, egress=egress, limit=limit)
    if pages.refusal:
        return cannot_tell(
            pages.refusal,
            detail=pages.detail,
            domain=pages.domain,
            home_url=pages.home_url,
            final_url=pages.final_url,
        )

    # ``LLMClient`` is synchronous; a pass reading 300 domains wants several
    # of these open at once, so the blocking call goes to a worker thread
    # rather than holding the event loop for its 2.2 seconds.
    verdict = await asyncio.to_thread(
        classify_pages,
        company_name, pages.domain, pages.pages,
        llm=llm, company_id=company_id, campaign_id=campaign_id, job_seeker_id=job_seeker_id,
    )
    verdict.home_url = pages.home_url
    verdict.final_url = pages.final_url or verdict.final_url
    return verdict
