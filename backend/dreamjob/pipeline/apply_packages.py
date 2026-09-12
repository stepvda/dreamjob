"""Apply Browser package generation (FR-321..FR-324, FR-329, FR-330, NFR-104, NFR-502).

The Apply Browser is a *surface* over the generators that already exist, not a
second implementation of them.  One selected opportunity in, four artefacts out:

(a) the tailored CV in DOCX and PDF, in the language of the opportunity, with
    the photograph and honouring do-not-disclose (``documents/cv_generator.py``,
    FR-322, FR-106);
(b) the company and job briefing PDF (``documents/briefing.py``, FR-329);
(c) the motivation and fit PDF (``documents/motivation.py``, FR-330);
(d) the application e-mail, with (a) attached.

What this module adds on top of ``documents/package.py`` is the three things the
browser needs and the review loop did not have:

**A different e-mail.**  The product owner asked for "a very brief motivation
letter that refers to the CV" rather than a second copy of it, so the e-mail
here is written from ``llm/prompts/apply_email.md`` (NFR-602) instead of the
introduction-email prompt.  Everything around the body - greeting, attachment
line, sign-off, signature and the NFR-302 objection sentence - is still taken
from ``documents/intro_email.py`` on purpose: that wording is what
``run_consistency`` registers as the system's own boilerplate, and an e-mail
assembled with different scaffolding would be reported as content of unknown
origin by the NFR-206 leak scan on every clean package.

**Bulk with a cost.**  ``generate_many`` runs through ``jobs/runner.py``, so a
batch of 268 packages is pausable, resumable and shows progress (NFR-502,
NFR-401), and every package reports what it cost.  When the campaign budget is
nearly spent, or one package has already cost more than its ceiling, the
artefacts that are read rather than sent lose the model first and the package is
still produced - degraded, and saying so (NFR-104).

**A dry-run guard.**  The full pipeline runs up to a generated e-mail with the
CV attached, and stops there.  ``send_package`` and ``send_all`` exist and are
wired, but :func:`send_guard` refuses to reach the dispatcher unless *two*
deliberate switches are both thrown - an environment variable on the server and
an administration setting in the database.  Neither is reachable from the
interface, the check is made here rather than in the browser, and it fails
closed: anything unexpected while reading the switches leaves the guard on.
Until they are thrown, a "send" renders the message to ``email.eml`` next to the
other artefacts so it can be read, and nothing leaves the machine.

FR-321 is enforced by the one function that assembles attachments,
:func:`email_attachments`: the briefing (FR-329) and the motivation and fit
document (FR-330) are prepared for the job seeker alone and are **never**
attached to an outgoing e-mail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.db.repositories import applications as repo
from dreamjob.db.repositories import contacts as contacts_repo
from dreamjob.db.repositories import dispatch as dispatch_repo
from dreamjob.documents import intro_email
from dreamjob.documents import package as package_module
from dreamjob.documents._llm import complete_json
from dreamjob.documents.package import GenerationError, GenerationOptions
from dreamjob.documents.pdf_builder import normalise_language
from dreamjob.documents.templates import CvDocument, from_dict
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError, TruncatedResponse
from dreamjob.mail import composer
from dreamjob.mail.base import Attachment, MailBackendError
from dreamjob.pipeline.enrichment import load_prompt

log = logging.getLogger(__name__)

#: NFR-602: the e-mail template, versioned, with its fixtures named in the file.
PROMPT_NAME = "apply_email"
#: The routing identifier stays ``generate.email`` so this e-mail is budgeted,
#: routed and reported next to the introduction e-mail it replaces (FR-362).
EMAIL_TASK = "generate.email"
#: The answer is ~150 words.  The ceiling is for the reasoning model's private
#: reasoning, which is charged against the same budget (see ``llm/client.py``);
#: ``documents/_llm.complete_json`` re-routes to the chat model if it is spent.
EMAIL_MAX_TOKENS = 6000
WORD_BUDGET = 150

#: The four artefacts, in the order they are produced.
DOCUMENT_PARTS: tuple[str, ...] = ("cv", "briefing", "motivation")

JOB_KIND = "apply_package"
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 8
#: Seconds one package takes with the model, for the NFR-502 time estimate.
SECONDS_PER_PACKAGE = 45

#: NFR-104: what one package may spend before the remaining artefacts are built
#: without the model.  Overridable per installation from the admin screen.
DEFAULT_MAX_COST_EUR = 0.20
MAX_COST_SETTING = "apply.max_cost_eur"

#: How far back the FR-364 call log is read to price one package.  A package
#: makes five or six calls; the window only has to be wider than that.
LLM_LOG_WINDOW = 200

#: The dry-run guard.  Both switches, or no send.
SEND_ENABLED_ENV = "DREAMJOB_APPLY_ALLOW_SEND"
SEND_ENABLED_SETTING = "apply.send_enabled"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class SendBlocked(RuntimeError):
    """The dry-run guard refused to let a message leave the machine."""


# ---------------------------------------------------------------------------
# NFR-302: an objected recipient blocks generation, approval and dispatch
# ---------------------------------------------------------------------------


def _objected_recipient(contact_id: str | None) -> str | None:
    """The address of a stored recipient who has objected, if any (NFR-302).

    Answers through :func:`dreamjob.documents.package._objected_contact`, which
    reads the shared block list and the flag on the row - the same predicate
    the approval gate and the send rails use, so the three cannot disagree.
    """
    return package_module._objected_contact(repo.get_contact(contact_id))


def _package_objection(package: dict[str, Any]) -> str | None:
    """Why this package's recipient may not be written to, if so (NFR-302).

    The stored contact is checked through the shared predicate, and the
    address on the package against the block list as well, because an
    objection may have arrived after the contact row was swept or re-collected.
    """
    who = _objected_recipient(package.get("contact_id"))
    if who:
        return f"{who} has objected to being contacted (NFR-302); this package cannot be sent."
    address = package.get("contact_email")
    if address and contacts_repo.is_objected(address):
        return f"{address} has objected to being contacted (NFR-302); this package cannot be sent."
    return None


# ---------------------------------------------------------------------------
# Options, cost and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """What the browser lets the job seeker choose before generating."""

    language: str | None = None
    cv_template: str | None = None
    briefing_template: str | None = None
    motivation_template: str | None = None
    contact_id: str | None = None
    instructions: str | None = None
    use_llm: bool = True
    max_cost_eur: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "cv_template": self.cv_template,
            "briefing_template": self.briefing_template,
            "motivation_template": self.motivation_template,
            "contact_id": self.contact_id,
            "instructions": self.instructions,
            "use_llm": self.use_llm,
            "max_cost_eur": self.max_cost_eur,
        }

    @classmethod
    def from_dict(cls, values: dict[str, Any] | None) -> Options:
        values = values or {}
        return cls(**{k: v for k, v in values.items() if k in cls.__dataclass_fields__})

    def ceiling(self) -> float:
        """The per-package cost ceiling (NFR-104), from the admin screen if set."""
        if self.max_cost_eur is not None:
            return float(self.max_cost_eur)
        try:
            configured = admin_repo.get_setting(MAX_COST_SETTING, None)
        except Exception:  # noqa: BLE001 - configuration must not break a run
            configured = None
        try:
            return float(configured) if configured is not None else DEFAULT_MAX_COST_EUR
        except (TypeError, ValueError):
            return DEFAULT_MAX_COST_EUR


@dataclass
class Spend:
    """What one package cost, read back from the FR-364 call log."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_eur: float = 0.0
    failed_calls: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "cost_eur": round(self.cost_eur, 6),
        }


