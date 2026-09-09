"""The employer-kind ladder's queue, trail and verdict store (FR-341, NFR-402).

Three questions, all of them SQL, none of them belonging in the pipeline
module that walks the ladder (:mod:`dreamjob.pipeline.employer_resolver`):

* **Who is next?**  ``companies_needing_verdict`` orders by vacancy count,
  because that is the number of rows a wrong answer damages - NOEL FRANKLIN's
  56 postings, FORUM JOBS' 38 - and because the corpus's 751 one-vacancy names
  can wait (docs/Interim_Agencies_Proposal.md section 1).  A company whose
  verdict is fresh is not in the queue; a ``cannot_tell`` whose ``retry_after``
  has not arrived is not in the queue either, which is what stops a monthly
  refresh from re-spending 32 minutes of register requests on names the
  register is silent about (docs/Agency_Research_Design.md section 4).

* **What has been tried?**  ``record_attempt`` / ``attempts_for`` keep one row
  per (company, rung).  NFR-402 asks that a verdict carry its evidence; a
  ``cannot_tell`` has no evidence except the list of rungs that ran and what
  each of them said, so that list is the evidence.

* **What did it conclude?**  ``read_verdict`` / ``write_verdict`` are the
  ladder's access to ``company_employer_kind`` (migration 110).

The verdict write introspects the table's columns before it writes.  That is
deliberate and not defensive habit: the verdict table is owned by another
slice, the ladder has to run whether or not the columns that slice added last
are present, and a write that names a column that does not exist is an
``OperationalError`` that would take a whole corpus pass down.  Introspecting
costs one ``PRAGMA`` per process and makes the ladder additive - a column the
resolver does not know about is left alone, and one it knows about that does
not exist yet is dropped rather than fatal.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import (
    execute,
    query_all,
    query_one,
    to_json,
    utcnow,
)

log = logging.getLogger(__name__)

VERDICT_TABLE = "company_employer_kind"
SIGNAL_TABLE = "employer_resolution_signal"
ATTEMPT_TABLE = "employer_resolution_attempt"

#: Keyed by (database file, table): a process serves more than one database -
#: the test suite points at a scratch file and modules build their own - and a
#: schema cache that ignored which file it read would answer for the wrong one.
_columns_cache: dict[tuple[str, str], tuple[str, ...]] = {}


def columns_of(table: str) -> tuple[str, ...]:
    """The columns a table actually has, or ``()`` when it has none/does not exist."""
    key = (str(get_settings().abs_db_path), table)
    if key not in _columns_cache:
        rows = query_all(f"PRAGMA table_info({table})")
        _columns_cache[key] = tuple(str(r["name"]) for r in rows)
    return _columns_cache[key]


def forget_columns() -> None:
    """Drop the schema cache.  Tests that migrate a fresh database call this."""
    _columns_cache.clear()


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def companies_needing_verdict(
    limit: int = 200,
    *,
    country: str | None = None,
    include_resolved: bool = False,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Employers without a usable verdict, the ones that matter most first.

    "Matter most" is vacancy count, then the rung-0b priority, then the freshest
    posting.  A company with no vacancy at all is not here: the tag exists to
    label postings, and an employer with nothing on the board costs a request
    for a badge nobody will see.

    ``include_resolved`` re-walks companies that already have a verdict, which
    is what a refresh pass wants; without it the queue holds only companies
    with no verdict, an expired one, or a ``cannot_tell`` whose ``retry_after``
    has come round.
    """
    moment = now or utcnow()
    params: list[Any] = []
    verdict_columns = columns_of(VERDICT_TABLE)

    if verdict_columns:
        join = f"LEFT JOIN {VERDICT_TABLE} k ON k.company_id = co.id"
        select_kind = "k.kind AS verdict_kind, k.expires_at AS verdict_expires_at"
        if include_resolved:
            freshness = ""
        else:
            # Three ways a company is due: never resolved, the verdict has
            # gone stale (FR-343), or it is a cannot_tell the retry policy
            # says is worth another rung today.
            freshness = (
                " AND (k.company_id IS NULL"
                "      OR (k.expires_at IS NOT NULL AND k.expires_at <= ?)"
                "      OR (k.kind = 'cannot_tell' AND k.retry_after IS NOT NULL"
                "          AND k.retry_after <= ?))"
                if "retry_after" in verdict_columns
                else " AND (k.company_id IS NULL"
                "      OR (k.expires_at IS NOT NULL AND k.expires_at <= ?))"
            )
            params.extend([moment, moment] if "retry_after" in verdict_columns else [moment])
    else:
        # The verdict table is not installed in this tree: everything with a
        # vacancy is due, which is the honest answer to "what is not tagged".
        join = ""
        select_kind = "NULL AS verdict_kind, NULL AS verdict_expires_at"
        freshness = ""

    country_clause = ""
    if country:
        country_clause = " AND UPPER(COALESCE(co.country, '')) = ?"
        params.append(str(country).upper()[:2])

    sql = f"""
        SELECT co.id                          AS company_id,
               co.name                        AS company_name,
               co.domain                      AS company_domain,
               co.country                     AS company_country,
               co.legal_id                    AS legal_id,
               co.sector_codes                AS sector_codes,
               COUNT(DISTINCT v.id)           AS vacancy_count,
               MAX(COALESCE(v.posted_at, v.collected_at)) AS latest_vacancy_at,
               COALESCE(s.priority, 0)        AS priority,
               s.hint                         AS hint,
               s.tier                         AS tier,
               s.score                        AS score,
               s.signals                      AS signals,
               {select_kind}
          FROM company co
          JOIN vacancy v ON v.company_id = co.id
          LEFT JOIN {SIGNAL_TABLE} s ON s.company_id = co.id
          {join}
         WHERE co.name IS NOT NULL AND co.name != ''{freshness}{country_clause}
         GROUP BY co.id
         ORDER BY vacancy_count DESC, priority DESC, latest_vacancy_at DESC
         LIMIT ?
    """
    params.append(max(1, int(limit)))
    return query_all(sql, tuple(params))


