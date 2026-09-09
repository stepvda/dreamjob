"""The employer-kind ladder: who actually employs, established once, with evidence.

An interim or staffing agency advertises a real job for an employer it does not
name.  Dream Job's whole premise is understanding *the employer* - profiling it,
reading five years of its accounts, judging its ability to pay, writing a letter
about why you want to work there - so every one of those postings is analysed
against the wrong organisation and looks authoritative while being wrong.  It is
not a fringe case: 17-20% of the corpus, 33-37% of the named Belgian EURES
slice, 80-92% of Actiris (docs/Interim_Agencies_Proposal.md section 1).

This module is the *orchestration*: it walks the rungs of
docs/Agency_Research_Design.md section 2 in order, cheapest and most certain
first, and turns whichever of them answers into one shared knowledge-base fact.
It owns no rung's technique.  The register lives in the registry slice, the
site read in the website slice, the per-employer aggregates in the signal
detector; each of them registers itself here with :func:`register_rung`, and a
rung that is not installed in this build is recorded as ``unavailable`` rather
than silently skipped.

The ladder
----------

===== ============== ======================================================
rung  name           what it proves, and what it costs
===== ============== ======================================================
0     ``kb``         a verdict already on record, or a promoted human
                     correction.  0 requests.
0b    ``signals``    **nothing.**  The free signals - the name lexicon, the
                     client phrases, the offering codes, the detector's band
                     - set the queue order and give a ``cannot_tell`` something
                     honest to say.  They never decide (section 2, rung 0b).
1     ``registry``   KBO / Companies House / KvK activity codes.  NACE-BEL
                     78.2 is a licensed temporary-employment activity and is
                     definitive; 2 requests, ~4 s, and it never needs
                     re-establishing.
2     ``eures``      the EURES ``EMPLOYER`` probe with ``sectorCodes=['O']``.
                     One request; it is what takes 100G BV - JS-only site,
                     NACE 62.x, every vacancy at somebody else's plant - from
                     a suspicion to a verdict.
3     ``website``    the company's own pages, read by a cheap model, every
                     verdict tied to a verbatim quote the code re-verifies
                     against the page.  ~5 requests + 1 LLM call.
===== ============== ======================================================

Four rules the orchestration enforces, whatever a rung returns
--------------------------------------------------------------

1. **``cannot_tell`` is a first-class answer.**  It is never a default to
   "employer" and never a default to "agency": "assume employer" hides the
   sixty-vacancy agency whose site could not be read, "assume agency" is the
   GitLab failure with a different excuse.  Every ``cannot_tell`` carries a
   reason from a closed vocabulary, the cheapest next rung (:func:`next_step`)
   and, when a clock can fix it, a ``retry_after``.

2. **No evidence, no verdict** (NFR-402).  A decided verdict must carry at
   least one evidence item that stands on its own: a register code with its
   legal id, or a quote the rung verified verbatim against the page text.  A
   verdict that arrives without one is stored as
   ``cannot_tell(no_verifiable_evidence)``.  This is the rule that makes a
   hallucinated verdict impossible to store, and it is why a signal - a score,
   a share, a band - can never be the thing that decides: a score is not a
   quote.

3. **Every rung that ran is in the evidence**, not only the one that decided.
   A job seeker who disagrees is owed "the register was searched and is silent,
   the site needs a browser" rather than a bare "we do not know", and the next
   pass needs to know which rungs are worth walking again.

4. **Website content is untrusted** (NFR-205).  Anomalies a rung reports - a
   page that addresses the model, an instruction planted in the text - are
   stored on the verdict and flag it for review; they never change it.  The
   measured behaviour of the classifier under a planted "ignore all previous
   instructions" was: verdict unchanged, attempt reported (research note
   section 3.5).  Nothing here may quietly repair a verdict on the strength of
   text that came off a web page.

Evidence item shapes (JSON, stored on ``company_employer_kind.evidence``)::

    {"type": "registry", "registry": "kbo", "legal_id": "0700275068",
     "nace_version": "2025", "regime": "NSSO", "code": "78.100",
     "label": "employment placement agencies", "supports": "agency"}
    {"type": "quote", "url": "https://…", "quote": "…", "supports": "agency",
     "verified": true, "raw_document_id": "…", "fetched_at": "…"}
    {"type": "signal", "signal": "T1", "measured": 0.98, "points": 3,
     "supports": "agency", "detail": "55 of 56 adverts begin 'Onze klant'"}
    {"type": "attempt", "rung": "registry", "outcome": "handed_down",
     "reason": "no 78.x code", "at": "2026-09-09T…"}

Where it runs: this is knowledge-base work (FR-341).  One row per company, no
job seeker on it (FR-344), populated by :func:`resolve_many` as a resumable job
and by an on-demand "research this employer" action, and merely *read* by
campaign planning (FR-342).  A company does not stop being an agency because a
different job seeker is looking at it.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import from_json, to_json, utcnow
from dreamjob.db.repositories import employer_resolution as repo

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The vocabulary.  Migration 110 constrains every one of these in the schema;
# they are repeated here so a typo is a NameError in this module rather than an
# IntegrityError three hundred companies into a corpus pass.
# ---------------------------------------------------------------------------

KIND_AGENCY = "agency"
KIND_EMPLOYER = "employer"
KIND_CANNOT_TELL = "cannot_tell"

ROLE_DIRECT = "direct"
ROLE_AGENCY = "agency"
ROLE_BOARD = "board"
ROLE_UNVERIFIED = "unverified"

RUNG_KB = "kb"
RUNG_SIGNALS = "signals"
RUNG_REGISTRY = "registry"
RUNG_EURES = "eures"
RUNG_WEBSITE = "website"
RUNG_MANUAL = "manual"

METHOD_KNOWLEDGE_BASE = "knowledge_base"
METHOD_SIGNALS = "signals"
METHOD_REGISTRY = "registry_nace"
METHOD_EURES = "eures_sector"
METHOD_WEBSITE = "website_llm"
METHOD_CORRECTION = "correction"

#: ``cannot_tell`` reasons, closed on purpose: the product offers a *different*
#: next step per reason, so a reason nobody has written behaviour for must not
#: appear.  Migration 110 carries the same list as a CHECK constraint.
REASON_NO_DOMAIN = "no_domain"
REASON_UNREACHABLE = "unreachable"
REASON_BOT_WALL = "bot_wall"
REASON_JS_RENDERED = "js_rendered"
REASON_PARKED = "parked"
REASON_ROBOTS = "robots"
REASON_OFF_DOMAIN_REDIRECT = "off_domain_redirect"
REASON_NAMESAKE = "namesake_collision"
REASON_REGISTRY_AMBIGUOUS = "registry_ambiguous"
REASON_AMBIGUOUS = "ambiguous_self_description"
REASON_NO_EVIDENCE = "no_verifiable_evidence"
REASON_LLM_FAILED = "llm_failed"

#: The research note and the rungs that quote it spell two of these longer than
#: the schema does.  Folding rather than widening keeps one bucket per reason on
#: the coverage screen, which is the point of a closed vocabulary.
REASON_ALIASES: dict[str, str] = {
    "js_rendered_or_empty": REASON_JS_RENDERED,
    "javascript": REASON_JS_RENDERED,
    "no_website": REASON_NO_DOMAIN,
    "timeout": REASON_UNREACHABLE,
    "namesake": REASON_NAMESAKE,
    "ambiguous": REASON_AMBIGUOUS,
}

STORABLE_REASONS = frozenset(
    {
        REASON_NO_DOMAIN, REASON_UNREACHABLE, REASON_BOT_WALL, REASON_JS_RENDERED,
        REASON_PARKED, REASON_ROBOTS, REASON_OFF_DOMAIN_REDIRECT, REASON_NAMESAKE,
        REASON_REGISTRY_AMBIGUOUS, REASON_AMBIGUOUS, REASON_NO_EVIDENCE, REASON_LLM_FAILED,
    }
)

#: The two other closed vocabularies migration 110 constrains.  A rung that
#: invents a value for either is normalised here rather than at the database,
#: where a CHECK would abort a corpus pass three hundred companies in over a
#: word nobody reads.
SERVICE_MODELS = frozenset(
    {
        "temp_agency", "recruitment_selection", "job_board", "payrolling",
        "consultancy_or_outsourcing", "product", "services", "public_body", "unknown",
    }
)
TIERS = frozenset({"certain", "probable", "possible", "unknown", "direct_likely"})

#: Rung outcomes, as recorded in the trail.
OUTCOME_DECIDED = "decided"
OUTCOME_HANDED_DOWN = "handed_down"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_FAILED = "failed"

#: Nothing below a rung runs once a higher rung has answered at least this
#: confidently (research note section 2).  A rung that wants corroborating -
#: KBO's 78.1 in the VAT list only, rule 3 of section 2.1, which is exactly
#: 0.85 - says so by setting ``corroborate`` on its verdict, and the ladder
#: keeps walking while remembering the answer.
DECIDING_CONFIDENCE = 0.85

#: A website verdict is capped here (research note section 3.4): the classifier
#: read three pages of marketing copy, and 0.95 is as certain as that can make
#: anyone.
WEBSITE_CONFIDENCE_CAP = 0.95
#: …and here when a single quote survived verification, because one sentence
#: carrying a verdict is one sentence away from being a coincidence.
WEBSITE_ONE_QUOTE_CAP = 0.8
#: Below this a self-description is ambiguous, not a verdict.  The threshold is
#: deliberately above the model's floor: on the 55 measured sites it was never
#: wrong above 0.8 and once wrong at 0.8 (Randstad Digital).
WEBSITE_AMBIGUITY_FLOOR = 0.8

#: How long a verdict stands before it is re-established (FR-343).  A register
#: code is a filing and changes when the company files; a website verdict is a
#: page, and a *domain* can change hands - think-about-it.com now sells
#: Hyundais.  A promoted human correction never expires on a clock.
REGISTRY_STALENESS_DAYS = 365
WEBSITE_STALENESS_DAYS = 180
STALENESS_DAYS: dict[str, int | None] = {
    RUNG_KB: None,
    RUNG_SIGNALS: None,
    RUNG_REGISTRY: REGISTRY_STALENESS_DAYS,
    RUNG_EURES: REGISTRY_STALENESS_DAYS,
    RUNG_WEBSITE: WEBSITE_STALENESS_DAYS,
    RUNG_MANUAL: None,
}

#: Retry by *reason*, not by clock (research note section 4).  ``None`` means
#: "not automatically": either a human has to act, or the answer will not
#: change without one.  ``no_domain`` is ``None`` for the other reason - it is
#: event-driven, and becomes due the moment a domain is derived or entered.
RETRY_AFTER_DAYS: dict[str, int | None] = {
    REASON_NO_DOMAIN: None,
    REASON_UNREACHABLE: 3,          # then UNREACHABLE_LATER_DAYS
    REASON_BOT_WALL: 90,
    REASON_JS_RENDERED: 90,
    REASON_PARKED: 90,
    REASON_ROBOTS: None,            # the site asked; asking again is not an answer
    REASON_OFF_DOMAIN_REDIRECT: None,
    REASON_NAMESAKE: None,
    REASON_REGISTRY_AMBIGUOUS: None,
    REASON_AMBIGUOUS: None,
    REASON_NO_EVIDENCE: None,
    REASON_LLM_FAILED: 1,
}
UNREACHABLE_LATER_DAYS = 30

#: The cheapest next rung per reason (research note section 7.3): a
#: ``cannot_tell`` is a work item, not a dead end.  The key is for the product
#: to switch on; the sentence is English only - the nl/fr wording belongs with
#: the rest of the presentation strings, not here.
NEXT_STEP: dict[str, tuple[str, str]] = {
    REASON_NO_DOMAIN: ("add_website", "add this employer's website"),
    REASON_UNREACHABLE: ("wait", "the site did not answer; it will be tried again"),
    REASON_BOT_WALL: ("read_in_browser", "the site blocks automated reading; check it in a browser"),
    REASON_JS_RENDERED: ("read_in_browser", "the site needs a browser to show any text"),
    REASON_PARKED: ("add_website", "that domain is parked; add the real website"),
    REASON_ROBOTS: ("read_in_browser", "the site asks not to be read automatically"),
    REASON_OFF_DOMAIN_REDIRECT: ("add_website", "that domain now belongs to somebody else; add the real website"),
    REASON_NAMESAKE: ("confirm_identity", "another company shares this name; say which one this is"),
    REASON_REGISTRY_AMBIGUOUS: ("choose_registry_row", "two registered companies share this name; say which one this is"),
    REASON_AMBIGUOUS: ("read_evidence", "the site says both things; read the quotes and decide"),
    REASON_NO_EVIDENCE: ("research_again", "nothing quotable was found; try again or say what you know"),
    REASON_LLM_FAILED: ("research_again", "the classifier failed; try again"),
}


def next_step(reason: str | None) -> dict[str, str]:
    """The cheapest next rung for a ``cannot_tell`` reason, in machine and words."""
    key, words = NEXT_STEP.get(reason or "", ("research_again", "research this employer again"))
    return {"action": key, "label": words}


# ---------------------------------------------------------------------------
# What a rung returns
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """One rung's answer about one company, in the columns migration 110 holds."""

    kind: str = KIND_CANNOT_TELL
    confidence: float = 0.0
    rung: str = RUNG_KB
    method: str = METHOD_KNOWLEDGE_BASE
    service_model: str = "unknown"
    employer_role: str | None = None
    reason: str | None = None
    tier: str | None = None
    score: float | None = None
    postings: int | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    identity_evidence: dict[str, Any] | None = None
    audiences: dict[str, Any] | None = None
    summary: str | None = None
    anomalies: list[str] = field(default_factory=list)
    prompt_template: str | None = None
    prompt_version: str | None = None
    model: str | None = None
    llm_call_id: str | None = None
    #: Set by a rung whose answer is strong enough to store but which asks for
    #: the next rung to corroborate it (KBO rule 3, section 2.1).
    corroborate: bool = False

    @property
    def decided(self) -> bool:
        return self.kind in (KIND_AGENCY, KIND_EMPLOYER)

    @property
    def stops_the_ladder(self) -> bool:
        return self.decided and self.confidence >= DECIDING_CONFIDENCE and not self.corroborate

    def role(self) -> str:
        """The product axis (proposal section 4), derived, never invented.

        It has to agree with the kind, whatever a rung filled in: an ``agency``
        verdict rendered as "employer type not verified" would show the badge
        the ladder exists to stop being wrong about, and a ``cannot_tell`` is
        ``unverified`` by definition - that is what the word means here.
        """
        if self.kind == KIND_CANNOT_TELL:
            return ROLE_UNVERIFIED
        if self.employer_role in (ROLE_AGENCY, ROLE_BOARD, ROLE_DIRECT):
            return self.employer_role
        if self.kind == KIND_AGENCY:
            return ROLE_BOARD if self.service_model == "job_board" else ROLE_AGENCY
        return ROLE_DIRECT


