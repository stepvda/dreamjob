"""Opportunity, scoring and compensation SQL (FR-261..265, FR-281..285, FR-383).

``opportunity`` is a **private** table, so every statement here takes a
``job_seeker_id`` and filters on it (FR-101, FR-344).  The rows it points at -
``company``, ``vacancy``, ``contact``, ``compensation_observation``,
``employer_review_summary`` - are shared knowledge-base rows and carry no link
back to a job seeker; they are only ever joined *outwards* from an opportunity
the caller already proved they own.

Two ordering rules are encoded here rather than in the pipeline, because they
are what the ranked list means:

``ORDER BY (manual_rank IS NULL), manual_rank``
    FR-284: a manual position always wins over the computed one.  Recomputation
    writes scores and never touches ``manual_rank``, so the seeker's order
    survives it.
``pinned DESC, score DESC``
    the computed order, applied only where the seeker has not spoken.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from dreamjob.db.connection import (
    from_json,
    insert_row,
    query_all,
    query_one,
    to_json,
    update_row,
    utcnow,
    write_tx,
)
from dreamjob.db.repositories import employer_kind as _employer_kind

#: Columns of ``opportunity`` that hold JSON and are decoded on the way out.
JSON_COLUMNS = (
    "required_skills",
    "desirable_skills",
    "comp_sources",
    "employer_review_themes",
    "dream_fit_detail",
    "score_detail",
    "tags",
)

#: Sort keys the ranked list accepts (FR-283).  Manual order is applied first.
SORT_EXPRESSIONS: dict[str, str] = {
    "score": "o.score DESC",
    "score_asc": "o.score ASC",
    "profile_fit": "o.score_profile_fit DESC",
    "dream_fit": "o.score_dream_fit DESC",
    "compensation": "o.comp_max DESC",
    "plausibility": "o.plausibility DESC",
    "posted": "o.posted_at DESC",
    "created": "o.created_at DESC",
    "title": "o.title COLLATE NOCASE ASC",
    "company": "company_name COLLATE NOCASE ASC",
}

# NFR-502: the employer-kind badge travels with the row rather than costing a
# query per line.  ``BADGE_JOIN`` is a primary-key look-up on the verdict
# table, and the correction is a correlated scalar on purpose - a LEFT JOIN
# would double any row that has both a private and a promoted correction
# (db/repositories/employer_kind.py::badge_correction_column).
_LIST_SELECT = f"""
    SELECT o.*,
           c.name           AS company_name,
           c.domain         AS company_domain,
           c.country        AS company_country,
           c.size_band      AS company_size_band,
           c.stage          AS company_stage,
           c.trajectory     AS company_trajectory,
           c.careers_url    AS company_careers_url,
           v.source_url     AS vacancy_source_url,
           v.posted_at      AS vacancy_posted_at,
           {_employer_kind.BADGE_COLUMNS.strip()},
           {_employer_kind.badge_correction_column()}
    FROM opportunity o
    LEFT JOIN company c ON c.id = o.company_id
    LEFT JOIN vacancy v ON v.id = o.vacancy_id
    {_employer_kind.BADGE_JOIN.format(company_column="o.company_id")}
