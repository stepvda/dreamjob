"""Browser automation API (FR-201..208, NFR-303, NFR-502, CR-401, FR-328).

The route order follows the way a browser run is actually done: see whether a
session is available, read the launch instructions if it is not, acknowledge
the LinkedIn terms (CR-401), look at the exact target list and the estimated
duration, confirm and start, then watch, pause, skip and cancel (FR-206).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker, owned_or_404
from dreamjob.browser import ats_form, glassdoor
from dreamjob.browser import linkedin as li
from dreamjob.browser import pacing as pacing_mod
from dreamjob.browser import session as session_mod
from dreamjob.config import get_settings
from dreamjob.db.repositories import browser as repo
from dreamjob.db.repositories import companies as company_repo
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.security import auth_service as auth

router = APIRouter()

# FR-101: every route here is scoped to the authenticated job seeker.
Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]
# The retention policy is installation-wide and the purge deletes other people's
# browser-collected contacts, so it is an administrator's decision (NFR-303,
# NFR-344).  The equivalent routes under /api/contacts already require one.
Admin = Annotated[CurrentSeeker, Depends(current_admin)]

SITES = {"linkedin": li, "glassdoor": glassdoor}


# ---------------------------------------------------------------------------
# Job wiring: one job kind, dispatched to the site module that planned it
# ---------------------------------------------------------------------------


async def browser_worker(ctx: JobContext) -> None:
    """Run the site worker named in the job's own state (FR-205)."""
    site = repo.job_state(ctx.job_id).get("site") or "linkedin"
    module = SITES.get(site, li)
    await module.worker(ctx)


runner.register_worker("browser", browser_worker)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AcknowledgementIn(BaseModel):
    """CR-401: the LinkedIn terms warning, acknowledged or withdrawn."""

    granted: bool = True
    detail: str | None = None


class RunRequest(BaseModel):
    campaign_id: str
    site: str = Field("linkedin", pattern="^(linkedin|glassdoor)$")
    extra_urls: list[str] = Field(default_factory=list)


class DriverChoice(BaseModel):
    """FR-208: Chromium over CDP, or a Playwright persistent context (Firefox)."""

    driver: str = Field("cdp", pattern="^(cdp|persistent)$")
    browser_family: str = Field("chromium", pattern="^(chromium|firefox)$")


class StartRunRequest(RunRequest, DriverChoice):
    # FR-204: no run starts without the user confirming the announced duration.
    confirmed: bool = False


class SkipIn(BaseModel):
    url: str


class RetentionIn(BaseModel):
    grace_days: int = Field(ge=0, le=3650)


class PrefillIn(BaseModel):
    """FR-328: pre-fill an ATS form, never submit it."""

    url: str
    values: dict[str, str] = Field(default_factory=dict)
    attachments: dict[str, str] = Field(default_factory=dict)
    application_package_id: str | None = None
    opportunity_id: str | None = None


def _module(site: str) -> Any:
    module = SITES.get(site)
    if module is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown site {site!r}")
    return module


def _job_or_404(job_id: str, seeker_id: str) -> dict:
    job = repo.get_job(job_id, seeker_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "browser run not found")
    return job


# ---------------------------------------------------------------------------
# Connection and launch instructions (FR-201, FR-202)
# ---------------------------------------------------------------------------


@router.get("/status")
async def connection_status(seeker: Seeker, site: str | None = None) -> dict:
    """Is a debuggable browser attached, and is the site signed in? (FR-201)"""
    status_ = await session_mod.probe()
    payload = status_.as_dict()
    if not status_.connected:
        # Degrade into something actionable rather than an error (FR-201).
        payload["instructions"] = session_mod.launch_instructions(site)
    return payload


@router.get("/instructions")
def instructions(seeker: Seeker, site: str | None = None, os: str | None = None) -> dict:
    """The step-by-step launch procedure, per platform (FR-201)."""
    return session_mod.launch_instructions(site, os)


@router.get("/drivers")
def drivers(seeker: Seeker) -> dict:
    """The drivers a run may be started with (FR-208)."""
    return {
        "default": {"driver": "cdp", "browser_family": "chromium"},
        "options": [
            {
                "driver": "cdp",
                "browser_family": "chromium",
                "display_name": "Chrome, Edge, Brave or Chromium over the DevTools Protocol",
            },
            {
                "driver": "persistent",
                "browser_family": "chromium",
                "display_name": "Chromium, launched by Dream Job on the dedicated profile",
            },
            {
                "driver": "persistent",
                "browser_family": "firefox",
                "display_name": "Firefox, launched by Dream Job on the dedicated profile",
            },
        ],
        "profile_dir": str(session_mod.profile_dir()),
    }


