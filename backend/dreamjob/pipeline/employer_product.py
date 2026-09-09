"""What the product does, and refuses to claim, when the employer is not named.

Requirements: FR-143 (company-type directives), FR-281/FR-282 (scored and
explainable), FR-330 (the motivation document), FR-383 (the fit meter),
NFR-205 (website text is untrusted), NFR-402 (every verdict carries its
evidence), CR-405 (advisory, never invented).

The detector establishes one fact per company - is the organisation that
posted this vacancy the organisation that will employ you? - in
:mod:`dreamjob.pipeline.employer_kind`, and the ladder that resolves it lives
in :mod:`dreamjob.pipeline.employer_resolver`.  This module is the product's
*reading* of that fact: the thin view the scorer, the motivation document and
``/api/employers`` share, and the one predicate everything else hangs off:

    ``tag.employer_disclosed`` - is there an employer we can say anything about?

Three rules, none of which is negotiable:

1. **``cannot_tell`` is a state, not a default.**  A company nobody has looked
   at is *not researched*; a company whose site could not be read is
   *unverified* with the reason and the cheapest next rung.  Neither collapses
   into "employer" (the sixty-vacancy agency nobody could read) or into
   "agency" (the GitLab failure with a different excuse).
2. **Undisclosed means undisclosed.**  For an agency or job-board row the
   company dimensions - profile, five-year accounts, ability to pay, values
   match - are not computed against the *agency*.  Randstad's balance sheet
   says nothing about the client's ability to pay.  They are excluded and
   named as excluded (:data:`COMPANY_DIMENSIONS`), which is a different thing
   from a quietly lower score.
3. **Evidence travels with the verdict** (NFR-402): the rung, the method, the
   URL, a verbatim quote and the date, all the way to the API response.

Website text is untrusted (NFR-205).  Quotes stored as evidence are *data
about a page*: they are shown to the job seeker, and they are never rendered
into an LLM prompt as instructions.  A verdict whose ``anomalies`` list is
non-empty - which is where the website rung records an attempted prompt
injection - stays flagged for review here and in everything downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from dreamjob.db.repositories import employer_kind as kind_repo
from dreamjob.pipeline.employer_kind import (
    EmployerRole,
    Evidence,
    Kind,
    Rung,
    Verdict,
    effective_verdict,
)

# --- the vocabulary the product reads --------------------------------------

ROLE_DIRECT = str(EmployerRole.DIRECT)
ROLE_AGENCY = str(EmployerRole.AGENCY)
ROLE_BOARD = str(EmployerRole.BOARD)
ROLE_UNVERIFIED = str(EmployerRole.UNVERIFIED)

#: ``reason`` for a company nobody has researched yet.  Deliberately *not* one
#: of :class:`Reason`'s values: those are refusals the ladder reached, and this
#: is the absence of a ladder walk.  Telling them apart is the difference
#: between "we looked and could not tell" and "nobody has looked".
REASON_NOT_RESEARCHED = "not_researched"

#: FR-281 components that describe *the employer* and therefore cannot be
#: computed when the employer is not named.  Named so the explain payload
#: (FR-282) can list them rather than leaving a hole where a number was.
COMPANY_DIMENSIONS: tuple[str, ...] = (
    "company attractiveness",
    "financial trajectory and ability to pay",
    "values and culture match",
)

# --- FR-143: the seeker's preference about intermediaries ------------------

INTERMEDIARY_DIRECT_ONLY = "direct_only"
INTERMEDIARY_PREFER_DIRECT = "prefer_direct"
INTERMEDIARY_NO_PREFERENCE = "no_preference"
INTERMEDIARY_VALUES = (
    INTERMEDIARY_DIRECT_ONLY,
    INTERMEDIARY_PREFER_DIRECT,
    INTERMEDIARY_NO_PREFERENCE,
)

#: The default, and the argument for it (proposal section 4.1).  Interim is a
#: common and legitimate route into employment in Belgium; the contract-type
#: default already keeps interim *contracts* out for seekers who did not ask
#: for them, so what ``direct_only`` would additionally hide is recruitment and
#: selection into permanent jobs - real jobs, which a seeker cannot opt into if
#: they are never shown.  And the promise cannot be kept: the detector finds
#: roughly three agencies in four from the advert alone, so "direct employers
#: only" would be a false comfort.  Hiding a route to a real job is paid by the
#: seeker; a labelled row costs a glance.
DEFAULT_INTERMEDIARY_PREFERENCE = INTERMEDIARY_PREFER_DIRECT


def intermediary_preference(directives: Any) -> str:
    """FR-143 ``company_type.intermediaries``, with the argued default.

    Read through ``getattr`` on purpose: the directive slice adds the field to
    ``CompanyTypeDirectives``, and until it does every seeker gets the default
    and the scorer behaves exactly as it will afterwards.
    """
    company_type = getattr(directives, "company_type", None)
    value = getattr(company_type, "intermediaries", None)
    value = getattr(value, "value", value)
    if isinstance(company_type, dict):
        value = company_type.get("intermediaries", value)
    text = str(value or "").strip().lower()
    return text if text in INTERMEDIARY_VALUES else DEFAULT_INTERMEDIARY_PREFERENCE


# --- the strings the surfaces show (proposal section 4.8) ------------------

BADGE_LABELS: dict[str, dict[str, str]] = {
    ROLE_AGENCY: {
        "en": "Via agency · employer not named",
        "nl": "Via bureau · werkgever niet genoemd",
        "fr": "Via agence · employeur non nommé",
    },
    ROLE_BOARD: {
        "en": "Via job board",
        "nl": "Via jobsite",
        "fr": "Via site d'emploi",
    },
    ROLE_UNVERIFIED: {
        "en": "Employer type not verified",
        "nl": "Type werkgever niet geverifieerd",
        "fr": "Type d'employeur non vérifié",
    },
}

#: The line the ranked list puts where a company score would be.  The proposal
#: is explicit that a quietly lower number reads as a judgement about the
#: employer; this sentence is the alternative (sections 4.2 and 4.4).
LIST_NOTES: dict[str, dict[str, str]] = {
    ROLE_AGENCY: {
        "en": "employer not disclosed — company dimensions not assessed",
        "nl": "werkgever niet bekendgemaakt — bedrijfsdimensies niet beoordeeld",
        "fr": "employeur non divulgué — dimensions d'entreprise non évaluées",
    },
    ROLE_BOARD: {
        "en": "employer not disclosed — company dimensions not assessed",
        "nl": "werkgever niet bekendgemaakt — bedrijfsdimensies niet beoordeeld",
        "fr": "employeur non divulgué — dimensions d'entreprise non évaluées",
    },
}

_DISCLOSURE: dict[str, str] = {
    "en": (
        "This vacancy was posted by {agency}, {what}, for an employer it does not name. "
        "We have not profiled that employer, read its accounts or judged its ability to "
        "pay, because we do not know who it is. The score rests on the role only; the "
        "company-related criteria are marked 'cannot assess'.{says} Ask the recruiter "
        "who the employer is before an interview - then regenerate the briefing."
    ),
    "nl": (
        "Deze vacature is geplaatst door {agency}, {what}, voor een werkgever die niet "
        "genoemd wordt. Wij hebben die werkgever niet geprofileerd, geen jaarrekening "
        "gelezen en geen oordeel over de betaalcapaciteit gegeven, omdat wij niet weten "
        "wie het is. De score berust alleen op de functie; de bedrijfscriteria staan op "
        "'niet te beoordelen'.{says} Vraag de recruiter wie de werkgever is vóór een "
        "gesprek - en genereer daarna de briefing opnieuw."
    ),
    "fr": (
        "Cette offre a été publiée par {agency}, {what}, pour un employeur qu'elle ne "
        "nomme pas. Nous n'avons pas profilé cet employeur, ni lu ses comptes, ni jugé "
        "sa capacité à payer, parce que nous ignorons de qui il s'agit. Le score ne "
        "porte que sur le poste ; les critères d'entreprise sont marqués "
        "« non évaluables ».{says} Demandez au recruteur qui est l'employeur avant un "
        "entretien - puis regénérez le briefing."
    ),
}

_SERVICE_MODEL_WORDS: dict[str, dict[str, str]] = {
    "temp_agency": {
        "en": "a temporary employment agency",
        "nl": "een uitzendbureau",
        "fr": "une agence d'intérim",
    },
    "recruitment_selection": {
        "en": "a recruitment and selection agency",
        "nl": "een wervings- en selectiebureau",
        "fr": "un cabinet de recrutement et de sélection",
    },
    "payrolling": {
        "en": "a payrolling company",
        "nl": "een payrollbedrijf",
        "fr": "une société de portage salarial",
    },
    "job_board": {"en": "a job board", "nl": "een jobsite", "fr": "un site d'emploi"},
}
_SERVICE_MODEL_FALLBACK = {
    "en": "a recruitment agency",
    "nl": "een rekruteringsbureau",
    "fr": "une agence de recrutement",
}

_CANNOT_ASSESS_REASON: dict[str, str] = {
    "en": "the employer is not named",
    "nl": "de werkgever wordt niet genoemd",
    "fr": "l'employeur n'est pas nommé",
}

#: What a company nobody has researched needs next, in the shape
#: :func:`employer_resolver.next_step` returns for the ladder's own reasons.
_NOT_RESEARCHED_STEP = {
    "action": "resolve",
    "label": "nobody has researched this employer yet; the register answers in seconds",
}


def _lang(language: str | None) -> str:
    code = (language or "en")[:2].lower()
    return code if code in ("en", "nl", "fr") else "en"


def cannot_assess_reason(language: str | None = "en") -> str:
    """The phrase every ``cannot_assess`` criterion carries (FR-383)."""
    return _CANNOT_ASSESS_REASON[_lang(language)]


def service_model_words(service_model: Any, language: str | None = "en") -> str:
    lang = _lang(language)
    table = _SERVICE_MODEL_WORDS.get(str(service_model or "").strip().lower())
    return (table or _SERVICE_MODEL_FALLBACK)[lang]


# ---------------------------------------------------------------------------
# The tag: the verdict as this posting's reader sees it
# ---------------------------------------------------------------------------


@dataclass
class EmployerTag:
    """A company's employer-kind verdict, adjusted for one posting.

    Thin by design.  :class:`~dreamjob.pipeline.employer_kind.Verdict` is the
    shared fact and stays authoritative; this adds only what the *product*
    needs on top of it - the posting-level override, the verbatim descriptors,
    the strings, and :attr:`employer_disclosed`.
    """

    company_id: str | None = None
    company_name: str | None = None
    verdict: Verdict | None = None
    #: Overrides the verdict's role for this posting only; see
    #: :func:`apply_posting`.
    role_override: str | None = None
    established_at: str | None = None
    expires_at: str | None = None
    correction: dict[str, Any] | None = None
    #: True when *this posting's* own text carries a strict on-behalf clause.
    posting_on_behalf: bool = False
    #: Verbatim sentences from the posting describing the unnamed employer.
    descriptors: list[str] = field(default_factory=list)
    extra_evidence: list[dict[str, Any]] = field(default_factory=list)

    # -- what the verdict says ---------------------------------------------

    @property
    def kind(self) -> str:
        return str(self.verdict.kind) if self.verdict else str(Kind.CANNOT_TELL)

    @property
    def role(self) -> str:
        if self.role_override:
            return self.role_override
        return str(self.verdict.employer_role) if self.verdict else ROLE_UNVERIFIED

    @property
    def reason(self) -> str | None:
        if self.verdict is None:
            return REASON_NOT_RESEARCHED
        return str(self.verdict.reason) if self.verdict.reason else None

    @property
    def tier(self) -> str | None:
        return str(self.verdict.tier) if self.verdict and self.verdict.tier else None

    @property
    def confidence(self) -> float | None:
        return float(self.verdict.confidence) if self.verdict else None

    @property
    def service_model(self) -> str | None:
        return str(self.verdict.service_model) if self.verdict else None

    @property
    def summary(self) -> str | None:
        return (self.verdict.summary or None) if self.verdict else None

    @property
    def anomalies(self) -> list[str]:
        return list(self.verdict.anomalies) if self.verdict else []

    # -- the predicate everything else hangs off ---------------------------

    @property
    def employer_disclosed(self) -> bool:
        """False only where we know the named organisation is not the employer.

        Deliberately not "unless proven otherwise".  An unresearched or
        unverified company keeps its company dimensions and carries a badge
        saying the type is not verified; refusing to score every employer
        nobody has looked at yet would punish most of the corpus for a gap in
        our own coverage rather than for anything about them.
        """
        return self.role not in (ROLE_AGENCY, ROLE_BOARD)

    @property
    def is_intermediary(self) -> bool:
        return not self.employer_disclosed

    @property
    def is_direct_likely(self) -> bool:
        """The band below which a single posting's clause may override upwards."""
        if self.verdict is None:
            return False
        if self.tier == "direct_likely":
            return True
        return self.verdict.kind is Kind.EMPLOYER and float(self.verdict.confidence) >= 0.85

    @property
    def is_researched(self) -> bool:
        return self.verdict is not None

    @property
    def needs_review(self) -> bool:
        """NFR-205: a reported injection attempt, or a register/human conflict."""
        return bool(self.anomalies) or bool(self.verdict and self.verdict.conflicts_with)

    @property
    def conflict(self) -> str | None:
        if self.verdict is None or not self.verdict.conflicts_with:
            return None
        return (
            "a human correction and the register disagree; neither is applied until an "
            "operator has looked at it"
        )

    # -- what the surfaces render ------------------------------------------

    def next_step(self) -> dict[str, str] | None:
        if self.verdict is None:
            return dict(_NOT_RESEARCHED_STEP)
        if self.verdict.kind is not Kind.CANNOT_TELL:
            return None
        from dreamjob.pipeline.employer_resolver import next_step  # noqa: PLC0415 - cycle

        return next_step(str(self.verdict.reason) if self.verdict.reason else None)

    def badge(self, language: str | None = "en") -> str | None:
        table = BADGE_LABELS.get(self.role)
        return None if table is None else table[_lang(language)]

    def list_note(self, language: str | None = "en") -> str | None:
        table = LIST_NOTES.get(self.role)
        return None if table is None else table[_lang(language)]

    def disclosure_note(self, language: str | None = "en") -> str | None:
        """The paragraph generated material and the opportunity screen carry."""
        if self.employer_disclosed:
            return None
        lang = _lang(language)
        says = ""
        if self.descriptors:
            joined = "; ".join(d.strip() for d in self.descriptors[:3])
            says = {
                "en": f" The posting says: {joined}.",
                "nl": f" De vacature zegt: {joined}.",
                "fr": f" L'annonce dit : {joined}.",
            }[lang]
        return _DISCLOSURE[lang].format(
            agency=self.company_name or "the posting's author",
            what=service_model_words(self.service_model, lang),
            says=says,
        )

    def evidence(self) -> list[dict[str, Any]]:
        """NFR-402: the sentence, the page, the method and the date.

        Untrusted web content: these quotes are shown as data and never fed to
        a model as instructions (NFR-205).
        """
        items = [*self.extra_evidence]
        for item in (self.verdict.evidence if self.verdict else ()):
            entry = item.to_dict()
            entry["rung"] = str(self.verdict.rung) if self.verdict else None
            entry["method"] = self.verdict.method if self.verdict else None
            entry.setdefault("established_at", self.established_at)
            if entry.get("established_at") is None:
                entry["established_at"] = self.established_at
            items.append(entry)
        return items

    def as_dict(self, language: str | None = "en") -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "kind": self.kind,
            "employer_role": self.role,
            "employer_disclosed": self.employer_disclosed,
            "tier": self.tier,
            "score": self.verdict.score if self.verdict else None,
            "confidence": self.confidence,
            "method": self.verdict.method if self.verdict else None,
            "rung": str(self.verdict.rung) if self.verdict else None,
            "reason": self.reason,
            "service_model": self.service_model,
            "summary": self.summary,
            "badge": self.badge(language),
            "list_note": self.list_note(language),
            "disclosure_note": self.disclosure_note(language),
            "next_step": self.next_step(),
            "established_at": self.established_at,
            "expires_at": self.expires_at,
            "researched": self.is_researched,
            "evidence": self.evidence(),
            "evidence_is_untrusted_web_content": True,
            "anomalies": self.anomalies,
            "needs_review": self.needs_review,
            "conflict": self.conflict,
            "correction": self.correction,
            "posting_on_behalf": self.posting_on_behalf,
            "employer_descriptors": self.descriptors,
            "dimensions_not_assessed": (
                [] if self.employer_disclosed else list(COMPANY_DIMENSIONS)
            ),
        }