@dataclass
class Plan:
    """Which artefacts get the model on this run, and why not (NFR-104)."""

    cv: bool = True
    documents: bool = True
    email: bool = True
    judge: bool = True
    reasons: list[str] = field(default_factory=list)


@dataclass
class PackageResult:
    """One package as the Apply Browser needs to show it."""

    opportunity_id: str
    package_id: str | None
    status: str = "ready"
    reused: bool = False
    ready: bool = False
    consistency_status: str = "not_run"
    leak_scan_status: str | None = None
    blockers: list[dict[str, Any]] = field(default_factory=list)
    documents: dict[str, str | None] = field(default_factory=dict)
    never_sent: list[str] = field(default_factory=list)
    email_subject: str | None = None
    recipient_email: str | None = None
    language: str | None = None
    spend: dict[str, Any] = field(default_factory=dict)
    degradation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "package_id": self.package_id,
            "status": self.status,
            "reused": self.reused,
            "ready": self.ready,
            "consistency_status": self.consistency_status,
            "leak_scan_status": self.leak_scan_status,
            "blockers": self.blockers,
            "documents": self.documents,
            "never_sent": self.never_sent,
            "email_subject": self.email_subject,
            "recipient_email": self.recipient_email,
            "language": self.language,
            "spend": self.spend,
            "degradation": self.degradation,
            "notes": self.notes,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# FR-321: one package
# ---------------------------------------------------------------------------


def generate_package(
    job_seeker_id: str,
    opportunity_id: str,
    *,
    regenerate: bool = False,
    options: Options | None = None,
    actor: str | None = None,
) -> PackageResult:
    """Generate the four artefacts for one opportunity, and check them.

    ``regenerate=False`` is what makes the bulk run cheap and resumable: a
    package that is already complete is returned untouched instead of being
    paid for a second time (NFR-104).  ``regenerate=True`` rebuilds it, which
    withdraws any approval it had - that is ``documents/package.py``'s rule and
    it is not circumvented here.

    The factual-consistency check of FR-322 always runs last, over the CV *and*
    the e-mail text, and its verdict is stored on the package.  A failure does
    not raise: it blocks approval and is reported in ``blockers`` so the screen
    can show it (FR-322, NFR-206, RK-03).
    """
    opts = options or Options()
    inputs = repo.generation_inputs(job_seeker_id, opportunity_id)
    if inputs is None:
        raise GenerationError(f"Opportunity {opportunity_id} not found for this job seeker")

    existing = repo.latest_for_opportunity(job_seeker_id, opportunity_id)
    if existing is not None and existing.get("status") == "sent":
        # FR-326: what went out is a record, not a draft.  Refreshing the
        # briefing before an interview is a different route (FR-329).
        return _result(existing, reused=True, degradation=["Already sent; not regenerated."])
    if existing is not None and not regenerate and _is_complete(existing):
        return _result(existing, reused=True, degradation=["Already generated; reused as it is."])

    # NFR-302: building or regenerating for an objected recipient is refused
    # here, before any document or model call, whether the recipient came from
    # the request or was pinned on the package that is about to be rebuilt.
    # A contact id the seeker does not own is left to the ownership refusal
    # in ``documents/package.generate`` rather than read here (NFR-303).
    objected = _objected_recipient((existing or {}).get("contact_id"))
    if not objected and opts.contact_id and contacts_repo.contact_owned_by(
        job_seeker_id, opts.contact_id
    ):
        objected = _objected_recipient(opts.contact_id)
    if objected:
        raise GenerationError(
            f"{objected} has objected to being contacted (NFR-302); a package cannot be "
            "generated or regenerated for them."
        )

    llm: LLMClient | None = None
    reason: str | None = "Model use was switched off for this run."
    if opts.use_llm:
        llm, reason = package_module.llm_for(
            job_seeker_id, (inputs.get("campaign") or {}).get("id")
        )
    plan = _plan(llm, reason)

    entities = [opportunity_id] + ([existing["id"]] if existing else [])
    before = _call_ids(job_seeker_id, entities)

    package = _generate_documents(job_seeker_id, opportunity_id, opts, plan, existing, actor)
    if package["id"] not in entities:
        entities.append(package["id"])

    ceiling = opts.ceiling()
    spend = _spend_since(job_seeker_id, entities, before)
    if plan.email and spend.cost_eur >= ceiling:
        # NFR-104: degrade rather than blow the budget.  The e-mail still gets
        # written, from the CV, by the deterministic path.
        plan.email = False
        plan.judge = False
        plan.reasons.append(
            f"NFR-104: the documents for this package already cost EUR {spend.cost_eur:.4f} "
            f"of a EUR {ceiling:.2f} ceiling; the e-mail was assembled without the model."
        )

    package = _write_email(job_seeker_id, package, inputs, opts, llm if plan.email else None)
    package = package_module.run_consistency(
        job_seeker_id, package, inputs=inputs, llm=llm if plan.judge else None
    )
    spend = _spend_since(job_seeker_id, entities, before)
    package = _record(job_seeker_id, package, spend, plan, actor)
    return _result(package, reused=False, spend=spend, degradation=plan.reasons)


