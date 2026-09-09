"""Company domains at scale (FR-301, FR-303, FR-304, FR-305, CR-405, NFR-402).

:mod:`dreamjob.pipeline.apply_contacts` cannot spell an address before it knows
a domain, and 867 of the 1,430 companies that have posted a vacancy carry none:
1,700 vacancies with nothing to apply to, which is the whole distance between
the coverage the Apply Browser shows and the coverage the brief asks for.  The
same gap stops the employer-kind resolver: 499 of its 531 ``cannot_tell``
verdicts are ``no_domain``.  One question blocks both features, so it is asked
once, here, and the answer is written onto the company record where every other
pass already looks for it.

**The ladder, cheapest and most certain first.**

1. ``held_contact`` / ``vacancy_text`` - a domain already implied by data in
   the database: the domain of an address on a contact at this company, or of
   an address the employer itself published in one of its postings.  These cost
   nothing and a person at the employer wrote them, so an address *at the
   employer's own domain* is taken as it stands - exactly as
   :func:`apply_contacts.resolve_domain` takes it.  An address on some other
   domain is not: a posting carries boilerplate as well as its employer, and
   "NATUURWERK VZW" prints ``student@work.be`` - the government's student-jobs
   portal - two lines above ``www.natuurwerk.be``.  Those go to the gate
   (:func:`names_the_company`).
2. ``vacancy_link`` - a bare URL in a posting's own text.  *Not* taken as it
   stands: a posting links partners, press and video as readily as it links the
   employer, so the link only names a candidate and the gate in step 5 decides -
   and for a link the gate is asked the strict question, because a recruiter's
   page about "Stad Oudenaarde" names the city as fluently as the city's own.
3. ``ats_board`` - the board slug on the company record.  Measured on this
   corpus it resolves nothing (eight companies carry a slug and none of them
   lacks a domain), and it is here because it costs one dictionary lookup and
   because the next collection run may fill the column.
4. ``register`` - the Belgian enterprise register's own "Web Address" line,
   read off the enterprise page :mod:`employer_registry_rung` already resolves
   behind its hardened exact-name gate.  Measured over the 391 Belgian
   companies in this pool: the register identified 38 of them strongly enough
   to read a domain off, published a web address for exactly one of those
   (``match-jobs.be`` for "Verzelen Maes Consultants BV"), and that one did not
   survive step 5's gate.  Every page was already in the HTTP cache from the
   employer-kind pass, so the rung costs nothing; it is free rather than
   valuable, and it is kept because the register fills the field over time and
   because a domain the state publishes is better evidence than any spelling.
5. ``derived_confirmed`` - a domain spelled from the company name and then
   **confirmed** against the page it answers with.

**Never invent a domain.**  Step 5 is where a pass like this goes wrong, and it
has gone wrong here before: an earlier gate accepted ``house.com`` for "HOUSE OF
RECRUITMENT SOLUTIONS BV" and ``federale.be`` for a federal ministry, and three
namesake domains had to be deleted from this database.  Two rules keep it
honest, and both are borrowed rather than re-invented:

* the candidate must have a mail exchanger, its home page must answer, and
  :func:`employer_registry_rung.page_identity` - the v2 gate, the same function
  the contact ladder and the re-verification pass judge with - must say the page
  belongs to *this* company;
* a name with fewer than two identifying words gets more, not less.  That is the
  class every namesake in this corpus came from, and 127 of the 473 companies
  with a derived domain are in it.  For those names this module requires the
  gate's ``confirmed`` verdict rather than its ``confirmed``-or-``review``,
  requires the page to show the market itself rather than merely to live on the
  market's top-level domain, and requires a *spelled* candidate to sit on that
  top-level domain at all (:func:`_high_risk`, :func:`_accepts`).  It also never
  spells a *subset* of such a name, so "HOUSE OF RECRUITMENT SOLUTIONS" never
  produces ``recruitment.be``.  The last of those rules was written after this
  pass confirmed ``adequat.eu`` for "Adéquat Belgium NV" - a chestnut fencing
  shop with a Belgian showroom, titled exactly "Adéquat" - which is the fourth
  namesake this corpus has produced and the first one caught before it was
  written down.

A company whose ladder produces nothing is written down as ``unresolved`` with
the reason.  That is a finding, not a failure: it is what stops the next pass
re-deriving the same eight dead spellings, and it is the honest answer.

**What the spellings add over the contact ladder's.**  ``candidate_domains``
tries the tokens run together and the tokens hyphenated.  This module keeps
those and adds three, each aimed at a shape the corpus actually contains: the
German ``ae``/``oe``/``ue``/``ss`` transliteration of a name written with
umlauts ("Rügamer & Steiner" is at ``ruegamer-steiner``, never ``rugamer``); the
name with its thousand-times-repeated words dropped, but **only when two
identifying words survive** ("ISAR Aerospace Technologies" -> ``isaraerospace``,
while "Quality Jobs@work" is left alone because ``quality.be`` is a guess about
whose domain it is); and the first two words of a longer name.  Every one of
them still has to survive the gate.

**FR-305 and CR-402.**  Nothing here fetches by any route but
:class:`~dreamjob.egress.client.EgressClient`, which enforces robots.txt, the
per-domain rate limit and the HTTP cache.  The MX answer is cached by
:func:`email_validate.mx_for` and the per-domain probe verdict by
``apply_domain_probe`` through :mod:`dreamjob.db.repositories.apply` - the same
two caches the contact ladder fills, so a domain judged for one company is not
re-fetched for the next.  What that cache cannot answer is whether a page
belongs to a *particular* company, so a cached ``confirmed`` or ``rejected``
that was recorded against a different company is re-judged rather than reused;
``no_mx`` and ``unreachable`` are about the domain alone and are trusted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from typing import Any

from dreamjob.adapters.registries.kbo import KBO_PUBLIC_SEARCH, KBOAdapter
from dreamjob.adapters.website import crawler
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import apply as apply_repo
from dreamjob.db.repositories import company_domains as repo
from dreamjob.db.repositories import contacts as contacts_repo
from dreamjob.db.repositories import registry_identity as identity_repo
from dreamjob.egress.client import EgressClient, RobotsDisallowed
from dreamjob.pipeline import apply_contacts as ladder
from dreamjob.pipeline import email_patterns as patterns
from dreamjob.pipeline import email_validate as validation
from dreamjob.pipeline.employer_registry_rung import (
    COUNTRY_MARKERS,
    GATE_VERSION,
    country_shown,
    identity_tokens,
    page_identity,
)

log = logging.getLogger(__name__)

# --- which rung answered, stored verbatim on the resolution row ------------
SOURCE_HELD_CONTACT = "held_contact"
SOURCE_VACANCY_TEXT = ladder.SOURCE_VACANCY_TEXT      # "vacancy_text"
SOURCE_VACANCY_LINK = "vacancy_link"
SOURCE_ATS = "ats_board"
SOURCE_REGISTER = "register"
SOURCE_DERIVED = ladder.SOURCE_DERIVED                # "derived_confirmed"

#: How long a company's domain verdict stands before the ladder walks again.
RESOLUTION_MAX_AGE_DAYS = 30

#: The market a board serves, which is evidence about which top-level domains a
#: spelling is worth trying.  It is *not* evidence about where the company is
#: registered, and :func:`market_for` records which of the two it used.
BOARD_MARKETS: dict[str, str] = {
    "www.arbeitnow.com": "DE",
    "arbeitnow.com": "DE",
    "www.arbeitnow.fr": "FR",
    "arbeitnow.fr": "FR",
    "www.arbeitnow.co.uk": "GB",
    "arbeitnow.co.uk": "GB",
    "www.jobat.be": "BE",
    "jobat.be": "BE",
    "www.actiris.be": "BE",
    "actiris.be": "BE",
    "www.vdab.be": "BE",
    "vdab.be": "BE",
    "www.stepstone.de": "DE",
    "stepstone.de": "DE",
}

#: The boards themselves, and the link targets a posting carries that are never
#: the employer's own domain.  ``apply_contacts.AGGREGATOR_DOMAINS`` holds most
#: of them; these are the ones this pass met that it does not - the French
#: Arbeitnow mirror, and the social and video hosts a posting links to.
NEVER_AN_EMPLOYER = frozenset(
    {
        "arbeitnow.fr", "youtube.com", "youtu.be", "facebook.com", "instagram.com",
        "twitter.com", "x.com", "tiktok.com", "bit.ly", "goo.gl", "g.page",
        "paypal.me", "notion.so", "wikipedia.org", "whatsapp.com", "calendly.com",
    }
)

#: The ATS hosts whose slug is the company's board, and the shape of the board
#: URL.  A slug is a *name* the employer chose on somebody else's host, so it
#: is spelled into a candidate and judged like any other (FR-303).
ATS_SLUG_TLDS: tuple[str, ...] = ("com", "io")

#: German names are registered with the umlaut written out.  ``strip_accents``
#: gives "rugamer"; the domain is "ruegamer".
UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                         "Ä": "ae", "Ö": "oe", "Ü": "ue"})

#: A bare URL in a posting's text.  Anchors are not stored, so this is what is
#: left to read.
_URL_IN_TEXT = re.compile(r"(?:https?://|www\.)([a-z0-9](?:[a-z0-9.\-]*[a-z0-9])?\.[a-z]{2,12})", re.I)

#: How many spellings one company is worth.  Sixteen covers the three shapes of
#: a name, run together and hyphenated, on a market's own top-level domain and
#: then on ``.com``; past that the candidates are guesses competing with each
#: other rather than a name being spelled.
MAX_CANDIDATES = 16


# ---------------------------------------------------------------------------
# Which market the spelling runs against
# ---------------------------------------------------------------------------


def _employer_domain(domain: str | None) -> str:
    """The domain, unless it belongs to a board, a mailbox provider or a link.

    :func:`apply_contacts.usable_employer_domain` with this module's own list
    of hosts added, so "arbeitnow.fr" and "youtube.com" never cost a page
    fetch to be told they are not the employer.
    """
    value = ladder.usable_employer_domain(domain)
    if not value:
        return ""
    if value in NEVER_AN_EMPLOYER or crawler.registrable_domain(value) in NEVER_AN_EMPLOYER:
        return ""
    return value


def market_for(company: dict[str, Any], hints: list[dict[str, Any]]) -> tuple[str, str]:
    """``(country, where it came from)`` - company, vacancy, board, or none.

    The market decides which top-level domains a name is spelled on and, in the
    gate, whether the page has to show the country.  123 companies in this pool
    have neither a country on the record nor one on any vacancy, and every one
    of them was collected from a board that serves a single market; saying so -
    and saying that is what it is - is better than spelling every German name
    on ``.com`` alone.
    """
    recorded = str(company.get("company_country") or "").strip().upper()[:2]
    if recorded:
        return recorded, str(company.get("country_source") or "company")
    for hint in hints:
        country = str(hint.get("country") or "").strip().upper()[:2]
        if country:
            return country, "vacancy"
    for host in repo.board_hosts(str(company.get("company_id") or "")):
        market = BOARD_MARKETS.get(host)
        if market:
            return market, "board"
    return "", "none"


# ---------------------------------------------------------------------------
# Spelling a domain from a name
# ---------------------------------------------------------------------------


def _high_risk(name: str) -> bool:
    """Is this the one-identifying-word class that produced every namesake?

    ``identity_tokens`` is the gate's own split into "words of the name" and
    "words that carry identity", so this asks the question in exactly the terms
    the gate will judge it in.
    """
    _, counting = identity_tokens(name or "")
    return len(counting) <= 1


def _shapes(name: str) -> list[list[str]]:
    """The token lists worth spelling for one written form of a name.

    Three shapes, and the order is how much of the name each keeps:

    1. the whole name;
    2. the name with the words thousands of companies share dropped - but only
       when **two** identifying words survive.  One survivor is the
       ``recruitment.be`` shape: a common English word that belongs to whoever
       registered it first, and the very failure this module exists to avoid;
    3. the first two words of a name of three or more, which is how a company
       with a descriptive tail is usually registered.

    A one-identifying-word name gets shape 1 and nothing else.  Spelling a
    *subset* of such a name is how "HOUSE OF RECRUITMENT SOLUTIONS BV" becomes
    ``recruitment.be`` and "ABSOLUTE@WORK BV" becomes ``absolute.be``.
    """
    tokens = ladder.name_tokens(name)
    if not tokens:
        return []
    shapes: list[list[str]] = [tokens]
    if not _high_risk(name):
        _, counting = identity_tokens(name)
        if len(counting) >= 2 and counting not in shapes:
            shapes.append(counting)
        if len(tokens) >= 3 and tokens[:2] not in shapes:
            shapes.append(tokens[:2])
    return shapes


def token_sets(name: str) -> list[list[str]]:
    """Every token list worth spelling, best first.

    :func:`_shapes` applied to the name as written and, when it carries
    umlauts, to the name as German writes it without them - interleaved shape
    by shape, so ``ruegamer-steiner`` is tried beside ``rugamer-steiner``
    rather than after every spelling of the longer name.  ``strip_accents``
    gives "rugamer"; the domain is "ruegamer".
    """
    variants = [name]
    if re.search(r"[\u00e4\u00f6\u00fc\u00df\u00c4\u00d6\u00dc]", name or ""):
        variants.append((name or "").translate(UMLAUTS))
    out: list[list[str]] = []
    for row in zip_longest(*(_shapes(v) for v in variants)):
        for tokens in row:
            if tokens and tokens not in out:
                out.append(tokens)
    return out


def spellings(name: str, country: str = "", *, limit: int = MAX_CANDIDATES) -> list[str]:
    """Domains this company might own, best guess first (CR-405).

    A superset of :func:`apply_contacts.candidate_domains` - the same two
    spellings of the whole name, on the same market top-level domains, plus the
    shapes :func:`token_sets` adds - and it inherits that function's refusals
    verbatim: a name that is already a domain is taken as it is, a single word
    shorter than :data:`apply_contacts.MIN_SINGLE_TOKEN` is a word rather than
    an identity, and a board or aggregator domain is never a candidate.

    The market's own top-level domain is tried for *every* spelling before any
    ``.com`` is tried for the first.  A Belgian company is on ``.be``; a
    ``.com`` bearing its name is as likely to be a parking service or an
    American namesake, and this is the order in which the candidates deserve a
    page fetch.
    """
    out: list[str] = []
    already = ladder.looks_like_domain(name)
    if already and _employer_domain(already):
        out.append(already)
    tlds = ladder.TLDS_BY_COUNTRY.get((country or "").upper()[:2], ladder.DEFAULT_TLDS)
    sets = [
        tokens for tokens in token_sets(name)
        if not (len(tokens) == 1 and len(tokens[0]) < ladder.MIN_SINGLE_TOKEN)
    ]
    for tld in tlds:
        for tokens in sets:
            joined = "".join(tokens)
            hyphenated = "-".join(tokens)
            for base in ([joined] if joined == hyphenated else [joined, hyphenated]):
                # ``MIN_SINGLE_TOKEN`` applied to the *base*, not to a lone
                # token: "P.R.S. NV" is three tokens and spells ``prs``, and a
                # three-letter string is the most contended kind of domain
                # there is - prs.com is titled "PRS" and belongs to somebody
                # else entirely.
                if not ladder.MIN_SINGLE_TOKEN <= len(base) <= 40:
                    continue
                domain = f"{base}.{tld}"
                if domain not in out and _employer_domain(domain):
                    out.append(domain)
    return out[:limit]


def ats_candidates(company: dict[str, Any]) -> list[str]:
    """The board slug, spelled as a domain.

    A Greenhouse or Lever slug is the short name an employer chose for itself,
    which is the same evidence a name is - no more - so it is judged by the same
    gate.  Nothing in this corpus exercises it; it costs a dictionary lookup.
    """
    slug = str(company.get("ats_slug") or "").strip().lower()
    if not slug or "/" in slug or "." in slug:
        return []
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    if len(slug) < ladder.MIN_SINGLE_TOKEN:
        return []
    return [d for d in (f"{slug}.{tld}" for tld in ATS_SLUG_TLDS) if _employer_domain(d)]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class Judgement:
    """One candidate domain, judged for one company."""

    domain: str
    source: str
    outcome: str            # accepted | confirmed | review | rejected | no_mx | unreachable
    reason: str = ""
    rules: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.outcome in ("accepted", "confirmed", "review")

    def as_row(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "source": self.source,
            "outcome": self.outcome,
            "reason": self.reason,
        }


def _accepts(
    name: str,
    domain: str,
    source: str,
    decision_verdict: str,
    page_text: str,
    declared: dict[str, Any],
    market: str,
    on_market_tld: bool,
) -> tuple[bool, str]:
    """The CR-405 acceptance rule, and the extra scrutiny one-word names get.

    For a name with two or more identifying words the rule is the contact
    ladder's, unchanged: ``page_identity`` confirmed it, or flagged it for
    review because the country could not be reconciled - which
    :attr:`IdentityDecision.confirmed` already treats as good enough to go on
    using, and which the re-verification pass leaves standing.

    For a name with fewer - "Adéquat Belgium NV", "DE BRANDT NV", "Alan", the
    class every namesake in this corpus came from - three things are required
    beyond that.

    1. ``review`` is not enough.  The only evidence such a name has is that the
       page calls itself exactly this, so a page that calls itself something
       longer, or sits somewhere the market cannot be seen, decides nothing.
    Before any of that, a *spelled* candidate has to have answered with a page
    that carries text at all: see the ``konverthr.com`` note below.

    2. The market has to be visible **on the page** rather than merely in the
       domain's suffix.  ``brandt.be`` is a Belgian top-level domain whoever
       owns it, so letting the suffix answer the country question would be
       letting the candidate confirm itself.
    3. A domain that was *spelled* must sit on the market's own top-level
       domain.  This is the rule the corpus argues for: every namesake it has
       recorded - ``house.com``, ``adequat.com``, ``brightplus.com``,
       ``think-about-it.com`` - was a generic suffix, and so was the one this
       pass caught while it was being written.  ``adequat.eu`` answers "Adéquat
       levert kastanjehouten hekwerken uit Frankrijk", a chestnut fencing shop
       with a Belgian showroom: it is titled exactly "Adéquat", it shows
       Belgium, and it is not "Adéquat Belgium NV" the staffing agency.  A
       ``.be`` for a Belgian company is a far weaker coincidence than a
       ``.com`` or a ``.eu``, and requiring it costs this pass one confirmation
       in nine and buys the difference between a domain and a namesake.  It
       applies to spellings only: a domain the employer published, or the
       register did, is evidence in its own right and does not rest on the
       suffix.
    """
    if decision_verdict == "rejected":
        return False, "the identity gate rejected the page"
    if source in (SOURCE_VACANCY_LINK, SOURCE_HELD_CONTACT) and not declared.get(
        "identity_equals"
    ):
        # A posting names its employer, and so does the recruiter's page about
        # it: ``jobsolutions.be`` is titled for a staffing agency and its job
        # page names "Stad Oudenaarde" in prose, which the gate's prose rule
        # accepts and which would have addressed the city's application to its
        # agency.  A link is only the employer's own domain when the page it
        # leads to *calls itself* that employer.
        return False, (
            "the page names the company but does not call itself that, so the link "
            "is about the company rather than the company's own domain"
        )
    if source in (SOURCE_DERIVED, SOURCE_ATS) and len(page_text) < ladder.MIN_PAGE_TEXT:
        # A page whose only content is its own title is not evidence about who
        # owns it.  ``konverthr.com`` answers 200 with the title "KonvertHR"
        # and nothing else, which satisfies the gate's identity equality
        # against "KONVERT HR NV" and says nothing at all.  The gate lets a
        # title stand for a page because a *published* domain is being
        # re-checked; a spelled one has to be established, and "we could not
        # read it" - JS-rendered, empty, a holding page - establishes nothing.
        return False, (
            f"the page carries only {len(page_text)} characters of text, which is not "
            "evidence about whose domain this is"
        )
    if not _high_risk(name):
        return decision_verdict in ("confirmed", "review"), ""
    if decision_verdict != "confirmed":
        return False, (
            "the name carries one identifying word, so a 'review' verdict is not "
            "enough to spell addresses on"
        )
    if source in (SOURCE_DERIVED, SOURCE_ATS) and not on_market_tld:
        return False, (
            f"the name carries one identifying word and {domain} is not a "
            f"{market.upper() or 'market'} domain, which is the shape every namesake "
            "in this corpus had"
        )
    if market and COUNTRY_MARKERS.get(market.upper()[:2]):
        if not country_shown(page_text, declared.get("jsonld_country"), market):
            return False, (
                "the name carries one identifying word and the page shows nothing of "
                f"{market.upper()}"
            )
    return True, ""


async def judge(
    domain: str,
    company: dict[str, Any],
    *,
    source: str,
    market: str,
    egress: EgressClient,
) -> Judgement:
    """MX, then the page, then :func:`page_identity` - for *this* company.

    The probe cache in ``apply_domain_probe`` is read first and is trusted for
    the two outcomes that are about the domain alone.  A cached ``confirmed`` or
    ``rejected`` recorded against a different company is not: "does this page
    belong to Acme Trading" and "does it belong to Acme Solutions" are different
    questions with one cache key, and reusing the answer is how a namesake
    spreads.  Re-judging costs nothing the HTTP cache does not already hold.
    """
    name = str(company.get("company_name") or "")
    company_id = str(company.get("company_id") or "")
    domain = _employer_domain(domain)
    if not domain:
        return Judgement(domain, source, "rejected", "not an employer domain")

    cached = await asyncio.to_thread(apply_repo.domain_probe, domain)
    if cached and _fresh(cached) and str(cached.get("outcome") or "") in ("no_mx", "unreachable"):
        return Judgement(
            domain, source, str(cached["outcome"]), str(cached.get("evidence") or "")
        )

    mx = await asyncio.to_thread(validation.mx_for, domain)
    if not mx.has_mx:
        await asyncio.to_thread(
            apply_repo.record_domain_probe, domain, "no_mx",
            company_id=company_id, evidence=mx.error or "no mail exchanger",
        )
        return Judgement(domain, source, "no_mx", mx.error or "no mail exchanger")

    try:
        page = await egress.fetch(f"https://{domain}")
    except RobotsDisallowed as exc:
        # CR-402: not permission to read, so not evidence.  Never worked around.
        return await _record(domain, source, "unreachable", f"robots.txt: {exc}"[:200], company_id)
    except Exception as exc:  # noqa: BLE001 - one dead site must not stop the pass
        return await _record(
            domain, source, "unreachable", f"{type(exc).__name__}: {exc}"[:200], company_id
        )
    if not page.ok:
        return await _record(
            domain, source, "unreachable", f"HTTP {page.status_code}", company_id
        )

    decision = page_identity(
        name,
        page.text,
        domain=domain,
        country=market,
        final_url=getattr(page, "url", "") or "",
        vat_number=str(company.get("vat_number") or company.get("legal_id") or ""),
    )
    accepted, refusal = _accepts(
        name,
        domain,
        source,
        decision.verdict,
        crawler.extract_text(page.text, drop_chrome=False),
        decision.rules,
        market,
        bool(decision.rules.get("on_market_tld")),
    )
    outcome = decision.verdict if accepted else "rejected"
    reason = decision.reason if accepted else (refusal or decision.reason)
    judgement = await _record(domain, source, outcome, reason, company_id)
    judgement.rules = decision.rules
    return judgement


async def _record(
    domain: str, source: str, outcome: str, reason: str, company_id: str
) -> Judgement:
    """Cache what is true of the *domain*; the identity verdict is not.

    ``apply_domain_probe`` is keyed on the domain alone, so only the answers
    that hold whoever is asking belong in it: the name does not resolve, or the
    site did not answer.  "This page belongs to Acme Trading" does not hold for
    Acme Solutions, and writing it under a key that cannot tell them apart is
    how one company's confirmation becomes another company's namesake.  Those
    verdicts go to ``company_domain_candidate``, which is keyed on both
    (migration 120).
    """
    if outcome in ("no_mx", "unreachable"):
        await asyncio.to_thread(
            apply_repo.record_domain_probe, domain, outcome,
            company_id=company_id, evidence=reason,
        )
    return Judgement(domain, source, outcome, reason)


def _fresh(row: dict[str, Any]) -> bool:
    age = _age_days(row.get("last_probed_at"))
    window = (
        ladder.UNREACHABLE_RETRY_DAYS
        if row.get("outcome") == "unreachable"
        else ladder.DOMAIN_PROBE_MAX_AGE_DAYS
    )
    return age < window


def _age_days(timestamp: str | None) -> float:
    if not timestamp:
        return 10**6
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return 10**6
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds() / 86400


# ---------------------------------------------------------------------------
# Rungs 1 to 4: the domains the database and the register already imply
# ---------------------------------------------------------------------------


def names_the_company(domain: str, name: str) -> bool:
    """Does this domain's label carry a word of this company's name?

    The question rung 1 turns on.  ``student@work.be`` is printed in a Belgian
    posting because Student@work is the government's student-jobs portal, not
    because "NATUURWERK VZW" owns ``work.be``; ``info@absolutejobs.be`` is in
    the knowledge base because "ABSOLUTE@WORK BV" trades as Absolute Jobs.
    What separates them is whether the label and the name share a word, so
    that is what is asked - of words of four letters or more, because a
    three-letter coincidence is a coincidence.
    """
    label = crawler.registrable_domain(domain).split(".")[0]
    tokens = [t for t in ladder.name_tokens(name) if len(t) >= 4]
    if not label or not tokens:
        return False
    return any(token in label or label in token for token in tokens)


def held_domains(
    company: dict[str, Any], hints: list[dict[str, Any]]
) -> list[tuple[str, str, str, bool]]:
    """``(domain, source, evidence, trusted)`` for the domains the data implies.

    An address a person at the employer wrote down - one already stored against
    this company, or one in the employer's own posting - is
    :func:`apply_contacts.resolve_domain`'s rung 2, and this is that rung with
    the knowledge base added in front of it.  It uses that module's extraction
    so that "is this an employer's domain" is decided in one place.

    ``trusted`` is what the rung claims: an address *at the employer's own
    domain* closes the ladder without a fetch, and an address on some other
    domain does not.  A posting carries boilerplate as well as its employer -
    "NATUURWERK VZW" prints ``student@work.be``, which is the government's
    student-jobs portal, and prints ``www.natuurwerk.be`` two lines later - so
    an address whose domain does not name the company is handed to the gate as
    a candidate rather than taken as the answer.
    """
    out: list[tuple[str, str, str, bool]] = []
    seen: set[str] = set()
    name = str(company.get("company_name") or "")

    def offer(domain: str, source: str, evidence: str) -> None:
        usable = _employer_domain(patterns.domain_of(domain))
        if usable and usable not in seen:
            seen.add(usable)
            out.append((usable, source, evidence, names_the_company(usable, name)))

    for row in contacts_repo.contacts_for_company(str(company.get("company_id") or "")):
        email = str(row.get("email") or "").strip()
        if email:
            offer(email, SOURCE_HELD_CONTACT, f"an address already held for this company: {email}")

    for address in ladder.addresses_in_vacancies(hints):
        offer(
            address.email, SOURCE_VACANCY_TEXT,
            f"published in the posting as {address.email}",
        )

    careers = _employer_domain(patterns.domain_of(company.get("careers_url")))
    if careers and careers not in seen:
        seen.add(careers)
        out.append((
            careers, SOURCE_HELD_CONTACT, "the careers URL on the company record",
            names_the_company(careers, name),
        ))
    # Trusted first, and among them the order the rungs are in.
    return sorted(out, key=lambda item: not item[3])


def linked_domains(hints: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Bare URLs in the postings' own text, as candidates for the gate.

    A posting links its employer, but it also links YouTube, LinkedIn, a
    partner and the town it is in, so nothing here is trusted: these are
    candidates and :func:`judge` decides, exactly as it does for a spelling.
    """
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for hint in hints:
        text = f"{hint.get('description') or ''}\n{hint.get('application_target') or ''}"
        for match in _URL_IN_TEXT.finditer(text):
            host = match.group(1).lower().strip(".")
            domain = _employer_domain(crawler.registrable_domain(host) or host)
            if domain and domain not in seen:
                seen.add(domain)
                out.append((domain, SOURCE_VACANCY_LINK, f"linked in the posting as {host}"))
    return out[:6]


