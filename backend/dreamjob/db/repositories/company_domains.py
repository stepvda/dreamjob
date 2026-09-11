"""The statements behind the domain ladder (FR-301, FR-303, FR-305, CR-408).

:mod:`dreamjob.pipeline.company_domains` holds the judgement - which spelling
is worth trying, which page proves identity, which candidate is refused - and
this module holds the SQL, which is the split CR-408 asks for.

Three things are read and two written.  The queue is companies that have
posted a vacancy and carry no domain, **ordered by how many vacancies sit
behind them**, because the pass is measured in vacancies unlocked and a
company with twenty-seven of them is worth twenty-seven single-vacancy names.
The writes are migration 120's two tables: what was concluded about a company,
and every candidate the identity gate judged - refusals included, because the
refusals are the evidence the gate is working (CR-405).
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import query_all, query_one, utcnow, write_tx

RESOLUTION_TABLE = "company_domain_resolution"
CANDIDATE_TABLE = "company_domain_candidate"


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def companies_without_domain(
    limit: int = 1000,
    *,
    include_resolved: bool = False,
    resolved_before: str | None = None,
    company_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Companies with vacancies and no domain, biggest vacancy count first.

    ``country`` is the company's own when it has one and otherwise the first
    country any of its vacancies names, resolved in SQL so the pass does not
    issue a follow-up query per company; the pipeline decides what to do when
    both are empty.  ``company_ids`` narrows it to a known set, which is how the
    enrichment pass resolves the companies it is about to profile.
    """
    freshness = ""
    params: list[Any] = []
    if not include_resolved:
        freshness = f" AND NOT EXISTS (SELECT 1 FROM {RESOLUTION_TABLE} r WHERE r.company_id = co.id)"
    elif resolved_before:
        freshness = (
            f" AND NOT EXISTS (SELECT 1 FROM {RESOLUTION_TABLE} r"
            "  WHERE r.company_id = co.id AND r.resolved_at >= ?)"
        )
        params.append(resolved_before)
    if company_ids:
        chunk = [str(c) for c in company_ids][:900]
        marks = ",".join("?" for _ in chunk)
        freshness += f" AND co.id IN ({marks})"
        params.extend(chunk)
    params.append(max(1, int(limit)))
    return query_all(
        f"""
        SELECT c.id            AS company_id,
               c.name          AS company_name,
               c.careers_url   AS careers_url,
               c.ats_vendor    AS ats_vendor,
               c.ats_slug      AS ats_slug,
               c.legal_id      AS legal_id,
               c.vat_number    AS vat_number,
               c.company_country AS company_country,
               CASE WHEN c.company_own != '' THEN 'company'
                    WHEN c.company_country != '' THEN 'vacancy'
                    ELSE '' END AS country_source,
               COUNT(DISTINCT v.id) AS vacancy_count,
               MAX(COALESCE(v.posted_at, v.collected_at)) AS latest_vacancy_at
          FROM (
              SELECT co.id, co.name, co.careers_url, co.ats_vendor, co.ats_slug,
                     co.legal_id, co.vat_number,
                     COALESCE(TRIM(co.country), '') AS company_own,
                     -- The vacancy-country fallback is resolved once here, not
                     -- twice in the projection and again in the CASE.
                     COALESCE(NULLIF(TRIM(co.country), ''),
                              (SELECT vv.country FROM vacancy vv
                                WHERE vv.company_id = co.id
                                  AND COALESCE(TRIM(vv.country), '') != ''
                                LIMIT 1)) AS company_country
                FROM company co
               WHERE COALESCE(TRIM(co.domain), '') = ''
                 AND COALESCE(TRIM(co.name), '') != ''{freshness}
          ) c
          JOIN vacancy v ON v.company_id = c.id
         GROUP BY c.id
         ORDER BY vacancy_count DESC, latest_vacancy_at DESC
         LIMIT ?
        """,
        tuple(params),
    )


def board_hosts(company_id: str, limit: int = 4) -> list[str]:
    """The boards this company's vacancies were collected from.

    The market a job board serves is evidence about which top-level domains a
    spelling is worth trying on, and it is the only evidence left for the 123
    companies whose vacancies carry no country at all.
    """
    rows = query_all(
        "SELECT DISTINCT application_target, source_url FROM vacancy "
        " WHERE company_id = ? ORDER BY COALESCE(posted_at, collected_at) DESC LIMIT ?",
        (company_id, max(1, int(limit))),
    )
    out: list[str] = []
    for row in rows:
        for value in (row.get("application_target"), row.get("source_url")):
            text = (value or "").strip().lower()
            if text.startswith(("http://", "https://")):
                host = text.split("//", 1)[1].split("/", 1)[0]
                if host and host not in out:
                    out.append(host)
    return out


# ---------------------------------------------------------------------------
# What was concluded
# ---------------------------------------------------------------------------


