"""SQL for browser-driven collection (FR-203, FR-207, NFR-203, NFR-303).

Everything the browser slice persists passes through this module:

* **captures** - the DOM text and screenshots a paced run takes are stored as
  ``raw_document`` rows with ``access_method = 'browser'`` (FR-207), after the
  caller has sanitised them.  Nothing else about the live session is written:
  no cookie, no session token, no credential ever reaches this file (NFR-203).
* **contacts** - people found through browser automation are campaign-scoped:
  ``shareable = 0``, an owning campaign and a ``retention_until`` date, and a
  purge that honours it (NFR-303).
* **pacing samples** - measured page-load times per site, kept as an
  exponentially weighted average so the duration estimate is grounded in what
  this machine and this connection actually do (FR-204).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import (
    execute,
    from_json,
    insert_row,
    query_all,
    query_one,
    to_json,
    update_row,
    utcnow,
)
from dreamjob.db.repositories import knowledge as kb

log = logging.getLogger(__name__)

# FR-207: the tag that makes the retention and sharing rules of section 4.3
# applicable to everything this slice collects.
ACCESS_METHOD = "browser"

RETENTION_SETTING = "browser.contact_retention_grace_days"
DEFAULT_RETENTION_GRACE_DAYS = 30

PAGE_LOAD_SETTING = "browser.page_load_seconds"
DEFAULT_PAGE_LOAD_SECONDS = 3.0
_EWMA_ALPHA = 0.3

_EXTENSIONS = {"text/html": ".html", "text/plain": ".txt", "image/png": ".png"}


# ---------------------------------------------------------------------------
# Captures (FR-203, FR-207)
# ---------------------------------------------------------------------------


def _store(url: str, payload: bytes, content_type: str, status: int | None) -> str:
    """Write one capture to the raw store and register it (DR-102, FR-207)."""
    content_hash = hashlib.sha256(payload).hexdigest()
    existing = query_one("SELECT id FROM raw_document WHERE content_hash = ?", (content_hash,))
    if existing:
        return existing["id"]

    settings = get_settings()
    shard = settings.raw_dir / "browser" / content_hash[:2]
    shard.mkdir(parents=True, exist_ok=True)
    path = shard / f"{content_hash}{_EXTENSIONS.get(content_type, '.bin')}"
    path.write_bytes(payload)
    return insert_row(
        "raw_document",
        {
            "url": url,
            "content_type": content_type,
            "content_hash": content_hash,
            "storage_path": str(path.relative_to(settings.abs_data_dir)),
            "byte_size": len(payload),
            "http_status": status,
            "access_method": ACCESS_METHOD,
            "fetched_at": utcnow(),
        },
    )


def store_capture(url: str, html: str, *, status: int | None = None) -> str | None:
    """Store sanitised page content.  Callers must sanitise first (NFR-203)."""
    if not html:
        return None
    try:
        return _store(url, html.encode("utf-8", errors="replace"), "text/html", status)
    except Exception:  # noqa: BLE001 - a lost capture must not stop a run
        log.exception("Could not store the capture of %s", url)
        return None


def store_screenshot(url: str, png: bytes) -> str | None:
    """Store one screenshot of what the user could see in their own browser (FR-203)."""
    if not png:
        return None
    try:
        return _store(url, png, "image/png", None)
    except Exception:  # noqa: BLE001
        log.exception("Could not store the screenshot of %s", url)
        return None


def capture_path(raw_document_id: str) -> str | None:
    row = query_one("SELECT storage_path FROM raw_document WHERE id = ?", (raw_document_id,))
    return row["storage_path"] if row else None


# ---------------------------------------------------------------------------
# Contacts collected through the browser (NFR-303, NFR-302)
# ---------------------------------------------------------------------------


def retention_grace_days() -> int:
    """Configurable grace period on top of the campaign's own life (NFR-303)."""
    try:
        return max(0, int(kb.get_setting(RETENTION_SETTING, DEFAULT_RETENTION_GRACE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_GRACE_DAYS


def set_retention_grace_days(days: int) -> int:
    days = max(0, int(days))
    kb.set_setting(RETENTION_SETTING, days)
    return days


def retention_until(campaign_id: str | None, *, grace_days: int | None = None) -> str:
    """When a browser-collected person record must be gone (NFR-303).

    The campaign's end plus the grace period; a campaign that has not finished
    yet is measured from now, and the date moves forward on re-collection.
    """
    grace = retention_grace_days() if grace_days is None else max(0, int(grace_days))
    base = datetime.now(UTC)
    if campaign_id:
        row = query_one("SELECT finished_at FROM campaign WHERE id = ?", (campaign_id,))
        finished = (row or {}).get("finished_at")
        if finished:
            try:
                base = max(base, datetime.fromisoformat(finished))
            except ValueError:
                log.warning("Campaign %s has an unparseable finished_at", campaign_id)
    return (base + timedelta(days=grace)).isoformat(timespec="seconds")


def contact_by_linkedin_url(url: str, campaign_id: str | None = None) -> dict | None:
    """The contact with this profile URL, scoped to its owning campaign.

    Scoping matters: the same person surfaces in more than one campaign, and an
    unscoped lookup let the second campaign take ownership of the first's row -
    moving ``owning_campaign_id``, ``shareable`` and the retention deadline.
    """
    if campaign_id:
        return query_one(
            "SELECT * FROM contact WHERE linkedin_url = ? AND owning_campaign_id = ? LIMIT 1",
            (url, campaign_id),
        )
    return query_one("SELECT * FROM contact WHERE linkedin_url = ? LIMIT 1", (url,))


#: Fields that belong to the campaign that collected the row, never to a later
#: pass that merely saw the same person again.
_CONTACT_OWNERSHIP_FIELDS = ("owning_campaign_id", "shareable", "retention_until")


def upsert_browser_contact(data: dict) -> tuple[str | None, bool]:
    """Insert or refresh one person found in the browser.  Returns ``(id, created)``.

    Identity is the LinkedIn profile URL, which is stable where an e-mail
    address is not yet known.  An objection blocks the record permanently
    (NFR-302), and every row written here stays campaign-scoped (NFR-303): a
    refresh updates what was observed and never re-parents the row.
    """
    data = dict(data)
    data["access_method"] = ACCESS_METHOD
    url = data.get("linkedin_url")
    existing = contact_by_linkedin_url(url, data.get("owning_campaign_id")) if url else None
    if existing and existing.get("objected"):
        log.info("Skipping %s: an objection is on file (NFR-302)", url)
        return existing["id"], False
    if existing:
        merged = {k: v for k, v in data.items() if v not in (None, "", [], {})}
        for field_name in _CONTACT_OWNERSHIP_FIELDS:
            merged.pop(field_name, None)
        merged["collected_at"] = utcnow()
        kb.update_shared("contact", existing["id"], merged)
        return existing["id"], False
    data.setdefault("collected_at", utcnow())
    return kb.insert_shared("contact", data), True


def campaign_contacts(campaign_id: str) -> list[dict]:
    return query_all(
        "SELECT * FROM contact WHERE owning_campaign_id = ? AND access_method = ? "
        "ORDER BY collected_at DESC",
        (campaign_id, ACCESS_METHOD),
    )


def expired_browser_contacts(now: str | None = None) -> list[dict]:
    return query_all(
        "SELECT id, full_name, owning_campaign_id, retention_until FROM contact "
        "WHERE access_method = ? AND retention_until IS NOT NULL AND retention_until <= ?",
        (ACCESS_METHOD, now or utcnow()),
    )


def purge_expired_browser_contacts(now: str | None = None) -> int:
    """Delete browser-collected people past their retention date (NFR-303)."""
    return execute(
        "DELETE FROM contact WHERE access_method = ? AND retention_until IS NOT NULL "
        "AND retention_until <= ?",
        (ACCESS_METHOD, now or utcnow()),
    )


# ---------------------------------------------------------------------------
# Measured pacing samples (FR-204)
# ---------------------------------------------------------------------------


def page_load_seconds(site: str) -> float:
    value = kb.get_setting(f"{PAGE_LOAD_SETTING}.{site}", DEFAULT_PAGE_LOAD_SECONDS)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_LOAD_SECONDS
    return seconds if 0.1 <= seconds <= 120 else DEFAULT_PAGE_LOAD_SECONDS


def record_page_load(site: str, seconds: float) -> float:
    """Fold one measured load into the running average used by the estimator."""
    if seconds <= 0 or seconds > 120:
        return page_load_seconds(site)
    blended = round(_EWMA_ALPHA * seconds + (1 - _EWMA_ALPHA) * page_load_seconds(site), 3)
    kb.set_setting(f"{PAGE_LOAD_SETTING}.{site}", blended)
    return blended


# ---------------------------------------------------------------------------
# The run's own record (FR-204, FR-206, NFR-502)
# ---------------------------------------------------------------------------


def save_job_state(job_id: str, state: dict[str, Any]) -> None:
    update_row("job_run", job_id, {"checkpoint": to_json(state)})


def job_state(job_id: str) -> dict[str, Any]:
    row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (job_id,))
    return from_json((row or {}).get("checkpoint"), {}) or {}


def get_job(job_id: str, job_seeker_id: str | None = None) -> dict | None:
    if job_seeker_id:
        return query_one(
            "SELECT * FROM job_run WHERE id = ? AND job_seeker_id = ?", (job_id, job_seeker_id)
        )
    return query_one("SELECT * FROM job_run WHERE id = ?", (job_id,))


def latest_browser_job(
    campaign_id: str, kind: str = "browser", adapter_key: str | None = None
) -> dict | None:
    """The campaign's most recent browser run, optionally for one site only.

    ``adapter_key`` matters: LinkedIn and Glassdoor share the ``browser`` job
    kind, so a reader after Glassdoor snapshots must not be handed the
    LinkedIn run that happened to finish last.
    """
    if adapter_key:
        return query_one(
            "SELECT * FROM job_run WHERE campaign_id = ? AND kind = ? AND adapter_key = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (campaign_id, kind, adapter_key),
        )
    return query_one(
        "SELECT * FROM job_run WHERE campaign_id = ? AND kind = ? ORDER BY created_at DESC LIMIT 1",
        (campaign_id, kind),
    )


def browser_jobs(job_seeker_id: str, kind: str = "browser", limit: int = 20) -> list[dict]:
    return query_all(
        "SELECT * FROM job_run WHERE job_seeker_id = ? AND kind = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (job_seeker_id, kind, limit),
    )


def set_job_error(job_id: str, message: str) -> None:
    update_row("job_run", job_id, {"last_error": message[:2000]})


def notify(
    job_seeker_id: str,
    kind: str,
    title: str,
    body: str = "",
    payload: dict | None = None,
) -> str | None:
    """Tell the user something happened - a challenge page, above all (FR-203)."""
    try:
        return insert_row(
            "notification",
            {
                "job_seeker_id": job_seeker_id,
                "kind": kind,
                "title": title[:300],
                "body": body[:2000],
                "payload": payload or {},
                "created_at": utcnow(),
            },
        )
    except Exception:  # noqa: BLE001 - notifying must not break the run it reports on
        log.exception("Could not record notification %s for %s", kind, job_seeker_id)
        return None


def unread_notifications(job_seeker_id: str, kind: str | None = None) -> list[dict]:
    if kind:
        return query_all(
            "SELECT * FROM notification WHERE job_seeker_id = ? AND kind = ? AND read_at IS NULL "
            "ORDER BY created_at DESC",
            (job_seeker_id, kind),
        )
    return query_all(
        "SELECT * FROM notification WHERE job_seeker_id = ? AND read_at IS NULL "
        "ORDER BY created_at DESC",
        (job_seeker_id,),
    )