"""


def decode(row: dict | None) -> dict | None:
    """Turn a stored row into the shape the API and the scorer work with."""
    if row is None:
        return None
    out = dict(row)
    for column in JSON_COLUMNS:
        if column in out:
            out[column] = from_json(out[column], None)
    for flag in ("pinned", "selected", "comp_is_stated"):
        if flag in out and out[flag] is not None:
            out[flag] = bool(out[flag])
    return out


# ---------------------------------------------------------------------------
# Reads (FR-283)
# ---------------------------------------------------------------------------


def get_opportunity(opportunity_id: str, job_seeker_id: str) -> dict | None:
    return decode(
        query_one(
            _LIST_SELECT + " WHERE o.id = ? AND o.job_seeker_id = ?",
            (opportunity_id, job_seeker_id),
        )
    )


def get_by_ids(opportunity_ids: list[str], job_seeker_id: str) -> list[dict]:
    """Side-by-side comparison reads a hand-picked set (FR-283)."""
    if not opportunity_ids:
        return []
    marks = ", ".join("?" for _ in opportunity_ids)
    rows = query_all(
        _LIST_SELECT + f" WHERE o.job_seeker_id = ? AND o.id IN ({marks})",
        (job_seeker_id, *opportunity_ids),
    )
    by_id = {r["id"]: decode(r) for r in rows}
    return [by_id[i] for i in opportunity_ids if i in by_id]


def _filter_clause(job_seeker_id: str, filters: dict[str, Any]) -> tuple[str, list[Any]]:
    sql = " WHERE o.job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]

    def add(fragment: str, *values: Any) -> None:
        nonlocal sql
        sql += fragment
        params.extend(values)

    if filters.get("campaign_id"):
        add(" AND o.campaign_id = ?", filters["campaign_id"])
    if filters.get("company_id"):
        add(" AND o.company_id = ?", filters["company_id"])
    if filters.get("kind"):
        add(" AND o.kind = ?", filters["kind"])
    if filters.get("user_status"):
        statuses = filters["user_status"]
        marks = ", ".join("?" for _ in statuses)
        add(f" AND o.user_status IN ({marks})", *statuses)
    if filters.get("exclude_not_interested"):
        add(" AND o.user_status <> 'not_interested'")
    if filters.get("selected") is not None:
        add(" AND o.selected = ?", 1 if filters["selected"] else 0)
    if filters.get("pinned") is not None:
        add(" AND o.pinned = ?", 1 if filters["pinned"] else 0)
    if filters.get("tag"):
        # tags is a JSON array of short labels; LIKE on the quoted label is
        # exact enough and keeps the query index-free but cheap.
        add(" AND o.tags LIKE ?", f'%"{filters["tag"]}"%')
    if filters.get("min_score") is not None:
        add(" AND IFNULL(o.score, 0) >= ?", float(filters["min_score"]))
    if filters.get("min_plausibility") is not None:
        add(" AND IFNULL(o.plausibility, 1) >= ?", float(filters["min_plausibility"]))
    if filters.get("work_arrangement"):
        add(" AND o.work_arrangement = ?", filters["work_arrangement"])
    if filters.get("contract_type"):
        add(" AND o.contract_type = ?", filters["contract_type"])
    if filters.get("country"):
        add(" AND UPPER(IFNULL(o.country, '')) = ?", str(filters["country"]).upper())
    if filters.get("seniority"):
        add(" AND o.seniority = ?", filters["seniority"])
    if filters.get("function_family"):
        add(" AND o.function_family = ?", filters["function_family"])
    if filters.get("timing_flag"):
        add(" AND o.timing_flag = ?", filters["timing_flag"])
    if filters.get("has_compensation"):
        add(" AND o.comp_max IS NOT NULL")
    if filters.get("q"):
        like = f"%{filters['q']}%"
        add(
            " AND (o.title LIKE ? OR IFNULL(o.description, '') LIKE ? "
            "OR IFNULL(c.name, '') LIKE ?)",
            like,
            like,
            like,
        )
    return sql, params


def list_opportunities(
    job_seeker_id: str,
    *,
    sort: str = "score",
    respect_manual_order: bool = True,
    limit: int = 50,
    offset: int = 0,
    **filters: Any,
) -> list[dict]:
    """The ranked list (FR-283, FR-284).

    ``respect_manual_order`` is on by default: the seeker's manual positions
    come first, then pins, then the computed score.
    """
    where, params = _filter_clause(job_seeker_id, filters)
    order = SORT_EXPRESSIONS.get(sort, SORT_EXPRESSIONS["score"])
    prefix = "(o.manual_rank IS NULL), o.manual_rank ASC, o.pinned DESC, " if respect_manual_order \
        else ""
    sql = f"{_LIST_SELECT}{where} ORDER BY {prefix}{order}, o.created_at ASC LIMIT ? OFFSET ?"
    rows = query_all(sql, (*params, int(limit), int(offset)))
    return [decode(r) for r in rows]  # type: ignore[misc]


def count_opportunities(job_seeker_id: str, **filters: Any) -> int:
    where, params = _filter_clause(job_seeker_id, filters)
    sql = (
        "SELECT COUNT(*) AS n FROM opportunity o LEFT JOIN company c ON c.id = o.company_id"
        + where
    )
    row = query_one(sql, tuple(params))
    return int(row["n"]) if row else 0


def iter_for_relevance(
    job_seeker_id: str, campaign_id: str | None = None, *, batch: int = 1000
) -> Iterator[dict]:
    """A lean walk over opportunities for a relevance clean-up (FR-142/144).

    Only the columns the gate reads, and **keyset** pagination rather than
    ``OFFSET``: an offset re-scans everything before it, so walking 50,000 rows
    a page at a time is quadratic and took minutes.  Paging on ``id`` - which is
    stable and never rewritten - makes it linear.

    The wide list query is deliberately not used: it joins the employer-kind
    badge and sorts in a temporary B-tree, neither of which the gate needs.
    """
    last = ""
    while True:
        sql = (
            "SELECT id, title, function_family, country, description, work_arrangement, "
            "       company_id, kind, latitude, longitude "
            "FROM opportunity WHERE job_seeker_id = ?"
        )
        params: list[Any] = [job_seeker_id]
        if campaign_id:
            sql += " AND campaign_id = ?"
            params.append(campaign_id)
        sql += " AND id > ? ORDER BY id ASC LIMIT ?"
        params += [last, int(batch)]
        rows = query_all(sql, tuple(params))
        if not rows:
            return
        for row in rows:
            last = str(row["id"])
            yield dict(row)
        if len(rows) < batch:
            return


def list_unscored(
    job_seeker_id: str, campaign_id: str, *, limit: int = 500, offset: int = 0
) -> list[dict]:
    """Opportunities in a campaign that carry no score yet (FR-281).

    The immediate-scoring path and the backfill both need exactly this set, and
    both need it bounded: a campaign can hold tens of thousands of rows, so this
    pages rather than reading them all into memory at once.
    """
    sql = (
        f"{_LIST_SELECT} WHERE o.job_seeker_id = ? AND o.campaign_id = ? "
        "AND o.score IS NULL ORDER BY o.created_at ASC LIMIT ? OFFSET ?"
    )
    rows = query_all(sql, (job_seeker_id, campaign_id, int(limit), int(offset)))
    return [decode(r) for r in rows]  # type: ignore[misc]


def count_unscored(job_seeker_id: str, campaign_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM opportunity WHERE job_seeker_id = ? AND score IS NULL"
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    row = query_one(sql, tuple(params))
    return int(row["n"]) if row else 0


def campaign_ids_with_unscored(job_seeker_id: str) -> list[str]:
    """Campaigns that still hold unscored opportunities (FR-281).

    Read from the opportunities themselves rather than from a campaign list:
    ``list_campaigns`` is bounded and ordered, so a backfill built on it silently
    skipped a campaign that had 241 unscored rows in it.
    """
    rows = query_all(
        "SELECT DISTINCT campaign_id FROM opportunity "
        "WHERE job_seeker_id = ? AND score IS NULL AND campaign_id IS NOT NULL",
        (job_seeker_id,),
    )
    return [str(r["campaign_id"]) for r in rows]


def facets(job_seeker_id: str, campaign_id: str | None = None) -> dict[str, Any]:
    """Values present in the current result set, for the filter controls (FR-283)."""
    where = " WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        where += " AND campaign_id = ?"
        params.append(campaign_id)

    def group(column: str) -> list[dict]:
        return query_all(
            f"SELECT {column} AS value, COUNT(*) AS count FROM opportunity{where} "
            f"AND {column} IS NOT NULL GROUP BY {column} ORDER BY count DESC",
            tuple(params),
        )

    tag_counts: dict[str, int] = {}
    for row in query_all(
        f"SELECT tags FROM opportunity{where} AND tags IS NOT NULL", tuple(params)
    ):
        for tag in from_json(row["tags"], []) or []:
            tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1

    return {
        "kind": group("kind"),
        "user_status": group("user_status"),
        "function_family": group("function_family"),
        "seniority": group("seniority"),
        "work_arrangement": group("work_arrangement"),
        "contract_type": group("contract_type"),
        "country": group("country"),
        "timing_flag": group("timing_flag"),
        "tags": [{"value": k, "count": v} for k, v in sorted(
            tag_counts.items(), key=lambda kv: -kv[1]
        )],
    }


def campaign_totals(job_seeker_id: str, campaign_id: str) -> dict[str, Any]:
    row = query_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN kind = 'speculative' THEN 1 ELSE 0 END) AS speculative,
               SUM(CASE WHEN kind = 'vacancy' THEN 1 ELSE 0 END) AS vacancies,
               SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected,
               SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) AS pinned,
               SUM(CASE WHEN user_status = 'not_interested' THEN 1 ELSE 0 END) AS not_interested,
               SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS unscored,
               MAX(scored_at) AS last_scored_at
        FROM opportunity WHERE job_seeker_id = ? AND campaign_id = ?
        """,
        (job_seeker_id, campaign_id),
    )
    return {k: (v or 0) if k != "last_scored_at" else v for k, v in (row or {}).items()}