#: The register's own contact block, in the four languages the page is served
#: in.  ``No data included in CBE`` is the register saying the field is empty.
_REGISTER_WEB = re.compile(
    r"(?:Web\s*Address|Webadres|Adresse\s*web|Web-Adresse)\s*:?\s*\n?\s*([^\n]{3,120})", re.I
)
_REGISTER_EMPTY = ("no data included", "geen gegevens", "aucune donnée", "keine daten")


async def register_domain(
    company: dict[str, Any], *, egress: EgressClient, adapter: KBOAdapter | None = None
) -> tuple[str, str]:
    """The Belgian register's "Web Address" line, or ``("", reason)``.

    The entity is resolved by :meth:`KBOAdapter.match_by_name` - the hardened
    exact-name gate the employer-kind ladder uses - and only an identity strong
    enough to close that ladder is strong enough to hand over a domain: a seat
    the vacancies confirm, or a name of two or more identifying words that
    exactly matches the one entity carrying it.  A one-word name matched
    phonetically is the class that search gets wrong, and a domain read off the
    wrong enterprise page is a domain belonging to somebody else.
    """
    adapter = adapter or KBOAdapter()
    name = str(company.get("company_name") or "")
    try:
        match = await adapter.match_by_name(
            name, egress=egress,
            municipalities=tuple(
                identity_repo.vacancy_places(str(company.get("company_id") or ""))
            ),
            legal_persons_only=True,
        )
    except Exception as exc:  # noqa: BLE001 - a register outage is not a crash
        return "", f"the register did not answer ({type(exc).__name__})"
    if not match.matched:
        return "", f"the register {match.decision}: {match.reason}"[:200]
    strong = match.municipality_checked or (
        match.rule == "exact_name" and len(match.queried_tokens) >= 2
    )
    if not strong:
        return "", (
            f"the register matched {match.name!r} on a {match.rule or 'weak rule'} "
            "without a seat check, which is not an identity to read a domain off"
        )
    try:
        page = await egress.fetch(
            f"{KBO_PUBLIC_SEARCH}?lang=en&ondernemingsnummer={match.number}"
        )
    except Exception as exc:  # noqa: BLE001
        return "", f"the enterprise page did not answer ({type(exc).__name__})"
    if not page.ok:
        return "", f"the enterprise page answered HTTP {page.status_code}"
    found = _REGISTER_WEB.search(crawler.extract_text(page.text, drop_chrome=False))
    raw = (found.group(1).strip() if found else "")
    if not raw or any(marker in raw.lower() for marker in _REGISTER_EMPTY):
        return "", f"the register holds no web address for {match.number}"
    domain = _employer_domain(patterns.domain_of(raw) or raw)
    if not domain:
        return "", f"the register's web address {raw!r} is not an employer domain"
    return domain, (
        f"the Belgian register publishes {raw} for enterprise {match.number} "
        f"({match.name!r}, matched on {match.rule})"
    )


