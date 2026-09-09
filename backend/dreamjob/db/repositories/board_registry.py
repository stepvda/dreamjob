"""The ATS board registry, as rows rather than as a file (FR-181, FR-342, DR-101).

``pipeline/data/board_registry.json`` is an operator-built artefact: one row per
public ATS board, refreshed monthly, committed, and read by discovery when a
campaign is planned.  A file is the right shape for *shipping* that list - a
fresh checkout has targets before anything has run - and the wrong shape for
everything that happens to a board afterwards.

Three things are per-installation facts, not repository content:

* **liveness** - whether this installation's own request to that board was
  answered, and when.  The file ships ``last_verified: null`` on every row by
  design, and the nightly re-verification has to write its answer somewhere.
* **the company it turned out to be** - a board is a route to an employer, and
  which employer is only known once it has been read (DR-101 keys on
  ``(ats_vendor, ats_slug)``).
* **boards this installation found itself** - a seeded employer whose careers
  page resolved to a board, or a company a job seeker named.  Those belong to
  the installation and must survive the next time the shipped file is replaced.

So the file is a *seed* and the table is the live register.  ``sync_from_file``
imports the former into the latter without ever overwriting the latter's own
columns, which is what lets an operator drop in a new file - or the seed
resolver add to it - without losing a single verification this installation
paid a request for.

The register's whole job is to *learn* (migration 131).  A harvested registry is
mostly right and reliably wrong at the edges: the design measured 87.5% liveness
for a slug seen in a fresh Common Crawl and 27.5% for one seen only in the
Wayback index, and this installation measured 85.4% across the 2,227 boards it
has actually read.  The 14% that answer 404 are not failures - a company that
closed its board is a fact about the world - but they are a fact the registry
has to record once, or the same requests are spent on them every campaign for
ever.  ``mark_gone`` is where that is written down and :func:`boards` is where
it is spent.

Three states, and the distinction between them is the point:

``unverified``
    Nobody has asked.  4,515 of this installation's 4,518 rows are here, and
    this is *not* evidence of death: an unverified board is planned.
``live``
    A request was answered.
``gone``
    The board answered 404/410 :data:`RETIRE_AFTER_FAILURES` times running.
    Planning stops offering it - until :data:`REVISIT_AFTER_DAYS` have passed,
    because boards come back.

A 5xx, a timeout or a parse crash is none of these.  It is a *failed* fetch, and
:func:`mark_unreachable` records it without touching the state, because "the
server broke" is not evidence about whether the board exists.  Conflating the
two is how a bad afternoon at one ATS vendor deletes 800 live employers from the
inventory.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import new_id, query_all, query_one, utcnow, write_tx

log = logging.getLogger(__name__)

#: The state a row can hold.  ``unverified`` is the shipped state and is not the
#: same as ``gone``: nothing has asked yet.  The words are the ones a plan item
#: uses for its own outcome, deliberately - one fact should not have two names.
STATES = ("unverified", "live", "gone")

#: Two consecutive 404/410s retire a board.  One is as likely to be a tenant
#: renaming its board mid-migration as a tenant that has left, and retiring on a
#: single response deletes a live employer from the inventory that nothing will
#: look for again.  Two costs one extra request per dead board, once.
#:
#: Migration 131 repeats this number in SQL (a migration cannot import Python);
#: ``tests/unit/test_registry_liveness.py`` asserts the two agree.
RETIRE_AFTER_FAILURES = 2

#: A retired board is re-tested rarely, not never.  Boards come back: a company
#: re-opens hiring, or moves from one ATS to another and back.  Ninety days is
#: the registry's own staleness class rather than the 30-day liveness refresh
#: (FR-343, C8) because the two questions differ - "has this live board changed"
#: is asked monthly, "has this dead board come back" is asked quarterly, and at
#: 0.5 req/s the second question is only affordable at that rate.  For this
#: installation it is one extra request per retired board per quarter.
REVISIT_AFTER_DAYS = 90

#: The only statuses that are evidence a board is *gone* rather than *broken*.
GONE_STATUSES = (404, 410)


def revisit_cutoff(now: datetime | None = None) -> str:
    """The instant before which a retired board is worth one more request."""
    moment = now or datetime.now(UTC)
    return (moment - timedelta(days=REVISIT_AFTER_DAYS)).isoformat(timespec="seconds")


def count(state: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM board_registry"
    params: tuple = ()
    if state:
        sql += " WHERE state = ?"
        params = (state,)
    row = query_one(sql, params)
    return int(row["n"]) if row else 0


def counts_by_vendor() -> dict[str, int]:
    rows = query_all(
        "SELECT vendor, COUNT(*) AS n FROM board_registry GROUP BY vendor ORDER BY n DESC"
    )
    return {str(r["vendor"]): int(r["n"]) for r in rows}


def summary(now: datetime | None = None) -> dict[str, int]:
    """What the register knows, in the shape an operator wants to read.

    ``retired`` is the number of requests this registry no longer spends per
    campaign; ``due_for_revisit`` is the small number it will spend once, this
    quarter, to find out whether any of them came back.
    """
    rows = query_all("SELECT state, COUNT(*) AS n FROM board_registry GROUP BY state")
    by_state = {str(r["state"]): int(r["n"]) for r in rows}
    due = query_one(
        "SELECT COUNT(*) AS n FROM board_registry "
        "WHERE state = 'gone' AND (last_verified IS NULL OR last_verified < ?)",
        (revisit_cutoff(now),),
    )
    carrying = query_one(
        "SELECT COUNT(*) AS n FROM board_registry "
        "WHERE state <> 'gone' AND consecutive_failures > 0"
    )
    return {
        "total": sum(by_state.values()),
        "live": by_state.get("live", 0),
        "unverified": by_state.get("unverified", 0),
        "gone": by_state.get("gone", 0),
        "due_for_revisit": int(due["n"]) if due else 0,
        # Boards one more 404 away from retirement.  Worth reading: it is the
        # size of next campaign's dead-board bill.
        "failing": int(carrying["n"]) if carrying else 0,
    }


def get(vendor: str, slug: str) -> dict | None:
    return query_one(
        "SELECT * FROM board_registry WHERE vendor = ? AND slug = ?",
        (vendor.lower(), slug),
    )


def state_of(vendor: str, slug: str) -> str:
    """The registry's opinion of one board.  An unknown board is ``unverified``."""
    row = get(vendor, slug)
    return str(row["state"]) if row else "unverified"


