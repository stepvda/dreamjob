"""KBO/BCE - the Belgian enterprise register (FR-241, FR-181, DR-101).

Belgium is the primary jurisdiction, and the KBO/BCE enterprise number is the
identity anchor everything else hangs from: the NBB Central Balance Sheet
Office is keyed on it, the VAT number is ``BE`` plus the same ten digits, and
DR-101 names it first in the key priority order.  Getting it right once means
the de-duplicator (FR-184) never has to guess again.

The register has no free JSON API, so two public sources are used in order:

1. **KBO Public Search** - the register's own page for an enterprise number.
   It carries everything DR-101 wants: legal name, legal form, status, seat
   address and NACE activities.
2. **VIES** - the European Commission's VAT validation service, which returns
   the registered name and address as JSON for any EU VAT number.  It is the
   fall-back when the KBO page is unreachable or robots.txt disallows it, and
   it independently corroborates the name.

Name search is supported the same way the register supports it - phonetic
search on the public site - and is deliberately not the primary path: a name
match is the weakest DR-101 key.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from dreamjob.adapters.base import AccessMethod, AdapterCapabilities, SourceType, register_adapter
from dreamjob.adapters.registries.common import (
    DEFAULT_YEARS,
    RegistryAdapter,
    RegistryResult,
    RegistryWall,
    enterprise_number,
    format_enterprise_number,
    identity_record,
)
from dreamjob.egress.client import RobotsDisallowed
from dreamjob.pipeline import html_dom
from dreamjob.pipeline.dedup import company_similarity, tokens

log = logging.getLogger(__name__)

KBO_PUBLIC_SEARCH = "https://kbopub.economie.fgov.be/kbopub/toonondernemingps.html"
KBO_NAME_SEARCH = "https://kbopub.economie.fgov.be/kbopub/zoeknaamfonetischform.html"
VIES_CHECK = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{number}"

#: The public search form is a GET form with paired checkbox markers: Spring
#: answers 404 when the ``_name=on`` half of a checkbox is missing, so the whole
#: set is sent exactly as the form posts it.
NAME_SEARCH_FORM = (
    "oudeBenaming=true&_oudeBenaming=on"
    "&ondNP=true&_ondNP=on"
    "&ondRP=true&_ondRP=on"
    "&vest=true&_vest=on"
    "&filterEnkelActieve=true&_filterEnkelActieve=on"
    "&actionNPRP=Search"
)

# ---------------------------------------------------------------------------
# The exact-name gate (DR-101, FR-184, CR-405)
# ---------------------------------------------------------------------------
#
# The register answers a *phonetic* search, and ``company_similarity`` strips
# "de", "the" and the legal form before comparing, so several rows on a hit
# list score 1.00 against a name that is not theirs.  Measured on thirty
# Belgian employer names, the similarity floor this replaced - accept the
# best-scoring row at 0.60 - picked the wrong legal entity **six times**: "SMALS" resolved to a gravel company, "DE BRANDT NV" to the
# construction firm "Brandt", and "TOURING NV" to "A TOURING COMPANY BV" at
# similarity 1.00 - because "a" is a stopword.
#
# Anchoring DR-101 on the wrong entity is worse than not resolving at all:
# the NBB figures, the VAT number and - since NACE 78.x is precision 1.00 for
# a staffing agency - the employer-kind verdict are then all confidently about
# a different company (CR-405, docs/Agency_Research_Design.md section 2.1).
# So for identity the gate is **equality, not similarity**, and it refuses
# rather than guesses.

#: Legal forms, and only legal forms.  "Company", "Group", "Belgium" and
#: "International" are deliberately absent from this set although
#: :data:`dreamjob.pipeline.dedup.LEGAL_FORMS` holds them: they are part of a
#: registered name, and stripping them is exactly what let "TOURING" match
#: "A TOURING COMPANY".
_GATE_LEGAL_FORMS = frozenset(
    """
    nv sa bv bvba sprl srl cv cvba cvoa scrl scs sca comm va vof snc gcv se sce
    vzw asbl ivzw aisbl esv eesv
    ltd limited plc llp lp llc inc incorporated corp corporation
    gmbh mbh ag kg kgaa ohg ug gbr eg ev
    sas sasu sarl eurl sci scop
    spa srls sapa oy oyj ab abp as asa aps hf ehf bhd sdn pty pte pvt
    """.split()
)

#: Words that carry no identity of their own.  They are **not** removed before
#: the comparison - "DE BRANDT" is not "Brandt" - they only decide whether the
#: name that was asked for has any identity to compare at all.
_GATE_STOPWORDS = frozenset(
    "de het the la le les der die das den een a an and en et und of van von du des".split()
)

#: ``Dumortierlaan 70 8300 Knokke-Heist`` - the register prints the seat as a
#: street line followed by a Belgian postcode and its municipality.
_SEAT_RE = re.compile(r"(\d{4})\s+([^\d,;]{2,60})$")

_ENTERPRISE_HREF = re.compile(r"ondernemingsnummer=(\d{9,10})")

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\xa0]+")

#: The register's own line for "I hold nothing under that name", as the four
#: language versions of Public Search print it - and only ever inside the
#: results ``<h1>``, which a page with results does not have at all.  Read
#: positively, the way ``EuresAdapter._stated_total`` reads ``numberRecords``:
#: an empty hit list on its own is indistinguishable from a layout change, and
#: claiming emptiness there would leave this adapter unable to report its own
#: breakage (NFR-403).  Measured over the 1,568 name searches this deployment
#: has stored: 260 carry the English line, 20 the Dutch one, 242 carry result
#: rows, and 1,046 - two thirds of them - are the bot wall below.  Counted by
#: distinct cached body instead, the wall is 2 of 524: every wall response is
#: byte-identical, so the cache folds a thousand of them into one file.  The
#: per-search figure is the one that matters here, because it is the share of
#: questions that came back unanswered: 56 of one campaign's own KBO fetches
#: were the CAPTCHA form, and reading the wall as either an answer or a layout
#: change would have mislabelled every one of them.
_NO_RESULT_H1 = re.compile(
    r"(?is)<h1[^>]*>\s*(?:no result|no data|geen (?:gegevens|resultaat|resultaten)"
    r"|aucun|kein)"
)

#: The CAPTCHA interstitial, which the register serves with HTTP 200 after a
#: run of searches.  Its form field is the marker: it is language-independent
#: and no result page carries it.  A wall is neither an answer nor a layout
#: change, and reporting it as either is a lie in a different direction.
_BOT_WALL = re.compile(r"j_captcha_response|/kbopub/captchaform\.html")


#: The three row markers the register actually prints, measured over every
#: result row this deployment has stored: 1072 ``ENT`` (registered entity), 549
#: ``EU`` and 75 ``VE`` (establishment units).  Anything else is a marker we do
#: not know, and :func:`_row_kind` says so rather than guessing - see there.
_ROW_KINDS = ("ENT", "EU", "VE")


def _row_kind(kind_text: str) -> str:
    """``ENT``, ``EU``, ``VE`` - or ``""`` for a marker this parser cannot read.

    The empty string matters as much as the other three.  Reading an unknown
    marker as an establishment unit would make a results table whose columns
    moved look exactly like the register answering "I hold establishments of
    this name but no registered entity" - a real answer, which 5 of the stored
    result pages genuinely are.  Kept apart, a page of rows we cannot classify
    stays a breakage, which is what NFR-403 is for (FR-181).
    """
    head = (kind_text or "").strip().upper()
    for kind in _ROW_KINDS:
        if head.startswith(kind):
            return kind
    return ""


def _states_no_result(markup: str) -> bool:
    """The register printing, in its own words, that it holds nothing (FR-181)."""
    return bool(_NO_RESULT_H1.search(markup or ""))


def _states_no_such_enterprise(markup: str) -> bool:
    """The register's own page saying this enterprise number is not one of its."""
    text = _text(markup).lower()
    return "not found" in text and "enterprise" in text[:400]