def _is_complete(package: dict[str, Any]) -> bool:
    """All four artefacts on disk, an e-mail, and a check that has run."""
    for key in ("cv_pdf_path", "cv_docx_path", "briefing_pdf_path", "motivation_pdf_path"):
        path = package.get(key)
        if not path or not Path(str(path)).is_file():
            return False
    if not (package.get("email_body") or "").strip():
        return False
    return package.get("consistency_status") not in (None, "", "not_run")


def _plan(llm: LLMClient | None, reason: str | None) -> Plan:
    """Decide up front how much model this package may have (NFR-104)."""
    if llm is None:
        return Plan(
            cv=False,
            documents=False,
            email=False,
            judge=False,
            reasons=[reason or "No model available; every artefact took its assembled path."],
        )
    if llm.budget.should_degrade():
        # The two documents nobody outside this machine ever reads lose the
        # model first; the CV and the e-mail are what the application is.
        return Plan(
            cv=True,
            documents=False,
            email=True,
            judge=False,
            reasons=[
                "NFR-104: the campaign token budget is nearly spent, so the briefing and the "
                "motivation document were built from stored data without the model, and the "
                "consistency judge did not run (the deterministic checks still did)."
            ],
        )
    return Plan()


def _generate_documents(
    job_seeker_id: str,
    opportunity_id: str,
    opts: Options,
    plan: Plan,
    existing: dict[str, Any] | None,
    actor: str | None,
) -> dict[str, Any]:
    """(a), (b) and (c), through ``documents/package.py``.

    One call when the CV and the two documents are treated alike, two when the
    budget says the documents must go without the model.  ``run_judge`` is off
    on both: the check that matters runs once at the end, over the e-mail too.
    """
    groups: list[tuple[tuple[str, ...], bool]]
    if plan.cv == plan.documents:
        groups = [(DOCUMENT_PARTS, plan.cv)]
    else:
        groups = [(("cv",), plan.cv), (("briefing", "motivation"), plan.documents)]

    def options_for(parts: tuple[str, ...], use_llm: bool) -> GenerationOptions:
        return GenerationOptions(
            language=opts.language,
            cv_template=opts.cv_template,
            briefing_template=opts.briefing_template,
            motivation_template=opts.motivation_template,
            contact_id=opts.contact_id,
            instructions=opts.instructions,
            parts=parts,
            use_llm=use_llm,
            run_judge=False,
        )

    package_id = existing["id"] if existing else None
    package: dict[str, Any] | None = None
    for parts, use_llm in groups:
        try:
            package = package_module.generate(
                job_seeker_id,
                opportunity_id,
                options_for(parts, use_llm),
                package_id=package_id,
                actor=actor,
            )
        except TruncatedResponse as exc:
            # The generators route a cut-off reasoning model to the chat model
            # themselves; if one ever does not, the artefact is built without
            # the model rather than becoming a 500 (NFR-104).
            log.warning("%s was cut off at the token budget for %s: %s", parts, opportunity_id, exc)
            plan.reasons.append(
                f"The model was cut off at its token budget while writing {', '.join(parts)}; "
                "those artefacts were built without it."
            )
            package = package_module.generate(
                job_seeker_id,
                opportunity_id,
                options_for(parts, False),
                package_id=package_id,
                actor=actor,
            )
        package_id = package["id"]
    if package is None:  # pragma: no cover - groups is never empty
        raise GenerationError("Nothing was generated for this opportunity")
    return package


def _record(
    job_seeker_id: str,
    package: dict[str, Any],
    spend: Spend,
    plan: Plan,
    actor: str | None,
) -> dict[str, Any]:
    """Store what the package cost and how it degraded, where the screen reads it."""
    notes = dict(package.get("generation_notes") or {})
    notes["apply"] = {
        "generated_at": utcnow(),
        "actor": actor,
        "spend": spend.as_dict(),
        "degradation": plan.reasons,
        "model_used": {
            "cv": plan.cv,
            "briefing": plan.documents,
            "motivation": plan.documents,
            "email": plan.email,
            "consistency_judge": plan.judge,
        },
        "prompt": {"template": PROMPT_NAME, "version": _prompt_version()},
    }
    if plan.reasons:
        notes["degradation"] = sorted({*(notes.get("degradation") or []), *plan.reasons})
    updated = repo.update_package(package["id"], job_seeker_id, {"generation_notes": notes})
    return updated or package


