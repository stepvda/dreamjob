"""The Apply Browser's job list (FR-321, FR-324, FR-263, NFR-302, NFR-502).

One question, asked of the database rather than of five endpoints stitched
together in the browser: *for every job I selected, what is its company, its
role, who I would write to, and how far its package has got?*

That is a join across four tables and the answer has to page, because the
selection is hundreds of rows and the screen is one screen.  Doing it here is
the difference between a list that stays instant at 500 selected jobs and one
that issues 500 follow-up requests to find out whether each has a contact.

The contact column deliberately reads through the ``usable_contact`` view
rather than ``contact``: an address that was validated ``invalid`` (FR-304) or
whose owner objected (NFR-302) must not be offered as somebody to write to,
and putting that rule in the view means no caller can forget it.

Two columns come from the Apply Browser's own tables (migration 100).
``apply_selection`` is the selection itself, because ``opportunity.selected``
is rewritten by the ranked list and cleared when a campaign is re-synthesised,
so a selection made here would not survive a re-score (FR-324).
``apply_contact_resolution`` is the reachability verdict written by
:mod:`dreamjob.pipeline.apply_contacts`: it lets the screen distinguish "this
company has no contact yet" from "the FR-301 ladder was walked and nothing
survived FR-304", which is the difference between offering a button and
telling the truth.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import (
    execute,
    from_json,
    new_id,
    query_all,
    query_one,
    utcnow,
    write_tx,
)
from dreamjob.db.repositories.pipeline_cards import invalidate_active_seeker_cache

log = logging.getLogger(__name__)

#: The live package for an opportunity - a discarded one never counts, matching
#: ``applications.latest_for_opportunity``.
_LIVE_PACKAGE = """
    SELECT p2.id FROM application_package p2
     WHERE p2.opportunity_id = o.id
       AND p2.job_seeker_id = o.job_seeker_id
       AND p2.status != 'discarded'
     ORDER BY p2.created_at DESC LIMIT 1
"""

#: Whom the message would go to: the contact the package is addressed to, or
#: else the most confident usable address at the company.
_BEST_CONTACT = """
    SELECT u.id FROM usable_contact u
     WHERE u.company_id = o.company_id
       AND u.email IS NOT NULL AND u.email != ''
     ORDER BY u.confidence DESC LIMIT 1
"""

_FROM = f"""
    FROM opportunity o
    LEFT JOIN company c ON c.id = o.company_id
    LEFT JOIN application_package p ON p.id = ({_LIVE_PACKAGE})
    LEFT JOIN contact ct ON ct.id = COALESCE(p.contact_id, ({_BEST_CONTACT}))
    LEFT JOIN apply_contact_resolution r ON r.company_id = o.company_id
    LEFT JOIN apply_selection s
           ON s.job_seeker_id = o.job_seeker_id AND s.opportunity_id = o.id
"""

_SELECT = f"""
    SELECT o.id                AS opportunity_id,
           o.title             AS title,
           o.kind              AS kind,
           o.language          AS language,
           o.score             AS score,
           o.user_status       AS user_status,
           o.campaign_id       AS campaign_id,
           o.company_id        AS company_id,
           c.name              AS company_name,
           c.country           AS company_country,
           p.id                AS package_id,
           p.status            AS package_status,
           p.consistency_status AS consistency_status,
           p.leak_scan_status  AS leak_scan_status,
           p.cv_pdf_path       AS cv_pdf_path,
           p.briefing_pdf_path AS briefing_pdf_path,
           p.motivation_pdf_path AS motivation_pdf_path,
           p.email_subject     AS email_subject,
           p.email_body        AS email_body,
           p.updated_at        AS package_updated_at,
           ct.id               AS contact_id,
           ct.full_name        AS contact_name,
           ct.role_title       AS contact_role,
           ct.email            AS contact_email,
           ct.email_validation AS contact_email_validation,
           ct.objected         AS contact_objected,
           ct.email_source_method AS contact_method,
           ct.is_generic_mailbox  AS contact_is_generic,
           r.status            AS reachability,
           r.reason            AS unreachable_reason,
           r.domain            AS company_domain,
           r.resolved_at       AS reachability_checked_at,
           s.status            AS selection_status,
           s.selected_at       AS selected_at,
           (SELECT d.sent_at FROM dispatch d
             WHERE d.application_package_id = p.id AND d.sent_at IS NOT NULL
             ORDER BY d.sent_at DESC LIMIT 1) AS dispatched_at
    {_FROM}