@dataclass
class RungOutcome:
    """What one rung did.  ``verdict`` is present only when it concluded something."""

    outcome: str = OUTCOME_HANDED_DOWN
    verdict: Verdict | None = None
    #: Why it handed down, in the ``cannot_tell`` vocabulary when it maps onto
    #: one, so that the last rung's reason can become the stored reason.
    reason: str | None = None
    note: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: Evidence a rung wants carried on the final verdict even though it did not
    #: decide - this is how rung 0b's signals end up next to a ``cannot_tell``.
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Attempt:
    """One row of the trail: a rung that ran, and what came of it."""

    rung: str
    position: int
    method: str
    outcome: str
    kind: str | None = None
    confidence: float | None = None
    reason: str | None = None
    note: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    attempted_at: str = field(default_factory=utcnow)

    def as_evidence(self) -> dict[str, Any]:
        item = {
            "type": "attempt",
            "rung": self.rung,
            "method": self.method,
            "outcome": self.outcome,
            "at": self.attempted_at,
        }
        if self.reason:
            item["reason"] = self.reason
        if self.note:
            item["note"] = self.note[:300]
        return item


@dataclass
class Rung:
    """A step of the ladder: its place, its vocabulary and the callable."""

    position: int
    name: str
    method: str
    run: Callable[[RungContext], Any]
    #: What it needs before it is worth walking; a rung whose need is missing is
    #: ``unavailable`` with that as the note.
    needs_domain: bool = False
    #: Whether this rung can establish anything at all.  The knowledge base can
    #: only repeat what a rung already established, and the free signals decide
    #: nothing by construction; if neither of the rungs that *can* look was
    #: available, the ladder has not searched, and saying ``cannot_tell`` would
    #: claim a search that never happened.
    can_establish: bool = True