def badge_from_row(row: dict[str, Any], language: str | None = "en") -> dict[str, Any]:
    """The list-row badge, rendered straight from the joined verdict columns.

    A ranked list carries hundreds of rows and needs the state, the role and
    the two sentences - not the evidence, which the popover fetches from
    ``/api/employers/{id}/kind`` the first time somebody clicks.  So this
    renders from the columns ``BADGE_COLUMNS`` already joined (NFR-502) and
    never builds a :class:`~dreamjob.pipeline.employer_kind.Verdict`: that
    class refuses to exist without evidence, which is exactly the guarantee
    that makes it the wrong object for a projection that has none.

    A row with no verdict is *not researched* - its own state, never blank and
    never "employer".
    """
    role = str(row.get("employer_role") or "") or ROLE_UNVERIFIED
    kind = str(row.get("employer_kind") or "") or str(Kind.CANNOT_TELL)
    researched = bool(row.get("employer_kind"))
    reason = row.get("employer_kind_reason") or (None if researched else REASON_NOT_RESEARCHED)
    if row.get("employer_kind_correction"):
        kind = str(row["employer_kind_correction"])
        role = {"agency": ROLE_AGENCY, "employer": ROLE_DIRECT}.get(kind, ROLE_UNVERIFIED)
        reason = None if kind != str(Kind.CANNOT_TELL) else reason
    if row.get("posting_on_behalf") and role == ROLE_UNVERIFIED:
        role = ROLE_AGENCY
    lang = _lang(language)
    disclosed = role == ROLE_DIRECT
    badge = BADGE_LABELS.get(role)
    note = LIST_NOTES.get(role)
    step = None
    if not researched:
        step = dict(_NOT_RESEARCHED_STEP)
    elif kind == str(Kind.CANNOT_TELL):
        from dreamjob.pipeline.employer_resolver import next_step  # noqa: PLC0415 - cycle

        step = next_step(str(reason) if reason else None)
    return {
        "company_id": row.get("company_id"),
        "company_name": row.get("company_name"),
        "kind": kind,
        "employer_role": role,
        "employer_disclosed": disclosed,
        "tier": row.get("employer_kind_tier"),
        "confidence": row.get("employer_kind_confidence"),
        "service_model": row.get("employer_service_model"),
        "reason": reason,
        "summary": row.get("employer_kind_summary"),
        "badge": None if badge is None else badge[lang],
        "list_note": None if note is None else note[lang],
        "next_step": step,
        "researched": researched,
        "corrected": bool(row.get("employer_kind_correction")),
        "dimensions_not_assessed": [] if disclosed else list(COMPANY_DIMENSIONS),
        "evidence_is_untrusted_web_content": True,
    }