# ---------------------------------------------------------------------------
# One company
# ---------------------------------------------------------------------------


@dataclass
class DomainOutcome:
    """What the ladder concluded about one company."""

    company_id: str
    company_name: str
    vacancy_count: int
    status: str = "unresolved"
    domain: str | None = None
    source: str | None = None
    evidence: str = ""
    market: str = ""
    market_source: str = "none"
    reason: str = ""
    high_risk: bool = False
    judged: list[Judgement] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def rejected_count(self) -> int:
        return sum(1 for j in self.judged if not j.usable)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "vacancy_count": self.vacancy_count,
            "status": self.status,
            "domain": self.domain,
            "source": self.source,
            "evidence": self.evidence,
            "market": self.market,
            "market_source": self.market_source,
            "reason": self.reason,
            "high_risk": self.high_risk,
            "candidates": len(self.judged),
            "rejected": self.rejected_count,
            "judged": [j.as_row() for j in self.judged],
        }


async def resolve_company(
    company: dict[str, Any],
    *,
    egress: EgressClient,
    use_register: bool = True,
    derive: bool = True,
    adapter: KBOAdapter | None = None,
    revoked: set[tuple[str, str]] | None = None,
) -> DomainOutcome:
    """Walk the whole ladder for one company and write down the answer."""
    company_id = str(company.get("company_id") or "")
    name = str(company.get("company_name") or "")
    outcome = DomainOutcome(
        company_id=company_id,
        company_name=name,
        vacancy_count=int(company.get("vacancy_count") or 0),
        high_risk=_high_risk(name),
    )
    hints = await asyncio.to_thread(apply_repo.vacancy_hints, company_id, 6)
    # Rung 1 reads the domain off an address the employer published in its own
    # posting, and the six newest postings are not necessarily where it is: a
    # company with twenty-seven vacancies that printed its address in the tenth
    # of them was invisible to this rung.  The extra rows cost one indexed scan
    # and no network, and they are read *after* the six so the market is still
    # taken from the freshest postings.
    seen = {hint.get("id") for hint in hints}
    hints += [
        row
        for row in await asyncio.to_thread(apply_repo.vacancies_carrying_an_address, company_id)
        if row.get("id") not in seen
    ]
    outcome.market, outcome.market_source = market_for(company, hints)
    forbidden = revoked or set()

    def taken_back(domain: str) -> bool:
        return (company_id, domain) in forbidden

    # 1 - domains the data already implies.  An address at the employer's own
    #     domain closes the ladder here; anything else joins the candidates.
    held: list[tuple[str, str, str]] = []
    for domain, source, evidence, trusted in held_domains(company, hints):
        if taken_back(domain):
            outcome.judged.append(
                Judgement(domain, source, "revoked", "the gate took this domain back before")
            )
            continue
        if trusted:
            outcome.judged.append(Judgement(domain, source, "accepted", evidence))
            return _settle(outcome, domain, source, evidence)
        held.append((domain, source, evidence))

    # 2 to 5 - candidates, every one of them judged.
    candidates: list[tuple[str, str, str]] = held + list(linked_domains(hints))
    candidates += [(d, SOURCE_ATS, "the ATS board slug on the company record")
                   for d in ats_candidates(company)]

    if use_register and (outcome.market or "").upper()[:2] == "BE":
        domain, note = await register_domain(company, egress=egress, adapter=adapter)
        if domain:
            candidates.insert(0, (domain, SOURCE_REGISTER, note))
        else:
            outcome.reason = note

    if derive:
        seen = {d for d, _, _ in candidates}
        for domain in spellings(name, outcome.market):
            if domain not in seen:
                seen.add(domain)
                candidates.append((domain, SOURCE_DERIVED, "spelled from the company name"))

    if not candidates:
        outcome.reason = outcome.reason or _nothing_to_spell(name)
        return _settle(outcome, "", "", "")

    live = await _with_mail_exchanger(candidates, company_id, outcome)
    for domain, source, evidence in live:
        if taken_back(domain):
            outcome.judged.append(
                Judgement(domain, source, "revoked", "the gate took this domain back before")
            )
            continue
        judgement = await judge(
            domain, company, source=source, market=outcome.market, egress=egress
        )
        outcome.judged.append(judgement)
        if judgement.usable:
            note = evidence if source == SOURCE_REGISTER else judgement.reason
            return _settle(outcome, domain, source, note)

    outcome.reason = _refusal(outcome)
    return _settle(outcome, "", "", "")


