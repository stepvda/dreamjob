"""Application package orchestration (FR-321, FR-322, FR-324, FR-331, NFR-206).

FR-321 asks for four artefacts per selected opportunity, and this module is
where they are produced together and kept together:

(a) the tailored CV, in DOCX and PDF (``cv_generator``);
(b) the company and job briefing PDF (``briefing``);
(c) the motivation and fit PDF (``motivation``);
(d) the introduction email, with the tailored CV attached (``intro_email``).

**(b) and (c) are for the job seeker only and are never sent.**  That is
enforced by ``dispatch_attachments``: it is the only function that turns a
package into a list of files for the mail backend, it returns CV paths and
nothing else, and asking it for a seeker-only document raises rather than
silently attaching one.

FR-322 makes the factual-consistency check a precondition for dispatch, so
``approve`` refuses a package whose check did not pass unless the job seeker
records a reason, and refuses outright - with no override - a package whose
leakage scan failed (NFR-206) or whose recipient has objected (NFR-302).

FR-324's review loop lives here too: preview, edit, regenerate with
instructions, approve, discard, and bulk approval with a mandatory summary of
what will be sent to whom, which is written to the audit trail (NFR-702).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import applications as repo
from dreamjob.documents import briefing as briefing_module
from dreamjob.documents import consistency as consistency_module
from dreamjob.documents import intro_email
from dreamjob.documents import motivation as motivation_module
from dreamjob.documents.cv_generator import generate_cv
from dreamjob.documents.intro_email import compose_email
from dreamjob.documents.pdf_builder import label, normalise_language
from dreamjob.documents.templates import (
    DEFAULT_TEMPLATE,
    MONTH_ABBREVIATIONS,
    from_dict,
    render,
    to_dict,
)
from dreamjob.llm.client import LLMClient

log = logging.getLogger(__name__)

#: The four artefacts of FR-321.
PARTS: tuple[str, ...] = ("cv", "briefing", "motivation", "email")

#: FR-321: only the CV is ever attached to an outgoing message.
SENDABLE_PATHS: tuple[str, ...] = ("cv_pdf_path", "cv_docx_path")
SEEKER_ONLY_PATHS: tuple[str, ...] = ("briefing_pdf_path", "motivation_pdf_path")

DOWNLOADABLE: dict[str, str] = {
    "cv_pdf": "cv_pdf_path",
    "cv_docx": "cv_docx_path",
    "briefing": "briefing_pdf_path",
    "motivation": "motivation_pdf_path",
}


class GenerationError(RuntimeError):
    """The package could not be generated from the data available."""


class NotApprovable(RuntimeError):
    """The package may not be approved for dispatch yet (FR-322, NFR-206)."""

    def __init__(self, message: str, findings: list[dict] | None = None):
        super().__init__(message)
        self.findings = findings or []


class NeverSent(RuntimeError):
    """FR-321: the briefing and motivation documents are never attached."""


@dataclass
class GenerationOptions:
    language: str | None = None
    cv_template: str | None = None
    briefing_template: str | None = None
    motivation_template: str | None = None
    contact_id: str | None = None
    instructions: str | None = None
    parts: tuple[str, ...] = PARTS
    use_llm: bool = True
    run_judge: bool = True


# ---------------------------------------------------------------------------
# LLM access, gated on CR-410 consent
# ---------------------------------------------------------------------------


def llm_for(job_seeker_id: str, campaign_id: str | None) -> tuple[LLMClient | None, str | None]:
    """A client, or ``(None, reason)`` - never an exception (NFR-104, CR-410).

    Generated content is built from profile data, so the transfer consent of
    CR-410 is a precondition for using the provider at all.  Without it the
    generators fall back to their deterministic paths rather than failing.
    """
    from dreamjob.pipeline.composite import has_consent_for  # noqa: PLC0415
    from dreamjob.pipeline.enrichment import default_llm  # noqa: PLC0415

    if not has_consent_for(job_seeker_id, "llm_transfer"):
        return None, "No consent recorded for transferring profile data to the LLM (CR-410)."
    client = default_llm(job_seeker_id, campaign_id)
    if client is None:
        return None, "No LLM provider is configured."
    return client, None


# ---------------------------------------------------------------------------
# Generation (FR-321)
# ---------------------------------------------------------------------------


def package_dir(job_seeker_id: str, package_id: str) -> Path:
    directory = get_settings().generated_dir / job_seeker_id / package_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def generate(
    job_seeker_id: str,
    opportunity_id: str,
    options: GenerationOptions | None = None,
    *,
    package_id: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Produce (or regenerate) the four artefacts for one opportunity."""
    options = options or GenerationOptions()
    inputs = repo.generation_inputs(job_seeker_id, opportunity_id)
    if inputs is None:
        raise GenerationError("Opportunity not found for this job seeker")
    if not inputs.get("profile_version"):
        raise GenerationError("No profile version exists yet; upload a profile first (FR-102)")

    opportunity = inputs["opportunity"]
    seeker = inputs.get("seeker") or {}
    language = normalise_language(
        options.language or opportunity.get("language") or seeker.get("locale")
    )
    if options.contact_id:
        contact = repo.get_contact(options.contact_id)
    else:
        contact = None

    existing = (
        repo.get_package(package_id, job_seeker_id)
        if package_id
        else repo.latest_for_opportunity(job_seeker_id, opportunity_id)
    )
    # FR-329 wants the briefing refreshable just before an interview, which is
    # normally *after* the application went out.  Only the two artefacts that
    # would change what was sent are refused on a sent package.
    dispatchable = bool({"cv", "email"} & set(options.parts))
    if existing and existing["status"] == "sent" and dispatchable:
        raise GenerationError("This package has been sent; its CV and email can no longer change")

    notes: dict[str, Any] = dict((existing or {}).get("generation_notes") or {})
    if existing:
        row_id = existing["id"]
        values: dict[str, Any] = {}
    else:
        row_id = repo.create_package(
            job_seeker_id,
            opportunity_id,
            {
                "language": language,
                "cv_template": options.cv_template or DEFAULT_TEMPLATE,
                "contact_id": (contact or {}).get("id"),
                "profile_version_id": (inputs.get("profile_version") or {}).get("id"),
                "company_snapshot_at": inputs.get("company_snapshot_at"),
            },
        )
        values = {}
    directory = package_dir(job_seeker_id, row_id)

    llm, llm_reason = (None, "Model use switched off for this run")
    if options.use_llm:
        llm, llm_reason = llm_for(job_seeker_id, (inputs.get("campaign") or {}).get("id"))
    if llm_reason:
        notes.setdefault("degradation", []).append(llm_reason)

    parts = tuple(options.parts)
    document = None

    if "cv" in parts:
        result = generate_cv(
            inputs,
            output_dir=directory,
            language=language,
            template=options.cv_template or (existing or {}).get("cv_template"),
            llm=llm,
            instructions=options.instructions,
        )
        document = result.document
        notes["cv"] = result.as_dict()
        notes["cv_document"] = to_dict(document)
        values.update(
            {
                "cv_template": result.template,
                "cv_docx_path": result.docx_path,
                "cv_pdf_path": result.pdf_path,
            }
        )
    elif notes.get("cv_document"):
        document = from_dict(notes["cv_document"])

    if "briefing" in parts:
        result = briefing_module.generate_briefing(
            inputs,
            output_dir=directory,
            language=language,
            template=options.briefing_template,
            llm=llm,
        )
        notes["briefing"] = result.as_dict()
        values["briefing_pdf_path"] = result.path

    motivation_text = ""
    if "motivation" in parts:
        result = motivation_module.generate_motivation(
            inputs,
            output_dir=directory,
            language=language,
            template=options.motivation_template,
            llm=llm,
            instructions=options.instructions,
        )
        notes["motivation"] = result.as_dict()
        notes["motivation_content"] = result.content
        motivation_text = result.plain_text()
        values["motivation_pdf_path"] = result.path
    elif notes.get("motivation_content"):
        motivation_text = motivation_module.MotivationResult(
            path="", template="", language=language, generated_at="", used_llm=False,
            content=notes["motivation_content"], notes=[],
        ).plain_text()

    if "email" in parts:
        if document is None:
            raise GenerationError("The email needs a CV; generate the CV in the same run")
        draft = compose_email(
            inputs,
            document,
            language=language,
            contact=contact or repo.get_contact((existing or {}).get("contact_id")),
            llm=llm,
            instructions=options.instructions,
        )
        notes["email"] = draft.as_dict()
        values.update({"email_subject": draft.subject, "email_body": draft.body})
        if draft.recipient_email and not (contact or {}).get("id"):
            match = next(
                (
                    c
                    for c in inputs.get("contacts") or []
                    if c.get("email") == draft.recipient_email
                ),
                None,
            )
            if match:
                values["contact_id"] = match["id"]

    values.update(
        {
            "language": language,
            "profile_version_id": (inputs.get("profile_version") or {}).get("id"),
            "company_snapshot_at": inputs.get("company_snapshot_at"),
        }
    )
    if dispatchable:
        # Regenerating what goes out withdraws any approval it already had, and
        # with it any override that approval carried: a fresh CV is a fresh
        # FR-322 check, and the old reason does not answer for the new text.
        values.update(
            {
                "status": "draft",
                "approved_at": None,
                "approved_by": None,
                "consistency_override": None,
            }
        )
    notes.setdefault("history", []).append(
        {
            "at": utcnow(),
            "action": "regenerate" if existing else "generate",
            "parts": list(parts),
            "instructions": options.instructions,
            "actor": actor,
        }
    )
    notes["history"] = notes["history"][-20:]
    values["generation_notes"] = notes
    repo.update_package(row_id, job_seeker_id, values)

    package = repo.get_package(row_id, job_seeker_id)
    return run_consistency(
        job_seeker_id,
        package,  # type: ignore[arg-type]
        inputs=inputs,
        document=document,
        motivation_text=motivation_text,
        llm=llm if options.run_judge else None,
    )