@dataclass
class RungContext:
    """Everything a rung is given.  A rung reads; the ladder writes."""

    company: dict[str, Any]
    signals: dict[str, Any] = field(default_factory=dict)
    egress: Any = None
    llm: Any = None
    campaign_id: str | None = None
    now: str = field(default_factory=utcnow)
    #: Verdicts earlier rungs produced but that did not stop the ladder, so a
    #: corroborating rung can see what it is corroborating.
    so_far: list[Verdict] = field(default_factory=list)
    #: False for a dry run: the ladder answers and writes nothing, the rung-0b
    #: snapshot included.  A "what would this say" must not leave a trail.
    store: bool = True

    @property
    def company_id(self) -> str:
        return str(self.company.get("company_id") or self.company.get("id") or "")

    @property
    def name(self) -> str:
        return str(self.company.get("company_name") or self.company.get("name") or "")

    @property
    def domain(self) -> str:
        return str(self.company.get("company_domain") or self.company.get("domain") or "")

    @property
    def vacancy_count(self) -> int:
        return int(self.company.get("vacancy_count") or 0)


@dataclass
class Resolution:
    """The ladder's answer for one company: the verdict and how it was reached."""

    company_id: str
    company_name: str = ""
    vacancy_count: int = 0
    verdict: Verdict | None = None
    attempts: list[Attempt] = field(default_factory=list)
    reused: bool = False
    stored: bool = False
    retry_after: str | None = None
    expires_at: str | None = None

    @property
    def kind(self) -> str:
        return self.verdict.kind if self.verdict else KIND_CANNOT_TELL

    @property
    def needs_review(self) -> bool:
        """Anomalies queue a row for review whatever the verdict says (NFR-205)."""
        return bool(self.verdict and self.verdict.anomalies)

    def as_dict(self) -> dict[str, Any]:
        verdict = self.verdict
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "vacancy_count": self.vacancy_count,
            "kind": self.kind,
            "employer_role": verdict.role() if verdict else ROLE_UNVERIFIED,
            "confidence": verdict.confidence if verdict else 0.0,
            "rung": verdict.rung if verdict else None,
            "method": verdict.method if verdict else None,
            "reason": verdict.reason if verdict else None,
            "next_step": next_step(verdict.reason) if verdict and verdict.reason else None,
            "summary": verdict.summary if verdict else None,
            "evidence": list(verdict.evidence) if verdict else [],
            "anomalies": list(verdict.anomalies) if verdict else [],
            "needs_review": self.needs_review,
            "reused": self.reused,
            "stored": self.stored,
            "retry_after": self.retry_after,
            "expires_at": self.expires_at,
            "attempts": [
                {
                    "rung": a.rung,
                    "position": a.position,
                    "outcome": a.outcome,
                    "reason": a.reason,
                    "note": a.note,
                    "duration_ms": a.duration_ms,
                }
                for a in self.attempts
            ],
        }


# ---------------------------------------------------------------------------
# Rungs the other slices own
# ---------------------------------------------------------------------------

#: A slice wires its rung in by calling :func:`register_rung` at import time.
_registered: dict[str, Callable[[RungContext], Any]] = {}

#: …and, as a convenience for a slice that has not, these paths are tried once
#: each.  They are the agreed module names, not a search: a rung that lives
#: somewhere else registers itself and this list never sees it.  A path that
#: does not import is not an error - it is a rung this build does not have,
#: which the trail records as ``unavailable``.
PROVIDER_PATHS: dict[str, tuple[tuple[str, str], ...]] = {
    RUNG_SIGNALS: (
        ("dreamjob.pipeline.employer_signals", "signal_snapshot"),
    ),
    RUNG_REGISTRY: (
        ("dreamjob.pipeline.employer_registry_rung", "registry_rung"),
    ),
    # Proposal N4, phase 1b.  Until it ships the trail says "unavailable",
    # which is the truthful thing to say about a rung nobody has walked.
    RUNG_EURES: (
        ("dreamjob.pipeline.employer_eures_rung", "eures_sector_rung"),
        ("dreamjob.pipeline.employer_registry_rung", "eures_sector_rung"),
    ),
    RUNG_WEBSITE: (
        ("dreamjob.pipeline.employer_website_rung", "website_rung"),
    ),
}


def register_rung(name: str, fn: Callable[[RungContext], Any] | None) -> None:
    """Wire a rung implementation in (or, with ``None``, take it out again)."""
    if fn is None:
        _registered.pop(name, None)
    else:
        _registered[name] = fn


def rung_provider(name: str) -> Callable[[RungContext], Any] | None:
    """The implementation of a rung, or ``None`` when this build has none."""
    if name in _registered:
        return _registered[name]
    for module_name, attribute in PROVIDER_PATHS.get(name, ()):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - a rung that will not import is a rung we do not have
            continue
        fn = getattr(module, attribute, None)
        if callable(fn):
            return fn
    return None


# ---------------------------------------------------------------------------
# Rung 0: the knowledge base
# ---------------------------------------------------------------------------


async def knowledge_base_rung(ctx: RungContext) -> RungOutcome:
    """A verdict already on record, or a promoted human correction.  0 requests.

    A shared correction beats a website verdict and a ``cannot_tell``; it does
    **not** beat the register (research note section 8).  If the register says
    78.2 and a human says employer, neither wins silently: the register's
    verdict stands, the disagreement is recorded as an anomaly, and the row is
    queued for review - the register is occasionally stale, the human is
    occasionally wrong, and a product that resolved that on its own would be
    guessing with somebody else's confidence.
    """
    company_id = ctx.company_id
    stored = await asyncio.to_thread(repo.read_verdict, company_id)
    correction = await asyncio.to_thread(repo.shared_correction, company_id)

    if correction and stored and stored.get("method") == METHOD_REGISTRY:
        verdict = _verdict_from_row(stored)
        if verdict and verdict.kind != str(correction.get("kind") or ""):
            verdict.anomalies = [
                *verdict.anomalies,
                "a shared human correction disagrees with the register: "
                f"{correction.get('kind')} - {str(correction.get('note') or '')[:160]}",
            ]
            return RungOutcome(OUTCOME_DECIDED, verdict=verdict, note="registry/correction conflict")

    if correction:
        kind = str(correction.get("kind") or "")
        if kind in (KIND_AGENCY, KIND_EMPLOYER):
            return RungOutcome(
                OUTCOME_DECIDED,
                verdict=Verdict(
                    kind=kind,
                    confidence=1.0,
                    rung=RUNG_MANUAL,
                    method=METHOD_CORRECTION,
                    summary=str(correction.get("note") or "")[:400] or None,
                    evidence=[
                        {
                            "type": "correction",
                            "supports": kind,
                            "note": str(correction.get("note") or "")[:400],
                            "url": correction.get("evidence_url"),
                            "established_at": correction.get("promoted_at")
                            or correction.get("created_at"),
                        }
                    ],
                ),
                note="a promoted correction; the machine keeps running but does not overrule it",
            )

    if not stored:
        return RungOutcome(OUTCOME_HANDED_DOWN, note="no verdict on record")
    if _expired(stored.get("expires_at"), ctx.now):
        return RungOutcome(OUTCOME_HANDED_DOWN, note="the verdict on record has gone stale")
    verdict = _verdict_from_row(stored)
    if verdict is None or not verdict.decided:
        return RungOutcome(
            OUTCOME_HANDED_DOWN,
            reason=str(stored.get("reason") or "") or None,
            note="the verdict on record is a cannot_tell; the ladder runs again",
        )
    return RungOutcome(OUTCOME_DECIDED, verdict=verdict, note="already established")