@router.get("/sites")
def sites(seeker: Seeker) -> list[dict]:
    return [
        {
            "key": profile.key,
            "display_name": profile.display_name,
            "home_url": profile.home_url,
            "login_url": profile.login_url,
            "terms_warning": profile.terms_warning,
            "consent_kind": profile.consent_kind,
        }
        for profile in session_mod.SITES.values()
    ]


@router.get("/login")
async def login_state(seeker: Seeker, site: str = "linkedin") -> dict:
    """Ask the live session whether the user is signed in (FR-202).

    Reads the rendered page, never the cookie jar (NFR-203).
    """
    if site not in session_mod.SITES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown site {site!r}")
    try:
        async with session_mod.BrowserSession() as live:
            return await live.check_login(site)
    except session_mod.BrowserUnavailable as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": "browser_unavailable", "message": str(exc),
             "instructions": session_mod.launch_instructions(site)},
        ) from exc


# ---------------------------------------------------------------------------
# The CR-401 acknowledgement
# ---------------------------------------------------------------------------


@router.get("/acknowledgement")
def acknowledgement(seeker: Seeker) -> dict:
    """The LinkedIn terms warning and whether it has been acknowledged (CR-401)."""
    return li.acknowledgement_state(seeker.id)


@router.post("/acknowledgement")
def record_acknowledgement(payload: AcknowledgementIn, seeker: Seeker) -> dict:
    return li.record_acknowledgement(seeker.id, payload.granted, payload.detail)


# ---------------------------------------------------------------------------
# Targets, estimate, confirmation (FR-204, FR-205, NFR-502)
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/targets")
def campaign_targets(campaign_id: str, seeker: Seeker, site: str = "linkedin") -> dict:
    """The closed target list this campaign's plan permits (FR-205)."""
    module = _module(site)
    try:
        return module.prepare_run(campaign_id, seeker.id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/runs/estimate")
def estimate_run(payload: RunRequest, seeker: Seeker) -> dict:
    """The duration the user is asked to confirm before anything opens (FR-204)."""
    module = _module(payload.site)
    try:
        return module.prepare_run(payload.campaign_id, seeker.id, extra_urls=payload.extra_urls)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (li.TargetRefused, glassdoor.TargetRefused) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def start_run(payload: StartRunRequest, seeker: Seeker) -> dict:
    """Confirm and start (FR-204). Refused without the CR-401 acknowledgement."""
    module = _module(payload.site)
    try:
        return await module.start_run(
            payload.campaign_id,
            seeker.id,
            confirmed=payload.confirmed,
            extra_urls=payload.extra_urls,
            driver=payload.driver,
            browser_family=payload.browser_family,
        )
    except auth.ConsentRequired as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {
                "error": "acknowledgement_required",
                "message": str(exc),
                "warning": li.terms_warning(),
                "consent_kind": li.CONSENT_KIND,
            },
        ) from exc
    except li.ConfirmationRequired as exc:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (ValueError, li.TargetRefused, glassdoor.TargetRefused) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# ---------------------------------------------------------------------------
# Watching and controlling a run (FR-206, NFR-502)
# ---------------------------------------------------------------------------


