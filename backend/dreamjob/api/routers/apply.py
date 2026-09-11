"""The Apply Browser API (FR-301, FR-304, FR-321..FR-331, FR-325, NFR-302, RK-05).

One surface over machinery that already exists.  Nothing here writes a CV,
infers an address or assembles a MIME message: :mod:`dreamjob.documents`,
:mod:`dreamjob.pipeline.contacts` and :mod:`dreamjob.mail` do all of that.  This
router's job is to put them in the one order a person actually works through -

    browse -> select -> find who to write to -> generate -> read and edit -> send

- and to make the last step safe.

``GET /browser`` is the list, and it is one query: ``db.repositories.apply``
joins the opportunity, its company, its live package and the contact that would
receive the message, so a page of fifty rows costs one round trip rather than
fifty follow-up requests to find out whether each has a contact.  The contact
column reads through ``usable_contact``, which means an address FR-304
validated ``invalid`` and one whose owner objected (NFR-302) are already gone
before this router sees the row.

``/send``, ``/send-all`` and ``/send-status`` go through
:mod:`dreamjob.mail.dry_run`.  While ``DREAMJOB_MAIL_DRY_RUN`` is on - and it
is on by default - the whole dispatch path runs and stops at the transport: the
message is assembled with the tailored CV attached, written to
``data/generated/dry_run/``, and recorded as a dispatch with
``delivery_status='dry_run'``; the response says in words that nothing was
sent and why.  The guard is in the mail layer rather than here, so importing it
protects every other send path in the process too, and this router could not
put a message on the wire even if it tried.

Two documents are downloadable by the job seeker and never attachable to
anything: the briefing (FR-329) and the motivation document (FR-330).
``package.document_path`` refuses them for a dispatch, and ``composer`` refuses
them again by path on the way to a message (FR-321).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.db.repositories import applications as packages_repo
from dreamjob.db.repositories import apply as apply_repo
from dreamjob.db.repositories import dispatch as dispatch_repo
from dreamjob.db.repositories import opportunities as opportunities_repo
from dreamjob.documents import package as package_module
from dreamjob.documents.package import GenerationError, GenerationOptions, NeverSent
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.llm.client import TruncatedResponse
from dreamjob.mail import dry_run
from dreamjob.mail.base import MailBackendError, MailBackendUnavailable
from dreamjob.mail.dispatcher import SendRefused, check_send_allowed
from dreamjob.pipeline import apply_packages
from dreamjob.pipeline import contacts as contacts_pipeline

log = logging.getLogger(__name__)
router = APIRouter()

MEDIA_TYPES: dict[str, str] = {
    "cv_pdf": "application/pdf",
    "briefing": "application/pdf",
    "motivation": "application/pdf",
    "cv_docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

#: The downloadable artefacts of FR-321, and the column each one lives in.
ARTEFACTS: dict[str, str] = {
    "cv_docx": "cv_docx_path",
    "cv_pdf": "cv_pdf_path",
    "briefing": "briefing_pdf_path",
    "motivation": "motivation_pdf_path",
}

#: Where a row is in the pipeline, in the order the job seeker moves through it.
STATES: tuple[str, ...] = (
    "not_generated",
    "draft",
    "approved",
    "dry_run",
    "sent",
    "discarded",
)

#: Modules that may define the FR-301 entry point the browser drives, most
#: specific first.  The contacts slice owns it; this router only calls it.
_CONTACT_MODULES: tuple[str, ...] = (
    "dreamjob.pipeline.apply_contacts",
    "dreamjob.pipeline.contacts",
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SelectIn(BaseModel):
    opportunity_ids: list[str] = Field(default_factory=list, max_length=2000)
    selected: bool = True
    status: str | None = Field(
        None, description="selected | skipped | generating | ready - defaults to 'selected'"
    )


class DiscoverIn(BaseModel):
    opportunity_ids: list[str] = Field(default_factory=list, max_length=2000)
    campaign_id: str | None = None
    selected_only: bool = True
    crawl_site: bool = True
    allow_smtp: bool = True
    limit: int = Field(200, ge=1, le=2000)


class GenerateIn(BaseModel):
    opportunity_ids: list[str] = Field(default_factory=list, max_length=2000)
    campaign_id: str | None = None
    selected_only: bool = True
    parts: list[str] | None = None
    language: str | None = Field(None, max_length=8)
    cv_template: str | None = None
    contact_id: str | None = None
    instructions: str | None = Field(None, max_length=4000)
    use_llm: bool = True
    regenerate: bool = False
    limit: int = Field(500, ge=1, le=2000)


class EmailIn(BaseModel):
    """FR-324: the job seeker's own edits, before anything is dispatched."""

    subject: str | None = Field(None, max_length=300)
    body: str | None = None
    contact_id: str | None = None
    language: str | None = Field(None, max_length=8)