# ---------------------------------------------------------------------------
# Rung 0b: the free signals - queue order and evidence, never a verdict
# ---------------------------------------------------------------------------


async def signals_rung(ctx: RungContext) -> RungOutcome:
    """What the postings suggest.  This rung cannot decide, by construction.

    The detector's band is a good ordering and a fair thing to *say* - "the
    adverts read like an agency's" - and it is measured at P 1.00 / R 0.80 at
    probable+ without any register (proposal section 2.3).  It is still not a
    verdict here, because this ladder writes a shared fact that a job seeker
    can check, and what makes that checkable is a register code or a quoted
    sentence.  A score is neither.  So the signals travel as evidence and as
    priority, the product shows them next to "employer type not verified", and
    the rungs below are what turn a suspicion into a fact.
    """
    snapshot: dict[str, Any] = {
        "vacancy_count": ctx.vacancy_count,
        "hint": str(ctx.company.get("hint") or "none"),
        "tier": ctx.company.get("tier"),
        "score": ctx.company.get("score"),
        "signals": [],
        "priority": float(ctx.vacancy_count),
    }
    provider = rung_provider(RUNG_SIGNALS)
    if provider is not None:
        produced = await _call(provider, ctx)
        if isinstance(produced, RungOutcome):
            # A detector that answers in the rung protocol still does not get to
            # decide: its verdict is demoted to evidence here rather than in
            # some later "except this one" branch.
            if produced.verdict is not None:
                snapshot["tier"] = produced.verdict.tier or snapshot["tier"]
                snapshot["score"] = (
                    produced.verdict.score if produced.verdict.score is not None
                    else snapshot["score"]
                )
                snapshot["signals"] = list(produced.verdict.evidence)
            snapshot["signals"] = snapshot["signals"] or list(produced.evidence)
        elif isinstance(produced, dict):
            snapshot.update({k: v for k, v in produced.items() if v is not None})

    snapshot["hint"] = _hint_for(snapshot)
    tier = str(snapshot.get("tier") or "")
    snapshot["tier"] = tier if tier in TIERS else None
    snapshot["priority"] = _priority(snapshot, ctx.vacancy_count)
    ctx.signals.update(snapshot)
    if ctx.store:
        await asyncio.to_thread(repo.record_signals, ctx.company_id, snapshot)

    evidence = [
        {**item, "type": item.get("type") or "signal"}
        for item in snapshot.get("signals") or []
        if isinstance(item, dict)
    ]
    return RungOutcome(
        OUTCOME_HANDED_DOWN,
        note=(
            f"free signals: {snapshot['hint']}"
            + (f", band {snapshot['tier']}" if snapshot.get("tier") else "")
            + " - they order the queue and explain, they do not decide"
        ),
        detail={k: snapshot[k] for k in ("hint", "tier", "score", "priority")},
        evidence=evidence,
    )


#: A suspicion, in a vocabulary that cannot be mistaken for a verdict.  There
#: is no "agency" here on purpose: rung 0b has no way to earn that word.
HINTS = frozenset({"suspected_agency", "suspected_direct", "none"})


def _hint_for(snapshot: dict[str, Any]) -> str:
    """The suspicion the free signals support, never more than that."""
    tier = str(snapshot.get("tier") or "")
    if tier in ("certain", "probable"):
        return "suspected_agency"
    if tier == "direct_likely":
        return "suspected_direct"
    hint = str(snapshot.get("hint") or "none")
    return hint if hint in HINTS else "none"


def _priority(snapshot: dict[str, Any], vacancy_count: int) -> float:
    """Queue order: the rows a wrong answer would damage, most first.

    Vacancy count dominates, because that is what is at stake - 56 postings at
    NOEL FRANKLIN, one at a name nobody will see again - and a suspicion adds a
    nudge rather than a rank of its own, so an unsuspected 200-vacancy employer
    is still resolved before a suspected single-posting one.
    """
    bonus = {"suspected_agency": 1.5, "suspected_direct": 0.5}.get(
        str(snapshot.get("hint") or "none"), 0.0
    )
    return round(float(vacancy_count) + bonus, 3)


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def default_ladder() -> list[Rung]:
    """The rungs, in order.  Cheapest and most certain first (research note section 2)."""
    return [
        Rung(0, RUNG_KB, METHOD_KNOWLEDGE_BASE, knowledge_base_rung, can_establish=False),
        Rung(1, RUNG_SIGNALS, METHOD_SIGNALS, signals_rung, can_establish=False),
        Rung(2, RUNG_REGISTRY, METHOD_REGISTRY, _provider_rung(RUNG_REGISTRY)),
        Rung(3, RUNG_EURES, METHOD_EURES, _provider_rung(RUNG_EURES)),
        Rung(4, RUNG_WEBSITE, METHOD_WEBSITE, _provider_rung(RUNG_WEBSITE), needs_domain=True),
    ]


def _provider_rung(name: str) -> Callable[[RungContext], Any]:
    async def run(ctx: RungContext) -> RungOutcome:
        provider = rung_provider(name)
        if provider is None:
            return RungOutcome(
                OUTCOME_UNAVAILABLE,
                note=f"the {name} rung is not installed in this build",
            )
        return _coerce(await _call(provider, ctx), rung=name)

    return run