def tag_for_company(
    company_id: str | None,
    *,
    job_seeker_id: str | None = None,
    company: dict | None = None,
) -> EmployerTag:
    """The product's reading of one company's verdict, correction included.

    A company with no verdict row comes back as *not researched* - not as an
    employer, and not as an agency.
    """
    if not company_id:
        return EmployerTag(company_id=None, company_name=(company or {}).get("name"))
    row = kind_repo.get(company_id)
    correction = kind_repo.correction_for(company_id, job_seeker_id)
    name = (company or {}).get("name")
    verdict = Verdict.from_row(row) if row else None
    if correction:
        verdict = effective_verdict(verdict, correction, company_name=name or "this company")
    return EmployerTag(
        company_id=company_id,
        company_name=name,
        verdict=verdict,
        established_at=(row or {}).get("established_at") or (correction or {}).get("created_at"),
        expires_at=(row or {}).get("expires_at"),
        correction=(
            {
                "kind": correction.get("kind"),
                "note": correction.get("note"),
                "scope": correction.get("scope"),
                "evidence_url": correction.get("evidence_url"),
                "created_at": correction.get("created_at"),
                "promoted_by": correction.get("promoted_by"),
                "is_mine": bool(
                    job_seeker_id and correction.get("job_seeker_id") == job_seeker_id
                ),
            }
            if correction
            else None
        ),
    )