async def _with_mail_exchanger(
    candidates: list[tuple[str, str, str]], company_id: str, outcome: DomainOutcome
) -> list[tuple[str, str, str]]:
    """The candidates that resolve at all, in order (FR-304, FR-305).

    DNS for every candidate at once, before any page is fetched: four of five
    spellings typically do not exist, and answering that from a cached UDP
    packet removes four of five requests to other people's servers (CR-402).
    ``mx_for`` falls back to the A record as RFC 5321 allows, so ``no_mx`` here
    means the name does not resolve, not that the company cannot receive mail.
    """

    async def check(item: tuple[str, str, str]) -> tuple[tuple[str, str, str], str, str]:
        domain, _, _ = item
        cached = await asyncio.to_thread(apply_repo.domain_probe, domain)
        if cached and _fresh(cached) and cached.get("outcome") in ("no_mx", "unreachable"):
            return item, str(cached["outcome"]), str(cached.get("evidence") or "")
        mx = await asyncio.to_thread(validation.mx_for, domain)
        if mx.has_mx:
            return item, "has_mx", ""
        await asyncio.to_thread(
            apply_repo.record_domain_probe, domain, "no_mx",
            company_id=company_id, evidence=mx.error or "no mail exchanger",
        )
        return item, "no_mx", mx.error or "no mail exchanger"

    results = await asyncio.gather(*(check(c) for c in candidates), return_exceptions=True)
    live: list[tuple[str, str, str]] = []
    for item, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            outcome.judged.append(Judgement(item[0], item[1], "unreachable", "DNS lookup failed"))
            continue
        _, verdict, why = result
        if verdict == "has_mx":
            live.append(item)
        else:
            outcome.judged.append(Judgement(item[0], item[1], verdict, why))
    return live


