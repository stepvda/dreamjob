"""Financial-filing SQL (FR-241..246, DR-103, NFR-404, FR-344).

``financial_year``, ``financial_analysis`` and ``company_group_link`` are
*shared* knowledge-base tables: a filing is a fact about a company, not about
the job seeker whose campaign happened to pay for the fetch.  Every write here
goes through :func:`dreamjob.db.repositories.knowledge.sanitise`, which refuses
a job-seeker link outright (FR-344).

One row per company and financial year (the schema's UNIQUE key), upserted so
that a later, better extraction of the same year replaces a thinner one instead
of duplicating it.
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    new_id,
    query_all,
    query_one,
    to_json,
    utcnow,
    write_tx,
)
from dreamjob.db.repositories.knowledge import sanitise

# Columns a re-extraction is allowed to blank out.  Everything else is merged:
# an abbreviated filing that omits turnover must not erase a turnover we
# already learned from a press release or a consolidated account (FR-245).
_MERGEABLE = (
    "period_end", "currency", "reporting_standard", "fx_rate_to_eur", "fx_date",
    "revenue", "gross_profit", "gross_margin", "ebit", "ebitda", "net_result",
    "equity", "cash", "total_debt", "total_assets", "current_assets",
    "current_liabilities", "headcount_fte", "personnel_costs", "capex",
    "filing_document_id", "source",
)


# ---------------------------------------------------------------------------
# financial_year (FR-241, FR-242, DR-103)
# ---------------------------------------------------------------------------


def get_financial_year(company_id: str, fiscal_year: int) -> dict | None:
    return query_one(
        "SELECT * FROM financial_year WHERE company_id = ? AND fiscal_year = ?",
        (company_id, int(fiscal_year)),
    )


def financial_years(company_id: str, limit: int = 5) -> list[dict]:
    """The most recent ``limit`` years, oldest first - the order the trend needs."""
    rows = query_all(
        "SELECT * FROM financial_year WHERE company_id = ? ORDER BY fiscal_year DESC LIMIT ?",
        (company_id, int(limit)),
    )
    return sorted(rows, key=lambda r: int(r["fiscal_year"]))


def years_present(company_id: str) -> list[int]:
    rows = query_all(
        "SELECT fiscal_year FROM financial_year WHERE company_id = ? ORDER BY fiscal_year",
        (company_id,),
    )
    return [int(r["fiscal_year"]) for r in rows]


def upsert_financial_year(data: dict) -> str:
    """Insert or merge one filing year.  Returns the row id (FR-241, NFR-404)."""
    values = sanitise("financial_year", data)
    company_id = values.get("company_id")
    fiscal_year = values.get("fiscal_year")
    if not company_id or fiscal_year is None:
        raise ValueError("financial_year needs company_id and fiscal_year")
    values["fiscal_year"] = int(fiscal_year)
    values.setdefault("collected_at", utcnow())

    existing = get_financial_year(str(company_id), values["fiscal_year"])
    payload = {
        k: (to_json(v) if isinstance(v, (dict, list)) else v)
        for k, v in values.items()
        if k != "id"
    }
    with write_tx() as conn:
        if existing is None:
            payload["id"] = values.get("id") or new_id()
            cols = ", ".join(payload)
            marks = ", ".join(f":{k}" for k in payload)
            conn.execute(f"INSERT INTO financial_year ({cols}) VALUES ({marks})", payload)
            return str(payload["id"])

        merged = {
            k: v
            for k, v in payload.items()
            if v is not None or k not in _MERGEABLE
        }
        merged.pop("company_id", None)
        merged.pop("fiscal_year", None)
        if not merged:
            return str(existing["id"])
        sets = ", ".join(f"{k}=:{k}" for k in merged)
        merged["__id"] = existing["id"]
        conn.execute(f"UPDATE financial_year SET {sets} WHERE id=:__id", merged)
        return str(existing["id"])


def mark_years_estimated(company_id: str, estimated: bool = True) -> int:
    """FR-245: flag every stored year of a company as estimated."""
    with write_tx() as conn:
        cur = conn.execute(
            "UPDATE financial_year SET is_estimated = ? WHERE company_id = ?",
            (1 if estimated else 0, company_id),
        )
        return cur.rowcount


def companies_missing_filings(
    *,
    jurisdiction: str | None = None,
    country: str | None = None,
    min_years: int = 3,
    limit: int = 50,
) -> list[dict]:
    """Companies in a jurisdiction with fewer than ``min_years`` filings stored.

    This is what the registry adapters plan against (FR-162): collecting a
    filing we already hold is exactly the waste FR-342 exists to avoid.
    """
    where = ["1 = 1"]
    params: list[Any] = []
    if jurisdiction:
        where.append("(c.jurisdiction = ? OR c.country = ?)")
        params.extend([jurisdiction.upper(), jurisdiction.upper()])
    elif country:
        where.append("c.country = ?")
        params.append(country.upper())
    params.extend([int(min_years), int(limit)])
    return query_all(
        f"""
        SELECT c.*, COUNT(f.id) AS years_stored
        FROM company c
        LEFT JOIN financial_year f ON f.company_id = c.id
        WHERE {' AND '.join(where)}
        GROUP BY c.id
        HAVING years_stored < ?
        ORDER BY years_stored ASC, COALESCE(c.refreshed_at, c.collected_at) DESC
        LIMIT ?
        """,
        tuple(params),
    )


def company(company_id: str) -> dict | None:
    return query_one("SELECT * FROM company WHERE id = ?", (company_id,))


def raw_document(document_id: str) -> dict | None:
    """The stored filing behind a financial year (DR-102, FR-183)."""
    return query_one("SELECT * FROM raw_document WHERE id = ?", (document_id,))


def hiring_signals(company_id: str, limit: int = 40) -> list[dict]:
    """Secondary signals used when filings are unavailable (FR-245)."""
    return query_all(
        "SELECT * FROM hiring_signal WHERE company_id = ? "
        "ORDER BY COALESCE(occurred_at, collected_at) DESC LIMIT ?",
        (company_id, int(limit)),
    )


# ---------------------------------------------------------------------------
# financial_analysis (FR-243, FR-244)
# ---------------------------------------------------------------------------


def get_analysis(company_id: str) -> dict | None:
    return query_one("SELECT * FROM financial_analysis WHERE company_id = ?", (company_id,))


def upsert_analysis(data: dict) -> str:
    values = sanitise("financial_analysis", data)
    if not values.get("company_id"):
        raise ValueError("financial_analysis needs company_id")
    values.setdefault("computed_at", utcnow())
    payload = {
        k: (to_json(v) if isinstance(v, (dict, list)) else v)
        for k, v in values.items()
        if k != "id"
    }
    existing = get_analysis(str(values["company_id"]))
    with write_tx() as conn:
        if existing is None:
            payload["id"] = values.get("id") or new_id()
            cols = ", ".join(payload)
            marks = ", ".join(f":{k}" for k in payload)
            conn.execute(f"INSERT INTO financial_analysis ({cols}) VALUES ({marks})", payload)
            return str(payload["id"])
        payload.pop("company_id", None)
        sets = ", ".join(f"{k}=:{k}" for k in payload)
        payload["__id"] = existing["id"]
        conn.execute(f"UPDATE financial_analysis SET {sets} WHERE id=:__id", payload)
        return str(existing["id"])


def analyses_for(company_ids: list[str]) -> dict[str, dict]:
    """Bulk read for the opportunity scorer (FR-281) and the negotiation brief (FR-444)."""
    if not company_ids:
        return {}
    marks = ", ".join("?" for _ in company_ids)
    rows = query_all(
        f"SELECT * FROM financial_analysis WHERE company_id IN ({marks})", tuple(company_ids)
    )
    return {r["company_id"]: r for r in rows}


def stale_analyses(cutoff: str, limit: int = 100) -> list[dict]:
    return query_all(
        "SELECT * FROM financial_analysis WHERE computed_at < ? ORDER BY computed_at LIMIT ?",
        (cutoff, int(limit)),
    )


# ---------------------------------------------------------------------------
# Group structure (FR-246)
# ---------------------------------------------------------------------------

#: Relations that put a company *inside* a group and therefore inside the
#: consolidated total.  Everything else the table carries - a former name, an
#: alias - is identity, not ownership, and must never be summed (FR-246).
GROUP_RELATIONS: tuple[str, ...] = ("subsidiary", "branch", "participation")



def link_subsidiary(
    parent_company_id: str,
    *,
    subsidiary_company_id: str | None = None,
    subsidiary_key: str,
    subsidiary_name: str | None = None,
    subsidiary_legal_id: str | None = None,
    relation: str = "subsidiary",
    ownership_pct: float | None = None,
    source: str | None = None,
) -> str:
    row_id = new_id()
    with write_tx() as conn:
        conn.execute(
            """
            INSERT INTO company_group_link
                (id, parent_company_id, subsidiary_company_id, subsidiary_key,
                 subsidiary_name, subsidiary_legal_id, relation, ownership_pct,
                 source, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(parent_company_id, subsidiary_key) DO UPDATE SET
                subsidiary_company_id = COALESCE(
                    excluded.subsidiary_company_id, company_group_link.subsidiary_company_id),
                subsidiary_name = COALESCE(
                    excluded.subsidiary_name, company_group_link.subsidiary_name),
                subsidiary_legal_id = COALESCE(
                    excluded.subsidiary_legal_id, company_group_link.subsidiary_legal_id),
                relation = excluded.relation,
                ownership_pct = COALESCE(
                    excluded.ownership_pct, company_group_link.ownership_pct),
                source = excluded.source,
                collected_at = excluded.collected_at
            """,
            (
                row_id, parent_company_id, subsidiary_company_id, subsidiary_key,
                subsidiary_name, subsidiary_legal_id, relation, ownership_pct,
                source, utcnow(),
            ),
        )
    existing = query_one(
        "SELECT id FROM company_group_link WHERE parent_company_id = ? AND subsidiary_key = ?",
        (parent_company_id, subsidiary_key),
    )
    return str(existing["id"]) if existing else row_id


def subsidiaries(parent_company_id: str, *, relations: tuple[str, ...] | None = None) -> list[dict]:
    """The group members below a parent.

    Only ownership relations count by default.  ``company_group_link`` also
    carries aliases a registry volunteers alongside the ownership graph - SEC
    ``formerNames``, for one - and a former name summed into a consolidated
    picture would double the group's turnover (FR-246).
    """
    wanted = GROUP_RELATIONS if relations is None else tuple(relations)
    marks = ", ".join("?" for _ in wanted)
    return query_all(
        f"SELECT * FROM company_group_link WHERE parent_company_id = ? "
        f"AND relation IN ({marks}) ORDER BY subsidiary_name",
        (parent_company_id, *wanted),
    )


def group_aliases(company_id: str) -> list[dict]:
    """The non-ownership names a registry recorded for a company (DR-101)."""
    return query_all(
        "SELECT * FROM company_group_link WHERE parent_company_id = ? "
        "AND relation NOT IN ('subsidiary', 'branch', 'participation') "
        "ORDER BY subsidiary_name",
        (company_id,),
    )


def parents_of(company_id: str) -> list[dict]:
    marks = ", ".join("?" for _ in GROUP_RELATIONS)
    return query_all(
        f"SELECT * FROM company_group_link WHERE subsidiary_company_id = ? "
        f"AND relation IN ({marks})",
        (company_id, *GROUP_RELATIONS),
    )


def group_financial_years(parent_company_id: str, limit: int = 5) -> list[dict]:
    """Every stored filing year of the parent and of its resolved subsidiaries."""
    marks = ", ".join("?" for _ in GROUP_RELATIONS)
    rows = query_all(
        f"""
        SELECT f.* FROM financial_year f
        WHERE f.company_id = ?
           OR f.company_id IN (
                SELECT subsidiary_company_id FROM company_group_link
                WHERE parent_company_id = ?
                  AND subsidiary_company_id IS NOT NULL
                  AND subsidiary_company_id <> ?
                  AND relation IN ({marks}))
        ORDER BY f.fiscal_year DESC
        """,
        (parent_company_id, parent_company_id, parent_company_id, *GROUP_RELATIONS),
    )
    if not rows:
        return []
    keep = sorted({int(r["fiscal_year"]) for r in rows}, reverse=True)[: int(limit)]
    return [r for r in rows if int(r["fiscal_year"]) in set(keep)]