#: What the gate says when the register served its wall instead of an answer.
#: ``unavailable`` rather than ``no_match`` because it is neither absence nor
#: ambiguity, and because the employer-kind rung and the identity store already
#: know that word (``db.repositories.registry_identity.DECISIONS``).
BOT_WALL_REASON = (
    "the register served its CAPTCHA wall instead of an answer: the searches "
    "this run made were refused, and nothing about the company was learned"
)


def _is_bot_wall(markup: str) -> bool:
    """The interstitial the register serves - with HTTP 200 - after a run of searches."""
    return bool(_BOT_WALL.search(markup or ""))


def _refuse_bot_wall(markup: str, key: str) -> None:
    """Raise rather than read a page the register served instead of an answer.

    Only the paths the collection worker drives call this: a raised failure is
    what makes the plan item say what happened instead of "the source layout has
    probably changed".  :meth:`KBOAdapter.match_by_name` deliberately does not -
    the employer-kind rung calls it with no handler of its own, and it reads the
    verdict, so the wall reaches it as ``unavailable`` (FR-182, NFR-403).
    """
    if _is_bot_wall(markup):
        raise RegistryWall(f"[{key}] {BOT_WALL_REASON} (FR-182, NFR-403)")

_STATUS_MAP = {
    "actief": "active", "active": "active", "actif": "active",
    "stopgezet": "ceased", "ceased": "ceased", "arrêté": "ceased", "arrete": "ceased",
}


