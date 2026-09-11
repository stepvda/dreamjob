"""Shared knowledge-base SQL (FR-341, FR-343, FR-344, FR-345, NFR-402).

Everything in this module reads or writes a *shared* table: company, vacancy,
contact, financial_year, hiring_signal, competitor_link, event, provenance.
Shared rows are the knowledge base every campaign draws on, so:

* no statement here accepts or stores a ``job_seeker_id`` - ``sanitise`` raises
  rather than letting one through (FR-344);
* every write refreshes the FTS index that backs the campaign-independent
  browse/search interface (FR-345);
* every write carries freshness metadata so the planner can decide reuse
  against the staleness policy (FR-343).
"""

from __future__ import annotations

import re
from typing import Any

from dreamjob.db.connection import (
    from_json,
    new_id,
    query_all,
    query_one,
    to_json,
    utcnow,
    write_tx,
)

# FR-344: a shared row must never carry a link back to the job seeker whose
# campaign produced it.  This is enforced on write, not left to convention.
FORBIDDEN_SHARED_COLUMNS = frozenset({"job_seeker_id", "seeker_id", "job_seeker", "owner_id"})

_COLUMN_CACHE: dict[str, frozenset[str]] = {}
_FTS_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÿ]+")


def columns(table: str) -> frozenset[str]:
    """Column names of a table, cached.  The schema is append-only (CR-408)."""
    cached = _COLUMN_CACHE.get(table)
    if cached:
        return cached
    cols = frozenset(r["name"] for r in query_all(f"PRAGMA table_info({table})"))
    if not cols:
        raise RuntimeError(f"Table {table!r} does not exist - run the migrations first")
    _COLUMN_CACHE[table] = cols
    return cols


def sanitise(table: str, data: dict) -> dict:
    """Keep only real columns; refuse any job-seeker link on a shared row (FR-344)."""
    for key in data:
        if key.lower() in FORBIDDEN_SHARED_COLUMNS:
            raise ValueError(
                f"FR-344: shared table {table!r} must not carry {key!r}; "
                "private data belongs in a table with its own job_seeker_id column"
            )
    known = columns(table)
    return {k: v for k, v in data.items() if k in known}


def _encode(values: dict) -> dict:
    return {k: (to_json(v) if isinstance(v, (dict, list)) else v) for k, v in values.items()}


def _insert(conn: Any, table: str, values: dict) -> str:
    values = dict(values)
    values.setdefault("id", new_id())
    payload = _encode(values)
    cols = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", payload)
    return str(values["id"])


def _update(conn: Any, table: str, row_id: str, values: dict) -> None:
    if not values:
        return
    payload = _encode(values)
    sets = ", ".join(f"{k}=:{k}" for k in payload)
    payload["__id"] = row_id
    conn.execute(f"UPDATE {table} SET {sets} WHERE id=:__id", payload)


def _flatten(value: Any) -> str:
    """Render a JSON column as plain text for the FTS index."""
    parsed = from_json(value, value)
    if parsed is None:
        return ""
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        return " ".join(_flatten(v) for v in parsed.values())
    if isinstance(parsed, (list, tuple)):
        return " ".join(_flatten(v) for v in parsed)
    return str(parsed)


# ---------------------------------------------------------------------------
# Full-text index (FR-345)
# ---------------------------------------------------------------------------