async def resolve_employer_kind(
    company_id: str,
    *,
    egress: Any = None,
    llm: Any = None,
    ladder: list[Rung] | None = None,
    campaign_id: str | None = None,
    force: bool = False,
    store: bool = True,
    now: str | None = None,
) -> Resolution:
    """Walk the ladder for one company and record what it established (FR-341).

    Stops at the first rung that answers at :data:`DECIDING_CONFIDENCE` or
    better.  Every rung that ran is recorded - in the trail table and in the
    verdict's own evidence - including the ones that handed down and the ones
    this build does not have, because "we could not tell" is only an answer
    when it says what was tried.

    ``force`` re-walks a company whose verdict is still fresh, which is what an
    on-demand "research this employer again" means.  ``store=False`` returns the
    answer without writing it, for a dry run.
    """
    moment = now or utcnow()
    company = await asyncio.to_thread(repo.company, company_id)
    if company is None:
        raise KeyError(f"No such company {company_id}")

    ctx = RungContext(
        company=company, egress=egress, llm=llm, campaign_id=campaign_id, now=moment, store=store
    )
    resolution = Resolution(
        company_id=company_id,
        company_name=ctx.name,
        vacancy_count=ctx.vacancy_count,
    )

    decided: Verdict | None = None
    carried: list[dict[str, Any]] = []
    anomalies: list[str] = []
    last_reason: str | None = None
    last_reason_rung: str = RUNG_KB
    ran_a_rung = False

    for rung in ladder or default_ladder():
        if force and rung.name == RUNG_KB:
            resolution.attempts.append(
                Attempt(
                    rung=rung.name, position=rung.position, method=rung.method,
                    outcome=OUTCOME_HANDED_DOWN, note="skipped: this is a refresh",
                    attempted_at=moment,
                )
            )
            continue
        if rung.needs_domain and not ctx.domain:
            resolution.attempts.append(
                Attempt(
                    rung=rung.name, position=rung.position, method=rung.method,
                    outcome=OUTCOME_UNAVAILABLE, reason=REASON_NO_DOMAIN,
                    note="no website is known for this employer", attempted_at=moment,
                )
            )
            last_reason, last_reason_rung = REASON_NO_DOMAIN, rung.name
            continue

        started = time.monotonic()
        try:
            outcome = _coerce(await _call(rung.run, ctx), rung=rung.name)
        except Exception as exc:  # noqa: BLE001 - one rung never stops the ladder
            log.exception("Rung %s failed for company %s", rung.name, company_id)
            outcome = RungOutcome(
                OUTCOME_FAILED,
                reason=REASON_UNREACHABLE,
                note=f"{type(exc).__name__}: {exc}"[:200],
            )
        elapsed = int((time.monotonic() - started) * 1000)

        verdict = outcome.verdict
        if verdict is not None:
            # A rung may spell its own place differently - the signal detector
            # calls itself "0b" - and the schema's vocabulary is closed, so the
            # ladder's name for the rung wins over the rung's name for itself.
            if verdict.rung == RUNG_KB or verdict.rung not in _METHOD_FOR_RUNG:
                verdict.rung = rung.name
            verdict.method = verdict.method or rung.method
            if rung.name == RUNG_KB and outcome.outcome == OUTCOME_DECIDED:
                resolution.reused = True

        attempt = Attempt(
            rung=rung.name,
            position=rung.position,
            method=rung.method,
            outcome=outcome.outcome,
            kind=verdict.kind if verdict else None,
            confidence=verdict.confidence if verdict else None,
            reason=outcome.reason or (verdict.reason if verdict else None),
            note=outcome.note,
            detail=outcome.detail,
            duration_ms=elapsed,
            attempted_at=moment,
        )
        resolution.attempts.append(attempt)
        carried.extend(outcome.evidence)
        if verdict is not None and not verdict.decided:
            # A refusal's evidence is worth as much as a verdict's: the
            # ambiguous case is stored *with the quotes on both sides* so the
            # job seeker reads the sentences and decides (research note 7.4).
            carried.extend(verdict.evidence)
        if verdict is not None and verdict.anomalies:
            anomalies.extend(verdict.anomalies)
        if rung.can_establish and outcome.outcome in (
            OUTCOME_DECIDED, OUTCOME_HANDED_DOWN, OUTCOME_FAILED
        ):
            ran_a_rung = True
        folded = fold_reason(attempt.reason)
        if folded:
            last_reason, last_reason_rung = folded, rung.name

        if verdict is not None:
            ctx.so_far.append(verdict)
            if verdict.decided and (decided is None or verdict.confidence > decided.confidence):
                decided = verdict
            if verdict.stops_the_ladder:
                break

    if decided is not None:
        verdict = _certify(decided)
    elif not ran_a_rung:
        # Nothing was even attempted: no rung of this build could look.  Writing
        # "cannot_tell" here would claim a search that did not happen, so the
        # company stays in the queue and the caller is told why.
        resolution.verdict = None
        await _record_trail(resolution, store=store)
        log.info(
            "Employer kind for %s (%s): no rung was available; nothing stored",
            resolution.company_name, company_id,
        )
        return resolution
    else:
        verdict = _cannot_tell(
            last_reason or REASON_UNREACHABLE,
            rung=last_reason_rung if last_reason else _last_rung(resolution),
        )

    verdict.anomalies = [*dict.fromkeys([*verdict.anomalies, *anomalies])]
    verdict.evidence = _evidence_for(verdict, carried, resolution.attempts)
    resolution.verdict = verdict
    resolution.expires_at = _expires_at(verdict, moment)
    resolution.retry_after = (
        retry_after_for(
            verdict.reason,
            await asyncio.to_thread(_attempt_count, company_id, store),
            moment,
        )
        if verdict.kind == KIND_CANNOT_TELL
        else None
    )

    await _record_trail(resolution, store=store)
    if store:
        resolution.stored = await asyncio.to_thread(
            repo.write_verdict, company_id, _as_row(resolution, moment)
        )
        if resolution.stored:
            await asyncio.to_thread(_record_provenance, company_id, verdict)
    log.info(
        "Employer kind for %s (%s): %s at %.2f from rung %s%s",
        resolution.company_name, company_id, verdict.kind, verdict.confidence, verdict.rung,
        f" ({verdict.reason})" if verdict.reason else "",
    )
    return resolution


def _last_rung(resolution: Resolution) -> str:
    for attempt in reversed(resolution.attempts):
        if attempt.outcome in (OUTCOME_HANDED_DOWN, OUTCOME_FAILED):
            return attempt.rung
    return RUNG_KB


async def _record_trail(resolution: Resolution, *, store: bool) -> None:
    if not store:
        return
    for attempt in resolution.attempts:
        await asyncio.to_thread(
            repo.record_attempt,
            resolution.company_id,
            {
                "rung": attempt.rung,
                "position": attempt.position,
                "method": attempt.method,
                "outcome": attempt.outcome,
                "kind": attempt.kind,
                "confidence": attempt.confidence,
                "reason": attempt.reason,
                "detail": {**attempt.detail, "note": attempt.note} if attempt.note else attempt.detail,
                "duration_ms": attempt.duration_ms,
                "retry_after": resolution.retry_after if attempt.rung == (
                    resolution.verdict.rung if resolution.verdict else ""
                ) else None,
            },
        )


def _attempt_count(company_id: str, store: bool) -> int:
    """How many times the ladder has now walked this company, this pass included.

    The trail's own counter is bumped when the row is written, which happens
    after this is read - so the answer is "what the trail says, plus this one",
    and an ``unreachable`` escalates from three days to thirty on the second
    attempt rather than the third.
    """
    if not store:
        return 1
    rows = repo.attempts_for(company_id)
    return 1 + max((int(r.get("attempts") or 0) for r in rows), default=0)


# ---------------------------------------------------------------------------
# The rules the orchestration enforces on any rung's answer
# ---------------------------------------------------------------------------


def _certify(verdict: Verdict) -> Verdict:
    """Apply the storage rules of NFR-402 and research note section 3.4.

    A verdict that cannot survive them is not repaired and not discarded: it
    becomes a ``cannot_tell`` with the reason that says which rule it failed,
    which is the honest record of "the classifier answered, and the answer was
    not good enough to write down".
    """
    supporting = [
        item for item in verdict.evidence if _stands_alone(item, verdict.kind, verdict.method)
    ]
    if not supporting:
        return _cannot_tell(REASON_NO_EVIDENCE, rung=verdict.rung, anomalies=verdict.anomalies)

    if verdict.method == METHOD_WEBSITE:
        quotes = [item for item in supporting if item.get("type", "quote") == "quote"]
        cap = WEBSITE_ONE_QUOTE_CAP if len(quotes) <= 1 else WEBSITE_CONFIDENCE_CAP
        verdict.confidence = min(verdict.confidence, cap)
        if verdict.confidence < WEBSITE_AMBIGUITY_FLOOR:
            # The Randstad Digital case: the site says both things.  Store the
            # quotes on both sides and let the job seeker read them.
            return _cannot_tell(
                REASON_AMBIGUOUS, rung=verdict.rung, anomalies=verdict.anomalies,
                evidence=list(verdict.evidence),
            )
    verdict.confidence = max(0.0, min(1.0, float(verdict.confidence)))
    verdict.employer_role = verdict.role()
    return verdict


def _stands_alone(item: Any, kind: str, method: str) -> bool:
    """Can this evidence item carry a verdict on its own?

    The general rule is *checkability*: an item has to point at something a
    person who disagrees can go and look at - a filed activity code, a page
    with a verbatim sentence on it, or a human's note.

    The model is the one participant that could invent its own evidence, so a
    verdict from the website classifier is held to the stricter rule: it needs
    a quote the rung *verified against the page text* (research note section
    3.4 - 161 of 167 quotes verified verbatim; the six failures were the model
    splicing two menu labels with an ellipsis).  ``"verified": False`` is a
    refusal wherever it appears.

    A statistic never stands alone, whoever supplies it: a share, a score or a
    band is a summary of a hundred adverts and there is nothing in it a job
    seeker can check.  In practice rung 0b cannot decide at all, so this is a
    second lock on a door that is already shut.
    """
    if not isinstance(item, dict):
        return False
    supports = str(item.get("supports") or "")
    if supports and supports != kind:
        return False
    if item.get("verified") is False:
        return False
    quote = str(item.get("quote") or "").strip()
    if method == METHOD_WEBSITE:
        return bool(quote) and bool(item.get("verified"))
    if item.get("code") and (item.get("registry") or item.get("url")):
        return True
    if str(item.get("type") or "") == "correction":
        return bool(str(item.get("note") or "").strip())
    if quote and (item.get("url") or item.get("verified")):
        return True
    # The registry and probe rungs read a filing, not prose: what they can
    # always show is the page they read it on and the code they read.
    return bool(item.get("url")) and bool(str(item.get("detail") or "").strip())


