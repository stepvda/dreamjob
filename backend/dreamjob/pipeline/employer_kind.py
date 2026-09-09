"""Who actually employs: the shared types and the decision (NFR-402, FR-341, FR-343).

An interim, staffing or selection agency posts a real vacancy on behalf of an
employer it does not name.  Everything this product does well - profiling the
company, reading five years of its accounts, judging its ability to pay,
inferring what it will hire for, writing a letter about why you want to work
*there* - is then computed against the wrong organisation and reads as
authoritative while being wrong.  Roughly one corpus row in five is such a
posting (docs/Interim_Agencies_Proposal.md section 1).

This module is the vocabulary the rest of the feature is written in: the enums
and records that the detector, the registry rung, the website rung, the
repository, the scorer and the badge all pass between them, and the pure
decision that turns measured per-employer signals into a verdict.  It holds no
I/O of any kind - no database, no HTTP, no LLM - which is what lets the tiering
in ``decide`` be tested against the labelled sets directly.

Three things about the shape are worth stating here, because they are decisions
rather than conveniences.

**The discriminator is voice, not diversity.**  A title-diversity test calls
GitLab an agency: 225 postings, 213 distinct titles, ratio 0.95.  What actually
separates the two is how much an employer repeats its own self-description
across its postings - GitLab's mean 6-word-shingle Jaccard is 0.303 and it
names itself in 100% of its adverts; NOEL FRANKLIN BV's is 0.007 and it names
itself in none, because every advert is a different, unnamed company.  So the
signals here are about *voice* (T1-T3, D1-D2) and about the register (R1-R3),
and every spread measure the brief hoped for is deliberately absent
(proposal section 2.4).

**``cannot_tell`` is an answer.**  It never defaults to ``employer`` (which
hides the sixty-vacancy agency whose site could not be read) and never to
``agency`` (which is the GitLab failure with a different excuse).  It carries
the reason and, through :func:`retry_after_for`, the cheapest next rung and
when it is worth spending.

**No verdict without evidence.**  :class:`Verdict` refuses to be constructed
decided-but-unevidenced, and the table refuses to store one.  A job seeker who
is told their prospective employer is an agency has to be able to read the
sentence that says so, on which page, established when - and to disagree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Kind(StrEnum):
    """What the knowledge base has established about this company."""

    AGENCY = "agency"
    EMPLOYER = "employer"
    #: Not a gap.  The rungs that ran could not establish who employs, and
    #: :class:`Reason` says which of the ways that happens this was.
    CANNOT_TELL = "cannot_tell"


class EmployerRole(StrEnum):
    """What the product does with the verdict (proposal section 4)."""

    DIRECT = "direct"          # no badge; every stage runs as it does today
    AGENCY = "agency"          # badge; no profile, no accounts, no speculation
    BOARD = "board"            # the same, for a job board that appears as an employer
    UNVERIFIED = "unverified"  # "employer type not verified": shown, never acted on


class Rung(StrEnum):
    """Which rung of the ladder established the verdict, cheapest first."""

    KB = "kb"              # a verdict already on record, or a promoted correction
    SIGNALS = "signals"    # the detector over this employer's own postings
    REGISTRY = "registry"  # a KBO / Companies House / KvK activity list
    EURES = "eures"        # the live EMPLOYER probe on NACE Rev 2.1 section O
    WEBSITE = "website"    # the classifier over the company's own pages
    MANUAL = "manual"      # a promoted human correction


class Tier(StrEnum):
    """The band of the additive score (proposal section 2.3)."""

    CERTAIN = "certain"
    PROBABLE = "probable"
    POSSIBLE = "possible"
    UNKNOWN = "unknown"
    DIRECT_LIKELY = "direct_likely"


class Reason(StrEnum):
    """Why a ``cannot_tell``.  The product offers a different next step per value."""

    NO_DOMAIN = "no_domain"
    UNREACHABLE = "unreachable"
    BOT_WALL = "bot_wall"
    JS_RENDERED = "js_rendered"
    PARKED = "parked"
    ROBOTS = "robots"
    OFF_DOMAIN_REDIRECT = "off_domain_redirect"
    NAMESAKE_COLLISION = "namesake_collision"
    REGISTRY_AMBIGUOUS = "registry_ambiguous"
    AMBIGUOUS_SELF_DESCRIPTION = "ambiguous_self_description"
    NO_VERIFIABLE_EVIDENCE = "no_verifiable_evidence"
    LLM_FAILED = "llm_failed"


#: One state, two spellings.  ``docs/Agency_Research_Design.md`` section 3.1
#: writes the JS-only case as ``js_rendered_or_empty``; the column and the
#: coverage screen use the shorter name, and a row written with the longer one
#: is folded onto it rather than becoming a second bucket that means the same
#: thing.  The schema accepts both so that a sibling slice's write is stored
#: rather than taking a corpus pass down with an IntegrityError.
REASON_ALIASES: dict[str, str] = {"js_rendered_or_empty": "js_rendered"}


def coerce_reason(value: Any) -> Reason | None:
    """The canonical :class:`Reason` for a stored or supplied string."""
    if value is None or value == "":
        return None
    if isinstance(value, Reason):
        return value
    text = str(value)
    return Reason(REASON_ALIASES.get(text, text))


class ServiceModel(StrEnum):
    TEMP_AGENCY = "temp_agency"
    RECRUITMENT_SELECTION = "recruitment_selection"
    JOB_BOARD = "job_board"
    PAYROLLING = "payrolling"
    CONSULTANCY_OR_OUTSOURCING = "consultancy_or_outsourcing"
    PRODUCT = "product"
    SERVICES = "services"
    PUBLIC_BODY = "public_body"
    UNKNOWN = "unknown"


#: Above this, no further rung is spent on a company: the ladder stops
#: (Agency_Research_Design.md section 2).  Below it, a more authoritative rung
#: may still improve the answer, which is what keeps ``possible`` moving.
DECIDED_CONFIDENCE = 0.85

#: Which verdict survives when two rungs disagree.  A register that lists a
#: regulated temporary-employment activity outranks a page read; a promoted
#: human correction outranks both.  A ``cannot_tell`` never blocks anything.
RUNG_AUTHORITY: dict[Rung, int] = {
    Rung.KB: 0,
    # The detector reads only what the knowledge base already holds, so it
    # ranks with it: free, and outranked by anything that costs a request.
    Rung.SIGNALS: 0,
    Rung.EURES: 1,
    Rung.WEBSITE: 2,
    Rung.REGISTRY: 3,
    Rung.MANUAL: 4,
}

#: FR-343 staleness, by the rung that decided.  A registry verdict is refreshed
#: yearly because activity codes are occasionally added, never because the
#: answer is expected to change; a website verdict at 180 days because a
#: *domain* can change hands - think-about-it.com now sells Hyundais.  A
#: promoted correction does not expire (proposal C6).
STALENESS_DAYS: dict[Rung, int | None] = {
    Rung.KB: 180,
    Rung.SIGNALS: 180,
    Rung.EURES: 365,
    Rung.WEBSITE: 180,
    Rung.REGISTRY: 365,
    Rung.MANUAL: None,
}

#: When a ``cannot_tell`` is worth trying again, as (first retry, thereafter).
#: Retry is by reason, not by clock (Agency_Research_Design.md section 5).
RETRY_DAYS: dict[Reason, tuple[int, int]] = {
    Reason.UNREACHABLE: (3, 30),
    Reason.LLM_FAILED: (3, 30),
    Reason.BOT_WALL: (90, 90),
    Reason.JS_RENDERED: (90, 90),
    Reason.PARKED: (90, 90),
    # The signal detector's own non-answer.  It is also re-decided whenever a
    # new posting for this employer is written, so the clock is a backstop.
    Reason.NO_VERIFIABLE_EVIDENCE: (90, 90),
}

#: Not on a clock: due the moment the event happens.  ``no_domain`` becomes
#: due when a domain is derived or a job seeker enters one, which is the
#: cheapest search engine available (section 4.4 of the research note).
EVENT_DRIVEN_REASONS = frozenset({Reason.NO_DOMAIN})

#: Never re-run on a timer: the answer will not change without a human.  Two
#: registered companies share the name; the redirect leaves the domain; the
#: site says both things at once; robots.txt says no (FR-182).
MANUAL_ONLY_REASONS = frozenset(
    {
        Reason.OFF_DOMAIN_REDIRECT,
        Reason.NAMESAKE_COLLISION,
        Reason.REGISTRY_AMBIGUOUS,
        Reason.AMBIGUOUS_SELF_DESCRIPTION,
        Reason.ROBOTS,
    }
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Quote:
    """A verbatim fragment and the page it was copied from (NFR-402).

    ``text`` is checked against the page by the rung that supplies it - the
    website rung verifies it against the fetched text, the detector copies it
    out of the advert it counted.  Nothing here re-verifies; this is the record.
    """

    text: str
    url: str | None = None
    source: str | None = None   # 'website' | 'vacancy' | 'registry' | ...


@dataclass(frozen=True, slots=True)
class Evidence:
    """One reason the verdict says what it says, in words a job seeker can read."""

    signal: str                      # 'R1' | 'T1' | 'website' | 'correction' | ...
    supports: str                    # 'agency' | 'employer'
    detail: str                      # plain words, for the badge popover
    url: str | None = None
    quote: str | None = None
    measured: float | None = None    # the share or score that made it fire
    points: float = 0.0
    established_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "supports": self.supports,
            "detail": self.detail,
            "url": self.url,
            "quote": self.quote,
            "measured": self.measured,
            "points": self.points,
            "established_at": self.established_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Evidence:
        return cls(
            signal=str(data.get("signal") or ""),
            supports=str(data.get("supports") or ""),
            detail=str(data.get("detail") or ""),
            url=data.get("url"),
            quote=data.get("quote"),
            measured=data.get("measured"),
            points=float(data.get("points") or 0.0),
            established_at=data.get("established_at"),
        )


class VerdictError(ValueError):
    """A verdict that must not exist: decided without evidence, or the reverse."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """One company's employer kind, with everything needed to defend it.

    ``corrected_for`` and ``conflicts_with`` are *view* fields: they describe
    the verdict as one job seeker sees it after their own correction is applied
    and are never written to the shared row (FR-344).
    """

    kind: Kind
    confidence: float
    rung: Rung
    method: str
    employer_role: EmployerRole = EmployerRole.UNVERIFIED
    service_model: ServiceModel = ServiceModel.UNKNOWN
    tier: Tier | None = None
    score: float | None = None
    postings: int | None = None
    reason: Reason | None = None
    evidence: tuple[Evidence, ...] = ()
    identity_evidence: Mapping[str, Any] | None = None
    audiences: Mapping[str, Any] | None = None
    summary: str = ""
    anomalies: tuple[str, ...] = ()
    prompt_template: str | None = None
    prompt_version: str | None = None
    model: str | None = None
    llm_call_id: str | None = None
    corrected_for: str | None = None
    conflicts_with: Rung | None = None

    def __post_init__(self) -> None:
        # Callers reach this from three directions - the detector with enums, a
        # decoded row with strings, an API payload with whatever it was given -
        # and a verdict whose ``kind`` is the string "agency" rather than
        # :data:`Kind.AGENCY` compares unequal to itself in half the places it
        # is used.  Coerce once, here, and every later comparison is honest.
        for name, enum in (
            ("kind", Kind),
            ("rung", Rung),
            ("employer_role", EmployerRole),
            ("service_model", ServiceModel),
        ):
            object.__setattr__(self, name, enum(str(getattr(self, name))))
        if self.tier is not None:
            object.__setattr__(self, "tier", Tier(str(self.tier)))
        object.__setattr__(self, "reason", coerce_reason(self.reason))
        object.__setattr__(
            self,
            "evidence",
            tuple(Evidence.from_dict(e) if isinstance(e, Mapping) else e for e in self.evidence),
        )
        object.__setattr__(self, "anomalies", tuple(self.anomalies))
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise VerdictError(f"confidence must be within 0..1, got {self.confidence!r}")
        if self.kind is Kind.CANNOT_TELL:
            if self.reason is None:
                raise VerdictError("a cannot_tell verdict must say why it cannot tell")
        else:
            if self.reason is not None:
                raise VerdictError(f"a {self.kind} verdict cannot carry reason {self.reason}")
            # NFR-402, and the rule that makes a hallucinated verdict unstorable.
            if not self.evidence:
                raise VerdictError(
                    f"a {self.kind} verdict needs at least one piece of evidence; "
                    "an unevidenced answer is cannot_tell(no_verifiable_evidence)"
                )

    @property
    def is_intermediary(self) -> bool:
        """Whether the product must not treat this company as the employer."""
        return self.employer_role in (EmployerRole.AGENCY, EmployerRole.BOARD)

    def to_row(self, *, established_at: str, expires_at: str | None = None,
               retry_after: str | None = None) -> dict[str, Any]:
        """The ``company_employer_kind`` columns, JSON left to the repository."""
        return {
            "kind": str(self.kind),
            "employer_role": str(self.employer_role),
            "service_model": str(self.service_model),
            "confidence": round(float(self.confidence), 4),
            "rung": str(self.rung),
            "method": self.method,
            "tier": str(self.tier) if self.tier else None,
            "score": None if self.score is None else round(float(self.score), 3),
            "postings": self.postings,
            "reason": str(self.reason) if self.reason else None,
            "evidence": [e.to_dict() for e in self.evidence],
            "identity_evidence": dict(self.identity_evidence) if self.identity_evidence else None,
            "audiences": dict(self.audiences) if self.audiences else None,
            "summary": self.summary or None,
            "anomalies": list(self.anomalies) or None,
            "prompt_template": self.prompt_template,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "llm_call_id": self.llm_call_id,
            "established_at": established_at,
            "expires_at": expires_at,
            "retry_after": retry_after,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Verdict:
        """Rebuild a verdict from a decoded row (JSON columns already parsed)."""
        evidence = row.get("evidence") or []
        if isinstance(evidence, Mapping):  # defensive: a single item, not a list
            evidence = [evidence]
        return cls(
            kind=Kind(str(row["kind"])),
            confidence=float(row["confidence"]),
            rung=Rung(str(row["rung"])),
            method=str(row.get("method") or ""),
            employer_role=EmployerRole(str(row.get("employer_role") or "unverified")),
            service_model=ServiceModel(str(row.get("service_model") or "unknown")),
            tier=Tier(str(row["tier"])) if row.get("tier") else None,
            score=None if row.get("score") is None else float(row["score"]),
            postings=None if row.get("postings") is None else int(row["postings"]),
            reason=Reason(str(row["reason"])) if row.get("reason") else None,
            evidence=tuple(Evidence.from_dict(e) for e in evidence if isinstance(e, Mapping)),
            identity_evidence=row.get("identity_evidence") or None,
            audiences=row.get("audiences") or None,
            summary=str(row.get("summary") or ""),
            anomalies=tuple(row.get("anomalies") or ()),
            prompt_template=row.get("prompt_template"),
            prompt_version=row.get("prompt_version"),
            model=row.get("model"),
            llm_call_id=row.get("llm_call_id"),
        )


@dataclass(frozen=True, slots=True)
class Signals:
    """Per-employer aggregates, measured over its postings and its register row.

    Every share is ``None`` when it could not be measured rather than 0.0: 442
    German employers have no free activity register and 145 of them have a
    single posting, and "not measured" must not read as "measured zero".
    """

    postings: int = 0
    # --- register and structured signals (proposal section 2.2) ------------
    registry_resolved: bool = False
    registry_staffing_codes: tuple[str, ...] = ()   # R1: 78.1 / 78.2 / 78.3
    registry_main_division: str | None = None       # e.g. '77' - discounts R2
    eures_section_o: bool = False                   # R2
    eures_o_share: float | None = None
    temp_share: float | None = None                 # R3: temporarytohire + temporary
    name_stem: str | None = None                    # R4: the stem that matched
    name_ambiguous_word: str | None = None          # R4b: never alone
    # --- voice signals: the coherence test ---------------------------------
    client_phrase_share: float | None = None        # T1 / T1'
    anonymous_share: float | None = None            # T2
    shingle_jaccard: float | None = None            # T3
    # --- negative signals --------------------------------------------------
    self_named_share: float | None = None           # D1
    first_person_share: float | None = None         # D2
    anti_agency_disclaimer: bool = False            # D3
    public_legal_form: str | None = None            # D4
    own_ats_board: bool = False                     # D5
    # --- shape of the verdict rather than its score ------------------------
    is_job_board: bool = False
    #: signal id -> the verbatim sentence that made it fire, with its page.
    quotes: Mapping[str, Quote] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The score (proposal section 2.2) and the bands (section 2.3)
# ---------------------------------------------------------------------------

#: Thresholds a share must reach for its signal to fire.
T1_STRICT_SHARE = 0.3
T1_LOOSE_SHARE = 0.1
T2_SHARE = 0.6
T3_JACCARD = 0.02
R3_SHARE = 0.5
D1_SHARE = 0.6
D2_SHARE = 0.6

#: Minimum postings a statistic needs before it means anything.
T2_MIN_POSTINGS = 3
T3_MIN_POSTINGS = 5
R3_MIN_POSTINGS = 3
#: Below this, voice statistics cannot carry an employer past ``possible``;
#: register signals are not capped (a one-posting agency with a stem name and a
#: 78 code is ``certain``).
CAP_MIN_POSTINGS = 3

#: Band floors.
CERTAIN_SCORE = 5.0
PROBABLE_SCORE = 3.0
POSSIBLE_SCORE = 1.5
DIRECT_LIKELY_SCORE = -1.5

#: The confidence stored per band is the precision measured for that band on
#: the hand-labelled sets (proposal section 2.3), not a guess: certain 1.00 on
#: 50 and on 103 employers (stored at 0.97 because n is 50 and 103, not
#: infinity), probable+ 0.97, the ``possible`` band on its own "a coin flip",
#: direct-likely 30/30.  For a ``cannot_tell`` row nothing scores on it.
CONFIDENCE_BY_TIER: dict[Tier, float] = {
    Tier.CERTAIN: 0.97,
    Tier.PROBABLE: 0.90,
    Tier.POSSIBLE: 0.50,
    Tier.UNKNOWN: 0.30,
    Tier.DIRECT_LIKELY: 0.90,
}

#: A band decides what the product may do, not only what it believes
#: (proposal section 2.3 and section 4).
TIER_KIND: dict[Tier, Kind] = {
    Tier.CERTAIN: Kind.AGENCY,
    Tier.PROBABLE: Kind.AGENCY,
    # "Employer type not verified": shown as its own state, acted on either way
    # by nothing.  The band on its own is 2 true positives to 1-2 false ones.
    Tier.POSSIBLE: Kind.CANNOT_TELL,
    # No badge, treated as direct by the product - but the knowledge base has
    # established nothing, so the record does not claim ``employer`` either.
    Tier.UNKNOWN: Kind.CANNOT_TELL,
    Tier.DIRECT_LIKELY: Kind.EMPLOYER,
}

TIER_ROLE: dict[Tier, EmployerRole] = {
    Tier.CERTAIN: EmployerRole.AGENCY,
    Tier.PROBABLE: EmployerRole.AGENCY,
    Tier.POSSIBLE: EmployerRole.UNVERIFIED,
    Tier.UNKNOWN: EmployerRole.DIRECT,
    Tier.DIRECT_LIKELY: EmployerRole.DIRECT,
}

TIER_REASON: dict[Tier, Reason] = {
    Tier.POSSIBLE: Reason.AMBIGUOUS_SELF_DESCRIPTION,
    Tier.UNKNOWN: Reason.NO_VERIFIABLE_EVIDENCE,
}

#: The one-line summary that goes in front of a reader before the evidence.
TIER_SENTENCE: dict[Tier, str] = {
    Tier.CERTAIN: "Posted by an agency for an employer it does not name",
    Tier.PROBABLE: "Probably posted by an agency for an employer it does not name",
    Tier.POSSIBLE: "Employer type not verified",
    Tier.UNKNOWN: "Employer type not verified",
    Tier.DIRECT_LIKELY: "Appears to be the employer itself",
}

_AGENCY = "agency"
_EMPLOYER = "employer"


def _share(value: float | None) -> float | None:
    return None if value is None else float(value)


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{round(float(value) * 100)}%"


def band_for(score: float) -> Tier:
    """The band a score falls in (proposal section 2.3)."""
    if score >= CERTAIN_SCORE:
        return Tier.CERTAIN
    if score >= PROBABLE_SCORE:
        return Tier.PROBABLE
    if score >= POSSIBLE_SCORE:
        return Tier.POSSIBLE
    if score <= DIRECT_LIKELY_SCORE:
        return Tier.DIRECT_LIKELY
    return Tier.UNKNOWN


def _fire(
    out: list[Evidence],
    signals: Signals,
    signal: str,
    supports: str,
    detail: str,
    points: float,
    measured: float | None = None,
) -> None:
    quote = signals.quotes.get(signal)
    out.append(
        Evidence(
            signal=signal,
            supports=supports,
            detail=detail,
            url=quote.url if quote else None,
            quote=quote.text if quote else None,
            measured=measured,
            points=points,
        )
    )


def score_signals(signals: Signals) -> tuple[float, list[Evidence]]:
    """The additive score and the evidence for it, one item per signal that fired."""
    n = int(signals.postings or 0)
    ev: list[Evidence] = []

    # --- R1: the register says staffing -----------------------------------
    if signals.registry_staffing_codes:
        codes = ", ".join(signals.registry_staffing_codes)
        _fire(ev, signals, "R1", _AGENCY,
              f"the register lists staffing activity (NACE {codes})", 3.0)

    # --- R2: EURES files the employer under NACE Rev 2.1 section O --------
    if signals.eures_section_o:
        # Section O is administrative and support services, 77-82: rental,
        # cleaning and facility management sit there too, so an employer whose
        # main registered activity is one of those is O for another reason.
        discounted = (signals.registry_main_division or "") in ("77", "79", "80", "81", "82")
        detail = (
            "EURES files this employer under section O (administrative and support "
            "services, which contains staffing)"
        )
        if discounted:
            detail += (
                f"; discounted because its main registered activity is division "
                f"{signals.registry_main_division}, which is also section O"
            )
        _fire(ev, signals, "R2", _AGENCY, detail, 1.0 if discounted else 3.0,
              _share(signals.eures_o_share))

    # --- R3: temp-to-hire offering codes ----------------------------------
    temp = _share(signals.temp_share)
    if temp is not None and temp >= R3_SHARE and n >= R3_MIN_POSTINGS:
        _fire(ev, signals, "R3", _AGENCY,
              f"{_pct(temp)} of {n} postings are temp-to-hire or temporary", 3.0, temp)

    # --- R4 / R4b: the name -----------------------------------------------
    if signals.name_stem:
        _fire(ev, signals, "R4", _AGENCY,
              f"the name contains the staffing term '{signals.name_stem}'", 2.0)
    ambiguous_pending = signals.name_ambiguous_word and not signals.name_stem

    # --- T1 / T1': a singular, hiring-framed third party -------------------
    client = _share(signals.client_phrase_share)
    if client is not None and client >= T1_STRICT_SHARE:
        _fire(ev, signals, "T1", _AGENCY,
              f"{_pct(client)} of adverts describe a single unnamed client "
              "('onze klant', 'in opdracht van', 'unser Mandant')", 3.0, client)
    elif client is not None and client >= T1_LOOSE_SHARE:
        _fire(ev, signals, "T1", _AGENCY,
              f"{_pct(client)} of adverts describe a single unnamed client", 1.5, client)

    # --- T2: nobody says who "we" are --------------------------------------
    anon = _share(signals.anonymous_share)
    if anon is not None and anon >= T2_SHARE and n >= T2_MIN_POSTINGS:
        _fire(ev, signals, "T2", _AGENCY,
              f"{_pct(anon)} of adverts neither name this employer nor speak in its "
              "own voice", 1.5, anon)

    # --- T3: no shared self-description ------------------------------------
    jaccard = _share(signals.shingle_jaccard)
    if jaccard is not None and jaccard < T3_JACCARD and n >= T3_MIN_POSTINGS:
        _fire(ev, signals, "T3", _AGENCY,
              f"its adverts share almost no self-description (mean 6-word-shingle "
              f"overlap {jaccard:.3f})", 1.0, jaccard)

    # --- D1 / D2: an employer introducing itself ---------------------------
    named = _share(signals.self_named_share)
    voice = _share(signals.first_person_share)
    no_client_voice = client is None or client < T1_LOOSE_SHARE
    if named is not None and named >= D1_SHARE and no_client_voice and not signals.name_stem:
        _fire(ev, signals, "D1", _EMPLOYER,
              f"it names itself in {_pct(named)} of its adverts, with no client voice",
              -2.5, named)
    if voice is not None and voice >= D2_SHARE and no_client_voice:
        _fire(ev, signals, "D2", _EMPLOYER,
              f"{_pct(voice)} of adverts speak in the employer's own voice", -1.0, voice)

    # --- D3 - D6 ------------------------------------------------------------
    if signals.anti_agency_disclaimer:
        _fire(ev, signals, "D3", _EMPLOYER,
              "an advert states that agencies should not apply", -2.0)
    if signals.public_legal_form:
        _fire(ev, signals, "D4", _EMPLOYER,
              f"a public or non-profit legal form ({signals.public_legal_form})", -3.0)
    if signals.own_ats_board:
        _fire(ev, signals, "D5", _EMPLOYER,
              "the postings come from the company's own ATS board", -2.0)
    if signals.registry_resolved and not signals.registry_staffing_codes:
        _fire(ev, signals, "D6", _EMPLOYER,
              "the register resolved this company and lists no staffing activity", -1.0)

    # R4b counts only alongside another signal: 'hr', 'people', 'select' and
    # 'work' name Swiss Life Select, SelectLine and the Digital Career
    # Institute as readily as they name an agency.
    if ambiguous_pending and ev:
        _fire(ev, signals, "R4b", _AGENCY,
              f"the name contains the ambiguous word '{signals.name_ambiguous_word}' "
              "(counted only alongside another signal)", 1.0)

    # Strongest first, so the badge popover reads top-down (section 4.3).
    ev.sort(key=lambda e: abs(e.points), reverse=True)
    return round(sum(e.points for e in ev), 3), ev


def _service_model(signals: Signals, kind: Kind) -> ServiceModel:
    if signals.is_job_board:
        return ServiceModel.JOB_BOARD
    if kind is not Kind.AGENCY:
        return ServiceModel.PUBLIC_BODY if signals.public_legal_form else ServiceModel.UNKNOWN
    codes = signals.registry_staffing_codes
    temp = _share(signals.temp_share)
    if any(c.startswith("78.2") for c in codes) or (temp is not None and temp >= R3_SHARE):
        return ServiceModel.TEMP_AGENCY
    if any(c.startswith("78.1") for c in codes):
        return ServiceModel.RECRUITMENT_SELECTION
    if any(c.startswith("78.3") for c in codes):
        return ServiceModel.PAYROLLING
    return ServiceModel.UNKNOWN


def decide(signals: Signals, *, rung: Rung = Rung.SIGNALS, method: str = "signals") -> Verdict:
    """Turn measured per-employer aggregates into a verdict (section 2.3).

    The bands were measured at employer level on hand-labelled sets: at
    ``probable`` and above, precision 0.97-1.00 and recall 0.95-0.97, with no
    direct employer flagged at any actionable tier.  The cap below is what
    keeps a two-advert startup with no self-description - Greenpocket, the one
    false positive on 103 employers - out of the acted-on bands on voice alone.
    """
    n = int(signals.postings or 0)
    score, evidence = score_signals(signals)
    tier = band_for(score)

    register_fired = any(e.signal in ("R1", "R2", "R3", "R4") for e in evidence)
    capped = False
    if n < CAP_MIN_POSTINGS and not register_fired and tier in (Tier.CERTAIN, Tier.PROBABLE):
        tier = Tier.POSSIBLE
        capped = True

    kind = TIER_KIND[tier]
    reason = TIER_REASON.get(tier) if kind is Kind.CANNOT_TELL else None
    role = TIER_ROLE[tier]
    if role is EmployerRole.AGENCY and signals.is_job_board:
        role = EmployerRole.BOARD

    # A decided verdict needs evidence; ``direct_likely`` has D-signals and the
    # agency bands have their own.  The band cannot be decided without them.
    if kind is not Kind.CANNOT_TELL and not evidence:  # pragma: no cover - unreachable
        kind, reason, role = Kind.CANNOT_TELL, Reason.NO_VERIFIABLE_EVIDENCE, EmployerRole.DIRECT

    parts = [TIER_SENTENCE[tier]]
    if evidence:
        parts.append("; ".join(e.detail for e in evidence[:3]))
    summary = ". ".join(parts) + "."

    anomalies: list[str] = []
    if capped:
        anomalies.append(
            f"score {score:+.1f} capped to 'possible': {n} posting(s) is too few for "
            "voice statistics to carry an employer past this band"
        )

    return Verdict(
        kind=kind,
        confidence=CONFIDENCE_BY_TIER[tier],
        rung=rung,
        method=method,
        employer_role=role,
        service_model=_service_model(signals, kind),
        tier=tier,
        score=score,
        postings=n,
        reason=reason,
        evidence=tuple(evidence),
        summary=summary,
        anomalies=tuple(anomalies),
    )


# ---------------------------------------------------------------------------
# Freshness, retry and precedence - pure, so the repository and the queue agree
# ---------------------------------------------------------------------------


def _stamp(moment: datetime) -> str:
    """The one timestamp format every TEXT date column in this schema holds."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def expires_at_for(rung: Rung, *, now: datetime | None = None) -> str | None:
    """When a verdict from this rung goes stale (FR-343, proposal C6)."""
    days = STALENESS_DAYS.get(rung)
    if days is None:
        return None
    return _stamp(_now(now) + timedelta(days=days))