def boards(
    vendor: str | None = None,
    *,
    include_gone: bool = False,
    limit: int = 5_000,
    now: datetime | None = None,
) -> list[dict]:
    """Boards worth planning, best-evidenced first (FR-186).

    A retired board is left out because a board that answered 404 twice running
    is a request this campaign should not spend again.  An ``unverified`` board
    is kept, because never having asked is not evidence of anything.

    A board retired more than :data:`REVISIT_AFTER_DAYS` ago comes back into the
    list, once: that is the whole re-test mechanism, and it rides along on the
    next campaign rather than needing a job of its own.  It sorts last, so a cap
    that bites spends its requests on boards that have answered.
    """
    sql = "SELECT * FROM board_registry"
    where: list[str] = []
    params: list[Any] = []
    if vendor:
        where.append("vendor = ?")
        params.append(vendor.lower())
    if not include_gone:
        where.append("(state <> 'gone' OR last_verified IS NULL OR last_verified < ?)")
        params.append(revisit_cutoff(now))
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Verified-live first, then never-asked, then the retired boards that are
    # due one more look, then by how recently anything was learned at all.
    sql += (
        " ORDER BY CASE state WHEN 'live' THEN 0 WHEN 'unverified' THEN 1 ELSE 2 END,"
        " COALESCE(last_verified, first_seen) DESC LIMIT ?"
    )
    params.append(int(limit))
    return query_all(sql, tuple(params))


def live_slugs(vendor: str, *, limit: int = 5_000, now: datetime | None = None) -> list[str]:
    """The slugs of one vendor that planning may still offer (FR-186).

    This is the function the discovery stage reads: it is the whole of the
    registry's contribution to "which boards does this campaign fetch", and it
    is a list of strings because that is what a plan item carries.

    "Live" here means *not known to be gone* - verified-live and never-asked
    both qualify, and a board retired longer ago than
    :data:`REVISIT_AFTER_DAYS` qualifies again.  Reading it as "verified live
    only" would drop 4,515 of this installation's 4,518 boards.
    """
    return [str(b["slug"]) for b in boards(vendor, limit=limit, now=now)]


def due_for_revisit(limit: int = 500, now: datetime | None = None) -> list[dict]:
    """Retired boards old enough to be worth one more request (FR-343)."""
    return query_all(
        "SELECT * FROM board_registry "
        "WHERE state = 'gone' AND (last_verified IS NULL OR last_verified < ?) "
        "ORDER BY COALESCE(last_verified, first_seen) ASC LIMIT ?",
        (revisit_cutoff(now), int(limit)),
    )