# ---------------------------------------------------------------------------
# FR-322 / NFR-206: the gate
# ---------------------------------------------------------------------------


def run_consistency(
    job_seeker_id: str,
    package: dict[str, Any],
    *,
    inputs: dict[str, Any] | None = None,
    document: Any = None,
    motivation_text: str = "",
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Validate the package and store the verdict (FR-322, NFR-206, RK-03)."""
    opportunity_id = package["opportunity_id"]
    inputs = inputs or repo.generation_inputs(job_seeker_id, opportunity_id) or {}
    notes = package.get("generation_notes") or {}
    if document is None and notes.get("cv_document"):
        document = from_dict(notes["cv_document"])
    if document is None:
        return package

    corpus = repo.provenance_corpus(job_seeker_id, opportunity_id)
    lang = normalise_language(package.get("language") or "en")
    # Wording the system itself writes: the email scaffolding and the month
    # names the CV templates render dates with (NFR-206 - known origin).
    corpus["boilerplate"] = "\n".join(
        [intro_email.scaffolding(lang), *MONTH_ABBREVIATIONS[lang], label(lang, "cv_present")]
    )
    extra = {}
    if package.get("email_body"):
        extra["email"] = f"{package.get('email_subject') or ''}\n{package['email_body']}"
    if motivation_text:
        extra["motivation"] = motivation_text
    elif notes.get("motivation_content"):
        extra["motivation"] = motivation_module.MotivationResult(
            path="", template="", language=package.get("language") or "en", generated_at="",
            used_llm=False, content=notes["motivation_content"], notes=[],
        ).plain_text()

    report = consistency_module.run_checks(
        document,
        inputs,
        corpus,
        extra_parts=extra,
        llm=llm,
        entity_id=package["id"],
        use_judge=llm is not None,
    )

    # FR-323: an email that implies an advertised vacancy blocks dispatch.
    for phrase in (notes.get("email") or {}).get("vacancy_assertions") or []:
        report.findings.append(
            consistency_module.Finding(
                "speculative_claim", "email", str(phrase), "high",
                "FR-323: a speculative application must not assert that a vacancy exists.",
                signal="deterministic",
            )
        )
    if any(f.severity == "high" for f in report.findings):
        report.status = consistency_module.FAIL

    updated = repo.update_package(
        package["id"],
        job_seeker_id,
        {
            "consistency_status": report.status,
            "consistency_report": report.as_dict(),
            "leak_scan_status": report.leak_status,
        },
    )
    return updated or package


# ---------------------------------------------------------------------------
# FR-324: review, approve, discard
# ---------------------------------------------------------------------------


def edit(job_seeker_id: str, package_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    """The job seeker's own edits to the email (FR-324).

    Editing re-opens the package: an approved package that is then edited is a
    draft again, and the checks run over the new text before it can be
    approved a second time.
    """
    allowed = {
        k: v
        for k, v in changes.items()
        if k in {"email_subject", "email_body", "contact_id", "language"}
    }
    if not allowed:
        return repo.get_package(package_id, job_seeker_id)
    package = repo.get_package(package_id, job_seeker_id)
    if package is None:
        return None
    if package["status"] == "sent":
        raise GenerationError("A sent package can no longer be edited")

    allowed["status"] = "draft"
    allowed["approved_at"] = None
    allowed["approved_by"] = None
    notes = dict(package.get("generation_notes") or {})
    notes.setdefault("history", []).append(
        {"at": utcnow(), "action": "edit", "fields": sorted(allowed)}
    )
    if "email_body" in allowed and (notes.get("email") or {}).get("speculative"):
        from dreamjob.documents.intro_email import vacancy_assertions  # noqa: PLC0415

        notes["email"]["vacancy_assertions"] = vacancy_assertions(
            str(allowed["email_body"]), str(allowed.get("language") or package["language"])
        )
    allowed["generation_notes"] = notes
    updated = repo.update_package(package_id, job_seeker_id, allowed)
    if updated is None:
        return None
    return run_consistency(job_seeker_id, updated)


def approval_blockers(package: dict[str, Any]) -> list[dict[str, Any]]:
    """Why this package may not be dispatched yet (FR-322, NFR-206, NFR-302)."""
    blockers: list[dict[str, Any]] = []
    report = package.get("consistency_report") or {}
    if package.get("status") == "sent":
        blockers.append({"kind": "status", "detail": "Already sent.", "overridable": False})
    if package.get("status") == "discarded":
        blockers.append({"kind": "status", "detail": "Discarded.", "overridable": False})
    if package.get("status") == "approved":
        blockers.append(
            {"kind": "status", "detail": "Already approved for dispatch.", "overridable": False}
        )
    if package.get("leak_scan_status") == consistency_module.FAIL:
        blockers.append(
            {
                "kind": "leak",
                "detail": "Content that is not traceable to this job seeker (NFR-206).",
                "overridable": False,
                "findings": report.get("leaks") or [],
            }
        )
    if package.get("contact_objected"):
        blockers.append(
            {
                "kind": "objection",
                "detail": "The contact objected to being contacted (NFR-302).",
                "overridable": False,
            }
        )
    if package.get("consistency_status") != consistency_module.PASS:
        blockers.append(
            {
                "kind": "consistency",
                "detail": "Claims in the CV are not supported by the profile (FR-322, RK-03).",
                "overridable": True,
                "findings": [
                    f for f in report.get("findings") or [] if f.get("severity") == "high"
                ],
            }
        )
    if not package.get("cv_pdf_path"):
        blockers.append(
            {"kind": "missing", "detail": "No CV has been generated.", "overridable": False}
        )
    if not package.get("email_body"):
        blockers.append(
            {"kind": "missing", "detail": "No introduction email.", "overridable": False}
        )
    return blockers


def approve(
    job_seeker_id: str,
    package_ids: list[str],
    *,
    actor: str,
    summary: str | None = None,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Approve one package, or many with a mandatory summary (FR-324, NFR-702)."""
    packages = repo.packages_by_ids(package_ids, job_seeker_id)
    missing = set(package_ids) - {p["id"] for p in packages}
    if missing:
        raise NotApprovable(f"Unknown package(s): {', '.join(sorted(missing))}")
    if len(packages) > 1 and not (summary or "").strip():
        # FR-324: bulk approval requires a statement of what goes to whom.
        raise NotApprovable(
            "Bulk approval requires a summary of what will be sent to whom (FR-324)"
        )

    refused: list[dict[str, Any]] = []
    approvable: list[str] = []
    for package in packages:
        blockers = approval_blockers(package)
        hard = [b for b in blockers if not b["overridable"]]
        soft = [b for b in blockers if b["overridable"]]
        if hard or (soft and not (override_reason or "").strip()):
            refused.append(
                {
                    "package_id": package["id"],
                    "opportunity": package.get("opportunity_title"),
                    "company": package.get("company_name"),
                    "blockers": blockers,
                }
            )
            continue
        approvable.append(package["id"])

    approved = repo.approve_packages(
        job_seeker_id, approvable, actor, override_reason=override_reason
    )
    if approved:
        repo.record_audit(
            job_seeker_id,
            "application_package.approve",
            actor=actor,
            entity_id=approved[0] if len(approved) == 1 else None,
            detail={
                "package_ids": approved,
                "summary": summary,
                "override_reason": override_reason,
                "recipients": [
                    {
                        "package_id": p["id"],
                        "company": p.get("company_name"),
                        "opportunity": p.get("opportunity_title"),
                        "contact": p.get("contact_name"),
                        "email": p.get("contact_email"),
                        "subject": p.get("email_subject"),
                        "consistency": p.get("consistency_status"),
                    }
                    for p in packages
                    if p["id"] in approved
                ],
            },
        )
    return {"approved": approved, "refused": refused}


def bulk_summary(job_seeker_id: str, package_ids: list[str]) -> dict[str, Any]:
    """What a bulk approval would send, to whom (FR-324's mandatory summary)."""
    packages = repo.packages_by_ids(package_ids, job_seeker_id)
    rows = [
        {
            "package_id": p["id"],
            "company": p.get("company_name"),
            "opportunity": p.get("opportunity_title"),
            "kind": p.get("opportunity_kind"),
            "language": p.get("language"),
            "contact_name": p.get("contact_name"),
            "contact_email": p.get("contact_email"),
            "subject": p.get("email_subject"),
            "attachments": [Path(a).name for a in dispatch_attachments(p, strict=False)],
            "consistency_status": p.get("consistency_status"),
            "leak_scan_status": p.get("leak_scan_status"),
            "blockers": approval_blockers(p),
        }
        for p in packages
    ]
    return {
        "count": len(rows),
        "recipients": sorted({r["contact_email"] for r in rows if r["contact_email"]}),
        "blocked": [r["package_id"] for r in rows if r["blockers"]],
        "packages": rows,
    }


def discard(job_seeker_id: str, package_id: str, *, actor: str, reason: str | None = None) -> bool:
    package = repo.get_package(package_id, job_seeker_id)
    if package is None:
        return False
    if package["status"] == "sent":
        raise GenerationError("A sent package cannot be discarded")
    repo.discard_package(package_id, job_seeker_id)
    repo.record_audit(
        job_seeker_id,
        "application_package.discard",
        actor=actor,
        entity_id=package_id,
        detail={"reason": reason},
    )
    return True


# ---------------------------------------------------------------------------
# FR-321: what may leave the machine
# ---------------------------------------------------------------------------


def dispatch_attachments(
    package: dict[str, Any], *, include_docx: bool = False, strict: bool = True
) -> list[str]:
    """The files that may be attached to the introduction email - CV only.

    FR-321 makes the briefing and the motivation document job-seeker material.
    Every path that leaves for the mail backend comes through here, so there is
    one place to read to be sure they never do.
    """
    keys = ("cv_pdf_path", "cv_docx_path") if include_docx else ("cv_pdf_path",)
    out: list[str] = []
    for key in keys:
        path = package.get(key)
        if path and Path(path).is_file():
            out.append(str(path))
        elif path and strict:
            raise GenerationError(f"Generated file is missing from disk: {path}")
    return out


def seeker_only_documents(package: dict[str, Any]) -> dict[str, str]:
    """The FR-329 and FR-330 PDFs, for download by the job seeker only."""
    return {
        key: str(package[key])
        for key in SEEKER_ONLY_PATHS
        if package.get(key) and Path(str(package[key])).is_file()
    }


def document_path(package: dict[str, Any], kind: str, *, for_dispatch: bool = False) -> Path:
    """Resolve a download request, refusing to hand a seeker-only file to dispatch."""
    key = DOWNLOADABLE.get(kind)
    if key is None:
        raise KeyError(f"Unknown document kind {kind!r}")
    if for_dispatch and key in SEEKER_ONLY_PATHS:
        raise NeverSent(
            f"{kind} is prepared for the job seeker only and is never sent (FR-321)"
        )
    path = package.get(key)
    if not path or not Path(str(path)).is_file():
        raise FileNotFoundError(f"{kind} has not been generated for this package")
    return Path(str(path))


# ---------------------------------------------------------------------------
# Preview (FR-324)
# ---------------------------------------------------------------------------


def preview(package: dict[str, Any]) -> dict[str, Any]:
    """The package as the review screen needs it."""
    notes = package.get("generation_notes") or {}
    view = {k: v for k, v in package.items() if k != "generation_notes"}
    view["documents"] = {
        kind: bool(package.get(key)) for kind, key in DOWNLOADABLE.items()
    }
    view["attachments"] = [Path(p).name for p in dispatch_attachments(package, strict=False)]
    view["never_sent"] = sorted(seeker_only_documents(package))
    view["blockers"] = approval_blockers(package)
    view["generation"] = {
        key: notes.get(key) for key in ("cv", "briefing", "motivation", "email", "degradation")
    }
    view["history"] = notes.get("history") or []
    view["notes"] = sorted(
        {
            note
            for key in ("cv", "briefing", "motivation", "email")
            for note in (notes.get(key) or {}).get("notes") or []
        }
    )
    return view


def regenerate_cv_only(
    job_seeker_id: str, package_id: str, *, template: str
) -> dict[str, Any] | None:
    """Re-render the stored document with a different template (FR-322).

    No model call: the same facts, a different layout.
    """
    package = repo.get_package(package_id, job_seeker_id)
    if package is None:
        return None
    notes = dict(package.get("generation_notes") or {})
    if not notes.get("cv_document"):
        return None
    document = from_dict(notes["cv_document"])
    directory = package_dir(job_seeker_id, package_id)
    rendered = render(document, template, directory / "cv.docx", directory / "cv.pdf")
    notes.setdefault("history", []).append(
        {"at": utcnow(), "action": "retemplate", "template": rendered["template"]}
    )
    return repo.update_package(
        package_id,
        job_seeker_id,
        {
            "cv_template": rendered["template"],
            "cv_docx_path": rendered["docx_path"],
            "cv_pdf_path": rendered["pdf_path"],
            "generation_notes": notes,
        },
    )