def retry_after_for(
    reason: Reason | None, *, attempts: int = 1, now: datetime | None = None
) -> str | None:
    """When this ``cannot_tell`` is worth another request.

    ``None`` means "not on a clock": either the answer will not change without
    a human, or the retry is event-driven - ``no_domain`` becomes due the
    moment a domain is derived or a job seeker enters one.
    """
    reason = coerce_reason(reason)
    if reason is None or reason in MANUAL_ONLY_REASONS or reason in EVENT_DRIVEN_REASONS:
        return None
    window = RETRY_DAYS.get(reason)
    if window is None:
        return None
    days = window[0] if int(attempts) <= 1 else window[1]
    return _stamp(_now(now) + timedelta(days=days))


def supersedes(new: Verdict, existing: Verdict | None) -> bool:
    """Whether ``new`` may replace ``existing`` (Agency_Research_Design.md section 2).

    A verdict row is never edited in place by a later rung; it is replaced, and
    the old one is kept in ``audit_event`` so "why did this change" is
    answerable.  What may replace what:

    * a re-run of the same rung always may - that is a refresh;
    * a non-answer never blocks a later rung, but a cheaper rung's non-answer
      does not overwrite a more authoritative one (losing "two registered
      companies share this name" for "no verifiable evidence" would throw away
      the work item that tells a human what to do);
    * otherwise the more authoritative rung wins: a register that lists a
      regulated temporary-employment activity outranks a page read, and a
      promoted human correction outranks both.
    """
    if existing is None:
        return True
    if new.rung == existing.rung:
        return True
    if existing.kind is Kind.CANNOT_TELL:
        if new.kind is Kind.CANNOT_TELL:
            return RUNG_AUTHORITY[new.rung] >= RUNG_AUTHORITY[existing.rung]
        return True
    return RUNG_AUTHORITY[new.rung] >= RUNG_AUTHORITY[existing.rung]