def retired_keys(now: datetime | None = None) -> set[tuple[str, str]]:
    """Every ``(vendor, slug)`` planning must not offer, as one set.

    One query, read once per plan.  The per-board question - "is this one gone?"
    - has the same answer 4,511 times, and asking it per row is 4,511 round
    trips against the single writer's connection (CR-408) inside the stage that
    is supposed to be the cheap one.

    A board retired longer ago than :data:`REVISIT_AFTER_DAYS` is deliberately
    absent from the set: it has served its retirement and is worth one more
    request, because boards come back.
    """
    rows = query_all(
        "SELECT vendor, slug FROM board_registry "
        "WHERE state = 'gone' AND last_verified IS NOT NULL AND last_verified >= ?",
        (revisit_cutoff(now),),
    )
    return {(str(r["vendor"]).lower(), str(r["slug"]).lower()) for r in rows}


def stale(cutoff: str, limit: int = 500) -> list[dict]:
    """Boards whose liveness has not been checked since ``cutoff`` (FR-343)."""
    return query_all(
        "SELECT * FROM board_registry "
        "WHERE state <> 'gone' AND (last_verified IS NULL OR last_verified < ?) "
        "ORDER BY COALESCE(last_verified, first_seen) ASC LIMIT ?",
        (cutoff, int(limit)),
    )


def upsert(row: dict) -> str:
    """Add or update one board, keeping every column this installation owns.

    A caller supplying only ``vendor``/``slug``/``source`` is describing where a
    board came from, not what happened to it, so state, verification and the
    resolved company are left exactly as they were.
    """
    vendor = str(row.get("vendor") or row.get("ats_vendor") or "").strip().lower()
    slug = str(row.get("slug") or row.get("ats_slug") or "").strip().strip("/")
    if not vendor or not slug:
        raise ValueError("a board registry row needs both a vendor and a slug")

    now = utcnow()
    existing = get(vendor, slug)
    values: dict[str, Any] = {
        "vendor": vendor,
        "slug": slug,
        "updated_at": now,
    }
    for key in ("name", "source", "state", "last_verified", "last_status",
                "job_count", "consecutive_failures", "company_id"):
        if row.get(key) is not None:
            values[key] = row[key]

    if existing is None:
        values.setdefault("name", None)
        values.setdefault("source", "manual")
        values.setdefault("state", "unverified")
        values.setdefault("consecutive_failures", 0)
        values["id"] = new_id()
        values["first_seen"] = row.get("first_seen") or now
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with write_tx() as conn:
            conn.execute(
                f"INSERT INTO board_registry ({columns}) VALUES ({marks})",
                tuple(values.values()),
            )
        return str(values["id"])

    assignments = ", ".join(f"{k} = ?" for k in values)
    with write_tx() as conn:
        conn.execute(
            f"UPDATE board_registry SET {assignments} WHERE vendor = ? AND slug = ?",
            (*values.values(), vendor, slug),
        )
    return str(existing["id"])


# ---------------------------------------------------------------------------
# What a request to a board taught us
# ---------------------------------------------------------------------------


def mark_live(
    vendor: str,
    slug: str,
    *,
    http_status: int = 200,
    job_count: int | None = None,
    company_id: str | None = None,
) -> str:
    """The board answered.  Clears the failure count, however it stood."""
    upsert(
        {
            "vendor": vendor,
            "slug": slug,
            "state": "live",
            "last_verified": utcnow(),
            "last_status": http_status,
            "job_count": job_count,
            "consecutive_failures": 0,
            "company_id": company_id,
        }
    )
    return "live"


def mark_gone(vendor: str, slug: str, http_status: int = 404) -> str:
    """The board answered 404/410.  Returns the state that leaves it in.

    Called by the collection slice when an ATS board is not there.  The first
    such response is recorded but does not retire the slug - one 404 is as
    likely to be a rename in flight as a departure - so the state stays
    ``unverified`` and the board is planned once more.  The second retires it,
    and :func:`boards` stops offering it for :data:`REVISIT_AFTER_DAYS`.

    A status outside :data:`GONE_STATUSES` cannot retire a board however often
    it repeats: a 500 is a broken server, not a missing tenant.  It is recorded
    and passed to :func:`mark_unreachable` instead, so a mis-routed call
    degrades to the honest answer rather than to a silent retirement.
    """
    status = int(http_status)
    if status not in GONE_STATUSES:
        log.warning(
            "mark_gone(%s/%s) called with HTTP %s; only %s are evidence a board is gone",
            vendor, slug, status, "/".join(str(s) for s in GONE_STATUSES),
        )
        return mark_unreachable(vendor, slug, http_status=status)

    existing = get(vendor, slug) or {}
    failures = int(existing.get("consecutive_failures") or 0) + 1
    state = "gone" if failures >= RETIRE_AFTER_FAILURES else "unverified"
    upsert(
        {
            "vendor": vendor,
            "slug": slug,
            "state": state,
            "last_verified": utcnow(),
            "last_status": status,
            "consecutive_failures": failures,
        }
    )
    if state == "gone":
        log.info(
            "Board registry: retiring %s/%s after %d consecutive HTTP %s responses",
            vendor, slug, failures, status,
        )
    return state