# ---------------------------------------------------------------------------
# Writes (FR-261, FR-262)
# ---------------------------------------------------------------------------


def find_by_vacancy(
    campaign_id: str, vacancy_id: str, *, job_seeker_id: str | None = None
) -> dict | None:
    """The opportunity this campaign already made from that vacancy, if any.

    ``job_seeker_id`` is optional only because a campaign already belongs to
    exactly one seeker; when the caller knows it, the owner filter is applied as
    well (FR-101).
    """
    sql = "SELECT * FROM opportunity WHERE campaign_id = ? AND vacancy_id = ?"
    params: list[Any] = [campaign_id, vacancy_id]
    if job_seeker_id:
        sql += " AND job_seeker_id = ?"
        params.append(job_seeker_id)
    return decode(query_one(sql, tuple(params)))


def find_speculative(
    campaign_id: str, company_id: str, title: str, *, job_seeker_id: str | None = None
) -> dict | None:
    sql = (
        "SELECT * FROM opportunity WHERE campaign_id = ? AND company_id = ? "
        "AND kind = 'speculative' AND lower(title) = lower(?)"
    )
    params: list[Any] = [campaign_id, company_id, title]
    if job_seeker_id:
        sql += " AND job_seeker_id = ?"
        params.append(job_seeker_id)
    return decode(query_one(sql, tuple(params)))