def needs_resolution(
    row: Mapping[str, Any] | None,
    *,
    for_rung: Rung | None = None,
    now: datetime | None = None,
    has_domain: bool = False,
) -> bool:
    """Whether this company is worth another rung's request.

    The ladder's rule is that nothing below a rung runs when a higher rung has
    answered with confidence >= 0.85; everything else here is the shape of the
    exceptions - staleness, a due retry, and the event that makes a
    ``no_domain`` non-answer worth revisiting.
    """
    if not row:
        return True
    moment = _stamp(_now(now))
    kind = str(row.get("kind") or "")
    rung = str(row.get("rung") or "")
    ceiling = RUNG_AUTHORITY[for_rung] if for_rung else max(RUNG_AUTHORITY.values())
    authority = RUNG_AUTHORITY.get(Rung(rung), 0) if rung else 0

    expires_at = row.get("expires_at")
    if expires_at and str(expires_at) <= moment:
        return True

    if kind == Kind.CANNOT_TELL:
        reason = str(row.get("reason") or "")
        if reason == Reason.NO_DOMAIN and has_domain:
            return True          # the event happened: a domain exists now
        retry_after = row.get("retry_after")
        if retry_after and str(retry_after) <= moment:
            return True
        # A non-answer from a cheaper rung is exactly what the next rung is for.
        return authority < ceiling

    return float(row.get("confidence") or 0.0) < DECIDED_CONFIDENCE and authority < ceiling