"""

#: FR-284's ordering, so the Apply Browser lists the selection in the same
#: order the ranked list did.
_ORDER = " ORDER BY (o.manual_rank IS NULL), o.manual_rank, o.pinned DESC, o.score DESC, o.id"

#: The filters the screen offers, as SQL fragments.  Keeping them in one map
#: means the list query and the count query can never disagree about what
#: "generated" means.
FILTERS: dict[str, str] = {
    "has_contact": "ct.email IS NOT NULL AND ct.email != ''",
    "no_contact": "(ct.email IS NULL OR ct.email = '')",
    "unreachable": "r.status = 'unreachable'",
    "contact_pending": "(r.company_id IS NULL AND (ct.email IS NULL OR ct.email = ''))",
    "generated": "p.id IS NOT NULL",
    "not_generated": "p.id IS NULL",
    "consistency_passed": "p.consistency_status = 'pass'",
    "consistency_failed": "p.consistency_status = 'fail'",
    "vacancy": "o.kind = 'vacancy'",
    "speculative": "o.kind = 'speculative'",
    "approved": "p.status = 'approved'",
    "sent": "p.status = 'sent'",
}


def _where(
    job_seeker_id: str,
    *,
    campaign_id: str | None,
    filters: list[str] | None,
    search: str | None,
) -> tuple[str, list[Any]]:
    # FR-324: either flag counts as selected.  ``opportunity.selected`` is what
    # the ranked list writes; ``apply_selection`` is what this screen writes and
    # what survives the next scoring pass.  ``skipped`` is an explicit "not this
    # one" and is excluded even when the ranked list still has the flag set.
    sql = (
        " WHERE o.job_seeker_id = ?"
        " AND (s.status IS NOT NULL OR o.selected = 1)"
        " AND COALESCE(s.status, '') != 'skipped'"
    )
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND o.campaign_id = ?"
        params.append(campaign_id)
    for key in filters or []:
        clause = FILTERS.get(key)
        if clause:
            sql += f" AND {clause}"
    if search:
        sql += " AND (o.title LIKE ? OR c.name LIKE ? OR ct.full_name LIKE ?)"
        like = f"%{search.strip()}%"
        params += [like, like, like]
    return sql, params


def list_jobs(
    job_seeker_id: str,
    *,
    campaign_id: str | None = None,
    filters: list[str] | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """One page of the selection, in ranked order (FR-284, NFR-502)."""
    where, params = _where(
        job_seeker_id, campaign_id=campaign_id, filters=filters, search=search
    )
    rows = query_all(_SELECT + where + _ORDER + " LIMIT ? OFFSET ?", (*params, limit, offset))
    return [_shape(row) for row in rows]


def count_jobs(
    job_seeker_id: str,
    *,
    campaign_id: str | None = None,
    filters: list[str] | None = None,
    search: str | None = None,
) -> int:
    where, params = _where(
        job_seeker_id, campaign_id=campaign_id, filters=filters, search=search
    )
    row = query_one(f"SELECT COUNT(*) AS n {_FROM} {where}", tuple(params))
    return int(row["n"]) if row else 0


def facet_counts(job_seeker_id: str, *, campaign_id: str | None = None) -> dict[str, int]:
    """How many jobs each filter would leave, so the screen can label them.

    One aggregate over the same join rather than one query per filter: the
    counts are what tell a job seeker where the work is, and eleven round trips
    to find that out is what makes a screen feel slow.
    """
    where, params = _where(job_seeker_id, campaign_id=campaign_id, filters=None, search=None)
    columns = ", ".join(
        f"SUM(CASE WHEN {clause} THEN 1 ELSE 0 END) AS {name}"
        for name, clause in FILTERS.items()
    )
    row = query_one(f"SELECT COUNT(*) AS total, {columns} {_FROM} {where}", tuple(params))
    if row is None:  # pragma: no cover - COUNT always returns a row
        return {"total": 0, **{name: 0 for name in FILTERS}}
    return {key: int(row[key] or 0) for key in ("total", *FILTERS)}


def package_ids_for(
    job_seeker_id: str,
    *,
    campaign_id: str | None = None,
    filters: list[str] | None = None,
    search: str | None = None,
    limit: int = 1000,
) -> list[str]:
    """Every live package id behind the current filter - "select all" (FR-324)."""
    where, params = _where(
        job_seeker_id, campaign_id=campaign_id, filters=filters, search=search
    )
    rows = query_all(
        f"SELECT p.id AS package_id {_FROM} {where} AND p.id IS NOT NULL {_ORDER} LIMIT ?",
        (*params, limit),
    )
    return [row["package_id"] for row in rows]


def job(job_seeker_id: str, opportunity_id: str) -> dict[str, Any] | None:
    """One row of the same shape, for the detail pane's header."""
    row = query_one(
        _SELECT + " WHERE o.job_seeker_id = ? AND o.id = ?", (job_seeker_id, opportunity_id)
    )
    return _shape(row) if row else None


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """Turn stored paths into booleans: the list says *whether*, not *where*."""
    out = dict(row)
    out["has_cv"] = bool(out.pop("cv_pdf_path", None))
    out["has_briefing"] = bool(out.pop("briefing_pdf_path", None))
    out["has_motivation"] = bool(out.pop("motivation_pdf_path", None))
    out["has_email"] = bool((out.pop("email_body", None) or "").strip())
    out["has_contact"] = bool((out.get("contact_email") or "").strip())
    out["contact_objected"] = bool(out.get("contact_objected"))
    out["contact_is_generic"] = bool(out.get("contact_is_generic"))
    # No resolution row means nobody has looked yet, which is not the same
    # answer as "looked and found nothing" (FR-301).
    out["reachability"] = out.get("reachability") or "unknown"
    return out


# ---------------------------------------------------------------------------
# FR-324: the Apply Browser's own selection
# ---------------------------------------------------------------------------