def _nothing_to_spell(name: str) -> str:
    tokens = ladder.name_tokens(name)
    if not tokens:
        return "the company name carries no word a domain could be spelled from"
    if len(tokens) == 1:
        return (
            f"the company name is one word ({tokens[0]!r}), too short to spell a domain "
            "from without guessing whose it is"
        )
    return "no candidate domain could be spelled from the company name"


def _refusal(outcome: DomainOutcome) -> str:
    counts = Counter(j.outcome for j in outcome.judged)
    named = ", ".join(f"{n} {k}" for k, n in counts.most_common())
    worst = next(
        (j for j in outcome.judged if j.outcome == "rejected"),
        next((j for j in outcome.judged if j.outcome == "review"), None),
    )
    tail = f"; {worst.domain}: {worst.reason}" if worst else ""
    return f"{len(outcome.judged)} candidate domain(s) judged and none confirmed ({named}){tail}"


def _settle(outcome: DomainOutcome, domain: str, source: str, evidence: str) -> DomainOutcome:
    if domain:
        outcome.status = "resolved"
        outcome.domain = domain
        outcome.source = source
        outcome.evidence = evidence
        outcome.reason = evidence
    return outcome


def persist(outcome: DomainOutcome, *, write: bool = True) -> bool:
    """Write the verdict down, and the domain onto the company when there is one.

    Returns whether ``company.domain`` was actually set by this call: a
    concurrent pass may have set it first, and a coverage figure that counted
    both would be wrong.
    """
    wrote = False
    if write and outcome.resolved and outcome.domain:
        wrote = bool(
            repo.set_domain(outcome.company_id, outcome.domain, source=outcome.source or "")
        )
    if write:
        repo.record_candidates(outcome.company_id, [j.as_row() for j in outcome.judged])
        repo.record_resolution(
            {
                "company_id": outcome.company_id,
                "company_name": outcome.company_name,
                "status": outcome.status,
                "domain": outcome.domain,
                "source": outcome.source,
                "evidence": outcome.evidence,
                "market": outcome.market,
                "market_source": outcome.market_source,
                "candidates": len(outcome.judged),
                "rejected": outcome.rejected_count,
                "reason": outcome.reason,
                "gate_version": GATE_VERSION,
                "vacancy_count": outcome.vacancy_count,
            }
        )
    return wrote


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


