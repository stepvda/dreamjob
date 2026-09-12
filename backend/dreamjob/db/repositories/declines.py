"""Durable source declines: which adapters are unusable, and why (FR-182, IR-101).

One row per adapter in ``adapter_decline`` (migration 154).  A row is an
*observation* until the pattern of refusals is judged persistent - the
thresholds live in :mod:`dreamjob.pipeline.declines`, because they are a
product decision and not SQL - and becomes a *decline* when ``declined_at`` is
stamped.  Only a decline excludes the adapter from planning and skips it in
collection.

The lifecycle, in SQL terms:

* :func:`record_observation` accumulates the counters; it stamps
  ``declined_at`` when the caller says the threshold was crossed;
* :func:`active_declines` is what the planner and the collection worker read:
  ``declined_at IS NOT NULL AND acknowledged_at IS NULL``;
* :func:`acknowledge_decline` is the administrator's explicit act: it stamps
  who acknowledged it and when, clears ``declined_at``, and resets the
  counters so a source that is refused again re-earns its decline.  The
  counter reset is deliberate - inheriting the old totals would re-decline a
  source on its first refusal after a licence was bought.
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import query_all, query_one, upsert_row, utcnow


def get_decline(adapter_key: str) -> dict | None:
    return query_one("SELECT * FROM adapter_decline WHERE adapter_key = ?", (adapter_key,))


def list_declines(*, active_only: bool = False) -> list[dict]:
    """Every decline row, newest first; ``active_only`` drops cleared ones."""
    sql = "SELECT * FROM adapter_decline"
    if active_only:
        sql += " WHERE declined_at IS NOT NULL AND acknowledged_at IS NULL"
    sql += " ORDER BY COALESCE(declined_at, last_seen_at) DESC, adapter_key"
    return query_all(sql)


def active_declines() -> dict[str, dict]:
    """The declined adapters, keyed for planning and collection to skip."""
    return {row["adapter_key"]: row for row in list_declines(active_only=True)}


def get_active_decline(adapter_key: str) -> dict | None:
    row = query_one(
        "SELECT * FROM adapter_decline WHERE adapter_key = ? "
        "AND declined_at IS NOT NULL AND acknowledged_at IS NULL",
        (adapter_key,),
    )
    return row


def record_observation(
    adapter_key: str,
    *,
    reason: str,
    detail: str | None,
    evidence_url: str | None,
    requests: int,
    refusals: int,
    campaign_id: str | None,
    declined: bool,
) -> dict:
    """Accumulate one run's evidence for an adapter, declining it if told to.

    ``declined`` is the policy's answer (``pipeline.declines``): this
    observation either crosses the threshold or does not.  The row's own
    counters are updated either way, because a source that is refused once a
    campaign is what the next campaign's threshold reads.
    """
    now = utcnow()
    existing = get_decline(adapter_key)
    if existing is None:
        payload: dict[str, Any] = {
            "adapter_key": adapter_key,
            "reason": reason,
            "detail": detail,
            "evidence_url": evidence_url,
            "request_count": max(0, int(requests)),
            "refused_count": max(0, int(refusals)),
            "run_count": 1,
            "last_campaign_id": campaign_id,
            "first_seen_at": now,
            "last_seen_at": now,
            "declined_at": now if declined else None,
            "source": "auto",
            "acknowledged_at": None,
            "acknowledged_by": None,
            "acknowledged_note": None,
            "updated_at": now,
        }
        upsert_row("adapter_decline", payload, ["adapter_key"])
        return get_decline(adapter_key) or payload

    new_run = bool(campaign_id) and (existing.get("last_campaign_id") or "") != campaign_id
    payload = {
        "adapter_key": adapter_key,
        "reason": reason or existing.get("reason"),
        "detail": detail or existing.get("detail"),
        "evidence_url": evidence_url or existing.get("evidence_url"),
        "request_count": int(existing.get("request_count") or 0) + max(0, int(requests)),
        "refused_count": int(existing.get("refused_count") or 0) + max(0, int(refusals)),
        "run_count": (
            int(existing.get("run_count") or 0) + 1
            if new_run
            else int(existing.get("run_count") or 0)
        ),
        "last_campaign_id": campaign_id if new_run else existing.get("last_campaign_id"),
        "first_seen_at": existing.get("first_seen_at") or now,
        "last_seen_at": now,
        "declined_at": existing.get("declined_at"),
        "source": existing.get("source") or "auto",
        "acknowledged_at": existing.get("acknowledged_at"),
        "acknowledged_by": existing.get("acknowledged_by"),
        "acknowledged_note": existing.get("acknowledged_note"),
        "updated_at": now,
    }
    if declined and not existing.get("declined_at"):
        # A new decline, or a re-decline after an acknowledgement cleared it.
        payload["declined_at"] = now
        payload["acknowledged_at"] = None
        payload["acknowledged_by"] = None
        payload["acknowledged_note"] = None
        payload["source"] = "auto"
    upsert_row("adapter_decline", payload, ["adapter_key"])
    return get_decline(adapter_key) or {}


def acknowledge_decline(
    adapter_key: str, *, actor: str | None = None, note: str | None = None
) -> dict | None:
    """Clear a decline and reset its evidence counters (FR-363, NFR-702).

    Returns the updated row, or ``None`` when the adapter has no row at all.
    The counters reset so that re-enabling is a real second chance: the
    refusals that justified the first decline are spent, and only new evidence
    can decline the adapter again.
    """
    existing = get_decline(adapter_key)
    if existing is None:
        return None
    now = utcnow()
    upsert_row(
        "adapter_decline",
        {
            **existing,
            "declined_at": None,
            "acknowledged_at": now,
            "acknowledged_by": actor,
            "acknowledged_note": note,
            "request_count": 0,
            "refused_count": 0,
            "run_count": 0,
            "last_campaign_id": None,
            "last_seen_at": now,
            "updated_at": now,
        },
        ["adapter_key"],
    )
    return get_decline(adapter_key)