def _text(markup: str) -> str:
    """The page as readable lines; the register lays its facts out as label/value rows."""
    if html_dom.LXML_AVAILABLE:
        tree = html_dom.Html(markup)
        for node in tree.css("script, style"):
            node.decompose()
        body = tree.body.text(separator="\n") if tree.body else tree.text(separator="\n")
    else:  # pragma: no cover - lxml is a declared dependency
        body = _TAGS.sub("\n", markup)
    lines = [_WS.sub(" ", line).strip() for line in body.replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line)


def _labelled(text: str, *labels: str) -> str | None:
    """Value of a ``Label: value`` row, tolerating the value on the next line."""
    lines = text.split("\n")
    lowered = [line.lower() for line in lines]
    for index, line in enumerate(lowered):
        for label in labels:
            if not line.startswith(label.lower()):
                continue
            remainder = lines[index][len(label):].lstrip(" :\t")
            if remainder:
                return remainder.strip()
            if index + 1 < len(lines):
                return lines[index + 1].strip()
    return None


@dataclass(frozen=True)
class EntityMatch:
    """What the exact-name gate concluded about one phonetic hit list.

    ``ambiguous`` is a first-class answer and is not the same as ``no_match``:
    the register *does* hold a company of this name, and cannot say which one.
    The employer-kind rung turns it into ``cannot_tell(registry_ambiguous)``
    with these candidates attached, so the job seeker is shown the two rows
    rather than a coin flip (docs/Agency_Research_Design.md sections 2.1, 7).
    """

    decision: str  # matched | ambiguous | no_match | unavailable
    number: str | None = None
    name: str = ""
    municipality: str = ""
    postcode: str = ""
    rule: str = ""  # exact_name | extended_name
    municipality_checked: bool = False
    #: True when this verdict was read off a body the register actually served
    #: and this adapter actually parsed.  A transport failure, a non-2xx and a
    #: name the register was never asked about all end in ``no_match`` too, and
    #: the difference between "the register says no" and "the register did not
    #: answer" is the whole of FR-181: only the first may be reported as a plan
    #: item that was read successfully and holds nothing.
    answered: bool = False
    #: The identifying words of the name that was asked for.  One of them is
    #: the class the phonetic search gets wrong; two or more, matched exactly,
    #: is the class it gets right (see ``employer_registry_rung``).
    queried_tokens: tuple[str, ...] = ()
    reason: str = ""
    candidates: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def matched(self) -> bool:
        return self.decision == "matched" and bool(self.number)

    def as_dict(self) -> dict[str, Any]:
        """The identity evidence, as it is stored beside a verdict (NFR-402)."""
        return {
            "gate": "exact_name_v2",
            "decision": self.decision,
            "legal_id": self.number,
            "registered_name": self.name,
            "municipality": self.municipality,
            "postcode": self.postcode,
            "rule": self.rule,
            "municipality_checked": self.municipality_checked,
            "answered": self.answered,
            "queried_tokens": list(self.queried_tokens),
            "reason": self.reason,
            "candidates": list(self.candidates),
        }


def _trading_name(name: str) -> str:
    """The name as the register indexes it: the legal form dropped, spelling kept."""
    kept = [word for word in (name or "").split() if _gate_tokens(word)]
    return " ".join(kept) or (name or "")