class RegenerateIn(BaseModel):
    instruction: str | None = Field(None, max_length=4000)
    parts: list[str] | None = None
    language: str | None = Field(None, max_length=8)
    cv_template: str | None = None
    contact_id: str | None = None
    use_llm: bool = True


class SendIn(BaseModel):
    backend: str | None = None
    ignore_window: bool = Field(
        False, description="Step over the FR-325 send window; recorded in the audit trail"
    )


class SendAllIn(SendIn):
    package_ids: list[str] | None = Field(None, max_length=500)
    limit: int = Field(100, ge=1, le=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exists(path: Any) -> bool:
    """Availability means the file is on disk, not that the column is filled."""
    return bool(path) and Path(str(path)).is_file()


def _state(row: dict[str, Any], dry_run_packages: set[str]) -> str:
    """Where this row has got to.

    ``dry_run`` sits between ``approved`` and ``sent`` on purpose: a message
    that was assembled and withheld is further along than one that is merely
    approved, and it is emphatically not sent.
    """
    package_status = row.get("package_status")
    if not row.get("package_id"):
        return "not_generated"
    if package_status == "discarded":
        return "discarded"
    if package_status == "sent" or row.get("dispatched_at"):
        return "sent"
    if row.get("package_id") in dry_run_packages:
        return "dry_run"
    if package_status == "approved":
        return "approved"
    return "draft"


def _dry_run_packages(seeker_id: str) -> dict[str, dict[str, Any]]:
    """The packages that have been assembled but not sent, by package id.

    One query for the page rather than one per row: the browser needs to
    distinguish "prepared and withheld" from "approved and untouched", and that
    distinction is the whole point of the dry run.
    """
    rows = dispatch_repo.list_dispatches(
        seeker_id, status=dry_run.DRY_RUN_STATUS, kind="application", limit=500
    )
    prepared: dict[str, dict[str, Any]] = {}
    for row in rows:
        prepared.setdefault(row["application_package_id"], row)
    return prepared


def _owned_opportunity(opportunity_id: str, seeker_id: str) -> dict[str, Any]:
    opportunity = opportunities_repo.get_opportunity(opportunity_id, seeker_id)
    if opportunity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Opportunity not found")
    return opportunity


def _owned_package(opportunity_id: str, seeker_id: str) -> dict[str, Any]:
    package = packages_repo.latest_for_opportunity(seeker_id, opportunity_id)
    if package is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Nothing has been generated for this opportunity yet; "
            "run POST /api/apply/packages/generate first (FR-321)",
        )
    return package


def _resolve_ids(
    seeker_id: str,
    ids: list[str],
    *,
    campaign_id: str | None,
    selected_only: bool,
    limit: int,
) -> list[str]:
    """The opportunities a batch acts on: the ones asked for, or the selection."""
    chosen = list(dict.fromkeys(ids))
    if chosen:
        # FR-344: an id that belongs to someone else is not silently worked on.
        owned = {o["id"] for o in opportunities_repo.get_by_ids(chosen, seeker_id)}
        chosen = [i for i in chosen if i in owned]
    elif selected_only:
        chosen = packages_repo.selected_opportunity_ids(seeker_id, campaign_id)
    if not chosen:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Select at least one opportunity, or pass opportunity_ids you own "
            "(FR-321, FR-284)",
        )
    return chosen[:limit]