# ---------------------------------------------------------------------------
# The posting's own voice
# ---------------------------------------------------------------------------

#: Strict, singular, hiring-framed third-party clauses (proposal section 2.2,
#: signal T1).  The discriminator is grammatical number: every false positive
#: of a loose pattern was plural or customer usage ("voor onze klanten", "our
#: client base").  Used here only to *quote the posting back* and to raise a
#: single row, never to reclassify an employer - that needs the employer-level
#: majority to be safe, because GitLab has 28 loose hits in 225 adverts.
_CLIENT_CLAUSE = re.compile(
    r"(onze klant\b(?!en)|in opdracht van\b|voor een [a-zà-ÿ\-]+ in [A-Z]"
    r"|notre client (?:est|recherche)\b|pour le compte d[eu']\b"
    r"|our client (?:is|'s|based)\b|on behalf of a\b"
    r"|unser (?:Mandant|Kunde) ist\b|für ein (?:etabliertes|renommiertes) Unternehmen\b)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.!?\n\r]{15,320}(?:[.!?]|$)")


def posting_descriptors(text: str | None, limit: int = 4) -> list[str]:
    """Verbatim sentences in which the posting describes the unnamed employer.

    Every string returned is a substring of ``text``.  Nothing is paraphrased,
    summarised or inferred, because these sentences end up in a document that a
    recruiter who knows the client will read (proposal section 5.1).
    """
    if not text:
        return []
    out: list[str] = []
    for match in _SENTENCE.finditer(str(text)[:8000]):
        sentence = match.group(0).strip()
        if _CLIENT_CLAUSE.search(sentence) and sentence not in out:
            out.append(sentence)
        if len(out) >= limit:
            break
    return out


