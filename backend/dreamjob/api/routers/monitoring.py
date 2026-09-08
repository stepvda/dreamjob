"""Monitoring API: watchlist, notifications, digests, the scheduler.

Covers FR-401 (watched companies rechecked on a configurable interval, with
notifications and automatic addition to the ranked list), FR-402 (the timing
window that recheck refreshes) and FR-403 (the weekly digest, in-app and
optionally by e-mail), plus the control surface for the periodic loop that
runs all of it.

Watchlist entries, notifications and digests are private (FR-344), so every
route filters on ``seeker.id``.  The recheck itself writes into the *shared*
knowledge base - the vacancies and signals it finds belong to everyone - and
only the notification and the ranked-list entry are private.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.monitoring import digest as digest_mod
from dreamjob.monitoring import scheduler as scheduler_mod
from dreamjob.monitoring import watchlist as watchlist_mod
from dreamjob.pipeline import signals as signals_mod

log = logging.getLogger(__name__)

router = APIRouter()

Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]
Admin = Annotated[CurrentSeeker, Depends(current_admin)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class WatchIn(BaseModel):
    company_id: str
    interval_days: int = Field(
        default=watchlist_mod.DEFAULT_INTERVAL_DAYS,
        ge=watchlist_mod.MIN_INTERVAL_DAYS,
        le=watchlist_mod.MAX_INTERVAL_DAYS,
    )
    campaign_id: str | None = None
    channels: list[str] | None = None
    reason: str | None = Field(default=None, max_length=500)


class WatchUpdateIn(BaseModel):
    interval_days: int | None = Field(
        default=None,
        ge=watchlist_mod.MIN_INTERVAL_DAYS,
        le=watchlist_mod.MAX_INTERVAL_DAYS,
    )
    active: bool | None = None
    campaign_id: str | None = None
    channels: list[str] | None = None
    reason: str | None = Field(default=None, max_length=500)


class DigestIn(BaseModel):
    period_days: int = Field(default=digest_mod.DEFAULT_PERIOD_DAYS, ge=1, le=60)
    email: bool = False


class SchedulerTaskIn(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# FR-401: the watchlist
# ---------------------------------------------------------------------------


@router.get("/watchlist")
def list_watchlist(seeker: Seeker, active_only: bool = False) -> list[dict]:
    return watchlist_mod.entries(seeker.id, active_only=active_only)


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
def add_watch(payload: WatchIn, seeker: Seeker) -> dict:
    """Watch a company (FR-401).  A new watch is due on the next cycle."""
    try:
        return watchlist_mod.add(
            seeker.id,
            payload.company_id,
            interval_days=payload.interval_days,
            campaign_id=payload.campaign_id,
            channels=payload.channels,
            reason=payload.reason,
        )
    except watchlist_mod.WatchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/watchlist/channels")
def watch_channels(seeker: Seeker) -> dict:
    return {
        "channels": list(watchlist_mod.CHANNELS),
        "default": list(watchlist_mod.DEFAULT_CHANNELS),
        "default_interval_days": watchlist_mod.DEFAULT_INTERVAL_DAYS,
    }


@router.patch("/watchlist/{entry_id}")
def update_watch(entry_id: str, payload: WatchUpdateIn, seeker: Seeker) -> dict:
    try:
        return watchlist_mod.update(
            seeker.id, entry_id, payload.model_dump(exclude_unset=True)
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except watchlist_mod.WatchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.delete("/watchlist/{entry_id}")
def remove_watch(entry_id: str, seeker: Seeker) -> dict:
    if not watchlist_mod.remove(seeker.id, entry_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watchlist entry not found")
    return {"removed": True}


@router.post("/watchlist/{entry_id}/check")
async def check_watch(entry_id: str, seeker: Seeker) -> dict:
    """Recheck one watched company now, without waiting for its interval."""
    entry = repo.get_watch(entry_id, seeker.id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watchlist entry not found")
    report = await watchlist_mod.check_entry(entry)
    return report.as_dict()


@router.post("/watchlist/run")
async def run_watchlist(seeker: Seeker, limit: int = Query(50, ge=1, le=200)) -> dict:
    """Run every watch of this job seeker that is due (FR-401)."""
    return await watchlist_mod.run_cycle(seeker.id, limit=limit)


@router.get("/timing/{company_id}")
def timing(company_id: str, seeker: Seeker) -> dict:
    """The preferred application window for a company (FR-402).

    Shared knowledge-base reading: the signals belong to every job seeker, and
    the recommendation is derived from them rather than from anything private.
    """
    return signals_mod.timing_summary(company_id)


# ---------------------------------------------------------------------------
# FR-401/FR-403: notifications
# ---------------------------------------------------------------------------


@router.get("/notifications")
def list_notifications(
    seeker: Seeker,
    unread_only: bool = False,
    kind: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    return {
        "unread": repo.unread_count(seeker.id),
        "notifications": repo.list_notifications(
            seeker.id, unread_only=unread_only, kind=kind, limit=limit
        ),
    }


@router.post("/notifications/read-all")
def read_all(seeker: Seeker) -> dict:
    return {"marked": repo.mark_all_read(seeker.id)}


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str, seeker: Seeker) -> dict:
    if not repo.mark_notification(notification_id, seeker.id, "read_at"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    return {"read": True}


@router.post("/notifications/{notification_id}/dismiss")
def dismiss_notification(notification_id: str, seeker: Seeker) -> dict:
    if not repo.mark_notification(notification_id, seeker.id, "dismissed_at"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    return {"dismissed": True}


# ---------------------------------------------------------------------------
# FR-403: digests
# ---------------------------------------------------------------------------


@router.get("/digests")
def list_digests(seeker: Seeker, limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    return repo.list_digests(seeker.id, limit=limit)


@router.get("/digests/latest")
def latest_digest(seeker: Seeker) -> dict:
    latest = repo.latest_digest(seeker.id)
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no digest has been generated yet")
    return latest


@router.post("/digests", status_code=status.HTTP_201_CREATED)
def generate_digest(payload: DigestIn, seeker: Seeker) -> dict:
    """Generate this seeker's digest now (FR-403).  Re-running refreshes it."""
    return digest_mod.generate(
        seeker.id, period_days=payload.period_days, email=payload.email
    )