def record_resolution(values: dict[str, Any]) -> None:
    """One row per company, overwritten when the ladder is walked again."""
    payload = {
        "company_id": values["company_id"],
        "company_name": str(values.get("company_name") or "")[:200],
        "status": str(values.get("status") or "unresolved"),
        "domain": values.get("domain"),
        "source": values.get("source"),
        "evidence": (values.get("evidence") or "")[:400] or None,
        "market": values.get("market") or None,
        "market_source": values.get("market_source") or None,
        "candidates": int(values.get("candidates") or 0),
        "rejected": int(values.get("rejected") or 0),
        "reason": (values.get("reason") or "")[:400] or None,
        "gate_version": values.get("gate_version"),
        "vacancy_count": int(values.get("vacancy_count") or 0),
        "resolved_at": utcnow(),
    }
    columns = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k != "company_id")
    with write_tx() as conn:
        conn.execute(
            f"INSERT INTO {RESOLUTION_TABLE} ({columns}) VALUES ({marks}) "
            f"ON CONFLICT(company_id) DO UPDATE SET {updates}",
            payload,
        )


def record_candidates(company_id: str, judged: list[dict[str, Any]]) -> None:
    """Every candidate this company was judged on, refusals included (CR-405)."""
    if not judged:
        return
    now = utcnow()
    rows = [
        {
            "company_id": company_id,
            "domain": str(item.get("domain") or "").lower(),
            "source": str(item.get("source") or "derived"),
            "outcome": str(item.get("outcome") or "rejected"),
            "reason": (item.get("reason") or "")[:400] or None,
            "decided_at": now,
        }
        for item in judged
        if item.get("domain")
    ]
    if not rows:
        return
    with write_tx() as conn:
        conn.executemany(
            f"INSERT INTO {CANDIDATE_TABLE} "
            "(company_id, domain, source, outcome, reason, decided_at) "
            "VALUES (:company_id, :domain, :source, :outcome, :reason, :decided_at) "
            "ON CONFLICT(company_id, domain) DO UPDATE SET "
            "  source=excluded.source, outcome=excluded.outcome,"
            "  reason=excluded.reason, decided_at=excluded.decided_at",
            rows,
        )


def set_domain(company_id: str, domain: str, *, source: str) -> int:
    """Write the domain onto the company, and only onto one that has none.

    The ``domain IS NULL OR domain = ''`` guard is
    :func:`dreamjob.db.repositories.apply.set_company_domain`'s, and it is here
    for the same reason: a domain already on the record was put there by
    something with more evidence than a spelling, and a concurrent pass must
    not overwrite it.  The return value says whether this call is what set it.
    """
    del source  # recorded on the resolution row, not on the company record
    with write_tx() as conn:
        cur = conn.execute(
            "UPDATE company SET domain = ? WHERE id = ? AND (domain IS NULL OR domain = '')",
            (domain.strip().lower(), company_id),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Reading the pass back
# ---------------------------------------------------------------------------


def resolution_for(company_id: str) -> dict[str, Any] | None:
    return query_one(
        f"SELECT * FROM {RESOLUTION_TABLE} WHERE company_id = ?", (company_id,)
    )


def summary() -> dict[str, Any]:
    """What the ladder has concluded across the corpus, in one round trip."""
    by_source = query_all(
        f"SELECT COALESCE(source, 'none') AS source, COUNT(*) AS companies,"
        f"       SUM(vacancy_count) AS vacancies"
        f"  FROM {RESOLUTION_TABLE} WHERE status = 'resolved' GROUP BY source"
    )
    by_outcome = query_all(
        f"SELECT outcome, COUNT(*) AS candidates FROM {CANDIDATE_TABLE} GROUP BY outcome"
    )
    totals = query_one(
        """
        SELECT COUNT(*) AS companies,
               SUM(CASE WHEN COALESCE(TRIM(c.domain), '') != '' THEN 1 ELSE 0 END) AS with_domain
          FROM company c
         WHERE EXISTS (SELECT 1 FROM vacancy v WHERE v.company_id = c.id)
        """
    ) or {}
    vacancies = query_one(
        """
        SELECT COUNT(*) AS vacancies,
               SUM(CASE WHEN COALESCE(TRIM(c.domain), '') != '' THEN 1 ELSE 0 END) AS with_domain
          FROM vacancy v JOIN company c ON c.id = v.company_id
        """
    ) or {}
    return {
        "companies_with_vacancies": int(totals.get("companies") or 0),
        "companies_with_domain": int(totals.get("with_domain") or 0),
        "vacancies": int(vacancies.get("vacancies") or 0),
        "vacancies_at_companies_with_domain": int(vacancies.get("with_domain") or 0),
        "resolved_by_source": {
            row["source"]: {
                "companies": int(row["companies"]),
                "vacancies": int(row["vacancies"] or 0),
            }
            for row in by_source
        },
        "candidates_by_outcome": {
            row["outcome"]: int(row["candidates"]) for row in by_outcome
        },
    }