@dataclass
class DomainPassReport:
    """What one pass achieved, in the terms the brief asks for."""

    requested: int
    companies_visited: int = 0
    companies_resolved: int = 0
    companies_unresolved: int = 0
    domains_written: int = 0
    vacancies_unlocked: int = 0
    candidates_judged: int = 0
    candidates_rejected: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    vacancies_by_source: Counter[str] = field(default_factory=Counter)
    by_candidate_outcome: Counter[str] = field(default_factory=Counter)
    unresolved_reasons: Counter[str] = field(default_factory=Counter)
    high_risk_resolved: list[dict[str, Any]] = field(default_factory=list)
    resolved: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=utcnow)
    finished_at: str = ""

    def absorb(self, outcome: DomainOutcome, wrote: bool) -> None:
        self.companies_visited += 1
        self.candidates_judged += len(outcome.judged)
        self.candidates_rejected += outcome.rejected_count
        for judgement in outcome.judged:
            self.by_candidate_outcome[judgement.outcome] += 1
        if outcome.resolved:
            self.companies_resolved += 1
            self.by_source[outcome.source or "unknown"] += 1
            self.vacancies_by_source[outcome.source or "unknown"] += outcome.vacancy_count
            self.domains_written += 1 if wrote else 0
            if wrote:
                self.vacancies_unlocked += outcome.vacancy_count
            row = {
                "company": outcome.company_name,
                "domain": outcome.domain,
                "source": outcome.source,
                "vacancies": outcome.vacancy_count,
                "market": outcome.market,
                "evidence": outcome.evidence[:160],
            }
            self.resolved.append(row)
            if outcome.high_risk:
                self.high_risk_resolved.append(row)
        else:
            self.companies_unresolved += 1
            self.unresolved_reasons[_reason_bucket(outcome.reason)] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "companies_visited": self.companies_visited,
            "companies_resolved": self.companies_resolved,
            "companies_unresolved": self.companies_unresolved,
            "domains_written": self.domains_written,
            "vacancies_unlocked": self.vacancies_unlocked,
            "candidates_judged": self.candidates_judged,
            "candidates_rejected": self.candidates_rejected,
            "by_source": dict(self.by_source),
            "vacancies_by_source": dict(self.vacancies_by_source),
            "by_candidate_outcome": dict(self.by_candidate_outcome),
            "unresolved_reasons": dict(self.unresolved_reasons.most_common(12)),
            "high_risk_resolved": self.high_risk_resolved,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "corpus": repo.summary(),
        }