def _index_company(conn: Any, row: dict) -> None:
    """Refresh one company's entry in ``company_fts`` (FR-345).

    Keyed by ``company.rowid``, not by ``company_id``.  ``company_id`` is
    declared UNINDEXED, which means FTS5 stores the value and indexes nothing,
    so ``DELETE ... WHERE company_id = ?`` had no index to seek on and SQLite
    scanned the whole table for every company written - the plan said ``SCAN
    company_fts VIRTUAL TABLE INDEX 0:``, and the cost grew with the corpus
    (6,674 virtual-machine steps per re-index at 500 companies, 52,174 at
    4,000).  FTS5 does seek on rowid: ``INDEX 0:=``, 164 steps at either size.

    That is the whole of the change.  The declaration, the tokens and the
    ``company_id`` the two search queries join on are exactly as they were, so
    a company search answers what it answered before - which is the point.
    ``vacancy_fts`` had the identical defect at 35,159 rows of 3.9 KiB each and
    is fixed differently in migration 133, as an external-content index
    maintained by triggers.  ``company_fts`` cannot follow it there:
    ``products_services`` is JSON that ``_flatten`` renders to prose before it
    is indexed, and an external-content table re-reads the base column, so
    every company would start matching a search for "description".  A change to
    what a search answers has no business travelling inside a performance fix.

    What the seek costs is an invariant somebody has to keep: every
    ``company_fts`` entry sits at the ``company.rowid`` of the company it
    describes.  Nothing in the product breaks it - the rebuild in migration 133
    establishes it and every write here maintains it - but SQLite renumbers the
    rowids of a TEXT-keyed table on a restore from ``.dump`` (measured: 15 of
    17), and the index is not told.  A misaligned entry does not go stale, it
    goes wrong and stays silent: the DELETE below removes whichever company now
    occupies that rowid, so that company disappears from search while this one
    ends up indexed twice, and every later write repeats it.  The repair is
    :func:`reindex_all`, which re-keys both indexes; ``'rebuild'`` on this
    table is accepted and repairs nothing, because a standalone FTS5 table
    rebuilds from its own shadow copy and keeps the rowids it already had.
    Prefer a binary ``.backup`` over a dump, which preserves rowids and needs
    none of this.
    """
    keyed = conn.execute("SELECT rowid FROM company WHERE id = ?", (row["id"],)).fetchone()
    if keyed is None:
        return
    rowid = keyed[0]
    conn.execute("DELETE FROM company_fts WHERE rowid = ?", (rowid,))
    conn.execute(
        "INSERT INTO company_fts (rowid, company_id, name, normalised_name, "
        "business_summary, products_services) VALUES (?, ?, ?, ?, ?, ?)",
        (
            rowid,
            row["id"],
            row.get("name") or "",
            row.get("normalised_name") or "",
            row.get("business_summary") or "",
            _flatten(row.get("products_services")),
        ),
    )


def reindex_all() -> dict[str, int]:
    """Rebuild both FTS tables from the base tables (FR-345 maintenance).

    ``vacancy_fts`` is an external-content index (migration 133), so it is
    rebuilt with FTS5's own command rather than emptied and refilled row by
    row.  Do not put a bare ``DELETE FROM vacancy_fts`` back: it does not raise
    against such a table, it silently empties the index and leaves every search
    answering nothing.

    This is also the repair for rowid drift.  Both indexes are addressed by
    their base table's rowid since migration 133, and a restore from ``.dump``
    renumbers those rowids without telling either index - after which a company
    write deletes another company's entry and a vacancy's words answer for its
    neighbour.  Running this refills ``company_fts`` from ``company`` keyed by
    ``company.rowid`` and re-reads ``vacancy`` through the tokeniser, so it puts
    both correspondences back.  Nothing calls it on a schedule; it is what an
    operator runs after a restore.
    """
    counts = {"company": 0, "vacancy": 0}
    with write_tx() as conn:
        conn.execute("DELETE FROM company_fts")
        for row in query_all("SELECT * FROM company"):
            _index_company(conn, row)
            counts["company"] += 1
        conn.execute("INSERT INTO vacancy_fts (vacancy_fts) VALUES ('rebuild')")
        counts["vacancy"] = int(
            (conn.execute("SELECT COUNT(*) AS n FROM vacancy").fetchone() or {"n": 0})["n"]
        )
    return counts


def fts_query(text: str | None) -> str | None:
    """Turn free user input into a safe FTS5 MATCH expression (prefix on the last term)."""
    if not text:
        return None
    terms = _FTS_TOKEN.findall(text)
    if not terms:
        return None
    quoted = [f'"{t}"' for t in terms[:-1]]
    quoted.append(f'"{terms[-1]}"*')
    return " AND ".join(quoted)


# ---------------------------------------------------------------------------
# Company writes and identity lookups (FR-184, DR-101)
# ---------------------------------------------------------------------------


def insert_company(data: dict) -> str:
    values = sanitise("company", data)
    values.setdefault("collected_at", utcnow())
    with write_tx() as conn:
        company_id = _insert(conn, "company", values)
        values["id"] = company_id
        _index_company(conn, values)
    return company_id