def select_opportunity(
    job_seeker_id: str, opportunity_id: str, *, status: str = "selected", note: str | None = None
) -> str:
    """Put one opportunity in the Apply Browser, or update its state.

    Both flags are written.  ``opportunity.selected`` is what the rest of the
    application already reads (the ranked list, the generation slice), and
    ``apply_selection`` is the record that outlives the next scoring pass
    (FR-324).  Writing one without the other is how the two disagree.
    """
    now = utcnow()
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO apply_selection "
            "(id, job_seeker_id, opportunity_id, status, note, selected_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_seeker_id, opportunity_id) DO UPDATE SET "
            "  status = excluded.status,"
            "  note = COALESCE(excluded.note, apply_selection.note),"
            "  updated_at = excluded.updated_at",
            (new_id(), job_seeker_id, opportunity_id, status, note, now, now),
        )
        conn.execute(
            "UPDATE opportunity SET selected = ?, updated_at = ? "
            "WHERE id = ? AND job_seeker_id = ?",
            (0 if status == "skipped" else 1, now, opportunity_id, job_seeker_id),
        )
        row = conn.execute(
            "SELECT id FROM apply_selection WHERE job_seeker_id = ? AND opportunity_id = ?",
            (job_seeker_id, opportunity_id),
        ).fetchone()
    invalidate_active_seeker_cache()
    return row["id"] if row else ""


def select_many(
    job_seeker_id: str, opportunity_ids: list[str], *, status: str = "selected"
) -> int:
    """Select a whole filtered page in one transaction (FR-324 "select all").

    The per-row path opened a transaction per opportunity, so "select all" over
    a page was hundreds of commits and a crash halfway left half a page
    selected.  Ownership is checked in the same transaction, so a stale id in
    the list cannot create a selection row for somebody else's opportunity.
    """
    now = utcnow()
    flag = 0 if status == "skipped" else 1
    selected = 0
    with write_tx() as conn:
        for opportunity_id in dict.fromkeys(opportunity_ids):
            owned = conn.execute(
                "SELECT 1 FROM opportunity WHERE id = ? AND job_seeker_id = ?",
                (opportunity_id, job_seeker_id),
            ).fetchone()
            if not owned:
                continue
            conn.execute(
                "INSERT INTO apply_selection "
                "(id, job_seeker_id, opportunity_id, status, note, selected_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(job_seeker_id, opportunity_id) DO UPDATE SET "
                "  status = excluded.status, updated_at = excluded.updated_at",
                (new_id(), job_seeker_id, opportunity_id, status, now, now),
            )
            conn.execute(
                "UPDATE opportunity SET selected = ?, updated_at = ? "
                "WHERE id = ? AND job_seeker_id = ?",
                (flag, now, opportunity_id, job_seeker_id),
            )
            selected += 1
    if selected:
        invalidate_active_seeker_cache()
    return selected


def deselect_opportunity(job_seeker_id: str, opportunity_id: str) -> int:
    """Take one opportunity out of the browser again."""
    with write_tx() as conn:
        cur = conn.execute(
            "DELETE FROM apply_selection WHERE job_seeker_id = ? AND opportunity_id = ?",
            (job_seeker_id, opportunity_id),
        )
        conn.execute(
            "UPDATE opportunity SET selected = 0, updated_at = ? "
            "WHERE id = ? AND job_seeker_id = ?",
            (utcnow(), opportunity_id, job_seeker_id),
        )
    invalidate_active_seeker_cache()
    return cur.rowcount


def set_selection_status(job_seeker_id: str, opportunity_id: str, status: str) -> None:
    """Move one row through the browser's workflow (generating, ready, sent)."""
    execute(
        "UPDATE apply_selection SET status = ?, updated_at = ? "
        "WHERE job_seeker_id = ? AND opportunity_id = ?",
        (status, utcnow(), job_seeker_id, opportunity_id),
    )


def selection(job_seeker_id: str, limit: int = 2000) -> list[dict[str, Any]]:
    """The raw selection rows, newest first."""
    return query_all(
        "SELECT * FROM apply_selection WHERE job_seeker_id = ? "
        "ORDER BY selected_at DESC LIMIT ?",
        (job_seeker_id, limit),
    )


def selection_counts(job_seeker_id: str) -> dict[str, int]:
    rows = query_all(
        "SELECT status, COUNT(*) AS n FROM apply_selection WHERE job_seeker_id = ? "
        "GROUP BY status",
        (job_seeker_id,),
    )
    return {row["status"]: int(row["n"]) for row in rows}


# ---------------------------------------------------------------------------
# FR-301: the contacts-at-scale work list
# ---------------------------------------------------------------------------

#: A company already has somebody to write to when ``usable_contact`` - which
#: has already excluded FR-304 ``invalid`` and NFR-302 objections - holds an
#: address for it.
_HAS_USABLE_CONTACT = """
    SELECT 1 FROM usable_contact u
     WHERE u.company_id = co.id AND u.email IS NOT NULL AND u.email != ''
"""


#: A verdict of "unreachable" that was reached *without* a domain is stale the
#: moment the company acquires one: every rung below the domain was skipped, so
#: nothing was actually tried.  The company-domain pass writes ``company.domain``
#: long after this table's row was written, and without this clause those
#: companies would be filtered out as "already resolved" and never retried -
#: which is exactly the pool the FR-301 ladder can now do something with.
_VERDICT_PREDATES_THE_DOMAIN = """
    (r.status = 'unreachable'
     AND COALESCE(r.domain, '') = ''
     AND COALESCE(co.domain, '') != '')
"""