#: Fields synthesis owns.  A refresh overwrites these and leaves everything the
#: job seeker touched - manual_rank, pinned, selected, user_status, tags - alone
#: (FR-284).
SYNTHESIS_FIELDS = frozenset(
    {
        "company_id", "vacancy_id", "kind", "title", "function_family", "seniority",
        "description", "speculative_rationale", "plausibility", "required_skills",
        "desirable_skills", "location", "country", "latitude", "longitude",
        "work_arrangement", "remote_days", "contract_type", "fte_percentage",
        "posted_at", "application_channel", "application_target", "source_url",
        "source_adapter", "comp_min", "comp_max", "comp_currency", "comp_is_stated",
        "language", "timing_flag",
    }
)

#: Fields the scorer owns (FR-281, FR-282, FR-383).  ``manual_rank`` is not one
#: of them, which is the whole point of FR-284's acceptance criterion.
SCORE_FIELDS = frozenset(
    {
        "score", "score_profile_fit", "score_dream_fit", "score_directive_fit",
        "score_company", "score_compensation", "score_plausibility",
        "score_reachability", "rationale", "dream_fit_detail", "score_detail",
        "scored_at",
    }
)

#: Fields the compensation pass owns (FR-264, FR-265).
COMPENSATION_FIELDS = frozenset(
    {
        "comp_min", "comp_max", "comp_currency", "comp_confidence", "comp_sources",
        "comp_is_stated", "employer_rating", "employer_review_themes",
    }
)


def _restrict(values: dict, allowed: frozenset[str]) -> dict:
    return {k: v for k, v in values.items() if k in allowed}


def create_opportunity(job_seeker_id: str, campaign_id: str, values: dict) -> str:
    now = utcnow()
    payload = {
        "job_seeker_id": job_seeker_id,
        "campaign_id": campaign_id,
        "created_at": now,
        "updated_at": now,
        **values,
    }
    return insert_row("opportunity", payload)


def update_opportunity(
    opportunity_id: str, values: dict, *, job_seeker_id: str | None = None
) -> None:
    """Update one opportunity, filtering on its owner whenever the caller knows it.

    ``opportunity`` is private (FR-101), so the owner belongs in the ``WHERE``
    clause and not in a promise the caller made earlier.  The parameter is
    optional because the pipeline reaches rows it has already read through an
    owner-filtered query.
    """
    if not values:
        return
    payload = {**values, "updated_at": utcnow()}
    if not job_seeker_id:
        update_row("opportunity", opportunity_id, payload)
        return
    encoded = {k: (to_json(v) if isinstance(v, dict | list) else v) for k, v in payload.items()}
    assignments = ", ".join(f"{k}=:{k}" for k in encoded)
    encoded["__id"] = opportunity_id
    encoded["__seeker"] = job_seeker_id
    with write_tx() as conn:
        conn.execute(
            f"UPDATE opportunity SET {assignments} "
            "WHERE id = :__id AND job_seeker_id = :__seeker",
            encoded,
        )


def upsert_synthesised(
    job_seeker_id: str, campaign_id: str, values: dict, existing: dict | None
) -> tuple[str, bool]:
    """Insert or refresh one synthesised opportunity.  Returns ``(id, created)``."""
    fields = _restrict(values, SYNTHESIS_FIELDS)
    if existing is None:
        return create_opportunity(job_seeker_id, campaign_id, fields), True
    update_opportunity(existing["id"], fields, job_seeker_id=job_seeker_id)
    return str(existing["id"]), False