def company(company_id: str) -> dict[str, Any] | None:
    """One company in the same shape the queue returns, for a single resolve."""
    rows = query_all(
        f"""
        SELECT co.id                          AS company_id,
               co.name                        AS company_name,
               co.domain                      AS company_domain,
               co.country                     AS company_country,
               co.legal_id                    AS legal_id,
               co.sector_codes                AS sector_codes,
               COUNT(DISTINCT v.id)           AS vacancy_count,
               MAX(COALESCE(v.posted_at, v.collected_at)) AS latest_vacancy_at,
               COALESCE(s.priority, 0)        AS priority,
               s.hint                         AS hint,
               s.tier                         AS tier,
               s.score                        AS score,
               s.signals                      AS signals
          FROM company co
          LEFT JOIN vacancy v ON v.company_id = co.id
          LEFT JOIN {SIGNAL_TABLE} s ON s.company_id = co.id
         WHERE co.id = ?
         GROUP BY co.id
        """,
        (company_id,),
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Rung 0b: the free signals
# ---------------------------------------------------------------------------


def read_signals(company_id: str) -> dict[str, Any] | None:
    if not columns_of(SIGNAL_TABLE):
        return None
    return query_one(f"SELECT * FROM {SIGNAL_TABLE} WHERE company_id = ?", (company_id,))


def record_signals(company_id: str, values: dict[str, Any]) -> None:
    """Store the rung-0b snapshot.  Never a verdict; queue order and evidence."""
    if not columns_of(SIGNAL_TABLE):
        return
    payload = {
        "company_id": company_id,
        "vacancy_count": int(values.get("vacancy_count") or 0),
        "hint": str(values.get("hint") or "none"),
        "priority": float(values.get("priority") or 0.0),
        "tier": values.get("tier"),
        "score": values.get("score"),
        "signals": to_json(values.get("signals") or []),
        "computed_at": utcnow(),
    }
    _upsert(SIGNAL_TABLE, payload, keys=("company_id",))


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------


def record_attempt(company_id: str, values: dict[str, Any]) -> None:
    """One row per (company, rung), with ``attempts`` accumulating."""
    if not columns_of(ATTEMPT_TABLE):
        return
    payload = {
        "company_id": company_id,
        "rung": str(values.get("rung") or "kb"),
        "position": int(values.get("position") or 0),
        "method": str(values.get("method") or "signals"),
        "outcome": str(values.get("outcome") or "handed_down"),
        "kind": values.get("kind"),
        "confidence": values.get("confidence"),
        "reason": values.get("reason"),
        "detail": to_json(values.get("detail") or {}),
        "duration_ms": int(values.get("duration_ms") or 0),
        "attempted_at": utcnow(),
        "retry_after": values.get("retry_after"),
    }
    columns = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    updates = ", ".join(
        f"{k}=excluded.{k}" for k in payload if k not in ("company_id", "rung")
    )
    execute(
        f"INSERT INTO {ATTEMPT_TABLE} ({columns}) VALUES ({marks}) "
        f"ON CONFLICT(company_id, rung) DO UPDATE SET {updates}, "
        f"attempts = {ATTEMPT_TABLE}.attempts + 1",
        payload,
    )


def attempts_for(company_id: str) -> list[dict[str, Any]]:
    if not columns_of(ATTEMPT_TABLE):
        return []
    return query_all(
        f"SELECT * FROM {ATTEMPT_TABLE} WHERE company_id = ? ORDER BY position",
        (company_id,),
    )


def coverage_by_rung() -> list[dict[str, Any]]:
    """How far the ladder got, by rung and outcome (research note section 7.5)."""
    if not columns_of(ATTEMPT_TABLE):
        return []
    return query_all(
        f"SELECT rung, position, outcome, COUNT(*) AS companies "
        f"FROM {ATTEMPT_TABLE} GROUP BY rung, position, outcome ORDER BY position, outcome"
    )


# ---------------------------------------------------------------------------
# The verdict (migration 110's table)
# ---------------------------------------------------------------------------


def read_verdict(company_id: str) -> dict[str, Any] | None:
    if not columns_of(VERDICT_TABLE):
        return None
    return query_one(f"SELECT * FROM {VERDICT_TABLE} WHERE company_id = ?", (company_id,))


def write_verdict(company_id: str, values: dict[str, Any]) -> bool:
    """Replace the verdict for one company.  Returns whether anything was written.

    A verdict row is never edited in place by a later rung - it is replaced,
    keeping ``established_at`` and accumulating ``attempts``, so "why did this
    change" stays answerable from the trail (research note section 6).  Values
    for columns this installation does not have are dropped rather than
    written, and a missing verdict table is "nothing was stored", not a crash:
    the ladder still returns its answer to the caller who asked for it.
    """
    available = columns_of(VERDICT_TABLE)
    if not available:
        return False
    payload = {k: v for k, v in values.items() if k in available and k != "company_id"}
    if not payload:
        return False
    payload["company_id"] = company_id
    existing = read_verdict(company_id)
    if existing and "established_at" in payload:
        # First established wins; the replacement carries its own timestamps in
        # refreshed_at, which is what FR-343 freshness reads.
        payload["established_at"] = existing.get("established_at") or payload["established_at"]
    columns = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k != "company_id")
    bump = (
        f", attempts = {VERDICT_TABLE}.attempts + 1" if "attempts" in available else ""
    )
    execute(
        f"INSERT INTO {VERDICT_TABLE} ({columns}) VALUES ({marks}) "
        f"ON CONFLICT(company_id) DO UPDATE SET {updates}{bump}",
        payload,
    )
    return True


def shared_correction(company_id: str) -> dict[str, Any] | None:
    """A promoted, shared human correction, if there is one (research note section 8).

    Private corrections are deliberately not read here: they belong to one job
    seeker and this ladder writes a shared knowledge-base fact (FR-344).
    """
    if not columns_of("employer_kind_correction"):
        return None
    return query_one(
        "SELECT * FROM employer_kind_correction "
        "WHERE company_id = ? AND job_seeker_id IS NULL AND scope = 'shared' "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id,),
    )


# ---------------------------------------------------------------------------


def _upsert(table: str, payload: dict[str, Any], *, keys: tuple[str, ...]) -> None:
    columns = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k not in keys)
    execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}",
        payload,
    )