def mark_unreachable(vendor: str, slug: str, *, http_status: int | None = None) -> str:
    """The request failed, and taught us nothing about whether the board exists.

    A 5xx, a transport error or a parse crash is the ``failed`` outcome, and it
    is the one an operator must see - but it is not evidence about the board.
    The status is recorded so the failure is visible in the register; the state
    and the retirement counter are left exactly as they were, so an ATS vendor
    having a bad afternoon cannot retire every board it serves.
    """
    existing = get(vendor, slug)
    if existing is None:
        return "unverified"
    upsert(
        {
            "vendor": vendor,
            "slug": slug,
            "last_status": http_status,
            # Deliberately not last_verified: nothing was verified.
        }
    )
    return str(existing["state"])


def record_verification(
    vendor: str,
    slug: str,
    *,
    http_status: int | None,
    job_count: int | None = None,
    company_id: str | None = None,
) -> str:
    """Write down what this installation's own request to the board answered.

    The single entry point for a caller that has a status code and does not want
    to classify it: 2xx is live, 404/410 is evidence of a gone board, and
    everything else - including no status at all, which is a transport error -
    is an unreachable board whose state is left alone.
    """
    if http_status is not None and 200 <= int(http_status) < 300:
        return mark_live(
            vendor, slug, http_status=int(http_status), job_count=job_count,
            company_id=company_id,
        )
    if http_status is not None and int(http_status) in GONE_STATUSES:
        return mark_gone(vendor, slug, int(http_status))
    return mark_unreachable(vendor, slug, http_status=http_status)


def sync_from_file(rows: list[dict], *, source_default: str = "file") -> dict[str, int]:
    """Import the shipped registry, without overwriting what this install learned.

    Returns ``{"added": n, "kept": n}``.  ``kept`` is the number of rows the file
    also names that this installation already holds: their state, their last
    verification and their resolved company are left alone, because the file
    ships ``last_verified: null`` on every row and importing that as fact would
    throw away every request this installation has already paid for - including,
    since migration 131, every retirement it has earned.
    """
    # The shipped file is ~4,500 rows and this runs at boot, so the existing
    # keys are read once and the new rows are written in one transaction: a
    # SELECT and a transaction per row is 9,000 round trips against the single
    # writer (CR-408) before the first request is served.
    known = {
        (str(r["vendor"]), str(r["slug"]))
        for r in query_all("SELECT vendor, slug FROM board_registry")
    }
    now = utcnow()
    pending: list[tuple] = []
    kept = 0
    for row in rows:
        # Two shapes reach here.  ``discovery.load_board_registry`` normalises
        # the vendor onto ``ats_vendor`` (the column name a company row uses),
        # while the importer and the raw file call it ``vendor``.  Reading only
        # one of them skips every row of the other and reports "0 imported"
        # while looking like it worked.
        vendor = str(row.get("vendor") or row.get("ats_vendor") or "").strip().lower()
        slug = str(row.get("slug") or row.get("ats_slug") or "").strip().strip("/")
        if not vendor or not slug:
            continue
        if (vendor, slug) in known:
            kept += 1
            continue
        known.add((vendor, slug))
        pending.append(
            (
                new_id(),
                vendor,
                slug,
                row.get("name"),
                row.get("source") or source_default,
                row.get("first_seen") or now,
                "unverified",
                0,
                now,
            )
        )

    if pending:
        with write_tx() as conn:
            conn.executemany(
                "INSERT INTO board_registry "
                "(id, vendor, slug, name, source, first_seen, state, "
                " consecutive_failures, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                pending,
            )
        log.info("Board registry: imported %d new board(s), kept %d existing", len(pending), kept)
    return {"added": len(pending), "kept": kept}