def _generation_options(
    payload: GenerateIn | RegenerateIn, instructions: str | None = None
) -> GenerationOptions:
    parts = tuple(payload.parts) if payload.parts else package_module.PARTS
    unknown = set(parts) - set(package_module.PARTS)
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown parts: {', '.join(sorted(unknown))}; the four artefacts of "
            f"FR-321 are {', '.join(package_module.PARTS)}",
        )
    return GenerationOptions(
        language=payload.language,
        cv_template=payload.cv_template,
        contact_id=payload.contact_id,
        instructions=(
            instructions if instructions is not None else getattr(payload, "instructions", None)
        ),
        parts=parts,
        use_llm=payload.use_llm,
    )


def _generate_one(
    seeker_id: str,
    opportunity_id: str,
    options: GenerationOptions,
    *,
    actor: str,
    package_id: str | None = None,
) -> dict[str, Any]:
    """Generate, and never let a cut-off model become a 500.

    ``llm/client.py`` raises :class:`TruncatedResponse` when a reasoning model
    spends its whole budget thinking and returns nothing.  The generators route
    that to the chat model themselves, so it should not reach here; if it does -
    a new artefact, a call that did not go through the shared helper - the run
    is repeated once with the model switched off.  Every generator has a
    deterministic path (NFR-104), so the job seeker gets a package and a note
    about the degradation rather than a stack trace.
    """
    try:
        return package_module.generate(
            seeker_id, opportunity_id, options, package_id=package_id, actor=actor
        )
    except TruncatedResponse as exc:
        log.warning(
            "Generation for opportunity %s was cut off at the model's token budget; "
            "repeating deterministically: %s",
            opportunity_id,
            exc,
        )
        deterministic = GenerationOptions(
            language=options.language,
            cv_template=options.cv_template,
            briefing_template=options.briefing_template,
            motivation_template=options.motivation_template,
            contact_id=options.contact_id,
            instructions=options.instructions,
            parts=options.parts,
            use_llm=False,
            run_judge=False,
        )
        return package_module.generate(
            seeker_id, opportunity_id, deterministic, package_id=package_id, actor=actor
        )


# ---------------------------------------------------------------------------
# The list (FR-284, FR-321, FR-331, NFR-502)
# ---------------------------------------------------------------------------


@router.get("/browser")
def browse(
    campaign_id: str | None = None,
    q: str | None = Query(None, max_length=200, description="Title, company or contact name"),
    filters: list[str] | None = Query(
        None, description=f"Any of: {', '.join(sorted(apply_repo.FILTERS))}"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict[str, Any]:
    """One page of the Apply Browser: opportunity, company, contact, package state."""
    unknown = set(filters or []) - set(apply_repo.FILTERS)
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown filter(s): {', '.join(sorted(unknown))}; "
            f"known filters are {', '.join(sorted(apply_repo.FILTERS))}",
        )

    rows = apply_repo.list_jobs(
        seeker.id,
        campaign_id=campaign_id,
        filters=filters,
        search=q,
        limit=limit,
        offset=offset,
    )
    total = apply_repo.count_jobs(
        seeker.id, campaign_id=campaign_id, filters=filters, search=q
    )
    prepared = _dry_run_packages(seeker.id)

    out: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        dispatch = prepared.get(row.get("package_id") or "")
        record["state"] = _state(row, set(prepared))
        record["dry_run"] = None if dispatch is None else {
            "dispatch_id": dispatch["id"],
            "prepared_at": dispatch.get("created_at"),
            "recipient_email": dispatch.get("recipient_email"),
            "mime_path": (dispatch.get("delivery_detail") or {}).get("mime_path"),
        }
        out.append(record)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "facets": apply_repo.facet_counts(seeker.id, campaign_id=campaign_id),
        "selection_counts": apply_repo.selection_counts(seeker.id),
        "states": list(STATES),
        "filters": sorted(apply_repo.FILTERS),
        # So the screen can label the Send button honestly before it is pressed.
        "sending": {"dry_run": dry_run.is_armed()},
        "rows": out,
    }