def save_scores(opportunity_id: str, values: dict, *, job_seeker_id: str | None = None) -> None:
    """Write sub-scores only.  Never touches manual order or user state (FR-284)."""
    update_opportunity(
        opportunity_id, _restrict(values, SCORE_FIELDS), job_seeker_id=job_seeker_id
    )


def save_compensation(
    opportunity_id: str, values: dict, *, job_seeker_id: str | None = None
) -> None:
    update_opportunity(
        opportunity_id, _restrict(values, COMPENSATION_FIELDS), job_seeker_id=job_seeker_id
    )


def delete_campaign_opportunities(job_seeker_id: str, campaign_id: str) -> int:
    with write_tx() as conn:
        cur = conn.execute(
            "DELETE FROM opportunity WHERE job_seeker_id = ? AND campaign_id = ?",
            (job_seeker_id, campaign_id),
        )
        return cur.rowcount


def delete_opportunities(job_seeker_id: str, opportunity_ids: list[str]) -> int:
    """Delete named opportunities, filtered on their owner (FR-344).

    Chunked so a large clean-up never builds a statement past SQLite's
    parameter limit, and so the single writer is released between batches
    (CR-408, NFR-102).
    """
    if not opportunity_ids:
        return 0
    deleted = 0
    for start in range(0, len(opportunity_ids), 400):
        chunk = opportunity_ids[start : start + 400]
        marks = ",".join("?" for _ in chunk)
        with write_tx() as conn:
            cur = conn.execute(
                f"DELETE FROM opportunity WHERE job_seeker_id = ? AND id IN ({marks})",
                (job_seeker_id, *chunk),
            )
            deleted += cur.rowcount
    return deleted


def ids_touching_user_decisions(job_seeker_id: str, opportunity_ids: list[str]) -> set[str]:
    """Of these opportunities, the ones a person has already acted on.

    A clean-up must never remove a row the seeker pinned, selected, ranked,
    rejected, applied to, or has a pipeline card for: their decision outranks a
    later verdict about relevance (NFR-305).
    """
    if not opportunity_ids:
        return set()
    protected: set[str] = set()
    for start in range(0, len(opportunity_ids), 400):
        chunk = opportunity_ids[start : start + 400]
        marks = ",".join("?" for _ in chunk)
        rows = query_all(
            f"""
            SELECT id FROM opportunity
            WHERE job_seeker_id = ? AND id IN ({marks})
              AND (pinned = 1 OR selected = 1 OR manual_rank IS NOT NULL
                   OR user_status <> 'new' OR tags IS NOT NULL)
            """,
            (job_seeker_id, *chunk),
        )
        protected.update(str(r["id"]) for r in rows)
        for table in ("application_package", "pipeline_card"):
            for row in query_all(
                f"SELECT DISTINCT opportunity_id FROM {table} "
                f"WHERE job_seeker_id = ? AND opportunity_id IN ({marks})",
                (job_seeker_id, *chunk),
            ):
                protected.add(str(row["opportunity_id"]))
    return protected



# ---------------------------------------------------------------------------
# User controls (FR-284)
# ---------------------------------------------------------------------------

USER_CONTROL_FIELDS = frozenset(
    {"selected", "pinned", "user_status", "not_interested_reason", "tags", "language"}
)


def set_user_controls(opportunity_id: str, job_seeker_id: str, values: dict) -> dict | None:
    """FR-284: write the seeker's own controls onto their own row.

    The owner is part of the ``WHERE`` clause rather than of a check the caller
    is trusted to have made first (FR-101): a route that forgot to verify
    ownership then writes nothing instead of writing to someone else's row.
    """
    payload = _restrict(values, USER_CONTROL_FIELDS)
    for flag in ("selected", "pinned"):
        if flag in payload:
            payload[flag] = 1 if payload[flag] else 0
    update_opportunity(opportunity_id, payload, job_seeker_id=job_seeker_id)
    return get_opportunity(opportunity_id, job_seeker_id)


def set_manual_order(job_seeker_id: str, campaign_id: str, ordered_ids: list[str]) -> int:
    """FR-284: write the seeker's own positions, 1..n, in one transaction.

    Only the listed opportunities get a rank; everything else keeps whatever it
    had, so a partial re-order of the top of the list is allowed.
    """
    if not ordered_ids:
        return 0
    now = utcnow()
    with write_tx() as conn:
        written = 0
        for position, opportunity_id in enumerate(ordered_ids, start=1):
            cur = conn.execute(
                "UPDATE opportunity SET manual_rank = ?, updated_at = ? "
                "WHERE id = ? AND job_seeker_id = ? AND campaign_id = ?",
                (position, now, opportunity_id, job_seeker_id, campaign_id),
            )
            written += cur.rowcount
        return written