def _gate_tokens(name: str | None) -> list[str]:
    """Identifying words of a registered name: legal form out, everything else in."""
    return [token for token in tokens(name or "") if token not in _GATE_LEGAL_FORMS]


def _name_rule(wanted: list[str], row: list[str]) -> str:
    """``exact_name``, ``extended_name`` or "" - how this row matches, if at all.

    ``extended_name`` is the one concession to how companies are named in a
    posting: the register holds "ACCENT Jobs For People" and the vacancy says
    "Accent Jobs".  It is a *prefix* rule, in order, which is what keeps
    "TOURING" away from "A TOURING COMPANY", and it never stands on its own -
    :meth:`KBOAdapter.match_search_result` requires the seat to confirm it.
    """
    if not wanted or not row:
        return ""
    if wanted == row:
        return "exact_name"
    if len(row) > len(wanted) and row[: len(wanted)] == wanted:
        return "extended_name"
    return ""


def _normalise_place(value: str | None) -> set[str]:
    """Place words, for comparing a seat with a vacancy location."""
    return {token for token in tokens(value or "") if len(token) > 2}


def _seat_matches(row: dict[str, Any], municipalities: tuple[str, ...] | list[str]) -> bool:
    """Is this entity seated where the company's vacancies are?

    Deliberately generous - a postcode anywhere in the hint, or one shared
    place word - because the check exists to *separate* two rows, not to prove
    a location.  An agency's vacancies are at its clients' sites, so a seat
    that does not appear is weak evidence of nothing; a seat that does appear
    is what tells the two "House of Recruitment Solutions" rows apart.
    """
    if not municipalities:
        return False
    seat_words = _normalise_place(row.get("municipality"))
    postcode = (row.get("postcode") or "").strip()
    for hint in municipalities:
        text = str(hint or "")
        if postcode and postcode in text:
            return True
        if seat_words and seat_words & _normalise_place(text):
            return True
    return False


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    """One register row, as evidence a person can read (NFR-402)."""
    return {
        "legal_id": row.get("number"),
        "registered_name": row.get("name"),
        "municipality": row.get("municipality"),
        "postcode": row.get("postcode"),
        "url": f"{KBO_PUBLIC_SEARCH}?ondernemingsnummer={row.get('number')}",
    }


def _matched(
    rule: str,
    row: dict[str, Any],
    *,
    municipality_checked: bool,
    wanted: tuple[str, ...] = (),
) -> EntityMatch:
    return EntityMatch(
        "matched",
        number=row.get("number"),
        name=row.get("name") or "",
        municipality=row.get("municipality") or "",
        postcode=row.get("postcode") or "",
        rule=rule,
        municipality_checked=municipality_checked,
        queried_tokens=wanted,
        reason=(
            f"the register holds one entity named {row.get('name')!r}"
            + (
                " and it is seated where the vacancies are"
                if municipality_checked
                else f", seated in {row.get('municipality') or 'an unknown place'}, "
                "which the vacancies do not confirm"
            )
        ),
        candidates=(_candidate(row),),
        answered=True,
    )