@router.post("/select")
def select(payload: SelectIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict[str, Any]:
    """FR-284, FR-324: the job seeker decides what the pipeline works on.

    Selecting writes both the Apply Browser's own record and
    ``opportunity.selected``, which is what the rest of the application reads;
    the repository keeps the two in step.  Ownership is checked here, because a
    selection row is keyed by ``(job_seeker_id, opportunity_id)`` and would
    otherwise accept an id belonging to somebody else (FR-344).
    """
    ids = list(dict.fromkeys(payload.opportunity_ids))
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "opportunity_ids is required")
    owned = [o["id"] for o in opportunities_repo.get_by_ids(ids, seeker.id)]
    if not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such opportunity for this job seeker")

    if payload.selected:
        changed = apply_repo.select_many(
            seeker.id, owned, status=payload.status or "selected"
        )
    else:
        changed = sum(apply_repo.deselect_opportunity(seeker.id, i) for i in owned)

    return {
        "selected": payload.selected,
        "requested": len(ids),
        "changed": changed,
        "ignored": sorted(set(ids) - set(owned)),
        "selection_counts": apply_repo.selection_counts(seeker.id),
    }


# ---------------------------------------------------------------------------
# Contacts (FR-301, FR-303, FR-304)
# ---------------------------------------------------------------------------


def _contact_entry() -> Any:
    """The FR-301 run this router drives.

    ``ensure_apply_contacts`` is the entry point the contacts slice owns.  It is
    looked up rather than imported so that the Apply Browser works both before
    and after that slice lands, falling back to ``discover_for_opportunity`` -
    the per-opportunity FR-301 run it is built on - in the meantime.
    """
    for module_name in _CONTACT_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:  # pragma: no cover - the module is optional
            continue
        entry = getattr(module, "ensure_apply_contacts", None)
        if entry is not None:
            return entry
    return contacts_pipeline.discover_for_opportunity


def _accepted(entry: Any) -> set[str]:
    try:
        return set(inspect.signature(entry).parameters)
    except (TypeError, ValueError):  # pragma: no cover - builtins have no signature
        return set()


async def _call_discovery(entry: Any, offered: dict[str, Any]) -> None:
    """Call the FR-301 entry point with the arguments it actually takes.

    The two candidate entry points do not take the same options - one works per
    opportunity, one per batch of companies - so the arguments are matched to
    the signature by name instead of being assumed.  Everything is passed by
    keyword, which is what makes that safe.
    """
    kwargs = {k: v for k, v in offered.items() if k in _accepted(entry)}
    result = entry(**kwargs)
    if inspect.isawaitable(result):
        await result