def clear_manual_order(
    job_seeker_id: str, campaign_id: str, opportunity_ids: list[str] | None = None
) -> int:
    sql = (
        "UPDATE opportunity SET manual_rank = NULL, updated_at = ? "
        "WHERE job_seeker_id = ? AND campaign_id = ?"
    )
    params: list[Any] = [utcnow(), job_seeker_id, campaign_id]
    if opportunity_ids:
        marks = ", ".join("?" for _ in opportunity_ids)
        sql += f" AND id IN ({marks})"
        params.extend(opportunity_ids)
    with write_tx() as conn:
        return conn.execute(sql, tuple(params)).rowcount


# ---------------------------------------------------------------------------
# Campaign inputs: which vacancies and companies this campaign may draw on
# ---------------------------------------------------------------------------


def campaign_vacancies(campaign_id: str) -> list[dict]:
    """Vacancies this campaign's plan items actually produced (FR-166).

    Every one of them: a campaign that collected 40k vacancies means to rank
    40k, and a cap here silently threw away most of a long collection before
    the seeker ever saw it.
    """
    return query_all(
        """
        SELECT DISTINCT v.* FROM vacancy v
        JOIN provenance p ON p.entity_type = 'vacancy' AND p.entity_id = v.id
        JOIN source_plan_item s ON s.id = p.source_plan_item_id
        WHERE s.campaign_id = ?
        ORDER BY COALESCE(v.posted_at, v.collected_at) DESC
        """,
        (campaign_id,),
    )


def campaign_company_ids(campaign_id: str) -> list[str]:
    """Companies this campaign touched, directly or through a vacancy."""
    rows = query_all(
        """
        SELECT DISTINCT p.entity_id AS id FROM provenance p
        JOIN source_plan_item s ON s.id = p.source_plan_item_id
        WHERE s.campaign_id = ? AND p.entity_type = 'company'
        UNION
        SELECT DISTINCT v.company_id AS id FROM vacancy v
        JOIN provenance p ON p.entity_type = 'vacancy' AND p.entity_id = v.id
        JOIN source_plan_item s ON s.id = p.source_plan_item_id
        WHERE s.campaign_id = ? AND v.company_id IS NOT NULL
        """,
        (campaign_id, campaign_id),
    )
    return [r["id"] for r in rows if r["id"]]


def watchlist_company_ids(job_seeker_id: str) -> list[str]:
    rows = query_all(
        "SELECT company_id FROM watchlist_entry WHERE job_seeker_id = ? AND active = 1",
        (job_seeker_id,),
    )
    return [r["company_id"] for r in rows]


def vacancies_for_companies(company_ids: list[str], since: str | None = None) -> list[dict]:
    """Knowledge-base vacancies of known companies - the reuse path (FR-342)."""
    if not company_ids:
        return []
    marks = ", ".join("?" for _ in company_ids)
    sql = f"SELECT * FROM vacancy WHERE company_id IN ({marks})"
    params: list[Any] = list(company_ids)
    if since:
        sql += " AND COALESCE(posted_at, collected_at) >= ?"
        params.append(since)
    sql += " ORDER BY COALESCE(posted_at, collected_at) DESC LIMIT 5000"
    return query_all(sql, tuple(params))


def companies_by_ids(company_ids: list[str]) -> list[dict]:
    if not company_ids:
        return []
    marks = ", ".join("?" for _ in company_ids)
    return query_all(f"SELECT * FROM company WHERE id IN ({marks})", tuple(company_ids))


def get_company(company_id: str) -> dict | None:
    return query_one("SELECT * FROM company WHERE id = ?", (company_id,))


def get_vacancy(vacancy_id: str) -> dict | None:
    return query_one("SELECT * FROM vacancy WHERE id = ?", (vacancy_id,))


def company_signals(company_id: str, limit: int = 40) -> list[dict]:
    return query_all(
        "SELECT * FROM hiring_signal WHERE company_id = ? "
        "ORDER BY COALESCE(occurred_at, collected_at) DESC LIMIT ?",
        (company_id, limit),
    )


def company_analysis(company_id: str) -> dict | None:
    return query_one("SELECT * FROM financial_analysis WHERE company_id = ?", (company_id,))


def company_structure_and_people(company_id: str) -> dict:
    row = query_one(
        "SELECT structure, key_people, tech_stack, products_services, business_summary, "
        "values_culture, news, size_fte, size_band, stage, ownership, trajectory, "
        "sector_codes, locations, country FROM company WHERE id = ?",
        (company_id,),
    )
    return dict(row) if row else {}