def update_company(company_id: str, data: dict) -> None:
    values = sanitise("company", data)
    values.pop("id", None)
    with write_tx() as conn:
        _update(conn, "company", company_id, values)
        row = conn.execute("SELECT * FROM company WHERE id = ?", (company_id,)).fetchone()
        if row is not None:
            _index_company(conn, dict(row))


def get_company(company_id: str) -> dict | None:
    return query_one("SELECT * FROM company WHERE id = ?", (company_id,))


def company_by_legal_id(legal_id: str, legal_id_type: str | None = None) -> dict | None:
    if legal_id_type:
        return query_one(
            "SELECT * FROM company WHERE legal_id = ? AND legal_id_type = ?",
            (legal_id, legal_id_type),
        )
    return query_one("SELECT * FROM company WHERE legal_id = ?", (legal_id,))


def company_by_vat(vat_number: str) -> dict | None:
    return query_one("SELECT * FROM company WHERE vat_number = ?", (vat_number,))


def company_by_domain(domain: str) -> dict | None:
    return query_one(
        "SELECT * FROM company WHERE domain = ? ORDER BY collected_at LIMIT 1", (domain,)
    )


def companies_by_normalised_name(
    normalised_name: str, country: str | None = None, limit: int = 25
) -> list[dict]:
    if country:
        return query_all(
            "SELECT * FROM company WHERE normalised_name = ? AND (country = ? OR country IS NULL) "
            "LIMIT ?",
            (normalised_name, country, limit),
        )
    return query_all(
        "SELECT * FROM company WHERE normalised_name = ? LIMIT ?", (normalised_name, limit)
    )


def company_name_candidates(prefix: str, country: str | None = None, limit: int = 50) -> list[dict]:
    """Cheap candidate set for the fuzzy fall-back: same first word, same country."""
    like = f"{prefix}%"
    if country:
        return query_all(
            "SELECT * FROM company WHERE normalised_name LIKE ? "
            "AND (country = ? OR country IS NULL) LIMIT ?",
            (like, country, limit),
        )
    return query_all("SELECT * FROM company WHERE normalised_name LIKE ? LIMIT ?", (like, limit))


# ---------------------------------------------------------------------------
# Vacancy writes and identity lookups (FR-184)
# ---------------------------------------------------------------------------


def insert_vacancy(data: dict) -> str:
    values = sanitise("vacancy", data)
    values.setdefault("collected_at", utcnow())
    with write_tx() as conn:
        vacancy_id = _insert(conn, "vacancy", values)
    return vacancy_id


def update_vacancy(vacancy_id: str, data: dict) -> None:
    values = sanitise("vacancy", data)
    values.pop("id", None)
    with write_tx() as conn:
        _update(conn, "vacancy", vacancy_id, values)


def get_vacancy(vacancy_id: str) -> dict | None:
    return query_one("SELECT * FROM vacancy WHERE id = ?", (vacancy_id,))


def vacancy_by_dedup_key(dedup_key: str) -> dict | None:
    return query_one(
        "SELECT * FROM vacancy WHERE dedup_key = ? ORDER BY collected_at DESC LIMIT 1",
        (dedup_key,),
    )


def vacancy_candidates(
    company_id: str | None, company_name_raw: str | None, since: str | None, limit: int = 50
) -> list[dict]:
    """Recent vacancies of the same employer, for the fuzzy comparison (FR-184)."""
    where = []
    params: list[Any] = []
    if company_id:
        where.append("company_id = ?")
        params.append(company_id)
    elif company_name_raw:
        where.append("company_name_raw = ?")
        params.append(company_name_raw)
    else:
        return []
    if since:
        where.append("collected_at >= ?")
        params.append(since)
    params.append(limit)
    return query_all(
        f"SELECT * FROM vacancy WHERE {' AND '.join(where)} ORDER BY collected_at DESC LIMIT ?",
        tuple(params),
    )


# ---------------------------------------------------------------------------
# Generic shared writes
# ---------------------------------------------------------------------------

WRITABLE_TABLES = frozenset(
    {
        "company", "vacancy", "contact", "financial_year", "hiring_signal",
        "competitor_link", "event", "compensation_observation",
    }
)


