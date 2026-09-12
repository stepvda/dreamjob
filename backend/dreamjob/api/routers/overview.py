"""Journey state and topbar counters for the overview screen (FR-361, NFR-502).

``/journey`` returns where the job seeker stands in the ten-stage pipeline of
specification section 2.3, grouped into the five phases the interface shows:
profile, plan, discover, apply, follow up.

Each stage reports one of:

    done      it has produced what the next stage needs
    active    work is running right now
    ready     it can be started
    blocked   an earlier stage has to finish first, and which one
    pending   nothing yet

The point is that a stage is never just "not done": it says what would unblock
it, because the commonest way to get lost in a ten-stage pipeline is not
knowing what the next move is.

``/counters`` returns the four numbers the topbar always shows.  Definitions:

    companies      global knowledge base: every row of ``company`` (a
                   ``suppressed`` column did not exist when this was written;
                   if one arrives, suppressed rows are excluded).
    jobs           advertised vacancies in the shared corpus:
                   ``COUNT(*) FROM vacancy``.
    contacts       contacts visible to the signed-in seeker, under the
                   ``browse_contacts`` scope (``shareable = 1 OR
                   owning_campaign_id IS NULL OR campaign belongs to the
                   seeker``), usable addresses only.
    opportunities  the seeker's own opportunities
                   (``opportunities.count_opportunities``).

Only the topbar reads this; the journey map's own ``counts`` keep their
screen-local meanings.
"""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import learning as repo
from dreamjob.db.repositories import overview as counters_repo

router = APIRouter()

RUNNING_STATUSES = {"running", "planned"}

#: How long a topbar answer is reused.  Every open tab would otherwise issue
#: the counter query on every tick; the corpus grows over minutes, not
#: milliseconds, so a few seconds of staleness is invisible and keeps shell
#: polling off the database (NFR-502).
COUNTERS_TTL_SECONDS = 8

#: ``(job_seeker_id, bucket)`` -> the exact payload the last call returned.
#: The payload is stored rather than the numbers so a cache hit answers with
#: the same object - including the same ``at`` - instead of rebuilding one.
_counters_cache: dict[tuple[str, int], dict] = {}
_counters_lock = threading.Lock()


def counters_for_seeker(seeker_id: str, *, fresh: bool = False) -> dict:
    """The counter payload for one seeker, shared for ``COUNTERS_TTL_SECONDS``.

    The cache key is ``(seeker_id, bucket)`` where the bucket is
    ``time.monotonic() // COUNTERS_TTL_SECONDS``: two callers in the same window
    get the same payload, and a seeker never sees another's numbers.  ``fresh``
    (``?fresh=1``) skips the read and replaces the entry.  Writes drop every
    entry from an earlier bucket, so a long-running process holds at most one
    payload per observed seeker.
    """
    bucket = int(time.monotonic()) // COUNTERS_TTL_SECONDS
    key = (seeker_id, bucket)
    if not fresh:
        hit = _counters_cache.get(key)
        if hit is not None:
            return hit
    payload = {**counters_repo.counters(seeker_id), "at": utcnow()}
    with _counters_lock:
        for stale in [k for k in _counters_cache if k[1] != bucket]:
            del _counters_cache[stale]
        _counters_cache[key] = payload
    return payload