def competitor_hiring(company_id: str, limit: int = 25) -> list[dict]:
    """What the peers are advertising - one of FR-262's inputs."""
    return query_all(
        """
        SELECT l.peer_name, c.name AS peer_company_name, v.title, v.function_family,
               v.seniority, COALESCE(v.posted_at, v.collected_at) AS posted_at
        FROM competitor_link l
        LEFT JOIN company c ON c.id = l.peer_company_id
        LEFT JOIN vacancy v ON v.company_id = l.peer_company_id
        WHERE l.company_id = ? AND v.id IS NOT NULL
        ORDER BY posted_at DESC
        LIMIT ?
        """,
        (company_id, limit),
    )


def companies_with_vacancy_opportunity(
    campaign_id: str, *, job_seeker_id: str | None = None
) -> set[str]:
    sql = (
        "SELECT DISTINCT company_id FROM opportunity "
        "WHERE campaign_id = ? AND kind = 'vacancy' AND company_id IS NOT NULL"
    )
    params: list[Any] = [campaign_id]
    if job_seeker_id:
        sql += " AND job_seeker_id = ?"
        params.append(job_seeker_id)
    return {r["company_id"] for r in query_all(sql, tuple(params))}


# ---------------------------------------------------------------------------
# Scoring inputs (FR-281)
# ---------------------------------------------------------------------------


def profile_skills(job_seeker_id: str, profile_version_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT normalised_label, raw_label, proficiency, years_experience, last_used_year "
        "FROM profile_skill WHERE job_seeker_id = ?"
    )
    params: list[Any] = [job_seeker_id]
    if profile_version_id:
        sql += " AND profile_version_id = ?"
        params.append(profile_version_id)
    return query_all(sql, tuple(params))


def reachability_inputs(job_seeker_id: str, company_id: str | None) -> dict[str, Any]:
    """FR-281 reachability: a validated contact, or a warm introduction path."""
    if not company_id:
        return {"contacts": [], "introduction_paths": 0, "best_path_strength": 0.0}
    contacts = query_all(
        "SELECT id, full_name, role_title, department, email, email_validation, "
        "is_generic_mailbox, objected, confidence FROM contact "
        "WHERE company_id = ? AND objected = 0",
        (company_id,),
    )
    row = query_one(
        "SELECT COUNT(*) AS n, MAX(strength) AS best FROM introduction_path "
        "WHERE job_seeker_id = ? AND company_id = ? AND status <> 'rejected'",
        (job_seeker_id, company_id),
    )
    return {
        "contacts": contacts,
        "introduction_paths": int((row or {}).get("n") or 0),
        "best_path_strength": float((row or {}).get("best") or 0.0),
    }


def contacts_for_company(company_id: str) -> list[dict]:
    return query_all(
        "SELECT * FROM contact WHERE company_id = ? AND objected = 0 ORDER BY confidence DESC",
        (company_id,),
    )


def introduction_paths(job_seeker_id: str, opportunity_id: str) -> list[dict]:
    return query_all(
        "SELECT * FROM introduction_path WHERE job_seeker_id = ? AND opportunity_id = ? "
        "ORDER BY strength DESC",
        (job_seeker_id, opportunity_id),
    )


# ---------------------------------------------------------------------------
# Weights and feedback (FR-281, FR-285)
# ---------------------------------------------------------------------------


def get_weights(job_seeker_id: str) -> dict | None:
    row = query_one("SELECT * FROM scoring_weights WHERE job_seeker_id = ?", (job_seeker_id,))
    if row is None:
        return None
    return {
        "weights": from_json(row["weights"], {}) or {},
        "learned_from": from_json(row["learned_from"], None),
        "updated_at": row["updated_at"],
    }


def save_weights(
    job_seeker_id: str, weights: dict[str, float], learned_from: Any = None
) -> None:
    existing = query_one(
        "SELECT id FROM scoring_weights WHERE job_seeker_id = ?", (job_seeker_id,)
    )
    payload = {
        "weights": to_json(weights),
        "learned_from": to_json(learned_from),
        "updated_at": utcnow(),
    }
    if existing:
        update_row("scoring_weights", existing["id"], payload)
        return
    insert_row("scoring_weights", {"job_seeker_id": job_seeker_id, **payload})