def insert_shared(table: str, data: dict) -> str:
    if table not in WRITABLE_TABLES:
        raise ValueError(f"{table!r} is not a shared knowledge-base table")
    values = sanitise(table, data)
    values.setdefault("collected_at", utcnow())
    with write_tx() as conn:
        return _insert(conn, table, values)


def contact_by_email(email: str) -> dict | None:
    return query_one("SELECT * FROM contact WHERE email = ? LIMIT 1", (email,))


# ---------------------------------------------------------------------------
# Provenance (FR-166, NFR-402)
# ---------------------------------------------------------------------------


def record_provenance(
    entity_type: str,
    entity_id: str,
    *,
    source_plan_item_id: str | None = None,
    raw_document_id: str | None = None,
    adapter_key: str | None = None,
    field_path: str | None = None,
    confidence: float | None = None,
) -> str:
    with write_tx() as conn:
        return _insert(
            conn,
            "provenance",
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "source_plan_item_id": source_plan_item_id,
                "raw_document_id": raw_document_id,
                "adapter_key": adapter_key,
                "field_path": field_path,
                "confidence": confidence,
                "created_at": utcnow(),
            },
        )


def provenance_for(entity_type: str, entity_id: str) -> list[dict]:
    return query_all(
        "SELECT * FROM provenance WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
        (entity_type, entity_id),
    )


# ---------------------------------------------------------------------------
# Freshness and reuse accounting (FR-342, FR-343)
# ---------------------------------------------------------------------------

_FRESHNESS_COLUMN = {"company": "collected_at", "vacancy": "collected_at"}


def count_fresh(entity_type: str, since: str, countries: list[str] | None = None) -> int:
    """How many rows of this type were collected on or after ``since``."""
    if entity_type not in _FRESHNESS_COLUMN:
        return 0
    column = _FRESHNESS_COLUMN[entity_type]
    sql = f"SELECT COUNT(*) AS n FROM {entity_type} WHERE COALESCE(refreshed_at, {column}) >= ?"
    params: list[Any] = [since]
    if entity_type == "vacancy":
        sql = f"SELECT COUNT(*) AS n FROM vacancy WHERE {column} >= ?"
    if countries:
        marks = ", ".join("?" for _ in countries)
        sql += f" AND (country IN ({marks}) OR country IS NULL)"
        params.extend(c.upper() for c in countries)
    row = query_one(sql, tuple(params))
    return int(row["n"]) if row else 0


def fresh_counts_by_adapter(entity_type: str, since: str) -> dict[str, int]:
    """Distinct entities each adapter contributed since ``since`` (via provenance)."""
    rows = query_all(
        "SELECT adapter_key, COUNT(DISTINCT entity_id) AS n FROM provenance "
        "WHERE entity_type = ? AND created_at >= ? AND adapter_key IS NOT NULL "
        "GROUP BY adapter_key",
        (entity_type, since),
    )
    return {r["adapter_key"]: int(r["n"]) for r in rows}


def fresh_vacancy_counts_by_source(
    since: str, countries: list[str] | None = None
) -> dict[str, int]:
    sql = "SELECT source_adapter AS k, COUNT(*) AS n FROM vacancy WHERE collected_at >= ?"
    params: list[Any] = [since]
    if countries:
        marks = ", ".join("?" for _ in countries)
        sql += f" AND (country IN ({marks}) OR country IS NULL)"
        params.extend(c.upper() for c in countries)
    sql += " GROUP BY source_adapter"
    return {r["k"] or "": int(r["n"]) for r in query_all(sql, tuple(params))}


def stale_rows(entity_type: str, cutoff: str, limit: int = 200) -> list[dict]:
    """Rows whose freshness has fallen outside the staleness policy (FR-343)."""
    if entity_type == "company":
        return query_all(
            "SELECT * FROM company WHERE COALESCE(refreshed_at, collected_at) < ? "
            "ORDER BY COALESCE(refreshed_at, collected_at) LIMIT ?",
            (cutoff, limit),
        )
    if entity_type == "vacancy":
        return query_all(
            "SELECT * FROM vacancy WHERE collected_at < ? ORDER BY collected_at LIMIT ?",
            (cutoff, limit),
        )
    return []


