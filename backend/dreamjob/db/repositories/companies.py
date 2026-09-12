"""Company-profiling SQL: crawls, signals, competitors (FR-221..226, FR-341, NFR-402).

Everything this module touches is a *shared* knowledge-base table - ``company``,
``competitor_link``, ``hiring_signal``, ``provenance``, ``raw_document`` - so no
statement here accepts a ``job_seeker_id`` (FR-344).  The one exception is
:func:`add_to_watchlist`, which writes the job seeker's own private
``watchlist_entry`` row when a competitor suggestion is adopted into the target
list (FR-224).

Two provenance conventions are used, both writing to the single ``provenance``
table (NFR-402):

``field_path = "website_page:<kind>"``
    one row per crawled page, so the page inventory of a company survives in the
    knowledge base and the raw HTML stays re-extractable (FR-183, FR-341).
``field_path = "<profile field>"``
    one row per extracted profile field, carrying the confidence the extractor
    assigned and the document the value came from (NFR-402).
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    insert_row,
    query_all,
    query_one,
    update_row,
    utcnow,
    write_tx,
)

#: Adapter key under which website crawls and their derived fields are recorded.
WEBSITE_ADAPTER_KEY = "website.crawl"

PAGE_PROVENANCE_PREFIX = "website_page:"


# ---------------------------------------------------------------------------
# Company reads (FR-222, FR-226)
# ---------------------------------------------------------------------------


def get_company(company_id: str) -> dict | None:
    return query_one("SELECT * FROM company WHERE id = ?", (company_id,))


def is_suppressed(company_id: str) -> bool:
    """Whether this shared company row is hidden from browse and counts."""
    row = query_one("SELECT suppressed FROM company WHERE id = ?", (company_id,))
    return bool(row and row["suppressed"])


def suppress_company(company_id: str, reason: str) -> dict | None:
    """Hide a shared company without deleting it (FR-341, NFR-302).

    The row and everything it evidences stay; only its visibility changes.
    Returns the updated row, or ``None`` when the company does not exist.
    """
    with write_tx() as conn:
        cur = conn.execute(
            "UPDATE company SET suppressed = 1, suppressed_at = ?, suppressed_reason = ? "
            "WHERE id = ?",
            (utcnow(), (reason or "").strip() or None, company_id),
        )
    if not cur.rowcount:
        return None
    return get_company(company_id)


def unsuppress_company(company_id: str) -> dict | None:
    """Restore a suppressed company to browse and counts."""
    with write_tx() as conn:
        cur = conn.execute(
            "UPDATE company SET suppressed = 0, suppressed_at = NULL, suppressed_reason = NULL "
            "WHERE id = ?",
            (company_id,),
        )
    if not cur.rowcount:
        return None
    return get_company(company_id)


def company_freshness(company_id: str) -> str | None:
    """The moment the profile last reflected the outside world (FR-226, FR-343)."""
    row = query_one(
        "SELECT COALESCE(refreshed_at, collected_at) AS freshness FROM company WHERE id = ?",
        (company_id,),
    )
    return row["freshness"] if row else None


def companies_in_country(
    country: str | None, exclude_id: str | None = None, limit: int = 400
) -> list[dict]:
    """Candidate peer set for the similarity passes (FR-224)."""
    sql = "SELECT * FROM company WHERE suppressed = 0"
    params: list[Any] = []
    if country:
        sql += " AND (country = ? OR country IS NULL)"
        params.append(country.upper())
    if exclude_id:
        sql += " AND id <> ?"
        params.append(exclude_id)
    sql += " ORDER BY COALESCE(refreshed_at, collected_at) DESC LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


def companies_by_sector_fragment(
    fragment: str, exclude_id: str | None = None, country: str | None = None, limit: int = 100
) -> list[dict]:
    """Companies whose ``sector_codes`` JSON contains a code fragment (FR-224)."""
    sql = "SELECT * FROM company WHERE suppressed = 0 AND sector_codes LIKE ?"
    params: list[Any] = [f"%{fragment}%"]
    if exclude_id:
        sql += " AND id <> ?"
        params.append(exclude_id)
    if country:
        sql += " AND (country = ? OR country IS NULL)"
        params.append(country.upper())
    sql += " LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


def companies_mentioning(
    fragment: str, exclude_id: str | None = None, limit: int = 60
) -> list[dict]:
    """Companies whose stored news or references mention a name (press co-mention)."""
    like = f"%{fragment}%"
    sql = (
        "SELECT * FROM company WHERE suppressed = 0 "
        "AND (news LIKE ? OR reference_customers LIKE ? OR business_summary LIKE ?)"
    )
    params: list[Any] = [like, like, like]
    if exclude_id:
        sql += " AND id <> ?"
        params.append(exclude_id)
    sql += " LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


# ---------------------------------------------------------------------------
# Crawled-page inventory (FR-221, FR-341, FR-183)
# ---------------------------------------------------------------------------


def record_crawled_page(
    company_id: str,
    kind: str,
    *,
    raw_document_id: str | None,
    confidence: float = 0.8,
    source_plan_item_id: str | None = None,
    adapter_key: str = WEBSITE_ADAPTER_KEY,
) -> str:
    return insert_row(
        "provenance",
        {
            "entity_type": "company",
            "entity_id": company_id,
            "source_plan_item_id": source_plan_item_id,
            "raw_document_id": raw_document_id,
            "adapter_key": adapter_key,
            "field_path": f"{PAGE_PROVENANCE_PREFIX}{kind}",
            "confidence": confidence,
            "created_at": utcnow(),
        },
    )


def crawled_pages(company_id: str, limit: int = 200) -> list[dict]:
    """The page inventory of the last crawls, newest first (FR-221)."""
    return query_all(
        """
        SELECT p.id, p.field_path, p.confidence, p.created_at, p.adapter_key,
               d.id AS raw_document_id, d.url, d.content_type, d.storage_path,
               d.byte_size, d.http_status, d.fetched_at
        FROM provenance p
        LEFT JOIN raw_document d ON d.id = p.raw_document_id
        WHERE p.entity_type = 'company' AND p.entity_id = ? AND p.field_path LIKE ?
        ORDER BY p.created_at DESC
        LIMIT ?
        """,
        (company_id, f"{PAGE_PROVENANCE_PREFIX}%", limit),
    )


def last_crawl_at(company_id: str) -> str | None:
    row = query_one(
        "SELECT MAX(created_at) AS last FROM provenance WHERE entity_type = 'company' "
        "AND entity_id = ? AND field_path LIKE ?",
        (company_id, f"{PAGE_PROVENANCE_PREFIX}%"),
    )
    return row["last"] if row and row["last"] else None


def clear_crawled_pages(company_id: str) -> int:
    """Drop the previous page inventory before recording a fresh crawl (FR-226)."""
    with write_tx() as conn:
        cur = conn.execute(
            "DELETE FROM provenance WHERE entity_type = 'company' AND entity_id = ? "
            "AND field_path LIKE ?",
            (company_id, f"{PAGE_PROVENANCE_PREFIX}%"),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Per-field provenance and confidence (NFR-402)
# ---------------------------------------------------------------------------


def record_field_provenance(
    company_id: str,
    fields: dict[str, dict[str, Any]],
    *,
    adapter_key: str = WEBSITE_ADAPTER_KEY,
    source_plan_item_id: str | None = None,
) -> int:
    """Write one provenance row per extracted field, replacing this adapter's rows.

    ``fields`` maps a field path onto ``{"confidence": float, "raw_document_id": str}``.
    Replacing rather than appending keeps the profile view honest: what is shown
    is where the *current* value came from.
    """
    now = utcnow()
    with write_tx() as conn:
        conn.execute(
            "DELETE FROM provenance WHERE entity_type = 'company' AND entity_id = ? "
            "AND adapter_key = ? AND field_path NOT LIKE ?",
            (company_id, adapter_key, f"{PAGE_PROVENANCE_PREFIX}%"),
        )
        written = 0
        for field_path, meta in fields.items():
            conn.execute(
                "INSERT INTO provenance (id, entity_type, entity_id, source_plan_item_id, "
                "raw_document_id, adapter_key, field_path, confidence, created_at) "
                "VALUES (lower(hex(randomblob(16))), 'company', ?, ?, ?, ?, ?, ?, ?)",
                (
                    company_id,
                    source_plan_item_id,
                    meta.get("raw_document_id"),
                    adapter_key,
                    field_path,
                    float(meta.get("confidence") or 0.0),
                    now,
                ),
            )
            written += 1
    return written


def field_provenance(company_id: str) -> list[dict]:
    """Per-field provenance for the profile view (NFR-402)."""
    return query_all(
        """
        SELECT p.field_path, p.confidence, p.adapter_key, p.created_at,
               d.url AS source_url, d.fetched_at
        FROM provenance p
        LEFT JOIN raw_document d ON d.id = p.raw_document_id
        WHERE p.entity_type = 'company' AND p.entity_id = ? AND p.field_path IS NOT NULL
              AND p.field_path NOT LIKE ?
        ORDER BY p.field_path
        """,
        (company_id, f"{PAGE_PROVENANCE_PREFIX}%"),
    )


# ---------------------------------------------------------------------------
# Hiring signals (FR-225, FR-402)
# ---------------------------------------------------------------------------


def list_signals(company_id: str, limit: int = 100) -> list[dict]:
    return query_all(
        "SELECT * FROM hiring_signal WHERE company_id = ? "
        "ORDER BY COALESCE(occurred_at, collected_at) DESC LIMIT ?",
        (company_id, limit),
    )


def signals_since(company_id: str, since: str, limit: int = 200) -> list[dict]:
    return query_all(
        "SELECT * FROM hiring_signal WHERE company_id = ? "
        "AND COALESCE(occurred_at, collected_at) >= ? "
        "ORDER BY COALESCE(occurred_at, collected_at) DESC LIMIT ?",
        (company_id, since, limit),
    )


def find_signal(
    company_id: str, signal_type: str, occurred_at: str | None, source_url: str | None
) -> dict | None:
    """The natural key of a signal: what happened, when, and where it was read."""
    return query_one(
        "SELECT * FROM hiring_signal WHERE company_id = ? AND signal_type = ? "
        "AND IFNULL(occurred_at, '') = IFNULL(?, '') AND IFNULL(source_url, '') = IFNULL(?, '') "
        "LIMIT 1",
        (company_id, signal_type, occurred_at, source_url),
    )


def upsert_signal(data: dict) -> str:
    """Insert a signal, or refresh the one already recorded for the same event."""
    values = dict(data)
    values.setdefault("collected_at", utcnow())
    existing = find_signal(
        values["company_id"],
        values["signal_type"],
        values.get("occurred_at"),
        values.get("source_url"),
    )
    if existing:
        update_row(
            "hiring_signal",
            existing["id"],
            {
                "description": values.get("description") or existing["description"],
                "strength": max(
                    float(existing["strength"] or 0), float(values.get("strength") or 0)
                ),
                "collected_at": values["collected_at"],
            },
        )
        return str(existing["id"])
    return insert_row("hiring_signal", values)


def recent_vacancy_texts(company_id: str, limit: int = 12) -> list[dict]:
    """Job-advertisement language, which FR-384 reads for values and working style."""
    return query_all(
        "SELECT title, description, location, work_arrangement, posted_at, source_url "
        "FROM vacancy WHERE company_id = ? AND description IS NOT NULL "
        "ORDER BY COALESCE(posted_at, collected_at) DESC LIMIT ?",
        (company_id, limit),
    )


def recent_posting_stats(company_id: str, since: str) -> dict:
    """Recent vacancy volume and breadth - the 'postings' signal (FR-225)."""
    row = query_one(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT function_family) AS families, "
        "MAX(COALESCE(posted_at, collected_at)) AS latest "
        "FROM vacancy WHERE company_id = ? AND COALESCE(posted_at, collected_at) >= ?",
        (company_id, since),
    )
    if not row:
        return {"count": 0, "families": 0, "latest": None}
    return {
        "count": int(row["n"] or 0),
        "families": int(row["families"] or 0),
        "latest": row["latest"],
    }


# ---------------------------------------------------------------------------
# Financial summary for the profile view (FR-222)
# ---------------------------------------------------------------------------


def financial_years(company_id: str, limit: int = 5) -> list[dict]:
    return query_all(
        "SELECT * FROM financial_year WHERE company_id = ? ORDER BY fiscal_year DESC LIMIT ?",
        (company_id, limit),
    )


def financial_analysis(company_id: str) -> dict | None:
    return query_one("SELECT * FROM financial_analysis WHERE company_id = ?", (company_id,))


# ---------------------------------------------------------------------------
# Competitors and peers (FR-224)
# ---------------------------------------------------------------------------


def list_competitors(company_id: str, limit: int = 50) -> list[dict]:
    return query_all(
        """
        SELECT l.*, c.name AS peer_resolved_name, c.domain AS peer_domain,
               c.country AS peer_country, c.size_band AS peer_size_band,
               c.business_summary AS peer_summary
        FROM competitor_link l
        LEFT JOIN company c ON c.id = l.peer_company_id
        WHERE l.company_id = ?
        ORDER BY l.strength DESC, l.collected_at DESC
        LIMIT ?
        """,
        (company_id, limit),
    )


def get_competitor_link(link_id: str) -> dict | None:
    return query_one("SELECT * FROM competitor_link WHERE id = ?", (link_id,))


def find_competitor_link(
    company_id: str, basis: str, peer_company_id: str | None, peer_name: str | None
) -> dict | None:
    """The UNIQUE key does not cover unresolved peers, so match on the name too."""
    if peer_company_id:
        return query_one(
            "SELECT * FROM competitor_link WHERE company_id = ? AND basis = ? "
            "AND peer_company_id = ?",
            (company_id, basis, peer_company_id),
        )
    return query_one(
        "SELECT * FROM competitor_link WHERE company_id = ? AND basis = ? "
        "AND peer_company_id IS NULL AND LOWER(IFNULL(peer_name, '')) = LOWER(IFNULL(?, ''))",
        (company_id, basis, peer_name),
    )


def upsert_competitor_link(
    company_id: str,
    *,
    basis: str,
    strength: float,
    peer_company_id: str | None = None,
    peer_name: str | None = None,
) -> str:
    existing = find_competitor_link(company_id, basis, peer_company_id, peer_name)
    now = utcnow()
    if existing:
        update_row(
            "competitor_link",
            existing["id"],
            {
                "strength": max(float(existing["strength"] or 0), float(strength)),
                "peer_company_id": peer_company_id or existing["peer_company_id"],
                "peer_name": peer_name or existing["peer_name"],
                "collected_at": now,
            },
        )
        return str(existing["id"])
    return insert_row(
        "competitor_link",
        {
            "company_id": company_id,
            "peer_company_id": peer_company_id,
            "peer_name": peer_name,
            "basis": basis,
            "strength": float(strength),
            "collected_at": now,
        },
    )


# ---------------------------------------------------------------------------
# Adopting a peer into the job seeker's target list (FR-224, FR-401)
# ---------------------------------------------------------------------------


def is_watchlisted(job_seeker_id: str, company_id: str) -> bool:
    row = query_one(
        "SELECT id FROM watchlist_entry WHERE job_seeker_id = ? AND company_id = ?",
        (job_seeker_id, company_id),
    )
    return row is not None


def add_to_watchlist(job_seeker_id: str, company_id: str) -> str:
    """Put a suggested peer on this job seeker's target list.  Idempotent."""
    existing = query_one(
        "SELECT id FROM watchlist_entry WHERE job_seeker_id = ? AND company_id = ?",
        (job_seeker_id, company_id),
    )
    if existing:
        return str(existing["id"])
    return insert_row(
        "watchlist_entry",
        {
            "job_seeker_id": job_seeker_id,
            "company_id": company_id,
            "active": 1,
            "created_at": utcnow(),
        },
    )