@router.get("/runs")
def list_runs(seeker: Seeker, limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    return repo.browser_jobs(seeker.id, limit=limit)


@router.get("/runs/{job_id}")
def run_status(job_id: str, seeker: Seeker) -> dict:
    """Live progress and the refined estimate (FR-204, NFR-502).

    The reader is site-agnostic: it reports whatever the run's own state says,
    so a Glassdoor run is read through the same route as a LinkedIn one.
    """
    _job_or_404(job_id, seeker.id)
    return li.run_status(job_id, seeker.id)


@router.post("/runs/{job_id}/pause")
def pause_run(job_id: str, seeker: Seeker) -> dict:
    _job_or_404(job_id, seeker.id)
    return li.pause(job_id).as_dict()


@router.post("/runs/{job_id}/resume")
def resume_run(job_id: str, seeker: Seeker) -> dict:
    _job_or_404(job_id, seeker.id)
    return li.resume(job_id).as_dict()


@router.post("/runs/{job_id}/cancel")
def cancel_run(job_id: str, seeker: Seeker) -> dict:
    _job_or_404(job_id, seeker.id)
    return li.cancel(job_id).as_dict()


@router.post("/runs/{job_id}/skip")
def skip_target(job_id: str, payload: SkipIn, seeker: Seeker) -> dict:
    """FR-206: leave one target alone without stopping the run."""
    _job_or_404(job_id, seeker.id)
    return li.skip(job_id, payload.url).as_dict()


@router.get("/runs/{job_id}/challenges")
def run_challenges(job_id: str, seeker: Seeker) -> list[dict]:
    """Challenge notifications raised by this session's runs (FR-203)."""
    _job_or_404(job_id, seeker.id)
    return repo.unread_notifications(seeker.id, "browser_challenge")


# ---------------------------------------------------------------------------
# Advisory data collected from Glassdoor (FR-264, FR-265)
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/glassdoor")
def glassdoor_snapshots(campaign_id: str, seeker: Seeker) -> dict:
    owned_or_404("campaign", campaign_id, seeker.id)
    return glassdoor.snapshots_for_campaign(campaign_id)


# ---------------------------------------------------------------------------
# Retention of browser-collected people (NFR-303)
# ---------------------------------------------------------------------------


@router.get("/retention")
def retention(admin: Admin) -> dict:
    return {
        "grace_days": repo.retention_grace_days(),
        "expired_now": len(repo.expired_browser_contacts()),
        "policy": (
            "People found through browser automation are kept for the life of the campaign "
            "that collected them plus the grace period, are never shared with another job "
            "seeker, and are deleted after that (NFR-303)."
        ),
    }


@router.put("/retention")
def set_retention(payload: RetentionIn, admin: Admin) -> dict:
    return {"grace_days": repo.set_retention_grace_days(payload.grace_days)}


@router.post("/retention/purge")
def purge_retention(admin: Admin) -> dict:
    return {"deleted": repo.purge_expired_browser_contacts()}


# ---------------------------------------------------------------------------
# ATS form pre-fill (FR-328)
# ---------------------------------------------------------------------------


@router.post("/ats/prefill")
async def prefill_ats_form(payload: PrefillIn, seeker: Seeker) -> dict:
    """Pre-fill the employer's form and pause for the user to submit (FR-328)."""
    values = dict(payload.values)
    attachments = dict(payload.attachments)
    if payload.application_package_id:
        package = owned_or_404(
            "application_package", payload.application_package_id, seeker.id
        )
        values.setdefault("cover_letter", package.get("email_body") or "")
        if package.get("cv_pdf_path"):
            attachments.setdefault("resume", package["cv_pdf_path"])
    values.setdefault("full_name", seeker.display_name)
    values.setdefault("email", seeker.email)
    # Only documents Dream Job itself produced or the job seeker uploaded may be
    # attached to a third party's form.
    data_dir = get_settings().abs_data_dir.resolve()
    for canonical, path in list(attachments.items()):
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(data_dir)
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Attachment for {canonical!r} must be a file under the Dream Job data directory",
            ) from exc
        attachments[canonical] = str(resolved)

    # The automation browser is signed in as the job seeker, so the prefill is
    # pinned to the employer's own site or a known ATS.  A pasted link to
    # anything else would have their name, email, letter and CV typed into it.
    opportunity = opp_repo.get_opportunity(payload.opportunity_id, seeker.id)
    allowed_hosts: list[str] = []
    company = (
        company_repo.get_company(opportunity["company_id"])
        if opportunity and opportunity.get("company_id")
        else None
    )
    if company:
        allowed_hosts.append(company.get("domain") or "")
        careers_host = urlsplit(company.get("careers_url") or "").hostname or ""
        allowed_hosts.append(careers_host)

    report = await ats_form.prefill(
        payload.url,
        {k: v for k, v in values.items() if v},
        attachments=attachments,
        job_seeker_id=seeker.id,
        opportunity_id=payload.opportunity_id,
        allowed_hosts=allowed_hosts,
    )
    if report.error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": "prefill_failed", "message": report.error,
             "instructions": session_mod.launch_instructions()},
        )
    return report.as_dict()


# ---------------------------------------------------------------------------
# Pacing, for the UI to show what "human pace" means here (FR-203, CR-406)
# ---------------------------------------------------------------------------


@router.get("/pacing")
def pacing_settings(seeker: Seeker) -> dict:
    pacing = pacing_mod.Pacing.from_settings()
    return {
        "min_delay_ms": pacing.min_delay_ms,
        "max_delay_ms": pacing.max_delay_ms,
        "scroll_steps": pacing.scroll_steps,
        "waits_per_target": pacing.waits_per_target,
        "mean_delay_seconds": pacing.mean_delay_seconds,
        "note": (
            "Browser automation is slow by design: seconds per page, one session, tens to "
            "low hundreds of targets per campaign (CR-406)."
        ),
    }