def _cannot_tell(
    reason: str,
    *,
    rung: str = RUNG_KB,
    anomalies: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> Verdict:
    return Verdict(
        kind=KIND_CANNOT_TELL,
        confidence=0.0,
        rung=rung if rung in _METHOD_FOR_RUNG else RUNG_KB,
        method=_METHOD_FOR_RUNG.get(rung, METHOD_KNOWLEDGE_BASE),
        reason=fold_reason(reason) or REASON_UNREACHABLE,
        employer_role=ROLE_UNVERIFIED,
        anomalies=list(anomalies or []),
        evidence=list(evidence or []),
    )


_METHOD_FOR_RUNG = {
    RUNG_KB: METHOD_KNOWLEDGE_BASE,
    RUNG_SIGNALS: METHOD_SIGNALS,
    RUNG_REGISTRY: METHOD_REGISTRY,
    RUNG_EURES: METHOD_EURES,
    RUNG_WEBSITE: METHOD_WEBSITE,
    RUNG_MANUAL: METHOD_CORRECTION,
}


def _evidence_for(
    verdict: Verdict, carried: list[dict[str, Any]], attempts: list[Attempt]
) -> list[dict[str, Any]]:
    """The deciding evidence, then what else was measured, then the trail.

    Order is the reading order: why we say this, what else we saw, what was
    tried.  The trail is last because it is the answer to "and what if I do not
    believe you", which is a question asked after the claim, not before it.
    """
    seen: list[dict[str, Any]] = []
    for item in [*verdict.evidence, *carried]:
        if isinstance(item, dict) and item not in seen:
            seen.append({**item, "type": item.get("type") or ("quote" if item.get("quote") else "signal")})
    return [*seen, *[a.as_evidence() for a in attempts]]


def _as_row(resolution: Resolution, moment: str) -> dict[str, Any]:
    """The verdict in the columns migration 110 holds."""
    verdict = resolution.verdict
    if verdict is None:  # pragma: no cover - callers reach here only with one
        raise ValueError("a resolution with no verdict has no row")
    service_model = str(verdict.service_model or "unknown")
    tier = str(verdict.tier) if verdict.tier else None
    return {
        "kind": verdict.kind,
        "employer_role": verdict.role(),
        "service_model": service_model if service_model in SERVICE_MODELS else "unknown",
        "confidence": round(float(verdict.confidence), 4),
        "rung": verdict.rung,
        "method": verdict.method,
        "tier": tier if tier in TIERS else None,
        "score": verdict.score,
        "postings": verdict.postings if verdict.postings is not None else resolution.vacancy_count,
        "reason": verdict.reason,
        "evidence": to_json(verdict.evidence),
        "identity_evidence": to_json(verdict.identity_evidence) if verdict.identity_evidence else None,
        "audiences": to_json(verdict.audiences) if verdict.audiences else None,
        "summary": verdict.summary,
        "anomalies": to_json(verdict.anomalies) if verdict.anomalies else None,
        "prompt_template": verdict.prompt_template,
        "prompt_version": verdict.prompt_version,
        "model": verdict.model,
        "llm_call_id": verdict.llm_call_id,
        "established_at": moment,
        "refreshed_at": moment,
        "expires_at": resolution.expires_at,
        "retry_after": resolution.retry_after,
    }


def _record_provenance(company_id: str, verdict: Verdict) -> None:
    """One provenance row per verdict, so /companies/{id}/provenance shows it too."""
    try:
        from dreamjob.db.repositories import companies as companies_repo  # noqa: PLC0415

        raw_document_id = next(
            (
                str(item["raw_document_id"])
                for item in verdict.evidence
                if isinstance(item, dict) and item.get("raw_document_id")
            ),
            None,
        )
        companies_repo.record_field_provenance(
            company_id,
            {"employer_kind": {"confidence": verdict.confidence, "raw_document_id": raw_document_id}},
            adapter_key="research.employer_kind",
        )
    except Exception:  # noqa: BLE001 - provenance is a record of the verdict, not the verdict
        log.debug("Could not record employer-kind provenance for %s", company_id, exc_info=True)


# ---------------------------------------------------------------------------
# Freshness and retry
# ---------------------------------------------------------------------------


def _expires_at(verdict: Verdict, moment: str) -> str | None:
    if verdict.kind == KIND_CANNOT_TELL:
        return None
    days = STALENESS_DAYS.get(verdict.rung)
    override = _policy_days()
    if days is None:
        return None
    if override is not None:
        days = min(days, override)
    return _plus_days(moment, days)


def _policy_days() -> int | None:
    """The operator's staleness setting for ``employer_kind``, when there is one.

    It shortens both windows rather than replacing either: an operator who says
    "re-establish these every 90 days" means the registry verdicts too, and a
    single number cannot lengthen a website verdict past the reason it is 180
    days - a domain can change hands.
    """
    try:
        from dreamjob.pipeline import knowledge_base  # noqa: PLC0415 - avoids a cycle

        value = knowledge_base.get_staleness_policy().get("employer_kind")
        return int(value) if value else None
    except Exception:  # noqa: BLE001 - a missing policy is the default policy
        return None


def retry_after_for(reason: str | None, attempts: int = 1, now: str | None = None) -> str | None:
    """When a ``cannot_tell`` is worth another rung.  ``None`` means "not on a clock"."""
    if reason is None:
        return None
    days = RETRY_AFTER_DAYS.get(reason, None)
    if reason == REASON_UNREACHABLE and attempts > 1:
        days = UNREACHABLE_LATER_DAYS
    if days is None:
        return None
    return _plus_days(now or utcnow(), days)


def _plus_days(moment: str, days: int) -> str:
    try:
        base = datetime.fromisoformat(moment)
    except ValueError:
        base = datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return (base + timedelta(days=days)).isoformat(timespec="seconds")


def _expired(expires_at: Any, moment: str) -> bool:
    return bool(expires_at) and str(expires_at) <= moment


# ---------------------------------------------------------------------------
# Adapting whatever a rung returns
# ---------------------------------------------------------------------------


async def _call(fn: Callable[[RungContext], Any], ctx: RungContext) -> Any:
    """Call a rung, whether it was written sync or async, and however it asks.

    Two calling conventions exist in the tree and both are legitimate.  A rung
    written for this ladder takes the :class:`RungContext` - whatever it calls
    the parameter - and reads ``company_id``, ``name``, ``domain``, ``egress``
    and ``llm`` off it.  A rung written to stand on its own takes
    ``(company_row, *, egress=…, locations=…)`` and knows nothing about this
    module; the registry rung is one, because its own pass runs against the
    corpus without the ladder.

    The discriminator is the *keyword* parameters, not the name of the first
    one: a rung that asks for ``egress`` or ``locations`` by name is asking for
    the stand-alone convention, and everything else is handed the context.
    Reading the first parameter's name would have been the obvious rule and is
    the wrong one - the signal rung's parameter is also called ``company`` and
    it wants the context object.
    """
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # a builtin or a C callable: assume the protocol
        parameters = {}
    keyword_only = {
        name for name, p in parameters.items() if p.kind is p.KEYWORD_ONLY
    }
    if keyword_only & {"egress", "locations", "adapter"}:
        argument: Any = ctx.company
        if "egress" in keyword_only:
            kwargs["egress"] = ctx.egress
        if "llm" in keyword_only:
            kwargs["llm"] = ctx.llm
        if "locations" in keyword_only:
            kwargs["locations"] = _locations(ctx)
    else:
        argument = ctx
    result = fn(argument, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _locations(ctx: RungContext) -> tuple[str, ...]:
    """Where this employer's vacancies are - the register's namesake tie-break."""
    location = str(ctx.company.get("location") or "").strip()
    return (location,) if location else ()


def _coerce(result: Any, *, rung: str) -> RungOutcome:
    """Accept whatever a rung returns and turn it into one outcome.

    A ``RungOutcome``, a :class:`Verdict`, a plain row dict, ``None``, or the
    result object a sibling slice defines for itself - the registry rung's
    ``RegistryOutcome``, the website rung's verdict object.  Being strict about
    the *wrapper* would buy nothing but a broken build the day one of them
    changes shape; being strict about the *content* - evidence, confidence, the
    reason vocabulary - is :func:`_certify`'s job and is not relaxed here.
    """
    if result is None:
        return RungOutcome(OUTCOME_HANDED_DOWN, note="no answer")
    if isinstance(result, RungOutcome):
        return result
    if isinstance(result, Verdict):
        return RungOutcome(
            OUTCOME_DECIDED if result.decided else OUTCOME_HANDED_DOWN,
            verdict=result,
            reason=result.reason,
        )
    if isinstance(result, dict):
        return _outcome_from_row(result, rung=rung)
    # A sibling slice's own result object: a verdict inside a wrapper that also
    # says why the next rung should still run.
    inner = getattr(result, "verdict", None)
    if inner is not None or hasattr(result, "hand_down"):
        return _outcome_from_wrapper(result, inner, rung=rung)
    row = _row_of(result)
    if row is not None:
        return _outcome_from_row(row, rung=rung)
    log.warning("Rung %s returned %r, which is not an answer this ladder understands", rung, result)
    return RungOutcome(OUTCOME_HANDED_DOWN, note="unrecognised answer")


def _outcome_from_row(row: dict[str, Any], *, rung: str) -> RungOutcome:
    verdict = _verdict_from_row({**row, "rung": row.get("rung") or rung})
    note = str(row.get("note") or row.get("hand_down") or row.get("detail") or "")[:200]
    if verdict is None:
        return RungOutcome(OUTCOME_HANDED_DOWN, note=note)
    return RungOutcome(
        OUTCOME_DECIDED if verdict.decided else OUTCOME_HANDED_DOWN,
        verdict=verdict,
        reason=verdict.reason,
        note=note,
    )


def _outcome_from_wrapper(result: Any, inner: Any, *, rung: str) -> RungOutcome:
    """A rung result that wraps a verdict and a reason to keep going.

    ``hand_down`` is the sibling slices' word for "strong enough to store, not
    strong enough to leave alone" - KBO's rule 3, where 78.1 sits beside
    fifteen IT codes and the site still has to be read.  That is exactly this
    ladder's ``corroborate``, so it is carried across rather than re-invented.
    """
    hand_down = str(getattr(result, "hand_down", "") or "")[:300]
    decision = str(getattr(result, "decision", "") or "")
    detail = getattr(result, "as_dict", None)
    verdict = None
    if inner is not None:
        row = _row_of(inner)
        verdict = _verdict_from_row({**(row or {}), "rung": (row or {}).get("rung") or rung})
    if verdict is not None and verdict.decided:
        verdict.corroborate = bool(hand_down)
        return RungOutcome(
            OUTCOME_DECIDED, verdict=verdict, note=hand_down,
            detail=_small(detail() if callable(detail) else {}),
        )
    outcome = OUTCOME_UNAVAILABLE if decision == "unavailable" else OUTCOME_HANDED_DOWN
    reason = REASON_REGISTRY_AMBIGUOUS if decision == "ambiguous" else None
    return RungOutcome(
        outcome,
        reason=reason,
        note=hand_down or decision,
        detail=_small(detail() if callable(detail) else {}),
    )


def _row_of(obj: Any) -> dict[str, Any] | None:
    """A sibling slice's verdict object as the row it would write."""
    for name, kwargs in (("as_row", {}), ("to_row", {"established_at": utcnow()})):
        method = getattr(obj, name, None)
        if callable(method):
            try:
                row = method(**kwargs)
            except TypeError:
                continue
            if isinstance(row, dict):
                return row
    return None


def _small(detail: Any) -> dict[str, Any]:
    """A rung's detail, trimmed to what the trail can carry without bloating it."""
    if not isinstance(detail, dict):
        return {}
    keep = ("registry", "decision", "legal_id", "registered_name", "staffing_codes",
            "main_division", "source_url", "duration_ms", "pages_read", "domain")
    return {k: detail[k] for k in keep if k in detail}


def fold_reason(reason: Any) -> str | None:
    """A reason in the schema's spelling, or ``None`` when there is none."""
    text = str(reason or "").strip().lower()
    if not text:
        return None
    text = REASON_ALIASES.get(text, text)
    return text if text in STORABLE_REASONS else None


def _verdict_from_row(row: dict[str, Any]) -> Verdict | None:
    """A stored row or a rung's dict, as a :class:`Verdict`."""
    kind = str(row.get("kind") or "")
    if kind not in (KIND_AGENCY, KIND_EMPLOYER, KIND_CANNOT_TELL):
        return None
    evidence = row.get("evidence")
    anomalies = row.get("anomalies")
    return Verdict(
        kind=kind,
        confidence=float(row.get("confidence") or 0.0),
        rung=str(row.get("rung") or RUNG_KB),
        method=str(row.get("method") or METHOD_KNOWLEDGE_BASE),
        service_model=str(row.get("service_model") or "unknown"),
        employer_role=row.get("employer_role") or None,
        reason=fold_reason(row.get("reason")),
        tier=row.get("tier") or None,
        score=row.get("score"),
        postings=row.get("postings"),
        evidence=list(evidence if isinstance(evidence, list) else from_json(evidence, []) or []),
        identity_evidence=(
            row.get("identity_evidence")
            if isinstance(row.get("identity_evidence"), dict)
            else from_json(row.get("identity_evidence"), None)
        ),
        audiences=(
            row.get("audiences")
            if isinstance(row.get("audiences"), dict)
            else from_json(row.get("audiences"), None)
        ),
        summary=row.get("summary") or None,
        anomalies=list(anomalies if isinstance(anomalies, list) else from_json(anomalies, []) or []),
        prompt_template=row.get("prompt_template"),
        prompt_version=row.get("prompt_version"),
        model=row.get("model"),
        llm_call_id=row.get("llm_call_id"),
        corroborate=bool(row.get("corroborate")),
    )


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

#: Concurrency is a company-level number and it buys less than it looks like it
#: does: the register is one host at 0.5 rps and EURES publishes
#: ``Crawl-delay: 10``, so those two rungs are serialised by the egress
#: client's per-domain limiter whatever this says.  What it does parallelise is
#: the website rung, where every company is a different domain, and the LLM
#: calls behind it (measured at six concurrent, 2.4 s each).
DEFAULT_CONCURRENCY = 4
DEFAULT_LIMIT = 200


@dataclass
class EmployerResolutionReport:
    """What one pass established, in the terms the coverage screen asks for.

    ``requested``/``resolved``/``cannot_tell``/``failed`` are named to match
    ``employer_resolve_run`` (migration 113) so the journal row can be filled
    from this without a translation layer.
    """

    requested: int = 0
    visited: int = 0
    resolved: int = 0
    cannot_tell: int = 0
    failed: int = 0
    reused: int = 0
    not_attempted: int = 0
    needs_review: int = 0
    vacancies_covered: int = 0
    status: str = "running"
    reason: str | None = None
    by_kind: Counter[str] = field(default_factory=Counter)
    by_rung: Counter[str] = field(default_factory=Counter)
    by_method: Counter[str] = field(default_factory=Counter)
    by_reason: Counter[str] = field(default_factory=Counter)
    results: list[Resolution] = field(default_factory=list)
    started_at: str = field(default_factory=utcnow)
    finished_at: str = ""

    def absorb(self, resolution: Resolution) -> None:
        self.visited += 1
        self.results.append(resolution)
        if resolution.verdict is None:
            # Two different nothings: a company the ladder could not look at,
            # and one it fell over on.  The failure is already counted where it
            # happened; counting it twice would make the coverage screen say
            # more companies were skipped than were visited.
            if not any(a.outcome == OUTCOME_FAILED for a in resolution.attempts):
                self.not_attempted += 1
            return
        verdict = resolution.verdict
        self.by_kind[verdict.kind] += 1
        self.by_rung[verdict.rung] += 1
        self.by_method[verdict.method] += 1
        if resolution.reused:
            self.reused += 1
        if resolution.needs_review:
            self.needs_review += 1
        if verdict.kind == KIND_CANNOT_TELL:
            self.cannot_tell += 1
            self.by_reason[verdict.reason or "unknown"] += 1
        else:
            self.resolved += 1
            self.vacancies_covered += resolution.vacancy_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "visited": self.visited,
            "resolved": self.resolved,
            "cannot_tell": self.cannot_tell,
            "failed": self.failed,
            "reused": self.reused,
            "not_attempted": self.not_attempted,
            "needs_review": self.needs_review,
            "vacancies_covered": self.vacancies_covered,
            "status": self.status,
            "reason": self.reason,
            "by_kind": dict(self.by_kind),
            "by_rung": dict(self.by_rung),
            "by_method": dict(self.by_method),
            "by_reason": dict(self.by_reason),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@asynccontextmanager
async def _maybe_egress(wanted: bool, given: Any) -> AsyncIterator[Any]:
    """An egress client only when a rung that fetches is actually installed."""
    if given is not None or not wanted:
        yield given
        return
    from dreamjob.egress.client import EgressClient  # noqa: PLC0415 - avoids a cycle

    async with EgressClient() as client:
        yield client


def _batches(rows: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


async def resolve_many(
    limit: int = DEFAULT_LIMIT,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    country: str | None = None,
    refresh: bool = False,
    company_ids: list[str] | None = None,
    ladder: list[Rung] | None = None,
    egress: Any = None,
    llm: Any = None,
    campaign_id: str | None = None,
    on_progress: Callable[[int, int, EmployerResolutionReport], None] | None = None,
) -> EmployerResolutionReport:
    """Resolve up to ``limit`` employers, the ones that matter most first (FR-341).

    The order is vacancy count: 210 of the corpus's agency vacancies sit at 34
    reachable agencies, so a pass that stops early has still covered the rows a
    wrong answer would have damaged most.  ``company_ids`` overrides the queue,
    which is how a resumed job continues exactly where it stopped rather than
    re-querying and getting a different list (NFR-401).

    Work runs in batches of ``concurrency``, and ``on_progress`` is called after
    each batch - after the writes of that batch have landed, so a crash between
    batches loses nothing that was reported as done.
    """
    report = EmployerResolutionReport(requested=limit)
    if company_ids is None:
        queue = await asyncio.to_thread(
            repo.companies_needing_verdict, limit, country=country, include_resolved=refresh
        )
        company_ids = [str(row["company_id"]) for row in queue]
    company_ids = company_ids[:limit] if limit else company_ids
    report.requested = len(company_ids)
    if not company_ids:
        report.status = "done"
        report.finished_at = utcnow()
        return report

    wants_fetch = any(
        rung_provider(name) is not None for name in (RUNG_REGISTRY, RUNG_EURES, RUNG_WEBSITE)
    )
    done = 0
    async with _maybe_egress(wants_fetch, egress) as client:

        async def one(company_id: str) -> Resolution:
            try:
                return await resolve_employer_kind(
                    company_id,
                    egress=client,
                    llm=llm,
                    ladder=ladder,
                    campaign_id=campaign_id,
                    force=refresh,
                )
            except Exception as exc:  # noqa: BLE001 - one company never stops a pass
                log.exception("Employer-kind resolution failed for company %s", company_id)
                report.failed += 1
                return Resolution(
                    company_id=company_id,
                    attempts=[
                        Attempt(
                            rung=RUNG_KB, position=0, method=METHOD_KNOWLEDGE_BASE,
                            outcome=OUTCOME_FAILED,
                            note=f"{type(exc).__name__}: {exc}"[:200],
                        )
                    ],
                )

        for batch in _batches(company_ids, max(1, concurrency)):
            for resolution in await asyncio.gather(*(one(cid) for cid in batch)):
                report.absorb(resolution)
            done += len(batch)
            if on_progress is not None:
                on_progress(done, len(company_ids), report)

    report.status = "done"
    report.finished_at = utcnow()
    log.info(
        "Employer kind: %d companies, %d established, %d cannot_tell (%s), %d reused, "
        "%d not attempted; %d vacancies covered",
        report.visited, report.resolved, report.cannot_tell,
        ", ".join(f"{k} {v}" for k, v in report.by_reason.most_common(4)) or "-",
        report.reused, report.not_attempted, report.vacancies_covered,
    )
    return report


def run_resolve_many(limit: int = DEFAULT_LIMIT, **kwargs: Any) -> dict[str, Any]:
    """Synchronous entry point, for scripts and for the API's dry runs."""
    return asyncio.run(resolve_many(limit, **kwargs)).as_dict()


# ---------------------------------------------------------------------------
# Background execution (FR-185, NFR-401)
# ---------------------------------------------------------------------------

JOB_KIND = "employer_kind"


async def employer_kind_worker(ctx: Any) -> None:
    """Job worker for ``job_run.kind = 'employer_kind'``.

    The company list and the position in it live in the checkpoint, so a run
    interrupted by a restart resumes at the next company instead of re-walking
    the register for the ones it already established (NFR-401).  The list is
    computed once, at the start: re-querying the queue on resume would return a
    *different* list - the rows just resolved have left it - and the checkpoint
    index would then point into the wrong list.
    """
    company_ids: list[str] = list(ctx.checkpoint.get("company_ids") or [])
    limit = int(ctx.checkpoint.get("limit") or DEFAULT_LIMIT)
    if not company_ids:
        queue = await asyncio.to_thread(
            repo.companies_needing_verdict,
            limit,
            country=ctx.checkpoint.get("country"),
            include_resolved=bool(ctx.checkpoint.get("refresh")),
        )
        company_ids = [str(row["company_id"]) for row in queue]
        ctx.save_checkpoint(company_ids=company_ids, done=0, limit=limit)

    done = int(ctx.checkpoint.get("done") or 0)
    ctx.progress(done, len(company_ids))
    remaining = company_ids[done:]
    if not remaining:
        return

    def progressed(batch_done: int, _total: int, report: EmployerResolutionReport) -> None:
        ctx.save_checkpoint(done=done + batch_done, report=report.as_dict())
        ctx.progress(done + batch_done, len(company_ids))

    await ctx.checkpoint_barrier()
    report = await resolve_many(
        len(remaining),
        company_ids=remaining,
        concurrency=int(ctx.checkpoint.get("concurrency") or DEFAULT_CONCURRENCY),
        refresh=bool(ctx.checkpoint.get("refresh")),
        campaign_id=getattr(ctx, "campaign_id", None),
        on_progress=progressed,
    )
    ctx.save_checkpoint(done=len(company_ids), report=report.as_dict())
    if report.failed:
        ctx.record_error(f"{report.failed} companies failed; see the trail per company")


def register_employer_kind_worker() -> None:
    """Wire the worker into the job runner (FR-185)."""
    from dreamjob.jobs.runner import runner  # noqa: PLC0415 - avoids a cycle

    runner.register_worker(JOB_KIND, employer_kind_worker)


async def launch(
    *,
    job_seeker_id: str | None = None,
    campaign_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    refresh: bool = False,
    country: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> str:
    """Start a resolution pass in the background.  Returns the job id.

    ``job_seeker_id`` records *who asked*, never who the verdict is about: the
    tag is a shared knowledge-base fact and the row it writes carries no person
    (FR-344, RK-08).
    """
    from dreamjob.db.connection import update_row  # noqa: PLC0415
    from dreamjob.jobs.runner import runner  # noqa: PLC0415 - avoids a cycle

    register_employer_kind_worker()
    job_id = runner.create(
        JOB_KIND, job_seeker_id=job_seeker_id, campaign_id=campaign_id, total=limit
    )
    update_row(
        "job_run",
        job_id,
        {
            "checkpoint": to_json(
                {"limit": limit, "refresh": refresh, "country": country, "concurrency": concurrency}
            )
        },
    )
    await runner.start(job_id)
    return job_id


register_employer_kind_worker()