def feedback_rows(job_seeker_id: str, limit: int = 500) -> list[dict]:
    """Pins and rejections with their sub-scores - the FR-285 learning signal."""
    rows = query_all(
        """
        SELECT o.id, o.kind, o.title, o.pinned, o.selected, o.user_status,
               o.not_interested_reason, o.score, o.score_profile_fit, o.score_dream_fit,
               o.score_directive_fit, o.score_company, o.score_compensation,
               o.score_plausibility, o.score_reachability, o.work_arrangement,
               o.contract_type, o.country, o.location, o.seniority, o.function_family,
               o.comp_min, o.comp_max, c.size_band, c.stage, c.trajectory
        FROM opportunity o
        LEFT JOIN company c ON c.id = o.company_id
        WHERE o.job_seeker_id = ?
          AND (o.pinned = 1 OR o.user_status IN ('not_interested', 'interested', 'applied'))
        ORDER BY o.updated_at DESC
        LIMIT ?
        """,
        (job_seeker_id, limit),
    )
    return rows


# ---------------------------------------------------------------------------
# Compensation (FR-264) and employer reviews (FR-265)
# ---------------------------------------------------------------------------


def posted_salary_corpus(
    *,
    function_family: str | None,
    seniority: str | None,
    country: str | None,
    title_terms: list[str] | None = None,
    company_id: str | None = None,
    limit: int = 400,
) -> list[dict]:
    """Posted ranges on comparable vacancies - the most defensible source (FR-264).

    Widening happens in the caller: this returns exactly what was asked for so
    the estimator can report which comparison set produced the range.
    """
    sql = (
        "SELECT id, company_id, title, function_family, seniority, country, location, "
        "salary_min, salary_max, salary_currency, source_url, source_adapter, "
        "COALESCE(posted_at, collected_at) AS posted_at "
        "FROM vacancy WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL "
        "AND salary_max > 0"
    )
    params: list[Any] = []
    if company_id:
        sql += " AND company_id = ?"
        params.append(company_id)
    if function_family:
        sql += " AND function_family = ?"
        params.append(function_family)
    if seniority:
        sql += " AND seniority = ?"
        params.append(seniority)
    if country:
        sql += " AND UPPER(IFNULL(country, '')) = ?"
        params.append(country.upper())
    if title_terms:
        sql += " AND (" + " OR ".join("title LIKE ?" for _ in title_terms) + ")"
        params.extend(f"%{t}%" for t in title_terms)
    sql += " ORDER BY COALESCE(posted_at, collected_at) DESC LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


def compensation_observations(
    *,
    function_family: str | None = None,
    seniority: str | None = None,
    country: str | None = None,
    company_id: str | None = None,
    sources: list[str] | None = None,
    normalised_titles: list[str] | None = None,
    market_wide: bool = False,
    limit: int = 100,
) -> list[dict]:
    sql = "SELECT * FROM compensation_observation WHERE 1 = 1"
    params: list[Any] = []
    if company_id:
        sql += " AND company_id = ?"
        params.append(company_id)
    if normalised_titles:
        marks = ", ".join("?" for _ in normalised_titles)
        sql += f" AND normalised_title IN ({marks})"
        params.extend(normalised_titles)
    if market_wide:
        # A national survey is published per occupation, not per function
        # family: its rows carry no family and would never match an equality
        # test against one.
        sql += " AND function_family IS NULL"
    elif function_family:
        sql += " AND function_family = ?"
        params.append(function_family)
    if seniority and not market_wide:
        sql += " AND seniority = ?"
        params.append(seniority)
    if country:
        sql += " AND UPPER(IFNULL(country, '')) = ?"
        params.append(country.upper())
    if sources:
        marks = ", ".join("?" for _ in sources)
        sql += f" AND source IN ({marks})"
        params.extend(sources)
    sql += " ORDER BY collected_at DESC LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


def record_observation(data: dict) -> str:
    """Landing place for compensation data collected by an adapter or the browser.

    Shared knowledge base: no ``job_seeker_id`` (FR-344), freshness recorded
    (FR-343).
    """
    values = dict(data)
    values.setdefault("collected_at", utcnow())
    values.setdefault("currency", "EUR")
    values.setdefault("period", "annual")
    return insert_row("compensation_observation", values)


def employer_reviews(company_id: str) -> list[dict]:
    rows = query_all(
        "SELECT * FROM employer_review_summary WHERE company_id = ? ORDER BY collected_at DESC",
        (company_id,),
    )
    for row in rows:
        row["themes"] = from_json(row["themes"], []) or []
    return rows


def record_employer_review(data: dict) -> None:
    values = dict(data)
    values.setdefault("collected_at", utcnow())
    existing = query_one(
        "SELECT id FROM employer_review_summary WHERE company_id = ? AND source = ?",
        (values.get("company_id"), values.get("source")),
    )
    if existing:
        update_row("employer_review_summary", existing["id"], values)
        return
    insert_row("employer_review_summary", values)