def _result(
    package: dict[str, Any],
    *,
    reused: bool,
    spend: Spend | None = None,
    degradation: list[str] | None = None,
) -> PackageResult:
    notes = package.get("generation_notes") or {}
    stored = notes.get("apply") or {}
    blockers = package_module.approval_blockers(package)
    # A status blocker ("already approved", "already sent") is not a defect;
    # anything else is something the job seeker has to see before approving.
    outstanding = [b for b in blockers if b.get("kind") != "status"]
    return PackageResult(
        opportunity_id=package["opportunity_id"],
        package_id=package["id"],
        status="reused" if reused else ("ready" if not outstanding else "blocked"),
        reused=reused,
        ready=not outstanding,
        consistency_status=str(package.get("consistency_status") or "not_run"),
        leak_scan_status=package.get("leak_scan_status"),
        blockers=blockers,
        documents={
            kind: package.get(column) for kind, column in package_module.DOWNLOADABLE.items()
        },
        # FR-321, made visible: these two are for the job seeker only.
        never_sent=sorted(package_module.seeker_only_documents(package)),
        email_subject=package.get("email_subject"),
        recipient_email=package.get("contact_email"),
        language=package.get("language"),
        spend=(spend or Spend()).as_dict() if spend else (stored.get("spend") or {}),
        degradation=list(degradation or stored.get("degradation") or []),
        notes=sorted(
            {
                note
                for key in ("cv", "briefing", "motivation", "email")
                for note in (notes.get(key) or {}).get("notes") or []
            }
        ),
    )


# ---------------------------------------------------------------------------
# NFR-104: what a package cost, from the FR-364 call log
# ---------------------------------------------------------------------------


def _call_ids(job_seeker_id: str, entity_ids: Iterable[str]) -> set[str]:
    """The calls already logged against these entities, before generating."""
    return {row["id"] for row in _calls(job_seeker_id, entity_ids)}


def _spend_since(job_seeker_id: str, entity_ids: Iterable[str], before: set[str]) -> Spend:
    """Price the calls this run added.

    Read from ``llm_call`` rather than counted in this module because the CV,
    the briefing and the motivation document are generated by code that owns
    its own client; the log is the one place every call lands, whichever
    generator made it, and it already carries the cost the budget was debited.
    """
    spend = Spend()
    for row in _calls(job_seeker_id, entity_ids):
        if row["id"] in before:
            continue
        spend.calls += 1
        spend.input_tokens += int(row.get("input_tokens") or 0)
        spend.output_tokens += int(row.get("output_tokens") or 0)
        spend.cost_eur += float(row.get("cost_eur") or 0.0)
        if row.get("status") != "ok":
            spend.failed_calls += 1
    return spend


