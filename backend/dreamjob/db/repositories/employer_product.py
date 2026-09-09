"""SQL for the product surfaces of the employer tag (migration 113).

Two jobs.  The first is the journal of the resolution pass,
``employer_resolve_run``, which is the one table this slice owns.  The second
is the handful of plain look-ups ``/api/employers`` needs - a company by id, a
vacancy count, a job's status - which live here rather than in the router
because every statement in this code base is in ``db/`` (CR-408).

Everything else the product needs about the tag - verdicts, corrections,
coverage by role and reason, the queue - already lives in
``db/repositories/employer_kind.py`` and
``db/repositories/employer_resolution.py`` and is read from there rather than
re-queried here.

Why the journal matters enough to be a table.  ``employer_resolution_attempt``
answers "what was tried for this company"; it cannot answer "did anybody sweep
the corpus this month, and did the sweep finish".  Without that second answer a
coverage figure of 62% reads as "38% are direct employers" instead of "38% have
never been looked at", which is the exact inference the whole design refuses to
let a reader make by accident.
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    from_json,
    insert_row,
    new_id,
    query_all,
    query_one,
    to_json,
    utcnow,
    write_tx,
)

TABLE = "employer_resolve_run"

_JSON_COLUMNS = ("by_kind", "by_rung", "by_reason")


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for column in _JSON_COLUMNS:
        if column in out:
            out[column] = from_json(out[column], {})
    return out


def start_run(
    *,
    job_run_id: str | None = None,
    job_seeker_id: str | None = None,
    requested: int = 0,
) -> str:
    """Open a journal row.  Returns its id, which the caller closes it with."""
    return insert_row(
        TABLE,
        {
            "id": new_id(),
            "job_run_id": job_run_id,
            "job_seeker_id": job_seeker_id,
            "requested": max(0, int(requested)),
            "status": "running",
            "started_at": utcnow(),
        },
    )


#: The report fields this table stores, in the names ``EmployerResolutionReport``
#: already uses, so closing a run is a projection rather than a translation.
_REPORT_FIELDS = (
    "requested", "visited", "resolved", "cannot_tell", "failed", "reused",
    "needs_review", "vacancies_covered",
)


def finish_run(
    run_id: str, report: dict[str, Any], *, status: str = "done", reason: str | None = None
) -> dict | None:
    """Close a journal row from a resolution report."""
    values: dict[str, Any] = {
        field: max(0, int(report.get(field) or 0)) for field in _REPORT_FIELDS
    }
    values["status"] = status
    values["reason"] = reason or report.get("reason")
    values["finished_at"] = utcnow()
    for column in _JSON_COLUMNS:
        source = report.get(column)
        values[column] = to_json(source) if source else None
    sets = ", ".join(f"{k} = :{k}" for k in values)
    values["__id"] = run_id
    with write_tx() as conn:
        conn.execute(f"UPDATE {TABLE} SET {sets} WHERE id = :__id", values)
    return get_run(run_id)


def get_run(run_id: str) -> dict | None:
    return _decode(query_one(f"SELECT * FROM {TABLE} WHERE id = ?", (run_id,)))


def last_run() -> dict | None:
    """The most recent pass, whatever became of it."""
    return _decode(query_one(f"SELECT * FROM {TABLE} ORDER BY started_at DESC LIMIT 1"))


def recent_runs(limit: int = 10) -> list[dict]:
    rows = query_all(
        f"SELECT * FROM {TABLE} ORDER BY started_at DESC LIMIT ?", (max(1, int(limit)),)
    )
    return [r for r in (_decode(row) for row in rows) if r is not None]


# ---------------------------------------------------------------------------
# Plain look-ups the API surfaces need
# ---------------------------------------------------------------------------


def company(company_id: str) -> dict | None:
    """The company behind a route parameter, or ``None`` for a 404."""
    row = query_one(
        "SELECT id, name, domain, country FROM company WHERE id = ?", (company_id,)
    )
    return dict(row) if row else None


def company_names(company_ids: list[str]) -> dict[str, str]:
    """Names for a batch of ids, so a list of verdicts is not N+1 queries."""
    ids = [c for c in dict.fromkeys(company_ids) if c]
    if not ids:
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ", ".join("?" for _ in chunk)
        for row in query_all(
            f"SELECT id, name FROM company WHERE id IN ({marks})", tuple(chunk)
        ):
            out[str(row["id"])] = str(row["name"])
    return out


def vacancy_count(company_id: str) -> int:
    row = query_one("SELECT COUNT(*) AS n FROM vacancy WHERE company_id = ?", (company_id,))
    return int((row or {}).get("n") or 0)


def vacancy_counts(company_ids: list[str]) -> dict[str, int]:
    ids = [c for c in dict.fromkeys(company_ids) if c]
    if not ids:
        return {}
    out: dict[str, int] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ", ".join("?" for _ in chunk)
        for row in query_all(
            f"SELECT company_id, COUNT(*) AS n FROM vacancy WHERE company_id IN ({marks}) "
            "GROUP BY company_id",
            tuple(chunk),
        ):
            out[str(row["company_id"])] = int(row["n"])
    return out


def total_vacancies() -> int:
    """Vacancy rows that have an employer at all - the coverage denominator."""
    row = query_one("SELECT COUNT(*) AS n FROM vacancy WHERE company_id IS NOT NULL")
    return int((row or {}).get("n") or 0)


def job_state(job_run_id: str) -> dict | None:
    """A background pass's status and checkpoint, for closing its journal row."""
    row = query_one(
        "SELECT id, status, last_error, checkpoint FROM job_run WHERE id = ?", (job_run_id,)
    )
    if row is None:
        return None
    out = dict(row)
    out["checkpoint"] = from_json(out.get("checkpoint"), {}) or {}
    return out