@router.get("/counters")
def counters(
    fresh: bool = False, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """The four always-visible counters: see the module docstring for what each
    one counts.  Cached for a few seconds; ``?fresh=1`` bypasses the cache."""
    return counters_for_seeker(seeker.id, fresh=fresh)


@router.get("/journey")
def journey(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    state = repo.journey_state(seeker.id)
    counts = repo.journey_counts(seeker.id)

    profile = state["profile"]
    composite = state["composite"]
    dream = state["dream_job"]
    directives = state["directives"]
    campaign = state["campaign"] or {}
    campaign_status = campaign.get("status")

    j: dict[str, dict] = {}

    def stage(key, *, done, active=False, blocked_by=None, count=None, detail=None):
        if done:
            s = "done"
        elif active:
            s = "active"
        elif blocked_by:
            s = "blocked"
        else:
            s = "ready"
        entry: dict = {"state": s}
        if blocked_by and not done:
            entry["blockedBy"] = blocked_by
        if count is not None:
            entry["count"] = count
        if detail:
            entry["detail"] = detail
        j[key] = entry

    # --- Profile -----------------------------------------------------------
    conflicts = state["unresolved_conflicts"]
    stage(
        "profile",
        done=bool(profile) and not conflicts,
        detail=(
            f"{conflicts} conflicts to resolve"
            if conflicts
            else (f"version {profile['version']}" if profile else None)
        ),
    )
    pending = state["pending_findings"]
    stage(
        "composite",
        done=bool(composite),
        blocked_by=None if profile else "a profile",
        detail=f"{pending} findings to confirm" if pending else None,
    )
    stage(
        "dream_job",
        done=bool(dream and dream["confirmed_by_user"]),
        blocked_by=None if profile else "a profile",
        detail=(
            "written, not yet confirmed"
            if dream and not dream["confirmed_by_user"]
            else None
        ),
    )

    # --- Plan --------------------------------------------------------------
    stage("directives", done=bool(directives), blocked_by=None if composite else "a composite profile")
    stage(
        "plan",
        done=bool(campaign_status and campaign_status != "draft"),
        blocked_by=None if directives else "directives",
        detail=campaign.get("name"),
    )
    stage(
        "collection",
        done=campaign_status == "completed",
        active=campaign_status == "running",
        blocked_by=None if campaign_status else "a campaign plan",
        detail=(
            f"{campaign.get('tokens_used', 0):,} of {campaign.get('token_budget', 0):,} tokens"
            if campaign_status in RUNNING_STATUSES
            else None
        ),
    )

    # --- Discover ----------------------------------------------------------
    stage("companies", done=counts["companies"] > 0, count=counts["companies"],
          blocked_by=None if campaign_status else "collection")
    stage(
        "opportunities",
        done=counts["opportunities"] > 0,
        count=counts["opportunities"],
        blocked_by=None if campaign_status else "collection",
        detail=(
            f"{counts['speculative']} speculative" if counts["speculative"] else None
        ),
    )
    # FR-262/FR-149: the spontaneous-application track is its own stage.  It was
    # only ever a detail line on "opportunities", which is why a search whose
    # whole point was unadvertised roles looked finished with none of them.
    stage(
        "speculative",
        done=counts["speculative"] > 0,
        count=counts["speculative"],
        blocked_by=None if counts["companies"] else "company profiles",
        detail=(
            None
            if counts["speculative"]
            else "run “Find unadvertised roles” on the opportunities screen"
        ),
    )
    stage("scoring", done=counts["scored"] > 0, count=counts["scored"],
          blocked_by=None if counts["opportunities"] else "opportunities")

    # --- Apply -------------------------------------------------------------
    stage("contacts", done=counts["contacts"] > 0, count=counts["contacts"],
          blocked_by=None if counts["scored"] else "a ranked list")
    stage(
        "documents",
        done=counts["packages"] > 0,
        count=counts["packages"],
        blocked_by=None if counts["scored"] else "a ranked list",
        detail=f"{counts['approved']} approved" if counts["packages"] else None,
    )
    stage("dispatch", done=counts["sent"] > 0, count=counts["sent"],
          blocked_by=None if counts["approved"] else "an approved application")

    # --- Follow up ---------------------------------------------------------
    stage(
        "responses",
        done=counts["replies"] > 0,
        count=counts["replies"],
        blocked_by=None if counts["sent"] else "a sent application",
        detail="record replies as they arrive" if counts["sent"] and not counts["replies"] else None,
    )
    stage("pipeline", done=counts["cards"] > 0, count=counts["cards"],
          blocked_by=None if counts["sent"] else "a sent application",
          detail=f"{counts['interviews']} at interview or beyond" if counts["interviews"] else None)

    # Learning needs a handful of resolved applications before it says anything.
    enough = counts["sent"] >= 6
    stage(
        "learning",
        done=counts["open_advice"] > 0,
        count=counts["open_advice"] or None,
        blocked_by=None if enough else "about six sent applications",
        detail=None if enough else f"{counts['sent']} of ~6 applications sent",
    )

    return {
        "journey": j,
        "counts": counts,
        "campaign": campaign or None,
        "discretion_mode": bool(directives and directives.get("discretion_mode")),
        "next_action": _next_action(j),
    }


def _next_action(j: dict[str, dict]) -> dict | None:
    """The first stage that can actually be moved forward."""
    order = [
        ("profile", "Import your LinkedIn export and CV", "/profile"),
        ("composite", "Build your composite profile", "/composite"),
        ("dream_job", "Describe and confirm your dream job", "/dream-job"),
        ("directives", "Set your search directives", "/directives"),
        ("plan", "Generate a campaign plan", "/campaigns"),
        ("collection", "Launch collection", "/campaigns"),
        ("speculative", "Find roles nobody has advertised", "/opportunities?kind=speculative"),
        ("scoring", "Review the ranked opportunities", "/opportunities"),
        ("contacts", "Find hiring contacts", "/contacts"),
        ("documents", "Generate application packages", "/applications"),
        ("dispatch", "Approve and send", "/applications"),
        ("responses", "Record the responses you receive", "/pipeline"),
        ("learning", "See what is working and where to redirect", "/pipeline"),
    ]
    for key, label, to in order:
        entry = j.get(key, {})
        if entry.get("state") in ("ready", "active"):
            return {"stage": key, "label": label, "to": to}
    return None