@register_adapter
class KBOAdapter(RegistryAdapter):
    """Belgian enterprise register: the DR-101 identity anchor."""

    key = "registry.kbo"
    display_name = "KBO/BCE (Belgian enterprise register)"
    source_type = SourceType.REGISTRY
    access_method = AccessMethod.HTTP
    coverage_countries = ["BE"]
    jurisdictions = ["BE"]
    capabilities = AdapterCapabilities(
        keyword_search=True,
        company_lookup=True,
        pagination=False,
        max_results_per_query=25,
    )
    rate_limit_rps = 0.5
    reporting_standard = "BE-GAAP"
    legal_notes = (
        "Public register, free consultation. Rate-limited to one request every two "
        "seconds and robots.txt honoured through the egress layer (FR-182)."
    )

    # -- registry contract --------------------------------------------------
    async def collect(
        self,
        company: dict,
        *,
        years: int = DEFAULT_YEARS,
        egress: Any = None,
        llm: Any = None,
    ) -> RegistryResult:
        """Identity only: the KBO holds no figures - those live at the NBB (FR-241)."""
        result = RegistryResult(adapter_key=self.key)
        async with self.session(egress) as client:
            number = enterprise_number(company)
            if number is None and company.get("name"):
                # The verdict itself, not ``search_by_name``'s ``str | None``:
                # "the register holds no company of this name" is an answer and
                # settles the plan item as read-and-empty, while an outage, a
                # bot wall and an ambiguity are not, and all four used to arrive
                # here as the same ``None`` (FR-181, NFR-403).
                match = await self.match_by_name(str(company["name"]), egress=client)
                if match.decision == "unavailable":
                    raise RegistryWall(f"[{self.key}] {match.reason} (FR-182, NFR-403)")
                if not match.matched:
                    if match.decision == "no_match" and match.answered:
                        self.record_stated_empty()
                    result.note(f"{company.get('name')!r}: {match.reason}")
                    return result
                number = match.number
            if number is None:
                result.note("No enterprise number could be resolved for this company")
                return result

            identity = await self.lookup(number, egress=client, company=company)
            if identity is None:
                identity = await self.vies_identity(number, egress=client, company=company)
        if identity is None:
            result.note(f"Enterprise {format_enterprise_number(number)} not found in the register")
            return result
        result.identity = identity
        return result

    # -- lookups ------------------------------------------------------------
    async def lookup(
        self, number: str, *, egress: Any, company: dict | None = None
    ) -> dict[str, Any] | None:
        """The register's own page for one enterprise number (DR-101)."""
        url = f"{KBO_PUBLIC_SEARCH}?lang=en&ondernemingsnummer={number}"
        try:
            response = await egress.fetch(url)
        except RobotsDisallowed:
            log.info("[%s] robots.txt disallows the public search; falling back to VIES", self.key)
            return None
        except Exception as exc:  # noqa: BLE001 - a registry outage is not a crash
            log.info("[%s] public search unavailable (%s)", self.key, exc)
            return None
        if not response.ok:
            return None
        _refuse_bot_wall(response.text, self.key)
        identity = self.parse_company_page(response.text, number, company or {})
        if identity is None and _states_no_such_enterprise(response.text):
            # The register's own page for this number says there is no such
            # enterprise.  That is an answer, not a page we failed to read.
            self.record_stated_empty()
        return identity

    def parse_company_page(self, markup: str, number: str, company: dict) -> dict[str, Any] | None:
        if _states_no_such_enterprise(markup):
            return None
        text = _text(markup)
        name = _labelled(text, "Name:", "Naam:", "Nom:", "Denomination:")
        if not name:
            return None
        name = re.sub(r"\s*\b(Dutch|French|German|English)\b\s*$", "", name).strip()

        status_raw = (_labelled(text, "Status:", "Toestand:", "Statut:") or "").lower()
        legal_form = _labelled(text, "Legal form:", "Rechtsvorm:", "Forme légale:")
        address = _labelled(
            text,
            "Registered seat's address:",
            "Address of the seat:",
            "Adres van de zetel:",
            "Adres van de maatschappelijke zetel:",
            "Adresse du siège:",
            "Adresse du siège social:",
        )
        start_date = _labelled(text, "Start date:", "Begindatum:", "Date de début:")
        nace = sorted({code for code in re.findall(r"\b\d{2}\.\d{2,3}\b", text)})

        return identity_record(
            company,
            name=name,
            legal_id=number,
            legal_id_type="kbo_bce",
            country="BE",
            source=f"{KBO_PUBLIC_SEARCH}?ondernemingsnummer={number}",
            vat_number=f"BE{number}",
            sector_codes=nace or None,
            locations=[{"kind": "seat", "address": address}] if address else None,
            business_summary=self._summary(legal_form, status_raw, start_date),
            stage="nonprofit" if legal_form and "asbl" in legal_form.lower() else None,
            ownership=None,
        )

    @staticmethod
    def _summary(legal_form: str | None, status: str, start_date: str | None) -> str | None:
        parts = []
        if legal_form:
            parts.append(f"Legal form: {legal_form}")
        mapped = _STATUS_MAP.get(status.strip().lower())
        if mapped:
            parts.append(f"Register status: {mapped}")
        if start_date:
            parts.append(f"Registered since {start_date}")
        return "; ".join(parts) or None

    async def vies_identity(
        self, number: str, *, egress: Any, company: dict | None = None
    ) -> dict[str, Any] | None:
        """VAT-registry fall-back: registered name and address, as JSON."""
        url = VIES_CHECK.format(country="BE", number=number)
        try:
            response = await egress.fetch(url)
            payload = json.loads(response.text)
        except Exception as exc:  # noqa: BLE001 - optional corroboration only
            log.info("[%s] VIES unavailable (%s)", self.key, exc)
            return None
        if not isinstance(payload, dict) or not payload.get("isValid"):
            return None
        name = (payload.get("name") or "").strip()
        if not name or name == "---":
            return None
        address = (payload.get("address") or "").replace("\n", ", ").strip()
        return identity_record(
            company or {},
            name=name,
            legal_id=number,
            legal_id_type="kbo_bce",
            country="BE",
            source=url,
            vat_number=f"BE{number}",
            locations=[{"kind": "seat", "address": address}] if address else None,
            confidence=0.85,
        )

    @staticmethod
    def name_search_url(name: str, *, legal_persons_only: bool = False) -> str:
        """The phonetic search as the register's own form posts it.

        ``legal_persons_only`` drops the natural-person and establishment-unit
        halves of the form (their ``_x=on`` markers stay: Spring answers 404
        without them).  A bare token - "100G", "VIND" - otherwise returns a
        page of sole traders and shop signs that the gate then has to throw
        away one by one.
        """
        form = NAME_SEARCH_FORM
        if legal_persons_only:
            form = form.replace("ondNP=true&", "").replace("vest=true&", "")
        # The legal form is part of what the search matches on, and including
        # it empties the hit list: measured live on 2026-09-09, "NOEL FRANKLIN
        # BV" returned 0 rows and "NOEL FRANKLIN" returned the enterprise
        # (0700.275.068); "100G BV" returned 20 rows, none of them 100G, and
        # "100G" returned 0803.543.941 alone.
        trading = _trading_name(name)
        return f"{KBO_NAME_SEARCH}?searchWord={quote(trading)}&{form}"

    async def search_by_name(
        self,
        name: str,
        *,
        egress: Any,
        municipalities: tuple[str, ...] | list[str] = (),
        legal_persons_only: bool = False,
    ) -> str | None:
        """Phonetic name search - the weakest DR-101 key, so it is the last resort."""
        match = await self.match_by_name(
            name,
            egress=egress,
            municipalities=municipalities,
            legal_persons_only=legal_persons_only,
        )
        return match.number if match.matched else None

    async def match_by_name(
        self,
        name: str,
        *,
        egress: Any,
        municipalities: tuple[str, ...] | list[str] = (),
        legal_persons_only: bool = False,
    ) -> EntityMatch:
        """The gate's own answer, so a caller can tell ambiguity from absence."""
        unanswerable = self._unanswerable(name)
        if unanswerable is not None:
            # No request is issued.  A name with no identifying word in it is
            # not a question the register can answer, and asking anyway spends a
            # request to be told nothing.  ``answered`` stays false, so the plan
            # item settles as "issued no request" rather than as a clean miss -
            # 16 of the 200 companies in one campaign were board slugs like
            # ``coeo | BE`` and ``mod:group`` (FR-181).
            log.info("[%s] %r is not a name the register can be asked: %s",
                     self.key, name, unanswerable.reason)
            return unanswerable
        url = self.name_search_url(name, legal_persons_only=legal_persons_only)
        try:
            response = await egress.fetch(url)
        except Exception as exc:  # noqa: BLE001
            log.info("[%s] name search unavailable (%s)", self.key, exc)
            return EntityMatch("no_match", reason=f"the register did not answer ({exc})"[:200])
        if not response.ok:
            log.info(
                "[%s] name search for %r returned HTTP %s", self.key, name, response.status_code
            )
            return EntityMatch(
                "no_match", reason=f"the register answered HTTP {response.status_code}"
            )
        return self.match_search_result(response.text, name, municipalities=municipalities)

    @staticmethod
    def parse_search_results(markup: str) -> list[dict[str, Any]]:
        """The result rows of the public search, as ``{kind, number, name}``.

        The result hrefs drop the leading zero (``ondernemingsnummer=473191041``
        for 0473.191.041), so a global ten-digit regex silently skips every
        classic Belgian enterprise and matches whatever else on the page happens
        to have ten digits.  The rows are read structurally instead, keeping the
        registered-entity/establishment-unit distinction the register draws.
        """
        if not html_dom.LXML_AVAILABLE:  # pragma: no cover - lxml is a declared dependency
            return []
        out: list[dict[str, Any]] = []
        for row in html_dom.Html(markup).css("tr"):
            cells = row.css("td")
            if len(cells) < 5:
                continue
            href = " ".join(
                (a.attributes or {}).get("href") or "" for a in row.css("a")
            )
            match = _ENTERPRISE_HREF.search(href)
            if not match:
                continue
            kind_text = _WS.sub(" ", cells[1].text(separator=" ")).strip()
            name_node = row.css_first("td.benaming")
            address = _WS.sub(" ", cells[-1].text(separator=" ")).replace("\n", " ").strip()
            address = _WS.sub(" ", address)
            seat = _SEAT_RE.search(address)
            out.append(
                {
                    "kind": _row_kind(kind_text),
                    "status": "active" if "actief" in kind_text.lower() else "",
                    "number": match.group(1).zfill(10),
                    "name": _WS.sub(" ", (name_node.text() if name_node else "")).strip(),
                    # The seat is what separates two entities with the same
                    # name, so it is read here rather than fetched per row.
                    "address": address,
                    "postcode": seat.group(1) if seat else "",
                    "municipality": seat.group(2).strip() if seat else "",
                }
            )
        return out

    @classmethod
    def pick_search_result(
        cls, markup: str, wanted: str, *, municipalities: tuple[str, ...] | list[str] = ()
    ) -> str | None:
        """The enterprise number of the row that is actually this company.

        A thin wrapper over :meth:`match_search_result`: it answers with a
        number only when the gate *matched*, and with ``None`` for both
        ``no_match`` and ``ambiguous``, which is what every caller that only
        wants an identifier can act on.  A caller that has to tell "no such
        company" from "two companies share this name" - the employer-kind
        registry rung does - reads the match itself.
        """
        match = cls.match_search_result(markup, wanted, municipalities=municipalities)
        if match.matched:
            log.info(
                "[registry.kbo] %r resolved to %s (%s, %s)",
                wanted, format_enterprise_number(match.number or ""), match.name, match.rule,
            )
            return match.number
        log.info("[registry.kbo] %r not resolved: %s", wanted, match.reason)
        return None

    @staticmethod
    def _unanswerable(wanted: str) -> EntityMatch | None:
        """Why this name is not a question the register can answer, if it is not.

        A name with no identifying word in it - a board slug, a legal form on
        its own - has no identity to compare, so the phonetic search would be
        asked about nothing and answer with a page of unrelated sole traders.
        ``answered`` is false on purpose: nothing was learned about the register
        (FR-181).
        """
        wanted_tokens = _gate_tokens(wanted)
        if not wanted_tokens:
            return EntityMatch("no_match", reason="the name carries no identifying word")
        if all(token in _GATE_STOPWORDS for token in wanted_tokens):
            return EntityMatch(
                "no_match", reason=f"{wanted!r} is nothing but stopwords and a legal form"
            )
        return None

    @classmethod
    def match_search_result(
        cls, markup: str, wanted: str, *, municipalities: tuple[str, ...] | list[str] = ()
    ) -> EntityMatch:
        """Apply the exact-name gate to a phonetic hit list (DR-101, CR-405).

        The rules, in the order they fire, and what each one is for:

        1. **Registered entities only.**  An establishment unit is a site of an
           enterprise, not the enterprise; its number is the parent's and its
           name is often a shop sign.
        2. **The normalised names must be equal**, legal form removed and
           nothing else - or, when the registered name merely *extends* the
           name asked for ("ACCENT Jobs" -> "ACCENT Jobs For People"), equal on
           the queried tokens as a prefix, which then has to be confirmed by
           the seat.  A stopword-only difference is a difference: "DE BRANDT"
           is not "Brandt".
        3. **A name that is nothing but stopwords and legal forms** has no
           identity to compare, so it is refused rather than matched.
        4. **Ties are broken by the seat municipality**, compared with the
           places the company's vacancies are in; when that does not separate
           them the answer is ``ambiguous``, never the first row.
        5. **A one-token name** ("VIND", "JOBZ", "SMALS") is the class the
           phonetic search gets wrong most often, so when locations are known
           the seat must agree.  With no locations to check against, a single
           exactly-named entity is still returned - it is the only candidate -
           but ``municipality_checked`` is false and the caller is expected to
           want corroboration before acting on it.
        """
        if _is_bot_wall(markup):
            # HTTP 200, and not an answer: the register asked for a CAPTCHA.
            # Neither absence nor a layout change, and saying so is what keeps
            # a wall from being read as either (FR-182).
            return EntityMatch("unavailable", reason=BOT_WALL_REASON)
        unanswerable = cls._unanswerable(wanted)
        if unanswerable is not None:
            return unanswerable
        wanted_tokens = _gate_tokens(wanted)

        parsed = cls.parse_search_results(markup)
        # Only rows whose marker this parser recognises are evidence that the
        # hit list was read.  A table of rows we could not classify is a layout
        # change wearing the shape of an answer (see ``_row_kind``).
        read = [row for row in parsed if row["kind"]]
        rows = [row for row in read if row["kind"] == "ENT"]
        if not rows:
            # Two different things arrive here.  The register printing its own
            # "no result found" line, or a hit list we parsed that holds only
            # establishment units, are answers: the register holds no registered
            # entity of this name.  An empty parse with neither marker is not -
            # it is what a changed results table also looks like, and claiming
            # emptiness there would make this adapter unable to report its own
            # breakage (NFR-403).
            return EntityMatch(
                "no_match",
                reason=(
                    "the register's hit list holds no registered entity"
                    if read or _states_no_result(markup)
                    else "nothing could be read from the register's answer"
                ),
                answered=bool(read) or _states_no_result(markup),
            )

        hits: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            rule = _name_rule(wanted_tokens, _gate_tokens(row.get("name")))
            if rule:
                hits.append((rule, row))
        if not hits:
            closest = max(rows, key=lambda r: company_similarity(wanted, r.get("name") or ""))
            return EntityMatch(
                "no_match",
                reason=(
                    f"no registered entity on the hit list is named {wanted!r} "
                    f"(closest: {closest.get('name')!r})"
                ),
                candidates=tuple(_candidate(row) for row in rows[:5]),
                answered=True,
            )

        confirmed = [(rule, row) for rule, row in hits if _seat_matches(row, municipalities)]
        exact = [(rule, row) for rule, row in hits if rule == "exact_name"]
        one_token = len(wanted_tokens) == 1

        if len(confirmed) == 1:
            rule, row = confirmed[0]
            return _matched(
                rule, row, municipality_checked=True, wanted=tuple(wanted_tokens)
            )
        if len(confirmed) > 1:
            return EntityMatch(
                "ambiguous",
                reason=(
                    f"{len(confirmed)} registered entities are named {wanted!r} and seated in "
                    "the same place; the register cannot say which one posted the vacancies"
                ),
                candidates=tuple(_candidate(row) for _, row in confirmed[:5]),
                answered=True,
            )

        # Nothing was confirmed by the seat.  That is only fatal when the seat
        # was the evidence the rule needed.
        single = exact if len(exact) == 1 else (hits if len(hits) == 1 else [])
        if not single:
            return EntityMatch(
                "ambiguous",
                reason=(
                    f"{len(hits)} registered entities carry the name {wanted!r} and none is "
                    "seated where the vacancies are"
                ),
                candidates=tuple(_candidate(row) for _, row in hits[:5]),
                answered=True,
            )
        rule, row = single[0]
        if rule != "exact_name":
            return EntityMatch(
                "ambiguous",
                reason=(
                    f"the registered name {row.get('name')!r} extends {wanted!r} and its seat "
                    f"({row.get('municipality') or 'unknown'}) does not appear in the vacancies"
                ),
                candidates=(_candidate(row),),
                answered=True,
            )
        # One exactly-named entity, and the seat did not confirm it - either
        # because no location was known or because the vacancies are somewhere
        # else, which for an agency is the normal case: its postings are at its
        # clients' sites.  That is not a reason to refuse the only candidate,
        # and it is a reason not to close the ladder on it: the match travels
        # with ``municipality_checked = False`` and the rung caps what may be
        # concluded from it (see ``employer_registry_rung.UNCHECKED_IDENTITY_CAP``).
        if one_token:
            log.info(
                "[registry.kbo] %r is a one-word name matched without a seat check", wanted
            )
        return _matched(
            rule, row, municipality_checked=False, wanted=tuple(wanted_tokens)
        )
