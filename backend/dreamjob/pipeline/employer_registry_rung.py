"""The registry rung, and the identity gate that makes it safe.

*(FR-341, FR-342, FR-241, FR-181, FR-184, FR-301, FR-304, FR-305, DR-101,
NFR-402, NFR-205, CR-402, CR-405, RK-08)*

The enterprise register is the strongest and the cheapest rung of the
employer-kind ladder, and the only one that can prove what it says.  NACE-BEL
**78.100** (employment placement), **78.200** (temporary employment agency) and
**78.300** (other human-resource provision) are precision **1.00** for a
staffing agency on every labelled set measured - twelve of twelve in
``docs/Agency_Research_Design.md`` appendix A, zero false positives among the
six direct employers that carry no 78 code - and a temporary-employment licence
is a regulated activity in Belgium that nobody registers casually.  Two requests
and about four seconds answer for a company whose website is JavaScript-only,
whose name says nothing and whose adverts never mention a client.

**Once you have the right legal entity.**  That is the whole difficulty, and it
is why this module is as much about identity as about activity codes.  The KBO
answers a *phonetic* name search, and the similarity floor the contact ladder
uses picked the wrong entity **six times in thirty** measured names - SMALS to a
gravel company, "TOURING NV" to "A TOURING COMPANY BV" at similarity 1.00
because "a" is a stopword.  A wrong entity does not produce a wrong verdict
occasionally; it produces a *confident* verdict about a different company, with
that company's accounts, seat and activity codes behind it (CR-405).  So the
gate in :class:`~dreamjob.adapters.registries.kbo.KBOAdapter` is equality, not
similarity, and this module never reaches past it.

Re-measured live against kbopub.economie.fgov.be on 2026-09-09, at the
adapter's 0.5 requests per second:

===============================  =========================================
Name asked for                   What the gate did
===============================  =========================================
NOEL FRANKLIN                    0700.275.068, one ENT row, exact name
100G                             0803.543.941, one ENT row, exact name
VIND                             0455.332.351, one ENT row, exact name
SMALS                            0406.798.006, seat Sint-Gillis confirmed
DE BRANDT                        refused: 20 rows, closest "Brandt"
TOURING                          refused: closest "A TOURING COMPANY BV"
HOUSE OF RECRUITMENT SOLUTIONS   ambiguous: two entities of that name
===============================  =========================================

NOEL FRANKLIN's page carries VAT 2025 **78.200** and **78.100** and NSSO 2025
**78.100**; 100G's carries 70.200/63.100/62.200 and no 78 anywhere.  Both are
the fixtures under ``tests/fixtures/registries``.

What the register cannot do
---------------------------

**"Resolved, and no 78" is not proof of a direct employer.**  It is worth
``-1`` and nothing more (signal D6): 100G, EDITX and CONESSENCE are recruitment
businesses registered as consultancies, and the register's recall on agencies
is 12 of 14 where its precision is 12 of 12.  This module therefore hands down
rather than concluding ``employer``, and the honest shape of that is a rung that
returns no verdict at all.

**Germany has no free activity register.**  The Handelsregister publishes no
activity codes and the Bundesagentur's list of Arbeitnehmerüberlassung licences
is not queryable, so 442 of the corpus's employer names - 38% - skip this rung
entirely and are answered by text, name and the website read.  Saying so is
better than pretending: :func:`registry_rung` returns
``no_activity_register`` for them, and the coverage screen reports it.

**GB and NL** map one-to-one: Companies House ``sic_codes`` 78100/78109/78200/
78300 and KvK ``sbiActiviteiten`` 78.1/78.2/78.3.  Both need a key, and both
adapters already extract the codes.

The namesake gate, hardened (section 3.2 of the research note)
--------------------------------------------------------------

A classifier is only as good as the identity of the page it reads, and the same
is true of an e-mail address.  ``apply_contacts.company_named_on_page`` was
built to keep ``house.com`` away from "HOUSE OF RECRUITMENT SOLUTIONS", which it
does; it does not keep ``brightplus.com`` - a Finnish coatings maker - away from
Bright Plus NV, because the page title "Home - brightplus.com" *does* contain
the words "bright plus".  Three such domains are in the database now, each with
an address spelled on it and each ``derived_confirmed``.

:func:`page_identity` is the gate those need, and it is the gate the contact
ladder now calls:

1. **Token hygiene.**  Words that are common to thousands of company names -
   *about, it, think, group, team, people, work, talent, jobs, plus, digital,
   solutions, consulting, services* - do not count towards identity.  "Bright
   Plus" has one counting token, not two.
2. **A weak name matches identity, not prose.**  With one counting token or
   none, the page's title, ``og:site_name`` or schema.org ``Organization.name``
   must *equal* the company name.  A title segment that is only the domain
   repeats the URL and proves nothing, which is what "Home - brightplus.com"
   is.  A name the page *extends* - "Accent Jobs" against the schema.org name
   "Accent Jobs for People NV" - counts when two identifying words matched in
   order, and otherwise waits for rule 3: "Adéquat, services linguistiques
   inc." is one word and a different trade.
3. **Country consistency.**  When the domain is not on the market's own
   top-level domain, the page should show the market somewhere - country name,
   dialling prefix, ``addressCountry``.  It is a *flag* rather than a refusal,
   because the market on record is where the vacancies are and not always
   where the company is registered; it refuses only the one case where nothing
   else can decide - a weak name the page merely extends, in another country.
   That case is Adéquat: a Belgian company's ``.com`` page in Canadian French.
4. **The enterprise number, when it is printed.**  Five of fifteen Belgian
   sites with a known number print it; a match is conclusive and is recorded.
5. **Redirect discipline.**  A redirect that leaves the domain raises the bar
   to identity rather than deciding on its own: ``eraneos.de`` answers from
   ``eraneos.com`` and is the same organisation, ``think-about-it.com``
   answers from a Hyundai error page and is not, and what separates them is
   whether the page that answered still says it is this company.

Rejections are recorded, never silent: :func:`reverify_derived_domains` walks
the confirmed derived domains, and every one it takes back is written to
``company_domain_revocation`` with its reason and what went with it - the
address included, because an address on a namesake's domain is a real mailbox
at an unrelated organisation (FR-306, RK-08).

Measured on 2026-09-09 against sixty of the corpus's own ``derived_confirmed``
domains - the twenty largest by vacancy count and a random forty - fetched
live:

==========  ===  ==========================================================
Verdict       n  What they were
==========  ===  ==========================================================
confirmed    42  the page names the company
review       15  kept and flagged: twelve are a market mismatch on a ``.com``
                 (gitlab.com under a Polish vacancy, transperfect.com under a
                 British one), three call themselves something longer
                 ("Anssems webshop", "Kovacic Technologies")
rejected      3  ``think-about-it.com`` (answers from a Hyundai error page),
                 ``adequat.com`` (a Canadian translation bureau),
                 ``quantumsystems.de`` ("quantumsystems.de is for sale")
==========  ===  ==========================================================

No correct domain was cleared in the sixty, and ``brightplus.com`` - checked
separately - is refused because its title names it only by repeating the
domain.  The rules that would have cleared correct domains were measured and
softened rather than kept: a redirect to the company's own group domain
(eraneos.de to eraneos.com, statista.de to statista.com, four of sixteen in an
earlier sample) and a country mismatch on an off-market top-level domain are
both flags, not refusals.

Boundaries
----------

All SQL is in :mod:`dreamjob.db.repositories.registry_identity`; every HTTP
request goes through :class:`~dreamjob.egress.client.EgressClient`, which
enforces robots.txt and the per-domain rate limit (FR-182, CR-402); no LLM call
happens here at all.  Register pages and company home pages are untrusted input
(NFR-205) and nothing in this module interprets their prose: the verdict comes
from a NACE code in a labelled row, and the quote stored beside it is verified
back against the page text before it is written.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.registries.kbo import KBO_PUBLIC_SEARCH, EntityMatch, KBOAdapter
from dreamjob.adapters.website import crawler
from dreamjob.db.repositories import registry_identity as repo
from dreamjob.egress.client import EgressClient, RobotsDisallowed
from dreamjob.pipeline.employer_kind import (
    EmployerRole,
    Evidence,
    Kind,
    Reason,
    Rung,
    ServiceModel,
    Verdict,
)

log = logging.getLogger(__name__)

METHOD = "registry_nace"

# ---------------------------------------------------------------------------
# What a 78 code is worth (Agency_Research_Design.md section 2.1)
# ---------------------------------------------------------------------------

#: 78.2 in either 2025 list.  A temporary-employment licence is regulated.
TEMP_AGENCY_CONFIDENCE = 0.97
#: 78.1 or 78.3 in the NSSO 2025 list - the regime that says what a company
#: *employs people for*, which is the question being asked.
NSSO_PROVISION_CONFIDENCE = 0.92
#: 78.1/78.3 only in the VAT list, beside unrelated main codes.  ICTJOB
#: registers 78.100 next to fifteen IT codes and is a job board, so this is an
#: answer that wants corroborating rather than one that closes the ladder.
VAT_ONLY_CONFIDENCE = 0.85
#: A 78 code that exists only in the lapsed 2008/2003 lists is a company that
#: stopped: enough to suspect, not enough to act on.
LAPSED_CODE_CONFIDENCE = 0.70

#: The ceiling on a verdict whose *identity* was matched without the seat
#: cross-check - a one-word name, or a register hit list with no location to
#: check against.  The code is still precision 1.00; what is unproven is that
#: this is the same company, and 0.85 leaves the website and EURES rungs to
#: corroborate rather than closing the ladder on an unconfirmed identity.
UNCHECKED_IDENTITY_CAP = 0.85

#: What "resolved, and no 78 code" is worth to the signal detector: -1, and
#: never a verdict.  CONESSENCE (63.9/64.x) and EDITX (62.x) are recruitment
#: businesses by their own websites.
NO_STAFFING_CODE_POINTS = -1.0

#: Measured on the register sample: precision 12/12, recall 12/14.
R1_PRECISION = 1.00
R1_RECALL = 0.86

#: NACE-BEL / SBI / SIC groups that mean employment activities.
STAFFING_GROUPS: dict[str, tuple[ServiceModel, str]] = {
    "78.1": (ServiceModel.RECRUITMENT_SELECTION, "activities of employment placement agencies"),
    "78.2": (ServiceModel.TEMP_AGENCY, "temporary employment agency activities"),
    "78.3": (ServiceModel.PAYROLLING, "other human resources provision"),
}

_CODE_RE = re.compile(r"^\d{2}\.\d{1,3}$")
#: ``VAT 2025``, ``NSSO2025``, ``BTW 2008``, ``RSZ2025``, ``TVA 2003``, ``ONSS2025``.
_REGIME_RE = re.compile(r"^(VAT|NSSO|BTW|RSZ|TVA|ONSS|MWST|LSS)\s*(2003|2008|2025)$", re.I)
#: The Dutch page puts the regime and the version in two elements, so the text
#: extractor sees "Btw" and "2025" on separate lines and the row would be
#: skipped.  Measured on the same enterprise in all four languages: English 14
#: activities, French 14, Dutch 2 before this, 14 after.
_REGIME_WORD_RE = re.compile(r"^(VAT|NSSO|BTW|RSZ|TVA|ONSS|MWST|LSS)$", re.I)
_VERSION_RE = re.compile(r"^(2003|2008|2025)$")
_REGIME_KIND = {"vat": "vat", "btw": "vat", "tva": "vat", "mwst": "vat",
                "nsso": "nsso", "rsz": "nsso", "onss": "nsso", "lss": "nsso"}

_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# The activity list, as the register actually prints it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Activity:
    """One NACE-BEL row: which regime declared it, in which code version.

    ``KBOAdapter.parse_company_page`` returns a flat sorted set of codes, which
    loses exactly the two distinctions the rules need: VAT (what the company
    invoices) against NSSO (what it employs people for), and 2025 against the
    lapsed 2008 and 2003 lists.  NOEL FRANKLIN's 78.100 in the NSSO 2025 list
    and ICTJOB's 78.100 in the VAT list alone are not the same fact.
    """

    regime: str      # vat | nsso
    version: str     # 2025 | 2008 | 2003
    code: str        # 78.200
    label: str       # "Temporary employment agency activities and other ..."

    @property
    def group(self) -> str:
        """``78.2`` for NACE-BEL 78.200 and for Companies House SIC 78200 alike.

        The code is stored as the register printed it - the KBO dots it, SIC
        and SBI do not - and the rules are written per group, so the dot is put
        back here rather than in the row.
        """
        digits = re.sub(r"\D", "", self.code)
        return f"{digits[:2]}.{digits[2]}" if len(digits) >= 3 else digits

    @property
    def division(self) -> str:
        return re.sub(r"\D", "", self.code)[:2]

    @property
    def current(self) -> bool:
        return self.version == "2025"

    def as_dict(self) -> dict[str, Any]:
        return {"regime": self.regime, "version": self.version,
                "code": self.code, "label": self.label}


def register_text(markup: str) -> str:
    """The register page as readable lines, chrome kept.

    The KBO lays its facts out as label/value rows and its activities over four
    short lines each, so the line structure *is* the parse; dropping the
    navigation would also drop the headings the rows sit under.
    """
    return crawler.extract_text(markup, drop_chrome=False)


def parse_activities(text: str) -> list[Activity]:
    """The activity rows of a KBO enterprise page, in the order it prints them.

    The page lays each row out over four lines - the regime and version, the
    code, a dash, the label - under a "Version of the Nacebel codes for the
    ... activities" heading, in whichever of the four site languages was
    requested.  Reading the regime marker on the row itself rather than the
    heading is what makes this survive a language change.
    """
    lines = _joined_markers([line.strip() for line in (text or "").split("\n")])
    out: list[Activity] = []
    for index, line in enumerate(lines):
        marker = _REGIME_RE.match(line.replace("\xa0", " ").strip())
        if not marker:
            continue
        regime = _REGIME_KIND.get(marker.group(1).lower(), "vat")
        version = marker.group(2)
        code = ""
        label = ""
        for offset in range(1, 5):
            if index + offset >= len(lines):
                break
            candidate = lines[index + offset].replace("\xa0", " ").strip()
            if not code and _CODE_RE.match(candidate):
                code = candidate
                continue
            if code and candidate not in ("-", "–", "—", ""):
                label = candidate
                break
        if code:
            out.append(Activity(regime=regime, version=version, code=code, label=label))
    return out


def _joined_markers(lines: list[str]) -> list[str]:
    """Put a regime marker back together when the page split it over two lines."""
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if _REGIME_WORD_RE.match(line) and _VERSION_RE.match(following):
            out.append(f"{line} {following}")
            index += 2
            continue
        out.append(line)
        index += 1
    return out


def staffing_activities(activities: list[Activity]) -> list[Activity]:
    return [a for a in activities if a.group in STAFFING_GROUPS]


def main_division(activities: list[Activity]) -> str | None:
    """The division of the first current activity - what the company mostly does.

    Used by the EURES rung to discount a section-O filing: a company whose main
    activity is 77/79-82 is in section O because it rents cars or cleans
    buildings, not because it supplies people.
    """
    for activity in activities:
        if activity.current:
            return activity.division
    return activities[0].division if activities else None


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


@dataclass
class RegistryOutcome:
    """What the rung established about one company, verdict or not.

    ``verdict`` is ``None`` whenever the register did not *prove* anything -
    which includes the case where it answered fully and listed no 78 code.
    That is the design's most important negative: a resolved company with no
    staffing code is worth ``-1`` to the detector and is never evidence that
    the company employs its own people.
    """

    company_id: str = ""
    company_name: str = ""
    registry: str = "kbo_bce"
    decision: str = "unavailable"          # matched | ambiguous | no_match | unavailable
    verdict: Verdict | None = None
    match: EntityMatch | None = None
    legal_id: str | None = None
    registered_name: str = ""
    activities: list[Activity] = field(default_factory=list)
    staffing_codes: tuple[str, ...] = ()
    main_division: str | None = None
    identity: dict[str, Any] | None = None  # the DR-101 record, for write-back
    hand_down: str = ""                     # why the next rung has to run
    source_url: str = ""
    duration_ms: int = 0

    @property
    def resolved(self) -> bool:
        return self.decision == "matched" and bool(self.legal_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "registry": self.registry,
            "decision": self.decision,
            "legal_id": self.legal_id,
            "registered_name": self.registered_name,
            "staffing_codes": list(self.staffing_codes),
            "main_division": self.main_division,
            "kind": str(self.verdict.kind) if self.verdict else None,
            "confidence": self.verdict.confidence if self.verdict else None,
            "service_model": str(self.verdict.service_model) if self.verdict else None,
            "hand_down": self.hand_down,
            "activities": [a.as_dict() for a in self.activities],
            "source_url": self.source_url,
            "duration_ms": self.duration_ms,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "").lower()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", folded.replace("\xa0", " ")).strip()


def verify_quote(quote: str, page_text: str) -> bool:
    """Is this fragment really on that page (NFR-402)?

    The same discipline the website rung applies to a model's quotes, applied
    to our own: a label read out of a table is only evidence if it is still
    there when someone looks.  Whitespace and case are normalised; nothing else
    is forgiven.
    """
    needle = _normalise(quote)
    return bool(needle) and needle in _normalise(page_text)


def verdict_from_activities(
    activities: list[Activity],
    *,
    legal_id: str,
    registered_name: str,
    source_url: str,
    page_text: str = "",
    identity_evidence: dict[str, Any] | None = None,
    confidence_cap: float = 1.0,
    registry: str = "kbo_bce",
) -> tuple[Verdict | None, str]:
    """``(verdict, hand-down reason)`` for one resolved company's activity list.

    The rules fire in this order, and the first one that fires decides:

    1. **78.2 in either 2025 list** - temporary employment agency, 0.97.
    2. **78.1 or 78.3 in the NSSO 2025 list** - the employment regime says the
       company employs people to place or supply them, 0.92.
    3. **78.1/78.3 in the VAT 2025 list only** - 0.85, and the website rung
       still runs: ICTJOB's 78.100 sits beside fifteen IT codes.
    4. **78.x only in a lapsed list** - 0.70; a dropped code is a company that
       stopped, and the website decides.
    5. **No 78 anywhere** - no verdict.  The register is silent, not negative.
    """
    staffing = staffing_activities(activities)
    if not staffing:
        codes = ", ".join(sorted({a.code for a in activities})) or "none"
        return None, (
            f"the register lists no 78 activity for {registered_name or legal_id} "
            f"({codes}); that is silence, not a direct employer"
        )

    current = [a for a in staffing if a.current]
    temp = [a for a in current if a.group == "78.2"]
    nsso = [a for a in current if a.regime == "nsso" and a.group in ("78.1", "78.3")]
    vat_only = [a for a in current if a.regime == "vat" and a.group in ("78.1", "78.3")]

    if temp:
        chosen, confidence, rule = temp[0], TEMP_AGENCY_CONFIDENCE, "78.2 in a 2025 list"
    elif nsso:
        chosen, confidence, rule = nsso[0], NSSO_PROVISION_CONFIDENCE, "78.1/78.3 under NSSO 2025"
    elif vat_only:
        chosen, confidence, rule = vat_only[0], VAT_ONLY_CONFIDENCE, "78.1/78.3 under VAT 2025"
    else:
        chosen, confidence, rule = staffing[0], LAPSED_CODE_CONFIDENCE, (
            f"78.x in the lapsed {staffing[0].version} list only"
        )

    service_model = STAFFING_GROUPS[chosen.group][0]
    confidence = min(confidence, confidence_cap)
    evidence: list[Evidence] = []
    for activity in staffing:
        quote = activity.label if (page_text and verify_quote(activity.label, page_text)) else None
        evidence.append(
            Evidence(
                signal="R1",
                supports="agency",
                detail=(
                    f"{registry.upper().replace('_', '/')} NACE-BEL {activity.version} "
                    f"{activity.code} ({activity.regime.upper()}) - "
                    f"{activity.label or STAFFING_GROUPS[activity.group][1]}"
                ),
                url=source_url,
                quote=quote,
                points=3.0,
                established_at=_now(),
            )
        )
    summary = (
        f"The register lists {registered_name or legal_id} under NACE "
        f"{chosen.code} - {STAFFING_GROUPS[chosen.group][1]}."
    )
    verdict = Verdict(
        kind=Kind.AGENCY,
        confidence=confidence,
        rung=Rung.REGISTRY,
        method=METHOD,
        employer_role=EmployerRole.AGENCY,
        service_model=service_model,
        evidence=tuple(evidence),
        identity_evidence=identity_evidence,
        summary=summary,
    )
    # A verdict at the VAT-only tier or below is stored and *also* asks for
    # another rung: ICTJOB registers 78.100 beside fifteen IT codes and is a
    # job board, and a code that survives only in a lapsed list is a company
    # that stopped.  The note is what the pass reports and what the website
    # rung's queue reads; the confidence is what the product acts on.
    hand_down = "" if confidence > VAT_ONLY_CONFIDENCE else (
        f"{rule}: strong enough to store, not strong enough to leave alone; "
        "the website rung should corroborate it"
    )
    return verdict, hand_down


def _group_of(code: str) -> str:
    """``78200`` and ``78.2`` and ``7820`` all name NACE group 78.2."""
    digits = re.sub(r"\D", "", code or "")
    if len(digits) < 3 or not digits.startswith("78"):
        return ""
    return f"78.{digits[2]}"


def activities_from_codes(codes: list[str], *, registry: str, label_source: str) -> list[Activity]:
    """Companies House SIC and KvK SBI codes as activity rows.

    Neither register draws the VAT/NSSO distinction the KBO does, so every code
    arrives as a current row under one regime: the rules then land on the
    NSSO-equivalent tier, which is the honest reading of "this is the activity
    the company is registered for".
    """
    out: list[Activity] = []
    for raw in codes or []:
        group = _group_of(str(raw))
        if not group:
            continue
        out.append(
            Activity(
                regime="nsso",
                version="2025",
                code=str(raw).strip(),
                label=f"{label_source}: {STAFFING_GROUPS[group][1]}",
            )
        )
    return out


# ---------------------------------------------------------------------------
# The rung: two requests on one host
# ---------------------------------------------------------------------------

#: Markets whose register answers activity codes for free, and how.
BE_REGISTRY = "kbo_bce"
#: Markets with a register behind a free key; the adapters already read them.
KEYED_REGISTRIES = {"GB": "companies_house", "UK": "companies_house", "NL": "kvk"}
#: Markets with no free activity register at all.  Saying so is the answer.
NO_REGISTER_NOTE = {
    "DE": (
        "the Handelsregister publishes no activity codes and the Bundesagentur's list of "
        "Arbeitnehmerüberlassung licences is not queryable, so this rung cannot answer for "
        "a German employer; the text and website rungs decide"
    ),
    "AT": "no free activity register",
    "CH": "no free activity register",
}


async def registry_rung(
    company: dict[str, Any],
    *,
    egress: Any = None,
    locations: tuple[str, ...] | list[str] = (),
    adapter: KBOAdapter | None = None,
) -> RegistryOutcome:
    """Resolve one company in its enterprise register and read its activities.

    Two requests on one host - the phonetic search and the enterprise page -
    measured at 4.1 s per name at the adapter's 0.5 requests per second.  The
    rung parallelises across nothing: it is one host, and the rate limit is the
    politeness (FR-182, CR-402).  Everything else in the pass runs beside it.

    ``locations`` are the places this company's vacancies are in, and they are
    used for one thing only: telling two registered entities of the same name
    apart.  When they cannot, the answer is ``ambiguous`` and a person decides.
    """
    started = datetime.now(UTC)
    outcome = RegistryOutcome(
        company_id=str(company.get("company_id") or company.get("id") or ""),
        company_name=str(company.get("company_name") or company.get("name") or ""),
    )
    country = str(company.get("company_country") or company.get("country") or "").upper()[:2]

    if country and country != "BE":
        note = NO_REGISTER_NOTE.get(country)
        if note:
            outcome.registry = "none"
            outcome.decision = "unavailable"
            outcome.hand_down = note
            return outcome
        registry = KEYED_REGISTRIES.get(country)
        if registry:
            outcome.registry = registry
            outcome.decision = "unavailable"
            outcome.hand_down = (
                f"{registry} answers activity codes for {country} but needs a key; "
                "the code path is registry_rung_from_codes()"
            )
            return outcome
        outcome.registry = "none"
        outcome.decision = "unavailable"
        outcome.hand_down = f"no activity register is configured for {country}"
        return outcome

    if not outcome.company_name:
        outcome.hand_down = "the company has no name to ask the register about"
        return outcome

    adapter = adapter or KBOAdapter()
    owns_client = egress is None
    client = egress or EgressClient(store_raw=False)
    try:
        if owns_client:
            await client.__aenter__()
        match = await adapter.match_by_name(
            outcome.company_name,
            egress=client,
            municipalities=tuple(locations),
            legal_persons_only=True,
        )
        outcome.match = match
        outcome.decision = match.decision
        if not match.matched:
            outcome.hand_down = match.reason
            return outcome

        outcome.legal_id = match.number
        outcome.registered_name = match.name
        outcome.source_url = f"{KBO_PUBLIC_SEARCH}?ondernemingsnummer={match.number}"
        page_url = f"{KBO_PUBLIC_SEARCH}?lang=en&ondernemingsnummer={match.number}"
        try:
            page = await client.fetch(page_url)
        except RobotsDisallowed as exc:
            # CR-402: not permission to read, so not evidence.  Never worked around.
            outcome.decision = "unavailable"
            outcome.hand_down = f"robots.txt disallows the register page: {exc}"[:200]
            return outcome
        except Exception as exc:  # noqa: BLE001 - a register outage is not a crash
            outcome.decision = "unavailable"
            outcome.hand_down = f"the register page did not answer ({type(exc).__name__}: {exc})"
            return outcome
        if not page.ok:
            outcome.decision = "unavailable"
            outcome.hand_down = f"the register page answered HTTP {page.status_code}"
            return outcome
    finally:
        if owns_client:
            await client.__aexit__(None, None, None)
        outcome.duration_ms = int(
            (datetime.now(UTC) - started).total_seconds() * 1000
        )

    page_text = register_text(page.text)
    outcome.activities = parse_activities(page_text)
    outcome.staffing_codes = tuple(
        sorted({a.code for a in staffing_activities(outcome.activities)})
    )
    outcome.main_division = main_division(outcome.activities)
    outcome.identity = adapter.parse_company_page(page.text, match.number or "", company)

    identity_evidence = match.as_dict()
    identity_evidence["source_url"] = outcome.source_url
    verdict, hand_down = verdict_from_activities(
        outcome.activities,
        legal_id=match.number or "",
        registered_name=match.name,
        source_url=outcome.source_url,
        page_text=page_text,
        identity_evidence=identity_evidence,
        confidence_cap=identity_cap(match),
    )
    outcome.verdict = verdict
    outcome.hand_down = hand_down or (
        "" if verdict else "the register answered and lists no employment activity"
    )
    return outcome


def identity_cap(match: EntityMatch) -> float:
    """How far a verdict may go on this identity alone.

    Two identities are strong enough to close the ladder: one confirmed by the
    seat, and a name of two or more identifying words matched exactly against
    the only registered entity that carries it.  "NOEL FRANKLIN" is the second
    kind, and it is right that its 78.200 answers at 0.97 even though its
    vacancies are at its clients' sites in Roeselare while its seat is in
    Harelbeke - which is what an agency looks like.

    Everything else is capped: a one-word name ("VIND", "JOBZ", "100G") is the
    class the phonetic search gets wrong, and a registered name that merely
    extends the one asked for is a weaker equality.  The code is still
    precision 1.00; what is unproven is that this is the same company, so the
    website and EURES rungs are left room to corroborate.
    """
    if match.municipality_checked:
        return 1.0
    if match.rule == "exact_name" and len(match.queried_tokens) >= 2:
        return 1.0
    log.info(
        "[registry] %r matched %s on a %s without a seat check; capping at %.2f",
        match.name, match.number, match.rule or "weak rule", UNCHECKED_IDENTITY_CAP,
    )
    return UNCHECKED_IDENTITY_CAP


def registry_rung_from_codes(
    company: dict[str, Any],
    codes: list[str],
    *,
    country: str,
    source_url: str = "",
) -> RegistryOutcome:
    """The GB and NL path: SIC / SBI codes an adapter has already fetched.

    Companies House returns ``sic_codes`` (78100 placement, 78109, 78200
    temporary, 78300 other HR provision) in the company profile and KvK returns
    ``sbiActiviteiten``; both map one-to-one onto the Belgian groups.  Neither
    is behind a name search here - the caller resolved the company through the
    adapter's own lookup - so there is no namesake risk to gate for, and the
    identity evidence says which register answered.
    """
    registry = KEYED_REGISTRIES.get(country.upper(), "companies_house")
    outcome = RegistryOutcome(
        company_id=str(company.get("company_id") or company.get("id") or ""),
        company_name=str(company.get("company_name") or company.get("name") or ""),
        registry=registry,
        decision="matched",
        legal_id=str(company.get("legal_id") or "") or None,
        registered_name=str(company.get("company_name") or company.get("name") or ""),
        source_url=source_url,
    )
    label = "Companies House SIC" if registry == "companies_house" else "KvK SBI"
    outcome.activities = activities_from_codes(codes, registry=registry, label_source=label)
    outcome.staffing_codes = tuple(sorted({a.code for a in outcome.activities}))
    outcome.main_division = main_division(outcome.activities)
    verdict, hand_down = verdict_from_activities(
        outcome.activities,
        legal_id=outcome.legal_id or "",
        registered_name=outcome.registered_name,
        source_url=source_url,
        identity_evidence={"gate": "adapter_lookup", "registry": registry},
        registry=registry,
    )
    outcome.verdict = verdict
    outcome.hand_down = hand_down or (
        "" if verdict else f"{label} lists no 78 code; that is silence, not a direct employer"
    )
    return outcome


def registry_signals(outcome: RegistryOutcome) -> list[Any]:
    """R1 and D6 as the detector's own :class:`Signal` objects.

    ``Signals.with_signals`` is the seam the free-signal detector documents for
    exactly this: the registry pass computes the signals it can afford and the
    band is recomputed over the union.  Imported lazily so this module stands
    on its own if the detector is not installed.

    Called by whatever runs the two passes together rather than by
    :func:`registry_pass` itself, which does not recompute the free signals it
    would have to merge into - the detector already has them, and the register
    outcome it needs is on ``company_registry_identity`` and in the company's
    ``sector_codes`` after this pass has run.
    """
    try:
        from dreamjob.pipeline.employer_signals import (  # noqa: PLC0415 - optional seam
            Evidence as SignalEvidence,
        )
        from dreamjob.pipeline.employer_signals import Signal
    except ImportError:  # pragma: no cover - the detector ships with the same release
        return []

    if outcome.staffing_codes:
        codes = ", ".join(outcome.staffing_codes)
        quotes = tuple(
            SignalEvidence(quote=a.label, url=outcome.source_url, matched=a.code)
            for a in staffing_activities(outcome.activities)
            if a.label
        )
        return [
            Signal(
                id="R1",
                label="The register files this company under employment activities",
                fired=True,
                points=3.0,
                value=float(len(outcome.staffing_codes)),
                detail=(
                    f"{outcome.registry.upper().replace('_', '/')} "
                    f"{outcome.legal_id or ''} carries NACE {codes}".strip()
                ),
                precision=R1_PRECISION,
                recall=R1_RECALL,
                evidence=quotes,
                source=f"registry:{outcome.registry}",
            )
        ]
    if outcome.resolved:
        return [
            Signal(
                id="D6",
                label="The register resolved this company and lists no 78 code",
                fired=True,
                points=NO_STAFFING_CODE_POINTS,
                detail=(
                    "weak evidence only: 100G, EDITX and CONESSENCE are recruitment "
                    "businesses registered as consultancies"
                ),
                precision=None,
                recall=None,
                source=f"registry:{outcome.registry}",
            )
        ]
    return []


def ambiguity_verdict(match: EntityMatch, *, source_url: str = "") -> Verdict:
    """``cannot_tell(registry_ambiguous)`` with both register rows attached.

    Built on demand rather than stored by the pass.  Two registered companies
    share the name; the product's answer is "which one is it?" with the rows
    beside it, and the *website* rung may still settle it - so writing this row
    would stop the ladder rather than move it (see
    ``employer_kind.needs_resolution``).  The product slice stores it when it
    has nothing better to show; ``company_registry_identity`` always keeps it.
    """
    identity = match.as_dict()
    if source_url:
        identity["source_url"] = source_url
    return Verdict(
        kind=Kind.CANNOT_TELL,
        confidence=0.0,
        rung=Rung.REGISTRY,
        method=METHOD,
        employer_role=EmployerRole.UNVERIFIED,
        service_model=ServiceModel.UNKNOWN,
        reason=Reason.REGISTRY_AMBIGUOUS,
        identity_evidence=identity,
        summary=(
            "Two registered companies share this name, so the register cannot say "
            "which one posted these vacancies."
        ),
    )


@dataclass
class RegistryPassReport:
    """What one pass over the register achieved, in the words the screen uses."""

    requested: int = 0
    visited: int = 0
    matched: int = 0
    ambiguous: int = 0
    no_match: int = 0
    unavailable: int = 0
    agencies: int = 0
    silent: int = 0
    verdicts_written: int = 0
    identities_written: int = 0
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    outcomes: list[dict[str, Any]] = field(default_factory=list)

    def absorb(self, outcome: RegistryOutcome) -> None:
        self.visited += 1
        counter = {
            "matched": "matched", "ambiguous": "ambiguous",
            "no_match": "no_match", "unavailable": "unavailable",
        }.get(outcome.decision)
        if counter:
            setattr(self, counter, getattr(self, counter) + 1)
        if outcome.verdict is not None:
            self.agencies += 1
        elif outcome.resolved:
            self.silent += 1
        self.outcomes.append(outcome.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "visited": self.visited,
            "matched": self.matched,
            "ambiguous": self.ambiguous,
            "no_match": self.no_match,
            "unavailable": self.unavailable,
            "agencies": self.agencies,
            "silent": self.silent,
            "verdicts_written": self.verdicts_written,
            "identities_written": self.identities_written,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcomes": self.outcomes,
        }


async def registry_pass(
    limit: int = 200,
    *,
    country: str = "BE",
    refresh: bool = False,
    egress: Any = None,
    write: bool = True,
) -> RegistryPassReport:
    """Walk the employers that have no verdict yet and ask the register.

    Serial by construction: every request goes to one host, and the adapter's
    0.5 requests per second is the politeness, not a throughput setting
    (FR-182, CR-402).  467 Belgian names at 4.1 s each is about 32 minutes,
    once, and then only for names that are new - an enterprise number is a
    fact for the life of the company.
    """
    report = RegistryPassReport(requested=limit)
    work = await asyncio.to_thread(
        repo.companies_for_registry, limit, market=country, refresh=refresh
    )
    if not work:
        report.finished_at = _now()
        return report

    adapter = KBOAdapter()
    owns_client = egress is None
    client = egress or EgressClient(store_raw=False)
    if owns_client:
        await client.__aenter__()
    try:
        for company in work:
            company_id = str(company.get("company_id") or "")
            places = await asyncio.to_thread(repo.vacancy_places, company_id)
            try:
                outcome = await registry_rung(
                    company, egress=client, locations=tuple(places), adapter=adapter
                )
            except Exception as exc:  # noqa: BLE001 - one name never stops a pass
                log.exception("Registry rung failed for %s", company.get("company_name"))
                outcome = RegistryOutcome(
                    company_id=company_id,
                    company_name=str(company.get("company_name") or ""),
                    hand_down=f"the rung failed: {type(exc).__name__}: {exc}"[:200],
                )
            report.absorb(outcome)
            if write and company_id:
                await asyncio.to_thread(_persist, outcome, report)
    finally:
        if owns_client:
            await client.__aexit__(None, None, None)

    report.finished_at = _now()
    log.info(
        "Registry pass: %d visited, %d resolved (%d ambiguous, %d not found), "
        "%d agencies by NACE, %d resolved with no 78 code",
        report.visited, report.matched, report.ambiguous, report.no_match,
        report.agencies, report.silent,
    )
    return report


def _persist(outcome: RegistryOutcome, report: RegistryPassReport) -> None:
    """Write what the rung established.  Identity always; a verdict only if proven."""
    match = outcome.match
    repo.record_identity(
        outcome.company_id,
        {
            "registry": outcome.registry,
            "decision": outcome.decision,
            "legal_id": outcome.legal_id,
            "registered_name": outcome.registered_name,
            "municipality": match.municipality if match else None,
            "postcode": match.postcode if match else None,
            "match_rule": match.rule if match else None,
            "municipality_checked": bool(match and match.municipality_checked),
            "queried_name": outcome.company_name,
            "candidates": list(match.candidates) if match else [],
            "reason": (match.reason if match else outcome.hand_down)[:400],
            "source_url": outcome.source_url,
        },
    )
    if outcome.resolved:
        written = repo.apply_registry_identity(
            outcome.company_id,
            legal_id=outcome.legal_id,
            legal_id_type="kbo_bce" if outcome.registry == BE_REGISTRY else outcome.registry,
            vat_number=f"BE{outcome.legal_id}" if outcome.registry == BE_REGISTRY else None,
            sector_codes=[a.code for a in outcome.activities],
            country="BE" if outcome.registry == BE_REGISTRY else None,
        )
        report.identities_written += int(bool(written))
    _record_attempt(outcome)
    if outcome.verdict is None:
        return
    try:
        from dreamjob.db.repositories import employer_kind as verdicts  # noqa: PLC0415
    except ImportError:  # pragma: no cover - the schema slice ships with this one
        return
    result = verdicts.record_verdict(outcome.company_id, outcome.verdict)
    report.verdicts_written += int(bool(result.get("written")))


def _record_attempt(outcome: RegistryOutcome) -> None:
    """Leave the ladder's trail: what this rung tried, and how far it got.

    ``handed_down`` is the honest outcome for a register that answered and
    listed no 78 code, and ``unavailable`` for a market whose register has no
    activity codes at all - neither is a failure, and neither is evidence
    about the company (migration 111's vocabulary).
    """
    try:
        from dreamjob.db.repositories import employer_resolution  # noqa: PLC0415 - shared trail
    except ImportError:  # pragma: no cover - the signals slice ships with this one
        return
    verdict = outcome.verdict
    if verdict is not None:
        result = "decided"
    elif outcome.decision in ("unavailable",):
        result = "unavailable"
    else:
        result = "handed_down"
    employer_resolution.record_attempt(
        outcome.company_id,
        {
            "rung": "registry",
            "position": 1,
            "method": METHOD,
            "outcome": result,
            "kind": str(verdict.kind) if verdict else None,
            "confidence": verdict.confidence if verdict else None,
            "reason": outcome.hand_down[:400] or None,
            "detail": {
                "registry": outcome.registry,
                "decision": outcome.decision,
                "legal_id": outcome.legal_id,
                "staffing_codes": list(outcome.staffing_codes),
                "main_division": outcome.main_division,
            },
            "duration_ms": outcome.duration_ms,
        },
    )


def run_registry_pass(limit: int = 200, **kwargs: Any) -> dict[str, Any]:
    """Synchronous entry point, for scripts and the job runner."""
    return asyncio.run(registry_pass(limit, **kwargs)).as_dict()


# ---------------------------------------------------------------------------
# The identity gate, hardened (Agency_Research_Design.md section 3.2)
# ---------------------------------------------------------------------------

GATE_VERSION = "v2"

#: Words that thousands of companies have in their names.  They are not removed
#: from the *match* - "Bright Plus" still has to appear - they are removed from
#: the count of what makes this company **identifiable**, which decides how
#: strong the evidence has to be.  Nine of the corpus's confirmed derived
#: domains sit on names made only of these ("The Rec Hub", "LET'S WORK",
#: "think about IT").
COMMON_WORD_TOKENS = frozenset(
    """
    about it think group team people work works working talent talents jobs job
    plus digital solutions solution consulting consultancy services service systems
    tech technology technologies partners partner company companies center centre
    global online media agency studio studios lab labs house net web software
    industries industrie industries international national europe european
    """.split()
)

#: The market's own top-level domain.  When a company sits on one of these the
#: country question is already answered; when it sits on a ``.com`` or a
#: ``.eu`` - 96 of the corpus's derived domains do - the page has to show the
#: market itself.
MARKET_TLD = {
    "BE": ("be",), "NL": ("nl",), "LU": ("lu",), "DE": ("de",), "AT": ("at",),
    "CH": ("ch",), "FR": ("fr",), "GB": ("uk", "co.uk"), "UK": ("uk", "co.uk"),
    "IE": ("ie",), "ES": ("es",), "IT": ("it",), "PL": ("pl",), "SE": ("se",),
    "DK": ("dk",), "NO": ("no",), "FI": ("fi",), "PT": ("pt",), "US": ("us",),
    "CA": ("ca",),
}

#: How a page shows which country it is in: the country's name in the languages
#: it is written in, and its dialling prefix.
COUNTRY_MARKERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "BE": (("belgium", "belgie", "belgique", "belgien"), ("+32", "0032")),
    "NL": (("netherlands", "nederland", "pays-bas", "niederlande"), ("+31", "0031")),
    "DE": (("germany", "deutschland", "allemagne", "duitsland"), ("+49", "0049")),
    "FR": (("france", "frankreich", "frankrijk"), ("+33", "0033")),
    "GB": (("united kingdom", "great britain", "england", "scotland", "wales"), ("+44",)),
    "UK": (("united kingdom", "great britain", "england", "scotland", "wales"), ("+44",)),
    "LU": (("luxembourg", "luxemburg"), ("+352",)),
    "AT": (("austria", "osterreich"), ("+43",)),
    "CH": (("switzerland", "schweiz", "suisse", "svizzera"), ("+41",)),
    "IE": (("ireland",), ("+353",)),
    "ES": (("spain", "espana", "espagne"), ("+34",)),
    "IT": (("italy", "italia", "italie"), ("+39",)),
    "PL": (("poland", "polska"), ("+48",)),
    "SE": (("sweden", "sverige"), ("+46",)),
    "DK": (("denmark", "danmark"), ("+45",)),
    "NO": (("norway", "norge"), ("+47",)),
    "FI": (("finland", "suomi"), ("+358",)),
    "PT": (("portugal",), ("+351",)),
    "US": (("united states", "usa"), ()),
    "CA": (("canada",), ()),
}

#: Placeholder wording the contact ladder's own marker list does not carry.
#: ``quantumsystems.de``, a confirmed derived domain in the corpus, answers
#: "quantumsystems.de is for sale" - which is a parking page whatever the
#: registrar calls it, and worth saying so rather than reporting it as a page
#: that failed to name the company.
EXTRA_PARKING_MARKERS = (" is for sale", "is te koop", "zu verkaufen", "a vendre", "à vendre")

#: Title separators.  A comma is deliberately not one: "Adéquat, services
#: linguistiques inc." is a Canadian translation bureau's whole name, not
#: "Adéquat" followed by a tagline.
_TITLE_SPLIT = re.compile(r"\s+[|·•–—]\s+|\s+[-–—]\s+|\s*\|\s*|:\s+")
_DOMAINISH = re.compile(r"^(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/?$")
_VAT_DIGITS = re.compile(r"\d{9,10}")
#: Links and addresses, removed before the prose is searched: a domain quoting
#: itself in its own footer proves only that the page knows its own URL, and
#: counting it would make every candidate confirm itself.
_URLISH_RE = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+", re.IGNORECASE)


@dataclass(frozen=True)
class IdentityDecision:
    """Whether this page belongs to this company, and what said so (NFR-402).

    Three verdicts, because two would force a wrong answer.  ``rejected`` is
    the page of a different organisation.  ``review`` is a page that names the
    company but whose country the gate could not reconcile: strong enough to go
    on using - it names the company, in its own words - and worth a person's
    glance before anything irreversible rests on it.
    """

    verdict: str                       # confirmed | review | rejected
    reason: str
    rules: dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.verdict in ("confirmed", "review")

    @property
    def rejected(self) -> bool:
        return self.verdict == "rejected"


def _ladder() -> Any:
    """The contact ladder's constants, imported late to keep the cycle out.

    ``apply_contacts`` calls :func:`page_identity`, so this module cannot
    import it at module scope; by the time any of these functions runs, it is
    loaded.
    """
    from dreamjob.pipeline import apply_contacts  # noqa: PLC0415 - deliberate late import

    return apply_contacts


def identity_tokens(name: str) -> tuple[list[str], list[str]]:
    """``(all identifying tokens, the ones that carry identity on their own)``."""
    tokens = _ladder().name_tokens(name)
    counting = [t for t in tokens if t not in COMMON_WORD_TOKENS]
    return tokens, counting


def _identity_strings(html: str, domain: str) -> tuple[list[str], dict[str, Any]]:
    """What the page says it *is*: title segments, og:site_name, schema.org name.

    A segment that is only the domain is dropped.  "Home - brightplus.com"
    names the company by repeating the URL, which is the circular evidence that
    confirmed a Finnish coatings maker as a Belgian staffing agency.
    """
    title = crawler.page_title(html)
    jsonld = crawler.identity_from_jsonld(crawler.organisation_jsonld(html)) or {}
    site_name = ""
    match = re.search(
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{1,120})',
        html or "", re.I,
    )
    if match:
        site_name = match.group(1)

    registrable = crawler.registrable_domain(domain) if domain else ""
    candidates = [*(_TITLE_SPLIT.split(title) if title else []), site_name,
                  str(jsonld.get("name") or ""), str(jsonld.get("legal_name") or "")]
    strings: list[str] = []
    for candidate in candidates:
        text = (candidate or "").strip()
        if not text:
            continue
        squashed = _normalise(text).replace(" ", "")
        if _DOMAINISH.match(_normalise(text).replace(" ", "")) or (
            registrable and squashed in (registrable, registrable.replace(".", ""))
        ):
            continue  # the page repeating its own URL is not identity
        strings.append(text)
    evidence = {
        "title": title[:200],
        "og_site_name": site_name[:200] or None,
        "jsonld_name": (jsonld.get("name") or jsonld.get("legal_name") or None),
        "jsonld_country": next(
            (loc.get("country") for loc in jsonld.get("locations") or [] if loc.get("country")),
            None,
        ),
    }
    return strings, evidence


def _identity_equals(name: str, strings: list[str]) -> str:
    """The identity string that *is* this company's name, or ""."""
    tokens = _ladder().name_tokens(name)
    wanted = "".join(tokens)
    if not wanted:
        return ""
    for candidate in strings:
        if "".join(_ladder().name_tokens(candidate)) == wanted:
            return candidate
    return ""


def _identity_extends(name: str, strings: list[str]) -> str:
    """The identity string that *begins* with this company's name, or "".

    The same rule the register's gate applies to "ACCENT Jobs" against the
    registered "ACCENT Jobs For People", and it is worth exactly as much here:
    on its own it is not identity.  Measured on sixty of the corpus's derived
    domains, this is the difference between accentjobs.be - whose schema.org
    name is "Accent Jobs for People NV", the same company - and adequat.com,
    whose title is "Adéquat, services linguistiques inc.", a Canadian
    translation bureau.  Which of the two a page is cannot be read off the
    prefix, so what a prefix is worth is decided in :func:`page_identity`, by
    how many words matched and by whether the country agrees.
    """
    wanted = _ladder().name_tokens(name)
    if not wanted:
        return ""
    for candidate in strings:
        tokens = _ladder().name_tokens(candidate)
        if len(tokens) > len(wanted) and tokens[: len(wanted)] == wanted:
            return candidate
    return ""


def country_shown(text: str, jsonld_country: str | None, market: str) -> bool:
    """Does the page show the market it is supposed to be in?"""
    market = (market or "").upper()[:2]
    names, prefixes = COUNTRY_MARKERS.get(market, ((), ()))
    if not names and not prefixes:
        return True  # no marker vocabulary: nothing to disprove
    declared = _normalise(str(jsonld_country or ""))
    if declared and (declared == market.lower() or any(n in declared for n in names)):
        return True
    lowered = _normalise(text)
    words = set(_WORD_RE.findall(lowered))
    for marker in names:
        if (" " in marker or len(marker) >= 5) and marker in lowered:
            return True
        if marker in words:
            return True
    return any(prefix in lowered.replace(" ", "") for prefix in prefixes)


def page_identity(
    name: str,
    html: str,
    *,
    domain: str = "",
    country: str = "",
    final_url: str = "",
    vat_number: str = "",
) -> IdentityDecision:
    """Does this page belong to this company?  The gate the derivation rests on.

    Ordered so that the cheapest and most conclusive refusals come first, and
    so that no rule can be satisfied by the page quoting its own URL.  See the
    module docstring for what each rule is for and which live domain it was
    written against.
    """
    ladder = _ladder()
    tokens, counting = identity_tokens(name)
    rules: dict[str, Any] = {
        "gate": GATE_VERSION,
        "tokens": tokens,
        "counting_tokens": counting,
        "country": country or None,
        "domain": domain or None,
    }
    if not tokens:
        return IdentityDecision(
            "rejected", "the company name carries no identifying word", rules
        )

    # 5. Redirect discipline.  A redirect off the domain is not by itself a
    #    refusal: measured live on the corpus's own derived domains,
    #    eraneos.de answers from eraneos.com, statista.de from statista.com,
    #    multiverse.com from multiverse.io and robco.de from rob.co - four
    #    organisations redirecting to their own primary domain.
    #    think-about-it.com answers from a Hyundai error page.  What separates
    #    them is not the redirect, it is whether the page that answered still
    #    says it is this company, so an off-domain redirect *raises the bar to
    #    identity* rather than deciding on its own.
    redirected_off_label = False
    if final_url and domain:
        final_domain = crawler.registrable_domain(urlparse(final_url).netloc)
        wanted = crawler.registrable_domain(domain)
        rules["final_domain"] = final_domain
        if final_domain and wanted and final_domain != wanted:
            rules["redirected"] = True
            label = wanted.split(".")[0]
            redirected_off_label = not (label and label == final_domain.split(".")[0])

    title = crawler.page_title(html)
    text = crawler.extract_text(html, drop_chrome=False)
    strings, identity_evidence = _identity_strings(html, domain)
    rules.update(identity_evidence)
    declared = " ".join(strings)
    lowered = _normalise(f"{title}\n{declared}\n{text}")

    for marker in (*ladder.PARKING_MARKERS, *EXTRA_PARKING_MARKERS):
        if marker in lowered:
            return IdentityDecision("rejected", f"the page is a placeholder ({marker!r})", rules)
    if len(text) < ladder.MIN_PAGE_TEXT and not declared:
        return IdentityDecision(
            "rejected", f"the page carries only {len(text)} characters of text", rules
        )

    # 4. The enterprise number, when the site prints it: conclusive.
    wanted_digits = _VAT_DIGITS.search(re.sub(r"\D", "", vat_number or ""))
    if wanted_digits:
        digits = wanted_digits.group(0).zfill(10)
        printed = {d.zfill(10) for d in _VAT_DIGITS.findall(re.sub(r"[.\s-]", "", text))}
        rules["vat_on_site"] = digits in printed
        if digits in printed:
            return IdentityDecision(
                "confirmed", f"the page prints enterprise number {digits}", rules
            )

    # 3. Country consistency.  Measured here, used twice below; what it is
    # worth is argued where it is applied.
    market_tlds = MARKET_TLD.get((country or "").upper()[:2], ())
    on_market_tld = bool(domain) and any(domain.lower().endswith("." + t) for t in market_tlds)
    country_ok: bool | None = None
    if country and COUNTRY_MARKERS.get(country.upper()[:2]):
        country_ok = True if on_market_tld else country_shown(
            f"{text}\n{declared}", identity_evidence.get("jsonld_country"), country
        )
    rules["country_ok"] = country_ok
    rules["on_market_tld"] = on_market_tld

    equal = _identity_equals(name, strings)
    extends = "" if equal else _identity_extends(name, strings)
    rules["identity_equals"] = equal or None
    rules["identity_extends"] = extends or None
    prose = _URLISH_RE.sub(" ", lowered)
    words = set(_WORD_RE.findall(prose))
    squashed = "".join(_WORD_RE.findall(prose))
    named_in_prose = "".join(tokens) in squashed or len(
        [t for t in tokens if t in words]
    ) >= (len(tokens) if len(tokens) <= 2 else max(2, len(tokens) - 1))
    rules["named_in_prose"] = named_in_prose

    # 1, 2 and 5.  Two ways a page has to prove identity rather than mention
    # the name: a name with fewer than two words of its own - the class that
    # produced every namesake in the corpus - and a page reached by a redirect
    # to a differently-named domain.
    strict = redirected_off_label or len(counting) <= 1
    rules["rule"] = "identity_equality" if strict else "named_on_page"
    if strict and not equal and extends:
        # The page calls itself something that *starts* with this company's
        # name.  Two identifying words matched in order is a company naming
        # itself in full ("Accent Jobs" -> "Accent Jobs for People NV"); one
        # word followed by a different trade is the namesake shape, and then
        # the country decides - a Belgian company's page in Canadian French is
        # somebody else, and a page that agrees with the market is a person's
        # call rather than a domain to take away.
        if len(tokens) >= 2:
            equal = extends
        elif country_ok is False:
            return IdentityDecision(
                "rejected",
                (
                    f"the page calls itself {extends!r}, which is not {' '.join(tokens)!r}, "
                    f"and shows nothing of {country.upper()}"
                ),
                rules,
            )
        else:
            return IdentityDecision(
                "review",
                (
                    f"the page calls itself {extends!r}, which extends "
                    f"{' '.join(tokens)!r} rather than matching it"
                ),
                rules,
            )
    if strict and not equal:
        if redirected_off_label:
            return IdentityDecision(
                "rejected",
                (
                    f"the page does not belong to {domain}: it answers from "
                    f"{rules.get('final_domain')} and calls itself {title[:60]!r}"
                ),
                rules,
            )
        return IdentityDecision(
            "rejected",
            (
                f"the page does not name the company: {' '.join(tokens)!r} is not what "
                f"the page calls itself ({title[:60]!r})"
            ),
            rules,
        )
    if not strict and not named_in_prose and not equal:
        matched = [t for t in tokens if t in words]
        return IdentityDecision(
            "rejected",
            f"the page does not name the company (matched {len(matched)} of {len(tokens)} words)",
            rules,
        )

    # 3. Country consistency, and why it is a flag rather than a refusal.  The
    # market on record is where the *vacancies* are, which is not always where
    # the company is registered: measured on sixteen of the corpus's derived
    # domains, a hard country rule would have cleared four correct ones -
    # "Auvaria Group GmbH" prints its legal form and no German address,
    # kinly.com and xsolla.com are foreign companies advertising British jobs.
    # None of the three known namesakes needs it: each fails identity first.
    # So a country mismatch is recorded, shown for review, and clears nothing.
    named = equal or " ".join(tokens)
    if country_ok is False:
        return IdentityDecision(
            "review",
            (
                f"the page calls itself {named!r} but shows nothing of {country.upper()}, "
                f"and {domain} is not a {country.upper()} domain"
            ),
            rules,
        )
    if strict:
        return IdentityDecision("confirmed", f"the page is titled for {equal!r}", rules)
    return IdentityDecision("confirmed", f"the page names {' '.join(tokens)!r}", rules)


# ---------------------------------------------------------------------------
# Re-verifying the domains the old gate confirmed
# ---------------------------------------------------------------------------

#: Reasons a domain is taken back, in the vocabulary the revocation row holds
#: and in the order :func:`_revocation_reason` decides them.
REVOKE_REASONS = (
    "off_domain_redirect", "parked", "country_mismatch", "js_rendered_or_empty", "namesake",
)


@dataclass
class ReverifyReport:
    """What re-verification did to the confirmed derived domains."""

    checked: int = 0
    kept: int = 0
    cleared: int = 0
    flagged: int = 0
    unreachable: int = 0
    contacts_removed: int = 0
    dry_run: bool = False
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "kept": self.kept,
            "cleared": self.cleared,
            "flagged": self.flagged,
            "unreachable": self.unreachable,
            "contacts_removed": self.contacts_removed,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "rows": self.rows,
        }