@router.get("/digests/preview")
def preview_digest(
    seeker: Seeker, period_days: int = Query(digest_mod.DEFAULT_PERIOD_DAYS, ge=1, le=60)
) -> dict:
    """What the digest would say, without storing it or notifying."""
    built = digest_mod.build(seeker.id, period_days=period_days)
    return {
        **built.as_dict(),
        "text": digest_mod.render_text(built, display_name=seeker.display_name),
    }


@router.get("/digests/{digest_id}")
def get_digest(digest_id: str, seeker: Seeker) -> dict:
    found = repo.get_digest(digest_id, seeker.id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "digest not found")
    return found


# ---------------------------------------------------------------------------
# The periodic loop
# ---------------------------------------------------------------------------


@router.get("/scheduler")
def scheduler_status(seeker: Seeker) -> dict[str, Any]:
    """What the loop is doing.  Readable by any seeker; only an admin may change it."""
    return scheduler_mod.scheduler.status()


@router.post("/scheduler/start")
def start_scheduler(admin: Admin) -> dict[str, Any]:
    """Start the in-process loop (FR-401, FR-403).

    Deployments that would rather not run it inside the API process leave this
    alone and drive ``python -m dreamjob.monitoring.scheduler --once`` from
    launchd or cron; the module docstring has both recipes.
    """
    return scheduler_mod.scheduler.start()


@router.post("/scheduler/stop")
async def stop_scheduler(admin: Admin) -> dict[str, Any]:
    return await scheduler_mod.scheduler.stop()


@router.post("/scheduler/tasks/{name}")
def toggle_task(name: str, payload: SchedulerTaskIn, admin: Admin) -> dict[str, Any]:
    try:
        return scheduler_mod.scheduler.set_enabled(name, payload.enabled)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/scheduler/tasks/{name}/run")
async def run_task(name: str, admin: Admin) -> dict[str, Any]:
    """Run one scheduled task immediately, whether or not it is due."""
    try:
        return await scheduler_mod.run_now(name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