@router.post("/contacts/discover", status_code=status.HTTP_202_ACCEPTED)
async def discover_contacts(
    payload: DiscoverIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """FR-301: find who to write to, for the whole selection, as a resumable job."""
    ids = _resolve_ids(
        seeker.id,
        payload.opportunity_ids,
        campaign_id=payload.campaign_id,
        selected_only=payload.selected_only,
        limit=payload.limit,
    )
    entry = _contact_entry()
    per_opportunity = "opportunity_id" in _accepted(entry)
    total = len(ids) if per_opportunity else 1

    job_id = runner.create(
        "apply_contacts",
        campaign_id=payload.campaign_id,
        job_seeker_id=seeker.id,
        total=total,
        # Discovery reads the company's own site, and the egress layer paces
        # itself to 0.5 rps per host (FR-182); a few seconds each is honest.
        estimated_seconds=total * 8,
    )

    base: dict[str, Any] = {
        "job_seeker_id": seeker.id,
        "campaign_id": payload.campaign_id,
        "crawl_site": payload.crawl_site,
        "allow_smtp": payload.allow_smtp,
        "limit": payload.limit,
    }

    async def worker(ctx: JobContext) -> Any:
        if not per_opportunity:
            # A batch entry point does its own work list; the browser only asks.
            try:
                await _call_discovery(entry, base)
            except Exception as exc:  # noqa: BLE001 - reported, never re-raised
                ctx.record_error(str(exc))
            ctx.progress(1, 1)
            yield "batch"
            return
        done = int((ctx.checkpoint or {}).get("done") or 0)
        for index, opportunity_id in enumerate(ids):
            if index < done:
                continue
            try:
                await _call_discovery(entry, {**base, "opportunity_id": opportunity_id})
            except Exception as exc:  # noqa: BLE001 - one company must not stop the run
                ctx.record_error(f"{opportunity_id}: {exc}")
            ctx.progress(index + 1, len(ids))
            ctx.save_checkpoint(done=index + 1)
            yield opportunity_id

    await runner.start(job_id, worker)
    return {
        "job_id": job_id,
        "kind": "apply_contacts",
        "count": total,
        "entry_point": getattr(entry, "__module__", "") + "." + getattr(entry, "__name__", ""),
    }


# ---------------------------------------------------------------------------
# Generation (FR-321, FR-322, FR-329, FR-330)
# ---------------------------------------------------------------------------


@router.post("/packages/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate_packages(
    payload: GenerateIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """FR-321: the four artefacts, for every selected opportunity, as a job."""
    ids = _resolve_ids(
        seeker.id,
        payload.opportunity_ids,
        campaign_id=payload.campaign_id,
        selected_only=payload.selected_only,
        limit=payload.limit,
    )
    options = _generation_options(payload)

    # The whole of FR-321, which is what the browser asks for, is what
    # ``pipeline/apply_packages`` generates: four packages in flight at once,
    # a complete package reused rather than paid for twice, the per-package
    # cost read back from the FR-364 call log, the NFR-104 degradation ladder
    # and the FR-322 gate over the CV *and* the e-mail.  It owns its own job,
    # so this route hands back that job rather than wrapping it in a second one.
    if tuple(options.parts) == tuple(package_module.PARTS):
        try:
            job = await apply_packages.generate_many(
                seeker.id,
                ids,
                campaign_id=payload.campaign_id,
                regenerate=payload.regenerate,
                options=apply_packages.Options(
                    language=options.language,
                    cv_template=options.cv_template,
                    briefing_template=options.briefing_template,
                    motivation_template=options.motivation_template,
                    contact_id=options.contact_id,
                    instructions=options.instructions,
                    use_llm=options.use_llm,
                ),
                actor=seeker.email,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return {**job, "count": len(ids)}

    # A run that asks for only *some* of the four artefacts is not what that
    # module generates, so it stays on the single-artefact path.
    job_id = runner.create(
        "apply_generation",
        campaign_id=payload.campaign_id,
        job_seeker_id=seeker.id,
        total=len(ids),
        estimated_seconds=len(ids) * 45,
    )

    async def worker(ctx: JobContext) -> Any:
        done = int((ctx.checkpoint or {}).get("done") or 0)
        for index, opportunity_id in enumerate(ids):
            if index < done:
                continue
            try:
                # Generation is synchronous and does real work; off the event
                # loop it goes, or progress and cancellation stop responding
                # for the whole batch (NFR-502).
                await asyncio.to_thread(
                    _generate_one, seeker.id, opportunity_id, options, actor=seeker.email
                )
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
                ctx.record_error(f"{opportunity_id}: {exc}")
            ctx.progress(index + 1, len(ids))
            ctx.save_checkpoint(done=index + 1)
            yield opportunity_id

    await runner.start(job_id, worker)
    return {"job_id": job_id, "kind": "apply_generation", "count": len(ids)}


# ---------------------------------------------------------------------------
# Sending - and the guard that makes it impossible (RK-05, FR-325)
# ---------------------------------------------------------------------------


@router.get("/send-status")
def send_status(seeker: CurrentSeeker = Depends(current_seeker)) -> dict[str, Any]:
    """Is sending armed, and if not, why not.

    Answered by the mail layer, so the screen reports the state of the guard
    rather than its own idea of it: the dry run, whether a mailbox is really
    configured, what is left of the FR-325 daily cap, the pacing rule, and the
    send window - which is expressed in the recipient's time zone.
    """
    return dry_run.send_status(seeker.id)


@router.post("/send-all", status_code=status.HTTP_202_ACCEPTED)
def send_all(payload: SendAllIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict[str, Any]:
    """FR-324, FR-325: the whole approved selection, one guarded message at a time."""
    try:
        return dry_run.send_all(
            seeker.id,
            approved_by=seeker.id,
            package_ids=payload.package_ids,
            backend_key=payload.backend,
            ignore_window=payload.ignore_window,
            limit=payload.limit,
        )
    except MailBackendUnavailable as exc:  # pragma: no cover - provider outage
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except MailBackendError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# ---------------------------------------------------------------------------
# One application in full (FR-324, FR-331)
# ---------------------------------------------------------------------------


def _recipient_advisories(row: dict) -> list[dict[str, str]]:
    """Non-blocking warnings about the chosen recipient (FR-304, NFR-305).

    A generic careers mailbox is a legitimate last-resort target, but applying
    there is slower and may never reach a person.  Saying so before Send is the
    difference between an informed decision and a silent one.
    """
    out: list[dict[str, str]] = []
    if row.get("contact_is_generic"):
        out.append(
            {
                "kind": "generic_mailbox",
                "detail": (
                    "This is the employer's general careers mailbox rather than a named "
                    "person: replies are slower, and the address is a pattern guess."
                ),
            }
        )
    validation = (row.get("contact_email_validation") or "").lower()
    if row.get("contact_email") and validation not in {"valid"}:
        out.append(
            {
                "kind": "unverified_email",
                "detail": (
                    f"This address is unverified ({validation or 'unknown'}); it may bounce. "
                    "Consider finding a named contact first."
                ),
            }
        )
    if row.get("reachability") == "unreachable":
        out.append(
            {
                "kind": "unreachable",
                "detail": row.get("unreachable_reason") or "This contact is unreachable.",
            }
        )
    return out


@router.get("/{opportunity_id}")
def get_application(
    opportunity_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """Everything about one application: the e-mail, the documents, the checks."""
    _owned_opportunity(opportunity_id, seeker.id)
    package = _owned_package(opportunity_id, seeker.id)
    row = apply_repo.job(seeker.id, opportunity_id) or {}
    view = package_module.preview(package)

    # Oldest first out of the repository, so the last row is the current one.
    dispatches = dispatch_repo.dispatches_for_package(package["id"], seeker.id)
    latest = dispatches[-1] if dispatches else None
    prepared = next(
        (d for d in reversed(dispatches) if d.get("delivery_status") == dry_run.DRY_RUN_STATUS),
        None,
    )

    # The FR-325 / FR-304 / NFR-302 rails as they stand now, so the screen can
    # say why Send will wait before the job seeker presses it.
    for_dispatch = dispatch_repo.package_for_dispatch(package["id"], seeker.id) or {}
    decision = check_send_allowed(
        seeker.id,
        recipient_email=for_dispatch.get("contact_email"),
        email_validation=for_dispatch.get("contact_email_validation"),
        objected=bool(for_dispatch.get("contact_objected")),
        country=for_dispatch.get("company_country"),
        locations=for_dispatch.get("company_locations"),
        jurisdiction=for_dispatch.get("company_jurisdiction"),
    )

    return {
        "opportunity": {
            "id": opportunity_id,
            "title": row.get("title"),
            "kind": row.get("kind"),
            "language": row.get("language"),
            "score": row.get("score"),
            "campaign_id": row.get("campaign_id"),
            "user_status": row.get("user_status"),
            "selection_status": row.get("selection_status"),
        },
        "company": {
            "id": row.get("company_id"),
            "name": row.get("company_name"),
            "country": row.get("company_country"),
            "domain": row.get("company_domain"),
        },
        "contact": {
            "id": row.get("contact_id"),
            "name": row.get("contact_name"),
            "role": row.get("contact_role"),
            "email": row.get("contact_email"),
            "email_validation": row.get("contact_email_validation"),
            "email_source_method": row.get("contact_method"),
            "is_generic_mailbox": row.get("contact_is_generic"),
            "objected": row.get("contact_objected"),
            "reachability": row.get("reachability"),
            "unreachable_reason": row.get("unreachable_reason"),
        },
        "package": view,
        "email": {
            "subject": package.get("email_subject"),
            "body": package.get("email_body"),
            "language": package.get("language"),
        },
        # Paths so the job seeker can open the file on their own machine; the
        # bytes come from /document/{kind}.  The briefing and the motivation
        # document are downloadable here and never attachable (FR-321).
        "documents": {
            kind: {
                "available": _exists(package.get(column)),
                "path": package.get(column),
                "download": f"/api/apply/{opportunity_id}/document/{kind}",
                "never_sent": column in package_module.SEEKER_ONLY_PATHS,
            }
            for kind, column in ARTEFACTS.items()
        },
        "consistency": {
            "status": package.get("consistency_status"),
            "leak_scan_status": package.get("leak_scan_status"),
            "report": package.get("consistency_report"),
        },
        "blockers": view.get("blockers"),
        "advisories": _recipient_advisories(row),
        "state": _state(
            {
                "package_id": package["id"],
                "package_status": package.get("status"),
                "dispatched_at": (latest or {}).get("sent_at"),
            },
            {package["id"]} if prepared else set(),
        ),
        "dispatch": latest,
        "dry_run": None if prepared is None else {
            "dispatch_id": prepared["id"],
            "prepared_at": prepared.get("created_at"),
            "mime_path": (prepared.get("delivery_detail") or {}).get("mime_path"),
        },
        "send_check": {
            "allowed": decision.allowed,
            "code": decision.code,
            "reason": decision.reason,
            "retry_at": decision.retry_at,
            "timezone": decision.timezone,
            "recipient_local_time": decision.recipient_local_time,
        },
        "sending": {"dry_run": dry_run.is_armed()},
    }


@router.put("/{opportunity_id}/email")
def edit_email(
    opportunity_id: str, payload: EmailIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """FR-324: edit the e-mail before it is sent.

    Editing re-opens the package - ``package.edit`` drops it back to ``draft``
    and re-runs the FR-322 check over the new text - so an approved message
    cannot be changed after approval without being approved again.
    """
    package = _owned_package(opportunity_id, seeker.id)
    changes: dict[str, Any] = {}
    if payload.subject is not None:
        changes["email_subject"] = payload.subject
    if payload.body is not None:
        changes["email_body"] = payload.body
    if payload.contact_id is not None:
        changes["contact_id"] = payload.contact_id
    if payload.language is not None:
        changes["language"] = payload.language
    if not changes:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Nothing to change: pass subject, body, contact_id or language",
        )
    try:
        updated = package_module.edit(seeker.id, package["id"], changes)
    except GenerationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if updated is None:  # pragma: no cover - the package was read a line above
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application package not found")
    return package_module.preview(updated)


@router.post("/{opportunity_id}/regenerate")
async def regenerate(
    opportunity_id: str, payload: RegenerateIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """FR-324: regenerate with an instruction, keeping the same package."""
    package = _owned_package(opportunity_id, seeker.id)
    options = _generation_options(payload, instructions=payload.instruction)
    try:
        updated = await asyncio.to_thread(
            _generate_one,
            seeker.id,
            opportunity_id,
            options,
            actor=seeker.email,
            package_id=package["id"],
        )
    except GenerationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return package_module.preview(updated)


@router.get("/{opportunity_id}/document/{kind}")
def download(
    opportunity_id: str, kind: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> FileResponse:
    """The generated file itself: cv_docx, cv_pdf, briefing or motivation.

    All four are downloadable *by the job seeker*.  Two of them - the briefing
    (FR-329) and the motivation document (FR-330) - are refused the moment
    anything asks for them on behalf of a dispatch, which is what
    ``for_dispatch`` in ``document_path`` exists for.
    """
    package = _owned_package(opportunity_id, seeker.id)
    try:
        path = package_module.document_path(package, kind)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown document {kind!r}; expected one of {', '.join(sorted(ARTEFACTS))}",
        ) from exc
    except NeverSent as exc:  # pragma: no cover - only reachable via dispatch
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    company = (package.get("company_name") or "application").replace("/", "-")
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(kind, "application/octet-stream"),
        filename=f"{company} - {kind}{path.suffix}",
    )


@router.post("/{opportunity_id}/send", status_code=status.HTTP_201_CREATED)
def send_one(
    opportunity_id: str, payload: SendIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict[str, Any]:
    """Send this application - or, while the dry run is on, build it and stop.

    The decision is not taken here.  ``dry_run.send_one`` runs the whole path
    and stops at the transport, and the concrete backends refuse again on their
    own account, so this route cannot put a message on the wire however it is
    called.
    """
    package = _owned_package(opportunity_id, seeker.id)
    try:
        return dry_run.send_one(
            package["id"],
            seeker.id,
            approved_by=seeker.id,
            backend_key=payload.backend,
            ignore_window=payload.ignore_window,
        )
    except SendRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except MailBackendUnavailable as exc:  # pragma: no cover - provider outage
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except MailBackendError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