# ---------------------------------------------------------------------------
# The human correction (Agency_Research_Design.md section 8)
# ---------------------------------------------------------------------------

#: Two job seekers correcting the same company the same way, each with a note,
#: promote the correction without an operator.
CONSENSUS_THRESHOLD = 2

#: What a human assertion is worth.  High enough to act on - they may have
#: worked there - and below a register's 0.97, because a register entry is a
#: regulated declaration and a correction is a sentence.
CORRECTION_CONFIDENCE = 0.90


def correction_conflicts(verdict: Verdict | None, kind: Kind | str) -> bool:
    """Whether a human correction contradicts a register.

    A shared correction beats a website verdict and a ``cannot_tell``.  It does
    not beat a registry verdict: if the register says 78.2 and a human says
    employer, the row is a conflict for the operator rather than resolved
    either way - the register is occasionally stale, the human is occasionally
    wrong, and neither should win silently.
    """
    if verdict is None or verdict.rung is not Rung.REGISTRY:
        return False
    return verdict.kind is not Kind.CANNOT_TELL and str(verdict.kind) != str(kind)


def effective_verdict(
    verdict: Verdict | None,
    correction: Mapping[str, Any] | None,
    *,
    company_name: str = "this company",
) -> Verdict | None:
    """The verdict as one job seeker sees it, with their correction applied.

    A correction applies immediately and only to the seeker who made it: their
    lists, their scoring and their directives read it over the machine's
    verdict.  It carries no weight for anyone else until it is promoted, and a
    shared correction still does not silently overturn a register - that case
    comes back marked through ``conflicts_with`` so the interface can show both
    sentences and let the reader decide.
    """
    if not correction:
        return verdict
    kind = Kind(str(correction["kind"]))
    scope = str(correction.get("scope") or "private")
    conflict = correction_conflicts(verdict, kind)
    if scope == "shared" and conflict:
        # Nobody wins silently: the shared row does not overwrite the register.
        return replace(verdict, conflicts_with=Rung.REGISTRY) if verdict else None

    note = str(correction.get("note") or "").strip()
    made_at = correction.get("created_at")
    item = Evidence(
        signal="correction",
        supports=str(kind),
        detail=(
            f"a job seeker corrected this to '{kind}': {note}"
            if scope == "private"
            else f"corrected to '{kind}' and confirmed for everyone: {note}"
        ),
        url=correction.get("evidence_url"),
        quote=note or None,
        established_at=made_at,
    )
    role = EmployerRole.AGENCY if kind is Kind.AGENCY else EmployerRole.DIRECT
    if kind is Kind.AGENCY and verdict is not None and verdict.is_intermediary:
        role = verdict.employer_role      # keep 'board' rather than flattening it
    kept = tuple(e for e in (verdict.evidence if verdict else ()) if e.signal != "correction")
    return Verdict(
        kind=kind,
        confidence=CORRECTION_CONFIDENCE,
        rung=Rung.MANUAL,
        method="correction",
        employer_role=role,
        service_model=verdict.service_model if verdict else ServiceModel.UNKNOWN,
        tier=verdict.tier if verdict else None,
        score=verdict.score if verdict else None,
        postings=verdict.postings if verdict else None,
        reason=None,
        evidence=(item, *kept),
        summary=f"{company_name}: corrected to '{kind}' by hand. {note}".strip(),
        anomalies=verdict.anomalies if verdict else (),
        corrected_for=correction.get("job_seeker_id"),
        conflicts_with=Rung.REGISTRY if conflict else None,
    )


def evidence_in_words(verdict: Verdict | None, limit: int = 4) -> list[str]:
    """The evidence list a job seeker reads, strongest first (section 4.3)."""
    if verdict is None:
        return []
    out: list[str] = []
    for item in verdict.evidence[:limit]:
        if item.quote:
            out.append(f"{item.detail} - “{item.quote}”")
        else:
            out.append(item.detail)
    return out


def summarise(verdicts: Sequence[Verdict]) -> dict[str, int]:
    """Counts by kind, for a coverage line that does not need SQL."""
    out: dict[str, int] = {str(k): 0 for k in Kind}
    for verdict in verdicts:
        out[str(verdict.kind)] += 1
    return out