# ---------------------------------------------------------------------------
# Campaign-independent browse / search (FR-345)
# ---------------------------------------------------------------------------


def search_companies(
    q: str | None = None,
    *,
    country: str | None = None,
    sector: str | None = None,
    size_band: str | None = None,
    ats_vendor: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    match = fts_query(q)
    if match:
        sql = (
            "SELECT c.*, bm25(company_fts) AS rank FROM company_fts f "
            "JOIN company c ON c.id = f.company_id WHERE company_fts MATCH ?"
        )
        params.append(match)
    else:
        sql = "SELECT c.* FROM company c WHERE 1 = 1"
    if country:
        where.append("c.country = ?")
        params.append(country.upper())
    if size_band:
        where.append("c.size_band = ?")
        params.append(size_band)
    if sector:
        where.append("c.sector_codes LIKE ?")
        params.append(f"%{sector}%")
    # "any" answers "which of these did we reach through an ATS board at all",
    # which is the question a corpus with one ATS-derived company could not be
    # asked before.
    if ats_vendor == "any":
        where.append("c.ats_vendor IS NOT NULL")
    elif ats_vendor == "none":
        where.append("c.ats_vendor IS NULL")
    elif ats_vendor:
        where.append("c.ats_vendor = ?")
        params.append(ats_vendor.lower())
    if where:
        sql += " AND " + " AND ".join(where)
    sql += " ORDER BY rank" if match else " ORDER BY COALESCE(c.refreshed_at, c.collected_at) DESC"
    sql += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return query_all(sql, tuple(params))


def count_companies(
    q: str | None = None,
    *,
    country: str | None = None,
    sector: str | None = None,
    size_band: str | None = None,
    ats_vendor: str | None = None,
) -> int:
    where: list[str] = []
    params: list[Any] = []
    match = fts_query(q)
    if match:
        sql = (
            "SELECT COUNT(*) AS n FROM company_fts f JOIN company c ON c.id = f.company_id "
            "WHERE company_fts MATCH ?"
        )
        params.append(match)
    else:
        sql = "SELECT COUNT(*) AS n FROM company c WHERE 1 = 1"
    if country:
        where.append("c.country = ?")
        params.append(country.upper())
    if size_band:
        where.append("c.size_band = ?")
        params.append(size_band)
    if sector:
        where.append("c.sector_codes LIKE ?")
        params.append(f"%{sector}%")
    if ats_vendor == "any":
        where.append("c.ats_vendor IS NOT NULL")
    elif ats_vendor == "none":
        where.append("c.ats_vendor IS NULL")
    elif ats_vendor:
        where.append("c.ats_vendor = ?")
        params.append(ats_vendor.lower())
    if where:
        sql += " AND " + " AND ".join(where)
    row = query_one(sql, tuple(params))
    return int(row["n"]) if row else 0


def company_facets() -> dict[str, Any]:
    """Where the company inventory came from, in one query each (FR-345).

    This exists because a corpus can look healthy at 1,337 rows and still hold
    one company reached through an ATS board and none of the large employers a
    search is judged on.  Nothing on any screen said so.  Counting by source and
    by ATS vendor makes a starved harvest stage visible without reading the
    plan-item table.
    """
    def _counts(sql: str) -> dict[str, int]:
        return {str(r["k"] or "unknown"): int(r["n"]) for r in query_all(sql)}

    return {
        "total": int((query_one("SELECT COUNT(*) AS n FROM company") or {"n": 0})["n"]),
        "by_source": _counts(
            "SELECT source AS k, COUNT(*) AS n FROM company GROUP BY source "
            "ORDER BY n DESC LIMIT 25"
        ),
        "by_country": _counts(
            "SELECT country AS k, COUNT(*) AS n FROM company GROUP BY country "
            "ORDER BY n DESC LIMIT 25"
        ),
        "by_ats_vendor": _counts(
            "SELECT ats_vendor AS k, COUNT(*) AS n FROM company "
            "WHERE ats_vendor IS NOT NULL GROUP BY ats_vendor ORDER BY n DESC"
        ),
        "with_ats_board": int(
            (query_one("SELECT COUNT(*) AS n FROM company WHERE ats_slug IS NOT NULL")
             or {"n": 0})["n"]
        ),
    }


def search_vacancies(
    q: str | None = None,
    *,
    country: str | None = None,
    company_id: str | None = None,
    since: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    match = fts_query(q)
    if match:
        sql = (
            "SELECT v.*, bm25(vacancy_fts) AS rank FROM vacancy_fts f "
            "JOIN vacancy v ON v.rowid = f.rowid WHERE vacancy_fts MATCH ?"
        )
        params.append(match)
    else:
        sql = "SELECT v.* FROM vacancy v WHERE 1 = 1"
    if country:
        where.append("v.country = ?")
        params.append(country.upper())
    if company_id:
        where.append("v.company_id = ?")
        params.append(company_id)
    if since:
        where.append("v.collected_at >= ?")
        params.append(since)
    if where:
        sql += " AND " + " AND ".join(where)
    sql += " ORDER BY rank" if match else " ORDER BY COALESCE(v.posted_at, v.collected_at) DESC"
    sql += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return query_all(sql, tuple(params))


def count_vacancies(
    q: str | None = None,
    *,
    country: str | None = None,
    company_id: str | None = None,
    since: str | None = None,
) -> int:
    where: list[str] = []
    params: list[Any] = []
    match = fts_query(q)
    if match:
        sql = (
            "SELECT COUNT(*) AS n FROM vacancy_fts f JOIN vacancy v ON v.rowid = f.rowid "
            "WHERE vacancy_fts MATCH ?"
        )
        params.append(match)
    else:
        sql = "SELECT COUNT(*) AS n FROM vacancy v WHERE 1 = 1"
    if country:
        where.append("v.country = ?")
        params.append(country.upper())
    if company_id:
        where.append("v.company_id = ?")
        params.append(company_id)
    if since:
        where.append("v.collected_at >= ?")
        params.append(since)
    if where:
        sql += " AND " + " AND ".join(where)
    row = query_one(sql, tuple(params))
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Application settings (FR-343 staleness policy lives here)
# ---------------------------------------------------------------------------


def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM app_setting WHERE key = ?", (key,))
    if row is None:
        return default
    return from_json(row["value"], row["value"])


def set_setting(key: str, value: Any) -> None:
    payload = to_json(value) if isinstance(value, (dict, list)) else str(value)
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO app_setting (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, payload, utcnow()),
        )


# ---------------------------------------------------------------------------
# Generic shared lookups used by the knowledge-base writer
# ---------------------------------------------------------------------------


def find_shared(table: str, criteria: dict) -> dict | None:
    """Look a shared row up by its natural key (column names are validated)."""
    known = columns(table)
    clauses: list[str] = []
    params: list[Any] = []
    for key, value in criteria.items():
        if key not in known:
            raise ValueError(f"{key!r} is not a column of {table!r}")
        if value is None:
            clauses.append(f"{key} IS NULL")
        else:
            clauses.append(f"{key} = ?")
            params.append(value)
    if not clauses:
        return None
    return query_one(f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} LIMIT 1", tuple(params))


def update_shared(table: str, row_id: str, data: dict) -> None:
    """Update any shared row, keeping the FTS index in step (FR-345)."""
    if table == "company":
        update_company(row_id, data)
        return
    if table == "vacancy":
        update_vacancy(row_id, data)
        return
    values = sanitise(table, data)
    values.pop("id", None)
    with write_tx() as conn:
        _update(conn, table, row_id, values)


def company_id_for_board(
    vendor: str | None, slug: str | None, *, exclude: str | None = None
) -> str | None:
    """The company already holding this ATS board, if any.

    ``uq_company_ats_board`` makes (ats_vendor, ats_slug) unique.  A profile
    write that claims a board another row holds fails the whole UPDATE and
    rolls back, so every field in the same write is lost - which is how ten of
    fifteen company profiles were failing and storing nothing.  The caller asks
    this first and leaves the board where it is.
    """
    if not vendor or not slug:
        return None
    row = query_one(
        "SELECT id FROM company WHERE ats_vendor = ? AND ats_slug = ? AND id <> ? LIMIT 1",
        (vendor, slug, exclude or ""),
    )
    return str(row["id"]) if row else None