#: How the work list is ordered.  ``recency`` spends the budget where the corpus
#: is freshest; ``vacancies`` spends it where the corpus is *largest*, which is
#: what a coverage target measured in vacancies asks for - twenty-seven
#: vacancies behind one name are worth twenty-seven single-vacancy names, and
#: they cost one site crawl instead of twenty-seven.
_ORDERINGS: dict[str, str] = {
    "recency": "backs_opportunity DESC, has_contact ASC, latest_vacancy_at DESC",
    "vacancies": "has_contact ASC, backs_opportunity DESC, vacancy_count DESC,"
                 " latest_vacancy_at DESC",
}


def companies_needing_contact(
    limit: int = 500,
    *,
    job_seeker_id: str | None = None,
    include_resolved: bool = False,
    resolved_before: str | None = None,
    order: str = "recency",
) -> list[dict[str, Any]]:
    """Companies with open vacancies, in the order the caller asks for (FR-301).

    Grouped by company because the FR-301 ladder is a *company* question: one
    resolution serves every vacancy that company has posted, and walking the
    vacancy list ungrouped would crawl the same site fifty-six times.
    ``vacancy_count`` travels with the row so the caller can stop once enough
    vacancies are covered rather than once enough companies have been visited,
    and ``order='vacancies'`` walks them in that same currency.

    ``job_seeker_id`` promotes the companies that back an opportunity of that
    job seeker, which is what the Apply Browser is about to show.

    A company that already carries a verdict is skipped, with one exception:
    a verdict of "unreachable" reached before the company had a domain
    (:data:`_VERDICT_PREDATES_THE_DOMAIN`) is not a verdict about the ladder,
    it is a verdict about the missing domain, and it is retried as soon as one
    arrives.
    """
    seeker_join = (
        "LEFT JOIN opportunity o ON o.company_id = co.id AND o.job_seeker_id = ?"
        if job_seeker_id
        else ""
    )
    seeker_rank = "MAX(CASE WHEN o.id IS NOT NULL THEN 1 ELSE 0 END)" if job_seeker_id else "0"
    params: list[Any] = [job_seeker_id] if job_seeker_id else []

    freshness = ""
    if not include_resolved:
        freshness = f" AND (r.company_id IS NULL OR {_VERDICT_PREDATES_THE_DOMAIN})"
    elif resolved_before:
        freshness = (
            " AND (r.company_id IS NULL OR r.resolved_at < ?"
            f" OR {_VERDICT_PREDATES_THE_DOMAIN})"
        )
        params.append(resolved_before)

    sql = f"""
        SELECT co.id                        AS company_id,
               co.name                      AS company_name,
               co.domain                    AS company_domain,
               co.careers_url               AS careers_url,
               co.country                   AS company_country,
               COUNT(DISTINCT v.id)         AS vacancy_count,
               MAX(COALESCE(v.posted_at, v.collected_at)) AS latest_vacancy_at,
               {seeker_rank}                AS backs_opportunity,
               -- Outside the aggregate deliberately: inside it, SQLite asks
               -- the question once per *vacancy* rather than once per company,
               -- which is 2,650 view scans for 1,200 answers.
               EXISTS ({_HAS_USABLE_CONTACT}) AS has_contact,
               r.status                     AS resolution_status,
               r.resolved_at                AS resolved_at
          FROM company co
          JOIN vacancy v ON v.company_id = co.id
          LEFT JOIN apply_contact_resolution r ON r.company_id = co.id
          {seeker_join}
         WHERE co.name IS NOT NULL AND co.name != ''{freshness}
         GROUP BY co.id
         ORDER BY {_ORDERINGS.get(order, _ORDERINGS["recency"])}
         LIMIT ?
    """
    params.append(limit)
    return query_all(sql, tuple(params))


#: How long a company's resolution verdict keeps it out of the ``scope='all'``
#: sweep.  A sweep that re-walks every company whose verdict it wrote on the
#: previous sweep never reaches the companies it has never looked at; seven
#: days is long enough that one sweep buys the next one new ground and short
#: enough that a fixed ladder re-tries a dead company within the month.
ALL_COMPANIES_FRESHNESS_DAYS = 7