def _reason_bucket(reason: str) -> str:
    text = (reason or "").lower()
    if "none confirmed" in text:
        return "candidates judged, none confirmed"
    if "no word a domain" in text:
        return "company name carries no identifying word"
    if "too short to spell" in text:
        return "company name is one short word"
    if "register" in text:
        return "the register could not identify the company"
    if not text:
        return "unknown"
    return text[:80]


def _stale_before(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


async def resolve_domains(
    limit: int = 1000,
    *,
    concurrency: int = 8,
    use_register: bool = True,
    derive: bool = True,
    write: bool = True,
    refresh: bool = False,
    stale_days: int = RESOLUTION_MAX_AGE_DAYS,
    egress: EgressClient | None = None,
) -> DomainPassReport:
    """Give as many domainless companies a domain as can be had honestly.

    Companies are walked biggest vacancy count first, so the budget is spent
    where the corpus is: twenty-seven vacancies behind one name are worth
    twenty-seven single-vacancy names.  They are resolved ``concurrency``-wide
    because every company is a different site; the shared
    :class:`EgressClient` is what keeps that polite, since the per-domain rate
    limit and the robots.txt cache live on the client rather than on the task
    (FR-182, FR-305, CR-402).  The register rung is deliberately *not* run
    concurrently past that: it is one host, and its adapter's rate limit is the
    politeness.

    ``refresh`` walks companies that already carry a verdict; ``stale_days``
    says how old that verdict has to be first, and ``stale_days=0`` walks them
    however fresh it is - which is what a change to the gate calls for, since
    the reason to look again is the rule and not the clock.
    """
    report = DomainPassReport(requested=limit)
    work = await asyncio.to_thread(
        repo.companies_without_domain,
        limit,
        include_resolved=refresh,
        resolved_before=_stale_before(stale_days) if refresh and stale_days > 0 else None,
    )
    if not work:
        report.finished_at = utcnow()
        return report

    revocations = await asyncio.to_thread(identity_repo.revocations, 2000)
    revoked = {
        (str(row.get("company_id") or ""), str(row.get("domain") or "").lower())
        for row in revocations
    }
    adapter = KBOAdapter()
    gate = asyncio.Semaphore(max(1, concurrency))
    owns_client = egress is None
    client = egress or EgressClient(store_raw=False)
    if owns_client:
        await client.__aenter__()
    try:

        async def one(company: dict[str, Any]) -> tuple[DomainOutcome, bool]:
            async with gate:
                try:
                    outcome = await resolve_company(
                        company, egress=client, use_register=use_register,
                        derive=derive, adapter=adapter, revoked=revoked,
                    )
                except Exception as exc:  # noqa: BLE001 - one company never stops a pass
                    log.exception(
                        "Domain resolution failed for company %s", company.get("company_id")
                    )
                    outcome = DomainOutcome(
                        company_id=str(company.get("company_id") or ""),
                        company_name=str(company.get("company_name") or ""),
                        vacancy_count=int(company.get("vacancy_count") or 0),
                        reason=f"resolution failed: {type(exc).__name__}: {exc}"[:200],
                    )
            return outcome, await asyncio.to_thread(persist, outcome, write=write)

        for task in asyncio.as_completed([asyncio.create_task(one(c)) for c in work]):
            outcome, wrote = await task
            report.absorb(outcome, wrote)
    finally:
        if owns_client:
            await client.__aexit__(None, None, None)

    report.finished_at = utcnow()
    log.info(
        "Company domains: %d of %d companies resolved (%d written), %d vacancies unlocked; "
        "%d candidates judged and %d refused by the identity gate",
        report.companies_resolved, report.companies_visited, report.domains_written,
        report.vacancies_unlocked, report.candidates_judged, report.candidates_rejected,
    )
    return report


def run_resolve_domains(limit: int = 1000, **kwargs: Any) -> dict[str, Any]:
    """Synchronous entry point, for scripts and the job runner."""
    return asyncio.run(resolve_domains(limit, **kwargs)).as_dict()