def apply_posting(
    base: EmployerTag, opportunity: dict, vacancy: dict | None = None
) -> EmployerTag:
    """The company's tag, adjusted for what *this* posting says.

    The employer-level verdict decides the normal case.  A single posting can
    still override it upwards - never downwards - when its own text carries a
    strict on-behalf clause and the employer is not direct-likely (proposal
    section 2.2).  For a direct-likely employer the flag is a note, not a
    switch: mixed employers exist (a consultancy recruiting for a client, an
    agency recruiting a recruiter for itself) and one advert cannot outvote
    two hundred.

    ``base`` is never mutated: it is the per-company fact, cached by the
    caller, and what comes back is a copy.
    """
    tag = replace(
        base,
        descriptors=list(base.descriptors),
        extra_evidence=list(base.extra_evidence),
    )
    row = {**(vacancy or {}), **(opportunity or {})}
    text = row.get("description") or (vacancy or {}).get("description") or ""
    flag = row.get("posting_on_behalf")
    if flag is None:
        flag = bool(posting_descriptors(text, limit=1))
    tag.posting_on_behalf = bool(flag)

    if tag.posting_on_behalf and tag.employer_disclosed and not tag.is_direct_likely:
        quote = (posting_descriptors(text, limit=1) or [""])[0]
        tag.role_override = ROLE_AGENCY
        tag.extra_evidence = [
            Evidence(
                signal="T1",
                supports=str(Kind.AGENCY),
                detail="this posting says it is recruiting on behalf of a third party",
                quote=quote or None,
                established_at=row.get("posted_at"),
            ).to_dict()
            | {"rung": str(Rung.KB), "method": "posting_text"},
            *tag.extra_evidence,
        ]
    if not tag.employer_disclosed:
        stored = row.get("employer_descriptors")
        if isinstance(stored, list) and stored:
            tag.descriptors = [str(s) for s in stored][:4]
        else:
            tag.descriptors = posting_descriptors(text)
    return tag


def tag_for_opportunity(
    opportunity: dict,
    *,
    job_seeker_id: str | None = None,
    company: dict | None = None,
    vacancy: dict | None = None,
) -> EmployerTag:
    """The tag as it applies to one posting, read from the database."""
    base = tag_for_company(
        (opportunity or {}).get("company_id"),
        job_seeker_id=job_seeker_id,
        company=company,
    )
    return apply_posting(base, opportunity, vacancy)


def presentation(tag: EmployerTag, language: str | None = "en") -> dict[str, Any]:
    """The fields every API response and every view carries for this axis."""
    return {
        "employer_role": tag.role,
        "employer_kind": tag.kind,
        "employer_disclosed": tag.employer_disclosed,
        "employer_badge": tag.badge(language),
        "employer_list_note": tag.list_note(language),
        "employer_disclosure_note": tag.disclosure_note(language),
        "employer_next_step": tag.next_step(),
        "employer_needs_review": tag.needs_review,
    }