def _stale_before(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


#: How ``all_companies_for_contact`` orders the whole company table.  The
#: default ``vacancies`` is the same currency the sweep counts in:
#: never-attempted companies first, then the oldest attempts, then the
#: never-contacted ones with the most vacancies, so one site crawl covers as
#: many postings as possible.  ``name`` stays as a stable alphabetical view of
#: the same pool.
_ALL_COMPANY_ORDERINGS: dict[str, str] = {
    "name": "COALESCE(co.name, '') COLLATE NOCASE ASC",
    "vacancies": "has_contact ASC, (r.resolved_at IS NULL) DESC, r.resolved_at ASC,"
                 " vacancy_count DESC, latest_vacancy_at DESC,"
                 " COALESCE(co.name, '') COLLATE NOCASE ASC",
    "recency": "backs_opportunity DESC, has_contact ASC, (r.resolved_at IS NULL) DESC,"
               " r.resolved_at ASC, latest_vacancy_at DESC,"
               " COALESCE(co.name, '') COLLATE NOCASE ASC",
}


def all_companies_for_contact(
    limit: int,
    *,
    job_seeker_id: str | None = None,
    include_covered: bool = False,
    ignore_backoff: bool = False,
    order: str = "vacancies",
) -> list[dict[str, Any]]:
    """Every company with no usable contact, for the ``scope='all'`` sweep (FR-301).

    :func:`companies_needing_contact` answers "which employers could carry a
    contact for a vacancy"; this answers the wider question the Contacts screen
    can ask - "which employers still need a contact" - and therefore selects from
    ``company`` with no requirement that a vacancy or an opportunity exists.
    ``vacancy_count`` may be 0, which is exactly the row the narrower work list
    cannot produce and the sweep must still visit: a company the corpus has
    heard of but never seen hire, and one that is worth a retry precisely because
    nobody is writing to it yet.

    Selection is on the *contact*, not on the resolution row: a company with no
    usable contact is always eligible.  A company whose latest verdict is newer
    than :data:`ALL_COMPANIES_FRESHNESS_DAYS` is skipped, however - re-walking a
    company the ladder resolved an hour ago spends the pass' budget on a verdict
    it already has instead of on the companies it has never looked at
    (FR-305) - and the queue orders never-attempted companies first and the
    oldest attempts next, so a resumed or repeated sweep moves forward.

    ``include_covered=True`` is the refresh: it also returns companies that
    already have somebody to write to and ignores the freshness backoff, so a
    caller can explicitly re-check them.  ``limit`` is therefore the only
    ceiling besides ``max_companies`` in the caller.

    ``ignore_backoff=True`` decouples the no-contact filter from the freshness
    window: companies that still have no usable contact are returned even when
    their verdict was written inside :data:`ALL_COMPANIES_FRESHNESS_DAYS`, while
    companies that already have somebody to write to stay excluded.  That is
    the pool a caller wants when every remaining company was attempted today
    and the backoff alone is what makes ``scope='all'`` return nothing; the
    covered-companies rule and the freshness rule are otherwise inseparable
    because ``include_covered`` turns both off at once.

    The row shape and the usable-contact predicate are deliberately identical to
    :func:`companies_needing_contact`, so the ladder receives the same company
    dict from either work list.  The predicate is materialised once here rather
    than evaluated per company: the sweep asks it of the whole company table, and
    a per-row ``EXISTS`` on the ``usable_contact`` view made that seven seconds.
    """
    seeker_join = (
        "LEFT JOIN opportunity o ON o.company_id = co.id AND o.job_seeker_id = ?"
        if job_seeker_id
        else ""
    )
    seeker_rank = "MAX(CASE WHEN o.id IS NOT NULL THEN 1 ELSE 0 END)" if job_seeker_id else "0"
    params: list[Any] = [job_seeker_id] if job_seeker_id else []

    # The eligible-company set, evaluated once rather than once per company.
    # ``_HAS_USABLE_CONTACT`` asks the ``usable_contact`` view - which itself
    # reads ``contact_objection`` - for every row of ``company``; on the live
    # corpus that is 5,960 view evaluations before the sweep can report its
    # first tick.  The predicate below is exactly ``_HAS_USABLE_CONTACT``'s
    # (the view already excludes FR-304 ``invalid`` and NFR-302 objections), so
    # the materialised answer is the same set the per-row ``EXISTS`` produced.
    # The ``DISTINCT`` keeps the ``IN`` a membership test.
    covered_cte = """
        WITH covered AS MATERIALIZED (
            SELECT DISTINCT u.company_id
              FROM usable_contact u
             WHERE u.email IS NOT NULL AND u.email != ''
               AND u.company_id IS NOT NULL
        )
    """
    # ``include_covered`` is the refresh: companies with a contact come back and
    # the freshness backoff is off, because the caller is asking for a re-walk.
    # Without it, a company whose verdict is younger than the window is not a
    # company this sweep has anything new to say about - unless the caller asked
    # for ``ignore_backoff``, which re-opens exactly that window while keeping
    # the no-contact filter.
    covered = ""
    freshness = ""
    if not include_covered:
        covered = " AND co.id NOT IN (SELECT company_id FROM covered)"
        if not ignore_backoff:
            freshness = " AND (r.company_id IS NULL OR r.resolved_at < ?)"
            params.append(_stale_before(ALL_COMPANIES_FRESHNESS_DAYS))

    sql = f"""
        {covered_cte}
        SELECT co.id                        AS company_id,
               co.name                      AS company_name,
               co.domain                    AS company_domain,
               co.careers_url               AS careers_url,
               co.country                   AS company_country,
               COALESCE(vac.vacancy_count, 0) AS vacancy_count,
               vac.latest_vacancy_at        AS latest_vacancy_at,
               {seeker_rank}                AS backs_opportunity,
               CASE WHEN co.id IN (SELECT company_id FROM covered)
                    THEN 1 ELSE 0 END       AS has_contact,
               r.status                     AS resolution_status,
               r.resolved_at                AS resolved_at
          FROM company co
          -- One grouped pass over ``vacancy`` answers both per-company
          -- aggregates.  The correlated subqueries this replaces asked the two
          -- questions once per company (78,875,000 steps measured on the live
          -- corpus, 68.9 s); ``COALESCE`` keeps the no-vacancy company at 0.
          LEFT JOIN (
                SELECT company_id,
                       COUNT(*)                               AS vacancy_count,
                       MAX(COALESCE(posted_at, collected_at)) AS latest_vacancy_at
                  FROM vacancy
                 GROUP BY company_id
          ) vac ON vac.company_id = co.id
          LEFT JOIN apply_contact_resolution r ON r.company_id = co.id
          {seeker_join}
         WHERE co.name IS NOT NULL AND co.name != ''{covered}{freshness}
         GROUP BY co.id
         ORDER BY {_ALL_COMPANY_ORDERINGS.get(order, _ALL_COMPANY_ORDERINGS["vacancies"])}
         LIMIT ?
    """
    params.append(limit)
    return query_all(sql, tuple(params))


def vacancy_hints(company_id: str, limit: int = 6) -> list[dict[str, Any]]:
    """The newest vacancies of one company, for the FR-303 "look there first".

    ``application_target`` is the employer's own stated channel and costs
    nothing to read; the description is scanned for a published address only
    because small employers write "send your CV to ..." in the body rather
    than filling the field in.
    """
    return query_all(
        "SELECT id, title, language, country, application_channel, application_target, "
        "       source_url, description "
        "  FROM vacancy WHERE company_id = ? "
        " ORDER BY COALESCE(posted_at, collected_at) DESC LIMIT ?",
        (company_id, limit),
    )


def vacancies_carrying_an_address(company_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """The postings of one company that contain an "@" at all (FR-303 rung 2).

    :func:`vacancy_hints` reads the six *newest* postings, which is the right
    budget for "what is this company like" but the wrong one for "did this
    employer ever write its own address down": a company with twenty-seven
    vacancies that printed ``sollicitatie@`` in the tenth of them was invisible
    to the ladder, and the address is already in the database - reading it
    costs nothing and fetches nothing.

    The ``LIKE '%@%'`` is what keeps it cheap: it is the whole point of asking
    the database rather than pulling every description into Python.
    """
    return query_all(
        "SELECT id, title, language, country, application_channel, application_target, "
        "       source_url, description "
        "  FROM vacancy "
        " WHERE company_id = ? "
        "   AND (COALESCE(application_target, '') LIKE '%@%' "
        "        OR COALESCE(description, '') LIKE '%@%') "
        " ORDER BY COALESCE(posted_at, collected_at) DESC LIMIT ?",
        (company_id, limit),
    )


def vacancies_for_company(company_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Stored postings of one company, newest first, for the backup scan (FR-303).

    The backup stage reads the posting text and URL fields the corpus already
    holds before anything is fetched.  :func:`vacancies_carrying_an_address`
    cannot serve it: that query only returns postings that contain an ``@`` and
    this stage also wants the URLs.  ``limit`` is the bound - fifty postings of
    one company is enough text to find an address in, and a bigger read would
    be neither necessary nor polite.
    """
    return query_all(
        "SELECT id, title, application_channel, application_target, source_url, description "
        "  FROM vacancy WHERE company_id = ? "
        " ORDER BY COALESCE(posted_at, collected_at) DESC LIMIT ?",
        (company_id, max(1, int(limit))),
    )


def company_people(company_id: str) -> dict[str, Any]:
    """``key_people`` and ``structure``, the input to FR-303 pattern inference."""
    row = query_one(
        "SELECT name, domain, careers_url, key_people, structure, country "
        "  FROM company WHERE id = ?",
        (company_id,),
    )
    if row is None:
        return {}
    row["key_people"] = from_json(row.get("key_people"), []) or []
    row["structure"] = from_json(row.get("structure"), {}) or {}
    return row


def company_for_resolution(company_id: str) -> dict[str, Any] | None:
    """One company in the shape :func:`apply_contacts.resolve_company` expects.

    The FR-301 ladder is a company question, and running it for a single company
    - what the Contacts screen's "Find contacts" control asks for - needs the
    same fields :func:`companies_needing_contact` hands the batch pass, without
    the batch pass's work-list filters.  A company that already carries a usable
    address is reported as such so the caller can reuse it rather than re-crawl.

    Returns ``None`` when the company does not exist, which the caller turns
    into a 404 rather than an invented row.
    """
    row = query_one(
        """
        SELECT co.id            AS company_id,
               co.name          AS company_name,
               co.domain        AS company_domain,
               co.careers_url   AS careers_url,
               co.country       AS company_country,
               (SELECT COUNT(*) FROM vacancy v WHERE v.company_id = co.id) AS vacancy_count,
               EXISTS (
                   SELECT 1 FROM usable_contact u
                    WHERE u.company_id = co.id
                      AND u.email IS NOT NULL AND u.email != ''
               ) AS has_contact
          FROM company co
         WHERE co.id = ?
        """,
        (company_id,),
    )
    if row is None:
        return None
    row["vacancy_count"] = int(row.get("vacancy_count") or 0)
    row["has_contact"] = bool(row.get("has_contact"))
    return row


def set_company_domain(company_id: str, domain: str) -> None:
    """Write back a domain the ladder confirmed, so the next pass is free."""
    execute(
        "UPDATE company SET domain = ? WHERE id = ? AND (domain IS NULL OR domain = '')",
        (domain.lower(), company_id),
    )


# ---------------------------------------------------------------------------
# FR-301: reachable or unreachable, recorded either way
# ---------------------------------------------------------------------------


#: How a stored address is labelled in a coverage figure.  Only an address
#: that nobody published - inferred *and* generic - is a conventional mailbox.
#: The backup methods (``ats_board``, ``stored_document``) are published
#: addresses and keep their own label, named explicitly so no future bucket
#: folds them into the inferred one.
_METHOD_BUCKET_RESOLUTION = (
    "CASE WHEN method = 'pattern_inference' AND is_generic = 1 "
    "     THEN 'conventional_mailbox' "
    "     WHEN method IN ('ats_board', 'stored_document') THEN method "
    "     ELSE method END"
)


def get_resolution(company_id: str) -> dict[str, Any] | None:
    return query_one(
        "SELECT * FROM apply_contact_resolution WHERE company_id = ?", (company_id,)
    )


def record_resolution(company_id: str, values: dict[str, Any]) -> None:
    """Record what the FR-301 ladder concluded about one company.

    ``attempts`` and ``first_seen_at`` accumulate rather than reset, so the
    interface can say "searched three times, still nothing" instead of
    presenting every re-run as the first.

    The one invariant enforced here rather than left to the callers: a
    resolution may only point at a contact of the *same* company.  Live data
    had 731 rows where the best address was another company's contact row
    (``jobs@arbeitnow.fr`` under 497 companies); a resolution that carried that
    id was how one company's mailbox became hundreds of companies' "reachable".
    A mismatched id is dropped and the row is recorded unreachable instead.
    """
    contact_id = values.get("contact_id")
    if contact_id:
        owner = query_one("SELECT company_id FROM contact WHERE id = ?", (contact_id,))
        if owner is None or str(owner.get("company_id") or "") != str(company_id):
            log.warning(
                "Refusing to record contact %s under company %s: it belongs to %s",
                contact_id, company_id, (owner or {}).get("company_id"),
            )
            values = {
                **values,
                "status": "unreachable",
                "contact_id": None,
                "email": None,
                "method": None,
                "reason": (
                    "the address matched a contact of another company, so it is "
                    "not this company's way in"
                ),
            }
    now = utcnow()
    payload = {
        "company_id": company_id,
        "status": values.get("status", "unreachable"),
        "contact_id": values.get("contact_id"),
        "email": values.get("email"),
        "domain": values.get("domain"),
        "domain_source": values.get("domain_source"),
        "method": values.get("method"),
        "validation": values.get("validation"),
        "is_generic": 1 if values.get("is_generic") else 0,
        "reason": values.get("reason"),
        "vacancy_count": int(values.get("vacancy_count") or 0),
        "first_seen_at": now,
        "resolved_at": now,
    }
    columns = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    updates = ", ".join(
        f"{k}=excluded.{k}" for k in payload if k not in ("company_id", "first_seen_at")
    )
    execute(
        f"INSERT INTO apply_contact_resolution ({columns}) VALUES ({marks}) "
        f"ON CONFLICT(company_id) DO UPDATE SET {updates}, "
        f"attempts = apply_contact_resolution.attempts + 1",
        payload,
    )


def resolution_summary() -> dict[str, Any]:
    """What the pass achieved, by FR-303 method and FR-304 verdict."""
    return {
        "by_status": {
            row["status"]: {"companies": int(row["companies"]), "vacancies": int(row["vacancies"])}
            for row in query_all(
                "SELECT status, COUNT(*) AS companies, SUM(vacancy_count) AS vacancies "
                "  FROM apply_contact_resolution GROUP BY status"
            )
        },
        # A conventional careers mailbox is reported apart from the inference
        # that produced it: both are ``pattern_inference`` in FR-303's
        # vocabulary, but one is a guess about a person and the other a guess
        # about a mailbox, and a coverage figure that blurs them is misleading.
        # ``info@`` *printed on the company's contact page* is neither: it is a
        # published address that happens to be a role mailbox, so it stays
        # under ``website``.
        "by_method": {
            (row["bucket"] or "none"): {
                "companies": int(row["companies"]),
                "vacancies": int(row["vacancies"]),
            }
            for row in query_all(
                f"SELECT {_METHOD_BUCKET_RESOLUTION} AS bucket,"
                "       COUNT(*) AS companies, SUM(vacancy_count) AS vacancies "
                "  FROM apply_contact_resolution WHERE status = 'reachable' GROUP BY bucket"
            )
        },
        # The distinction that decides how much a coverage figure is worth:
        # somebody published or stated this address, or nobody did.
        "evidence": {
            (row["bucket"]): {
                "companies": int(row["companies"]),
                "vacancies": int(row["vacancies"]),
            }
            for row in query_all(
                "SELECT CASE WHEN method = 'pattern_inference' "
                "            THEN 'inferred' ELSE 'published_or_stated' END AS bucket,"
                "       COUNT(*) AS companies, SUM(vacancy_count) AS vacancies "
                "  FROM apply_contact_resolution WHERE status = 'reachable' GROUP BY bucket"
            )
        },
        "by_validation": {
            (row["validation"] or "none"): int(row["companies"])
            for row in query_all(
                "SELECT validation, COUNT(*) AS companies FROM apply_contact_resolution "
                " WHERE status = 'reachable' GROUP BY validation"
            )
        },
    }


def vacancies_with_contact() -> dict[str, int]:
    """The number the brief asks for: vacancies that have something to apply to.

    Counted off ``usable_contact`` rather than off the resolution table, so it
    stays true when a contact arrives by another route and false when an
    objection (NFR-302) or an FR-304 verdict later removes one.
    """
    row = query_one(
        """
        WITH covered AS MATERIALIZED (
            SELECT DISTINCT company_id
              FROM usable_contact
             WHERE email IS NOT NULL AND email <> '' AND company_id IS NOT NULL
        )
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN v.company_id IN (SELECT company_id FROM covered)
                        THEN 1 ELSE 0 END) AS with_contact
          FROM vacancy v
         WHERE v.company_id IS NOT NULL
        """
    )
    if row is None:  # pragma: no cover - COUNT always returns a row
        return {"vacancies": 0, "with_contact": 0}
    return {"vacancies": int(row["total"] or 0), "with_contact": int(row["with_contact"] or 0)}


# ---------------------------------------------------------------------------
# FR-305: one verdict per domain
# ---------------------------------------------------------------------------


def domain_probe(domain: str) -> dict[str, Any] | None:
    return query_one("SELECT * FROM apply_domain_probe WHERE domain = ?", (domain.lower(),))


def domain_probes(domains: list[str]) -> dict[str, dict[str, Any]]:
    """Every cached verdict for a batch, so the pass asks the table once."""
    if not domains:
        return {}
    marks = ", ".join("?" for _ in domains)
    rows = query_all(
        f"SELECT * FROM apply_domain_probe WHERE domain IN ({marks})",
        tuple(d.lower() for d in domains),
    )
    return {row["domain"]: row for row in rows}


def record_domain_probe(
    domain: str, outcome: str, *, company_id: str | None = None, evidence: str = ""
) -> None:
    """FR-305: remember the answer so the same domain is not probed again."""
    now = utcnow()
    execute(
        "INSERT INTO apply_domain_probe "
        "(domain, outcome, company_id, evidence, probe_count, first_probed_at, last_probed_at) "
        "VALUES (:domain, :outcome, :company_id, :evidence, 1, :now, :now) "
        "ON CONFLICT(domain) DO UPDATE SET "
        "  outcome = excluded.outcome,"
        "  company_id = COALESCE(apply_domain_probe.company_id, excluded.company_id),"
        "  evidence = excluded.evidence,"
        "  probe_count = apply_domain_probe.probe_count + 1,"
        "  last_probed_at = excluded.last_probed_at",
        {
            "domain": domain.lower(),
            "outcome": outcome,
            "company_id": company_id,
            "evidence": evidence[:400],
            "now": now,
        },
    )


def coverage_by_method() -> dict[str, Any]:
    """Vacancies that have somebody to write to, broken down by FR-303 method.

    Counted off ``usable_contact`` and the ``vacancy`` table rather than off
    ``apply_contact_resolution``, so it measures the corpus rather than the
    last pass: a contact that arrived through the per-opportunity FR-301 route,
    or one an objection later removed (NFR-302), moves this figure and does not
    move the resolution table.

    A *conventional* careers mailbox - inferred, published by nobody - is
    reported apart from the methods that read an address somebody wrote down.
    Both are ``pattern_inference`` to FR-303, but "an address this employer
    published" and "the mailbox most employers happen to use" are different
    claims, and a coverage number that merges them overstates what is known.
    ``info@`` printed on a contact page stays under ``website``: it is a role
    mailbox, but it is a published one.
    """
    rows = query_all(
        """
        WITH ranked AS (
            SELECT company_id, id, email_source_method, is_generic_mailbox, email_validation,
                   ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY confidence DESC) AS rn
              FROM usable_contact
             WHERE email IS NOT NULL AND email != '' AND company_id IS NOT NULL
        ),
        best AS (
            SELECT v.id AS vacancy_id, r.id AS contact_id
              FROM vacancy v
              JOIN ranked r ON r.company_id = v.company_id AND r.rn = 1
        )
        SELECT CASE WHEN ct.email_source_method = 'pattern_inference'
                          AND ct.is_generic_mailbox = 1 THEN 'conventional_mailbox'
                    WHEN ct.email_source_method IN ('ats_board', 'stored_document')
                         THEN ct.email_source_method
                    ELSE COALESCE(ct.email_source_method, 'unknown') END AS method,
               COALESCE(ct.email_validation, 'unknown') AS validation,
               COUNT(*) AS vacancies,
               COUNT(DISTINCT ct.company_id) AS companies
          FROM best
          JOIN contact ct ON ct.id = best.contact_id
         GROUP BY method, validation
         ORDER BY vacancies DESC
        """
    )
    totals = vacancies_with_contact()
    by_method: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_method.setdefault(
            row["method"], {"vacancies": 0, "companies": 0, "by_validation": {}}
        )
        entry["vacancies"] += int(row["vacancies"])
        entry["companies"] += int(row["companies"])
        entry["by_validation"][row["validation"]] = int(row["vacancies"])
    return {
        "vacancies": totals["vacancies"],
        "vacancies_with_contact": totals["with_contact"],
        "by_method": by_method,
        "unreachable": {
            row["status"]: {"companies": int(row["companies"]), "vacancies": int(row["vacancies"])}
            for row in query_all(
                "SELECT status, COUNT(*) AS companies, SUM(vacancy_count) AS vacancies "
                "  FROM apply_contact_resolution GROUP BY status"
            )
        },
    }