def _revocation_reason(reason: str) -> str:
    """The revocation vocabulary for a refusal, read off the reason it carries.

    ``namesake`` is the default because it is what a page that simply does not
    say it is this company amounts to: somebody else's site, spelled from the
    same name.
    """
    detail = (reason or "").lower()
    if "answers from" in detail:
        return "off_domain_redirect"
    if "placeholder" in detail:
        return "parked"
    if "shows nothing of" in detail:
        return "country_mismatch"
    if "characters of text" in detail:
        return "js_rendered_or_empty"
    return "namesake"


async def reverify_derived_domains(
    limit: int = 1000,
    *,
    egress: Any = None,
    clear: bool = True,
    concurrency: int = 8,
    sources: tuple[str, ...] = ("derived_confirmed",),
) -> ReverifyReport:
    """Re-read every spelled-and-confirmed domain through the v2 gate.

    A domain that fails is *taken back*, not merely re-labelled: the address
    on it, the company's ``domain``, the FR-301 resolution and the FR-305
    probe verdict all go together, and a row in ``company_domain_revocation``
    says what happened and why (see
    :func:`dreamjob.db.repositories.registry_identity.revoke_domain`).

    A domain that does not answer is *kept*.  The network is not evidence
    about who owns a name, and clearing on a timeout would empty the contact
    ladder every time a site was slow.  ``review`` verdicts are counted and
    listed rather than cleared: the page names the company but sits on an
    off-market domain that shows nothing of the market, or calls itself
    something longer than the name asked for.  Fifteen of sixty sampled
    domains are in that state, and every one of them is a person's call and
    not a domain to take away.

    Fetches run ``concurrency``-wide because every row is a different domain;
    the shared :class:`EgressClient` keeps each one to 0.5 requests per second
    and honours its robots.txt (FR-182, FR-305, CR-402).
    """
    report = ReverifyReport(dry_run=not clear)
    rows = await asyncio.to_thread(repo.derived_domains, limit, sources=sources)
    if not rows:
        report.finished_at = _now()
        return report

    gate = asyncio.Semaphore(max(1, concurrency))
    owns_client = egress is None
    client = egress or EgressClient(store_raw=False)
    if owns_client:
        await client.__aenter__()
    try:

        async def one(row: dict[str, Any]) -> dict[str, Any]:
            domain = str(row.get("domain") or "").strip().lower()
            name = str(row.get("company_name") or "")
            country = str(row.get("country") or "").upper()[:2]
            record = {
                "company_id": row.get("company_id"),
                "company_name": name,
                "domain": domain,
                "country": country or None,
                "vacancy_count": int(row.get("vacancy_count") or 0),
                "email": row.get("email"),
            }
            async with gate:
                try:
                    page = await client.fetch(f"https://{domain}")
                except RobotsDisallowed as exc:
                    return {**record, "outcome": "unreachable", "reason": f"robots.txt: {exc}"[:200]}
                except Exception as exc:  # noqa: BLE001 - one dead site stops nothing
                    return {
                        **record, "outcome": "unreachable",
                        "reason": f"{type(exc).__name__}: {exc}"[:200],
                    }
            if not page.ok:
                return {**record, "outcome": "unreachable", "reason": f"HTTP {page.status_code}"}
            decision = page_identity(
                name,
                page.text,
                domain=domain,
                country=country,
                final_url=getattr(page, "url", "") or "",
                vat_number=str(row.get("legal_id") or ""),
            )
            return {
                **record,
                "outcome": decision.verdict,
                "reason": decision.reason,
                "rules": decision.rules,
            }

        results = await asyncio.gather(*(one(row) for row in rows))
    finally:
        if owns_client:
            await client.__aexit__(None, None, None)

    for result in results:
        report.checked += 1
        outcome = result["outcome"]
        if outcome == "unreachable":
            report.unreachable += 1
        elif outcome == "confirmed":
            report.kept += 1
        elif outcome == "review":
            report.flagged += 1
        elif outcome == "rejected":
            report.cleared += 1
            if clear:
                reason = _revocation_reason(str(result.get("reason") or ""))
                written = await asyncio.to_thread(
                    repo.revoke_domain,
                    result.get("company_id"),
                    company_name=result.get("company_name") or "",
                    domain=result.get("domain") or "",
                    reason=reason,
                    evidence=str(result.get("reason") or ""),
                    previous_source="derived_confirmed",
                )
                report.contacts_removed += int(written.get("contacts_removed") or 0)
                result["revoked_as"] = reason
        report.rows.append(result)

    report.finished_at = _now()
    log.info(
        "Derived-domain re-verification: %d checked, %d kept, %d cleared, %d flagged, "
        "%d unreachable (%d addresses removed)%s",
        report.checked, report.kept, report.cleared, report.flagged, report.unreachable,
        report.contacts_removed, " [dry run]" if report.dry_run else "",
    )
    return report


def run_reverify_derived_domains(limit: int = 1000, **kwargs: Any) -> dict[str, Any]:
    """Synchronous entry point, for scripts and the job runner."""
    return asyncio.run(reverify_derived_domains(limit, **kwargs)).as_dict()