def backfill_country_from_vacancies() -> int:
    """Give each country-less company the country most of its postings are in.

    The knowledge base records no country for 94% of companies, because ATS
    discovery wrote them from a board without one.  That is what let a Brussels
    search plan ATS boards for companies in Oslo, Illinois and Singapore - the
    planner could not see that they were out of scope.  A company is where most
    of its jobs are, so the vacancies already collected answer the question
    without a single network call.

    Only country-less companies are touched, so a value already established by
    the registries or the website crawler is never overwritten (DR-101).
    """
    with write_tx() as conn:
        cur = conn.execute(
            """
            UPDATE company SET country = (
                SELECT v.country FROM vacancy v
                WHERE v.company_id = company.id AND v.country IS NOT NULL AND v.country <> ''
                GROUP BY v.country
                ORDER BY COUNT(*) DESC, v.country
                LIMIT 1
            )
            WHERE (country IS NULL OR country = '')
              AND EXISTS (
                  SELECT 1 FROM vacancy v2
                  WHERE v2.company_id = company.id
                    AND v2.country IS NOT NULL AND v2.country <> ''
              )
            """
        )
        return cur.rowcount


def enrichment_coverage(limit: int = 200) -> dict[str, Any]:
    """What the knowledge base knows about each employer, and how fresh it is.

    The passes that build a company record - website crawl, filings, signals,
    competitors, the employer-kind verdict - each wrote somewhere different, so
    "is this employer enriched?" had no single answer and the company screen had
    no single place to say it.  This is that answer, in one read: aggregate
    counts for the operator, and per-company status for the list, ordered by how
    many postings the employer holds so the rows a job seeker sees come first.
    """
    aggregate = query_one(
        """
        SELECT
            (SELECT COUNT(*) FROM company WHERE suppressed = 0) AS companies,
            (SELECT COUNT(*) FROM company WHERE suppressed = 0
                AND business_summary IS NOT NULL AND business_summary <> '') AS with_profile,
            (SELECT COUNT(*) FROM financial_analysis) AS with_financials,
            (SELECT COUNT(*) FROM financial_analysis WHERE is_estimated = 0) AS with_filed_financials,
            (SELECT COUNT(DISTINCT company_id) FROM hiring_signal) AS with_signals,
            (SELECT COUNT(DISTINCT company_id) FROM competitor_link) AS with_competitors,
            (SELECT COUNT(*) FROM company_employer_kind) AS with_employer_kind,
            (SELECT COUNT(*) FROM company WHERE suppressed = 0 AND ats_slug IS NOT NULL)
                AS with_ats_board,
            (SELECT COUNT(*) FROM raw_document) AS raw_documents
        """
    ) or {}

    rows = query_all(
        """
        SELECT c.id, c.name, c.domain, c.country, c.collected_at, c.refreshed_at,
               CASE WHEN c.business_summary IS NOT NULL AND c.business_summary <> ''
                    THEN 1 ELSE 0 END AS has_profile,
               (SELECT COUNT(*) FROM financial_analysis fa WHERE fa.company_id = c.id)
                   AS financial_years,
               (SELECT COALESCE(fa.is_estimated, 1) FROM financial_analysis fa
                 WHERE fa.company_id = c.id LIMIT 1) AS financials_estimated,
               (SELECT COUNT(*) FROM hiring_signal hs WHERE hs.company_id = c.id) AS signals,
               (SELECT COUNT(*) FROM competitor_link cl WHERE cl.company_id = c.id) AS competitors,
               (SELECT k.kind FROM company_employer_kind k WHERE k.company_id = c.id LIMIT 1)
                   AS employer_kind,
               (SELECT COUNT(*) FROM opportunity o WHERE o.company_id = c.id) AS opportunities
        FROM company c
        WHERE c.suppressed = 0
          AND (EXISTS (SELECT 1 FROM opportunity o WHERE o.company_id = c.id)
            OR EXISTS (SELECT 1 FROM vacancy v WHERE v.company_id = c.id))
        ORDER BY opportunities DESC, signals DESC
        LIMIT ?
        """,
        (int(limit),),
    )

    def pct(n: int) -> float | None:
        total = int(aggregate.get("companies") or 0)
        return round(n / total, 4) if total else None

    return {
        "aggregate": {
            **{k: int(v or 0) for k, v in aggregate.items()},
            "share_with_profile": pct(int(aggregate.get("with_profile") or 0)),
            "share_with_financials": pct(int(aggregate.get("with_financials") or 0)),
            "share_with_signals": pct(int(aggregate.get("with_signals") or 0)),
            "share_with_competitors": pct(int(aggregate.get("with_competitors") or 0)),
            "share_with_employer_kind": pct(int(aggregate.get("with_employer_kind") or 0)),
        },
        "companies": [dict(r) for r in rows],
        "note": (
            "A company is enriched when it has a website profile; the filings, signals, "
            "competitors and employer kind are added on top. A financial analysis marked "
            "estimated means no filing could be read - usually a registry key that is not "
            "configured - not that the company has no accounts."
        ),
    }


def busiest_companies(limit: int = 25) -> list[str]:
    """Company ids with the most postings - the ones worth enriching first.

    The order that matters everywhere else in the product: a company holding a
    hundred vacancies makes a hundred rows better when it is researched, and one
    holding a single posting makes one.
    """
    rows = query_all(
        """
        SELECT v.company_id AS id, COUNT(*) AS n FROM vacancy v
        JOIN company c ON c.id = v.company_id
        WHERE v.company_id IS NOT NULL AND c.suppressed = 0
        GROUP BY v.company_id ORDER BY n DESC LIMIT ?
        """,
        (int(limit),),
    )
    return [str(r["id"]) for r in rows]