def _calls(job_seeker_id: str, entity_ids: Iterable[str]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for entity_id in entity_ids:
        if not entity_id:
            continue
        try:
            rows = admin_repo.list_llm_calls(
                job_seeker_id=job_seeker_id, entity_id=entity_id, limit=LLM_LOG_WINDOW
            )
        except Exception:  # noqa: BLE001 - accounting must not break generation
            log.exception("Could not read the call log for %s", entity_id)
            continue
        for row in rows:
            seen[row["id"]] = dict(row)
    return list(seen.values())


# ---------------------------------------------------------------------------
# FR-321(d): the application e-mail
# ---------------------------------------------------------------------------


def _write_email(
    job_seeker_id: str,
    package: dict[str, Any],
    inputs: dict[str, Any],
    opts: Options,
    llm: LLMClient | None,
) -> dict[str, Any]:
    notes = dict(package.get("generation_notes") or {})
    if not notes.get("cv_document"):
        raise GenerationError("The application e-mail needs a tailored CV; generate the CV first")
    document = from_dict(notes["cv_document"])
    opportunity = inputs.get("opportunity") or {}
    language = normalise_language(
        opts.language or package.get("language") or opportunity.get("language")
    )
    contact = repo.get_contact(opts.contact_id or package.get("contact_id"))
    objected = package_module._objected_contact(contact)
    if objected:
        raise GenerationError(
            f"{objected} has objected to being contacted (NFR-302); the application e-mail "
            "cannot be written for them."
        )

    draft = compose_apply_email(
        inputs,
        document,
        language=language,
        contact=contact,
        llm=llm,
        instructions=opts.instructions,
        fit_notes=notes.get("motivation_content"),
    )

    values: dict[str, Any] = {
        "email_subject": draft.subject,
        "email_body": draft.body,
        "language": draft.language,
        # A new e-mail is a new thing to approve (FR-324).
        "status": "draft",
        "approved_at": None,
        "approved_by": None,
    }
    if draft.recipient_email and not package.get("contact_id"):
        match = next(
            (c for c in inputs.get("contacts") or [] if c.get("email") == draft.recipient_email),
            None,
        )
        if match:
            values["contact_id"] = match["id"]

    stored = draft.as_dict()
    stored["prompt_template"] = PROMPT_NAME
    stored["prompt_version"] = _prompt_version()
    notes["email"] = stored
    values["generation_notes"] = notes
    updated = repo.update_package(package["id"], job_seeker_id, values)
    return updated or package


def compose_apply_email(
    inputs: dict[str, Any],
    cv: CvDocument,
    *,
    language: str | None = None,
    contact: dict[str, Any] | None = None,
    llm: LLMClient | None = None,
    instructions: str | None = None,
    fit_notes: dict[str, Any] | None = None,
) -> intro_email.EmailDraft:
    """A brief motivation letter that refers to the attached CV (FR-321(d)).

    The scaffolding around the body is ``intro_email``'s, deliberately: the
    greeting, the attachment line, the sign-off and the NFR-302 objection
    sentence are facts and legal text, not prose for a model to paraphrase, and
    ``documents/package.run_consistency`` registers exactly that wording as the
    system's own so the NFR-206 scan does not report it as unattributable.

    FR-263 / FR-323: for a speculative opening the finished text is re-read for
    phrasings that would assert a vacancy exists.  A hit becomes a high-severity
    finding on the package rather than a warning in a log, so the job seeker has
    to edit it before it can be approved - the model's word is not taken for it.
    """
    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    seeker = inputs.get("seeker") or {}
    lang = normalise_language(language or opportunity.get("language") or seeker.get("locale"))
    speculative = str(opportunity.get("kind") or "") == "speculative"
    role = str(opportunity.get("title") or "")
    recipient = contact or intro_email._pick_contact(inputs)
    # NFR-302: the model is never asked to write to an objected address, even
    # when a caller assembled its own inputs instead of going through the
    # repository query that already excludes them.
    objected = package_module._objected_contact(recipient)
    if objected:
        raise GenerationError(
            f"{objected} has objected to being contacted (NFR-302); the application e-mail "
            "cannot be written for them."
        )

    notes: list[str] = []
    used_llm = False
    subject = intro_email.SUBJECT["speculative" if speculative else "vacancy"][lang].format(
        role=role or company.get("name") or ""
    )
    # CR-405: the assembled body is safe to send on its own, and is what the
    # package falls back to whenever the model is unavailable or refused.
    core = intro_email._fallback_body(inputs, cv, lang, speculative)

    if llm is not None:
        try:
            generated = _ask_model(
                inputs, cv, lang, speculative, recipient, llm, instructions, fit_notes
            )
            if generated.get("body"):
                core = str(generated["body"]).strip()
                used_llm = True
            if generated.get("subject"):
                subject = str(generated["subject"]).strip()[:120]
            notes += [str(n) for n in generated.get("notes") or [] if str(n).strip()]
        except BudgetExhausted:
            notes.append("Token budget exhausted (NFR-104); the e-mail is the assembled version.")
        except TruncatedResponse as exc:
            # Already retried on the chat model by ``documents/_llm``; if that
            # was cut off too the package is still produced, and says so.
            log.warning("Apply e-mail truncated for %s: %s", opportunity.get("id"), exc)
            notes.append("The model was cut off at its token budget; the e-mail is assembled.")
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            log.warning("Apply e-mail generation failed for %s: %s", opportunity.get("id"), exc)
            notes.append(f"E-mail assembled without the model ({exc.__class__.__name__}).")

    body = intro_email._assemble(core, cv, lang, recipient)
    hits = intro_email.vacancy_assertions(body, lang) if speculative else []
    if hits:
        notes.append(
            "FR-323: the draft implies an advertised vacancy exists. Edit before approving: "
            + ", ".join(f"'{h}'" for h in hits)
        )
    return intro_email.EmailDraft(
        subject=subject,
        body=body,
        language=lang,
        speculative=speculative,
        recipient_name=(recipient or {}).get("full_name"),
        recipient_email=(recipient or {}).get("email"),
        used_llm=used_llm,
        notes=notes,
        assertions=hits,
    )


def _ask_model(
    inputs: dict[str, Any],
    cv: CvDocument,
    lang: str,
    speculative: bool,
    recipient: dict[str, Any] | None,
    llm: LLMClient,
    instructions: str | None,
    fit_notes: dict[str, Any] | None,
) -> dict[str, Any]:
    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    prompt = load_prompt(PROMPT_NAME)
    who = (
        f"{recipient.get('full_name') or 'the hiring contact'}"
        f"{', ' + recipient['role_title'] if recipient and recipient.get('role_title') else ''}"
        if recipient
        else "the hiring team (no named contact was found)"
    )
    fit = _fit_block(fit_notes)
    system, user = prompt.render(
        language=lang,
        opening_rule=(
            intro_email.SPECULATIVE_OPENING_RULE
            if speculative
            else intro_email.VACANCY_OPENING_RULE
        ),
        role_title=opportunity.get("title") or "",
        company_name=company.get("name") or "",
        recipient=who,
        cv_summary=intro_email._cv_summary(cv),
        word_budget=WORD_BUDGET,
        fit_hint=(
            "The motivation and fit document (FR-330) for this opportunity already argues the "
            "points in the untrusted block `fit`. Use at most one of them, in one sentence, and "
            "leave the rest to the attached CV."
            if fit
            else ""
        ),
    )
    if instructions:
        user += f"\n\nThe job seeker asked for this revision:\n{str(instructions)[:2000]}"

    # NFR-205: everything that came off the web goes in a fenced, labelled block
    # and never into the instructions - the derived fit points included, because
    # the requirements they quote are the opening's own words.
    untrusted = {
        "opening": json.dumps(
            {"opportunity": opportunity, "vacancy": inputs.get("vacancy") or {}},
            ensure_ascii=False,
            default=str,
        )[:12_000],
        "company": json.dumps(company, ensure_ascii=False, default=str)[:12_000],
    }
    if fit:
        untrusted["fit"] = fit

    return complete_json(
        llm,
        EMAIL_TASK,
        system,
        user,
        untrusted=untrusted,
        entity_type="opportunity",
        entity_id=opportunity.get("id"),
        prompt_template=prompt.name,
        prompt_version=prompt.version,
        schema_hint='{"subject": str, "body": str, "notes": [str]}',
        max_tokens=EMAIL_MAX_TOKENS,
    )


def _fit_block(content: dict[str, Any] | None) -> str:
    """The strongest points the FR-330 document made, so the two agree."""
    if not content:
        return ""
    lines: list[str] = []
    for row in (content.get("why_this_job") or [])[:2]:
        text = str((row or {}).get("text") or "").strip()
        if text:
            lines.append(f"- why this job: {text}")
    for row in content.get("why_fit_job") or []:
        if (row or {}).get("strength") != "strong":
            continue
        lines.append(f"- fit: {row.get('requirement')} - {row.get('evidence')}")
        if len(lines) >= 5:
            break
    return "\n".join(lines)[:2000]


def _prompt_version() -> str:
    try:
        return load_prompt(PROMPT_NAME).version
    except (OSError, ValueError):  # pragma: no cover - the file ships with the code
        return "unknown"


# ---------------------------------------------------------------------------
# NFR-502: bulk, with progress, pausable and resumable
# ---------------------------------------------------------------------------


async def generate_many(
    job_seeker_id: str,
    opportunity_ids: list[str],
    concurrency: int = DEFAULT_CONCURRENCY,
    *,
    campaign_id: str | None = None,
    regenerate: bool = False,
    options: Options | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Generate packages for many opportunities as one resumable job.

    Returns as soon as the job is running: the browser follows it with
    :func:`progress`, and can pause, resume or cancel it through the runner
    (FR-185, NFR-502).  Work is checkpointed after every package, so a restart
    resumes at the next one rather than paying for the batch again (NFR-401).
    """
    ids = [i for i in dict.fromkeys(opportunity_ids) if i]
    if not ids:
        raise ValueError("Select at least one opportunity to generate for (FR-321)")
    workers = max(1, min(int(concurrency or DEFAULT_CONCURRENCY), MAX_CONCURRENCY))
    opts = options or Options()

    job_id = runner.create(
        JOB_KIND,
        campaign_id=campaign_id,
        job_seeker_id=job_seeker_id,
        total=len(ids),
        estimated_seconds=int(len(ids) * SECONDS_PER_PACKAGE / workers),
    )
    state = {
        "opportunity_ids": ids,
        "options": opts.as_dict(),
        "regenerate": regenerate,
        "concurrency": workers,
        "actor": actor,
    }

    async def worker(ctx: JobContext) -> AsyncIterator[dict[str, Any]]:
        if not ctx.checkpoint.get("opportunity_ids"):
            ctx.save_checkpoint(**state)
        async for record in _run_batch(ctx):
            yield record

    await runner.start(job_id, worker)
    return {"job_id": job_id, "kind": JOB_KIND, "count": len(ids), "concurrency": workers}


async def _run_batch(ctx: JobContext) -> AsyncIterator[dict[str, Any]]:
    """The batch itself: at most ``concurrency`` packages in flight at once."""
    ids: list[str] = list(ctx.checkpoint.get("opportunity_ids") or [])
    if not ids or not ctx.job_seeker_id:
        ctx.record_error("No opportunities recorded on this job; nothing to generate.")
        return
    opts = Options.from_dict(ctx.checkpoint.get("options") or {})
    regenerate = bool(ctx.checkpoint.get("regenerate"))
    actor = ctx.checkpoint.get("actor")
    requested = int(ctx.checkpoint.get("concurrency") or DEFAULT_CONCURRENCY)
    workers = max(1, min(requested, MAX_CONCURRENCY))
    seeker_id = ctx.job_seeker_id

    done: list[str] = list(ctx.checkpoint.get("done") or [])
    results: list[dict[str, Any]] = list(ctx.checkpoint.get("results") or [])
    pending = deque(i for i in ids if i not in set(done))
    ctx.progress(len(done), len(ids))

    running: dict[asyncio.Task, str] = {}
    try:
        while pending or running:
            # FR-185: pause and cancel are honoured between packages, and the
            # barrier is called here as well as by the runner so that a cancel
            # lands inside this generator and the in-flight tasks are dropped.
            await ctx.checkpoint_barrier()
            while pending and len(running) < workers:
                opportunity_id = pending.popleft()
                task = asyncio.create_task(
                    asyncio.to_thread(
                        _generate_one, seeker_id, opportunity_id, regenerate, opts, actor
                    )
                )
                running[task] = opportunity_id
            finished, _ = await asyncio.wait(set(running), return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                opportunity_id = running.pop(task)
                record = task.result()
                if record.get("error"):
                    ctx.record_error(f"{opportunity_id}: {record['error']}")
                done.append(opportunity_id)
                results.append(record)
                ctx.progress(len(done), len(ids))
                ctx.save_checkpoint(done=done, results=results[-500:])
                yield record
    finally:
        for task in running:
            task.cancel()


def _generate_one(
    job_seeker_id: str,
    opportunity_id: str,
    regenerate: bool,
    opts: Options,
    actor: str | None,
) -> dict[str, Any]:
    """One package, in a worker thread.  A failure is recorded, never raised."""
    try:
        result = generate_package(
            job_seeker_id, opportunity_id, regenerate=regenerate, options=opts, actor=actor
        )
        return result.as_dict()
    except Exception as exc:  # noqa: BLE001 - one bad package must not stop the batch
        log.exception("Package generation failed for opportunity %s", opportunity_id)
        return PackageResult(
            opportunity_id=opportunity_id,
            package_id=None,
            status="failed",
            error=f"{exc.__class__.__name__}: {exc}",
        ).as_dict()


async def _resume_worker(ctx: JobContext) -> AsyncIterator[dict[str, Any]]:
    """Registered worker, so a job left behind by a restart can be resumed."""
    async for record in _run_batch(ctx):
        yield record


runner.register_worker(JOB_KIND, _resume_worker)


def progress(job_id: str) -> dict[str, Any] | None:
    """Everything the progress bar needs (NFR-502): done, left, cost, errors."""
    row = runner.status(job_id)
    if row is None:
        return None
    checkpoint = from_json(row.get("checkpoint"), {}) or {}
    results = checkpoint.get("results") or []
    total = int(row.get("progress_total") or len(checkpoint.get("opportunity_ids") or []) or 0)
    done = int(row.get("progress_done") or 0)
    counts: dict[str, int] = {}
    cost = 0.0
    for record in results:
        counts[str(record.get("status"))] = counts.get(str(record.get("status")), 0) + 1
        cost += float((record.get("spend") or {}).get("cost_eur") or 0.0)
    per_item = float(row.get("estimated_seconds") or SECONDS_PER_PACKAGE) / max(total, 1)
    return {
        "job_id": job_id,
        "kind": row.get("kind"),
        "status": row.get("status"),
        "running": runner.is_running(job_id),
        "done": done,
        "total": total,
        "remaining": max(total - done, 0),
        "percent": round(100.0 * done / total, 1) if total else 0.0,
        "seconds_remaining": int(max(total - done, 0) * per_item),
        "errors": int(row.get("error_count") or 0),
        "last_error": row.get("last_error"),
        "counts": counts,
        "cost_eur": round(cost, 6),
        "packages": results[-50:],
    }


def pause(job_id: str) -> bool:
    return runner.pause(job_id)


def resume(job_id: str) -> bool:
    return runner.resume(job_id)


def cancel(job_id: str) -> bool:
    return runner.cancel(job_id)


# ---------------------------------------------------------------------------
# FR-321: what may be attached, and what may never be
# ---------------------------------------------------------------------------


def email_attachments(package: dict[str, Any]) -> list[Attachment]:
    """The tailored CV, and nothing else.

    FR-321 makes the briefing (FR-329) and the motivation and fit document
    (FR-330) material for the job seeker alone: they are read before an
    interview and are **never attached to an outgoing e-mail**.  Every
    attachment in the Apply Browser is assembled here, ``mail/composer`` refuses
    those two by path, and the result is checked again below - so a future
    caller that passes the whole package still cannot leak them.
    """
    attachments = composer.collect_attachments(package)
    forbidden = {
        str(composer.resolve_path(package.get(column)) or package.get(column) or "")
        for column in package_module.SEEKER_ONLY_PATHS
        if package.get(column)
    }
    for attachment in attachments:
        if attachment.source_path and str(attachment.source_path) in forbidden:
            raise composer.AttachmentRefused(
                "FR-321: the briefing and the motivation document are for the job seeker "
                "only and are never attached to an e-mail."
            )
    return attachments


# ---------------------------------------------------------------------------
# The dry-run guard: no e-mail leaves this machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendGuard:
    """Whether sending is possible at all, and what would have to change."""

    dry_run: bool
    env_allows: bool
    setting_allows: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "env_allows": self.env_allows,
            "setting_allows": self.setting_allows,
            "reasons": self.reasons,
            "to_enable": [
                f"set {SEND_ENABLED_ENV}=1 in the server environment and restart the API",
                f"turn on the administration setting {SEND_ENABLED_SETTING!r}",
            ],
        }


def send_guard() -> SendGuard:
    """Two switches, both deliberate, or nothing is sent.

    The product owner asked for the whole pipeline up to a generated e-mail with
    the CV attached, and explicitly not for the send.  So the guard lives here,
    on the server, in the one function every send path in this module goes
    through - a control in the interface would only be a suggestion.

    One switch is an environment variable that no screen can write, the other an
    administration setting that no environment can set; both have to be thrown
    by hand.  Anything unexpected while reading them leaves the guard on.
    """
    env_allows = os.environ.get(SEND_ENABLED_ENV, "").strip().lower() in _TRUTHY
    try:
        setting_allows = _is_true(admin_repo.get_setting(SEND_ENABLED_SETTING, False))
    except Exception:  # noqa: BLE001 - fail closed: an unreadable switch is off
        log.exception("Could not read %s; the dry-run guard stays on", SEND_ENABLED_SETTING)
        setting_allows = False

    reasons: list[str] = []
    if not env_allows:
        reasons.append(f"{SEND_ENABLED_ENV} is not set in the server environment.")
    if not setting_allows:
        reasons.append(f"The administration setting {SEND_ENABLED_SETTING!r} is off.")
    return SendGuard(
        dry_run=not (env_allows and setting_allows),
        env_allows=env_allows,
        setting_allows=setting_allows,
        reasons=reasons,
    )


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in _TRUTHY


def assert_send_allowed(*, count: int = 1) -> None:
    """Raise unless both switches are thrown.  Call this before any dispatch."""
    guard = send_guard()
    if guard.dry_run:
        raise SendBlocked(
            f"Dry run: {count} message(s) were prepared but not sent. "
            + " ".join(guard.reasons)
            + " Until both are on, nothing is dispatched."
        )


def render_email(job_seeker_id: str, package_id: str) -> dict[str, Any]:
    """Build the message the send *would* produce, and write it beside the CV.

    This is the end of the pipeline the product owner asked for: a complete
    RFC 5322 message with the tailored CV attached, on disk as ``email.eml``,
    where it can be opened and read.  No backend is resolved and no connection
    is made - resolving a mailbox is already a step towards sending.
    """
    package = dispatch_repo.package_for_dispatch(package_id, job_seeker_id)
    if package is None:
        raise GenerationError(f"No application package {package_id} for this job seeker")
    # NFR-302: even assembling the .eml for an objected recipient is refused,
    # so a refused send leaves no message on disk.
    objected = _package_objection(package)
    if objected:
        raise GenerationError(objected)
    seeker = dispatch_repo.seeker_identity(job_seeker_id) or {}
    from_email = (get_settings().mail_from or seeker.get("email") or "").strip()
    if not from_email:
        return {"rendered": False, "reason": "No sender address is configured."}

    try:
        message = composer.compose(
            package,
            seeker=seeker,
            from_email=from_email,
            language=package.get("language") or package.get("opportunity_language"),
            attachments=email_attachments(package),
        )
        mime = composer.to_mime(message)
    except (MailBackendError, OSError) as exc:
        return {"rendered": False, "reason": f"{exc.__class__.__name__}: {exc}"}

    path = package_module.package_dir(job_seeker_id, package_id) / "email.eml"
    path.write_bytes(bytes(mime))
    warnings: list[str] = []
    body = message.body_text
    sentence = intro_email.OBJECTION_SENTENCE[composer.normalise_language(message.language)]
    if sentence[:40] in body and body.count("--") > 1:
        warnings.append(
            "The NFR-302 objection sentence appears twice: once from the generated body and "
            "once appended by mail/composer.build_body."
        )
    return {
        "rendered": True,
        "path": str(path),
        "bytes": path.stat().st_size,
        "to": message.to_email,
        "subject": message.subject,
        "attachments": [a.filename for a in message.attachments],
        # FR-321, restated where the message is built: these never go out.
        "never_attached": sorted(package_module.seeker_only_documents(package)),
        "warnings": warnings,
    }


def send_package(job_seeker_id: str, package_id: str, *, actor: str) -> dict[str, Any]:
    """The Send control.  Wired end to end, and blocked by the dry-run guard.

    An approved package is composed, its attachment list assembled and the
    message written to disk; the dispatcher - and with it every FR-325 rail and
    the actual network call - is reached only when :func:`send_guard` says both
    switches are on.
    """
    package = repo.get_package(package_id, job_seeker_id)
    if package is None:
        raise GenerationError(f"No application package {package_id} for this job seeker")

    guard = send_guard()
    result: dict[str, Any] = {
        "package_id": package_id,
        "opportunity_id": package.get("opportunity_id"),
        "company": package.get("company_name"),
        "recipient": package.get("contact_email"),
        "sent": False,
        "dry_run": guard.dry_run,
    }
    # NFR-302: an objection that arrived after approval - or after the package
    # was built - blocks here, before the dry run can write the message.
    objected = _package_objection(package)
    if objected:
        result["refused"] = objected
        result["blockers"] = package_module.approval_blockers(package)
        return result
    if package.get("status") != "approved":
        # FR-324: nothing is sent that the job seeker has not approved.
        result["refused"] = (
            f"The package is {package.get('status')!r}; FR-324 requires approval before dispatch."
        )
        result["blockers"] = package_module.approval_blockers(package)
        return result

    if guard.dry_run:
        result["message"] = render_email(job_seeker_id, package_id)
        result["reasons"] = guard.reasons
        result["guard"] = guard.as_dict()
        repo.record_audit(
            job_seeker_id,
            "apply.send.dry_run",
            actor=actor,
            entity_id=package_id,
            detail={
                "recipient": package.get("contact_email"),
                "subject": package.get("email_subject"),
                "message": result["message"],
                "reasons": guard.reasons,
            },
        )
        log.info(
            "Dry run: package %s prepared, not sent (%s)",
            package_id,
            "; ".join(guard.reasons),
        )
        return result

    # Past the guard, and only past it.
    assert_send_allowed()
    from dreamjob.mail.dispatcher import send_package as dispatch_send  # noqa: PLC0415

    dispatch = dispatch_send(package_id, job_seeker_id, approved_by=actor)
    result["dispatch"] = dispatch
    result["sent"] = dispatch.get("status") == "sent"
    return result


def send_all(job_seeker_id: str, package_ids: list[str], *, actor: str) -> dict[str, Any]:
    """The Send all control.  The same guard, once per package.

    One unknown or unapprovable package refuses itself and no more: a batch of
    forty must not be abandoned because the third of them was discarded.
    """
    guard = send_guard()
    results: list[dict[str, Any]] = []
    for package_id in package_ids:
        try:
            results.append(send_package(job_seeker_id, package_id, actor=actor))
        except GenerationError as exc:
            results.append(
                {"package_id": package_id, "sent": False, "dry_run": guard.dry_run,
                 "refused": str(exc)}
            )
    return {
        "dry_run": guard.dry_run,
        "guard": guard.as_dict(),
        "requested": len(package_ids),
        "sent": sum(1 for r in results if r.get("sent")),
        "prepared": sum(1 for r in results if not r.get("sent") and not r.get("refused")),
        "refused": [r for r in results if r.get("refused")],
        "results": results,
    }
