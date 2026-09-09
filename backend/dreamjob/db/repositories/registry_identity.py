"""Registry identity, and the domains the identity gate had to take back.

Two questions, one module, because they are the same question asked of two
sources: **is this row about the company we think it is?**

* ``company_registry_identity`` - which legal entity a company name resolved
  to in the enterprise register, by which rule, and when the register refused
  to say.  DR-101 puts the enterprise number first in the key priority order,
  so anchoring it on the wrong entity poisons the de-duplicator (FR-184), the
  NBB filings (FR-241) and - because NACE 78.x is precision 1.00 for a
  staffing agency once the entity is right - the employer-kind verdict.
* ``company_domain_revocation`` - which confirmed domain turned out to belong
  to somebody else, why, and what was removed with it.

Both tables are shared knowledge-base facts (FR-341, FR-344): one row per
company, no job seeker on either, populated by the registry pass and merely
read by campaign planning.

All SQL for the registry slice lives here; :mod:`dreamjob.pipeline.employer_registry_rung`
holds the judgement and this module holds the statements, which is the split
the architecture asks for.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.db.connection import (
    execute,
    new_id,
    query_all,
    query_one,
    to_json,
    utcnow,
    write_tx,
)

log = logging.getLogger(__name__)

TABLE = "company_registry_identity"
REVOCATION_TABLE = "company_domain_revocation"

#: What the gate concluded about a name.  ``ambiguous`` is not ``no_match``:
#: the register *does* hold a company of this name and cannot say which.
DECISIONS = ("matched", "ambiguous", "no_match", "unavailable")


# ---------------------------------------------------------------------------
# Which legal entity this company is
# ---------------------------------------------------------------------------


def record_identity(company_id: str, values: dict[str, Any]) -> None:
    """Store what the exact-name gate concluded, refusals included.

    ``attempts`` accumulates rather than resets, so "the register has been
    asked three times and still cannot say" is answerable - which is what
    stops a pass from spending a request on the same hopeless name weekly.
    """
    payload = {
        "company_id": company_id,
        "registry": str(values.get("registry") or "kbo_bce"),
        "decision": str(values.get("decision") or "unavailable"),
        "legal_id": values.get("legal_id"),
        "registered_name": values.get("registered_name"),
        "municipality": values.get("municipality"),
        "postcode": values.get("postcode"),
        "match_rule": values.get("match_rule"),
        "municipality_checked": 1 if values.get("municipality_checked") else 0,
        "queried_name": str(values.get("queried_name") or ""),
        "candidates": to_json(values.get("candidates") or []),
        "reason": values.get("reason"),
        "source_url": values.get("source_url"),
        "established_at": utcnow(),
        "expires_at": values.get("expires_at"),
    }
    columns = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    updates = ", ".join(
        f"{k}=excluded.{k}" for k in payload if k not in ("company_id", "established_at")
    )
    execute(
        f"INSERT INTO {TABLE} ({columns}) VALUES ({marks}) "
        f"ON CONFLICT(company_id) DO UPDATE SET {updates}, "
        f"attempts = {TABLE}.attempts + 1",
        payload,
    )


def identity_for(company_id: str) -> dict[str, Any] | None:
    return query_one(f"SELECT * FROM {TABLE} WHERE company_id = ?", (company_id,))


def identities_for(company_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not company_ids:
        return {}
    marks = ", ".join("?" for _ in company_ids)
    rows = query_all(f"SELECT * FROM {TABLE} WHERE company_id IN ({marks})", tuple(company_ids))
    return {row["company_id"]: row for row in rows}


def ambiguous(limit: int = 100) -> list[dict[str, Any]]:
    """The "which one is it?" queue: names the register holds more than once.

    This is a work item, not a dead end (Agency_Research_Design.md section 7):
    the company page offers the two register rows and a person picks.
    """
    return query_all(
        f"""
        SELECT i.*, c.name AS company_name,
               (SELECT COUNT(*) FROM vacancy v WHERE v.company_id = i.company_id) AS vacancy_count
          FROM {TABLE} i
          JOIN company c ON c.id = i.company_id
         WHERE i.decision = 'ambiguous'
         ORDER BY vacancy_count DESC
         LIMIT ?
        """,
        (max(1, int(limit)),),
    )


def coverage() -> dict[str, int]:
    """How many companies the register answered for, by decision."""
    rows = query_all(f"SELECT decision, COUNT(*) AS n FROM {TABLE} GROUP BY decision")
    out = {str(row["decision"]): int(row["n"]) for row in rows}
    checked = query_one(
        f"SELECT COUNT(*) AS n FROM {TABLE} WHERE decision = 'matched' AND municipality_checked = 1"
    )
    out["matched_with_seat_check"] = int(checked["n"]) if checked else 0
    return out


def apply_registry_identity(
    company_id: str,
    *,
    legal_id: str | None,
    legal_id_type: str = "kbo_bce",
    vat_number: str | None = None,
    sector_codes: list[str] | None = None,
    country: str | None = None,
) -> bool:
    """Write the DR-101 keys onto the company - only where it has none.

    Deliberately additive.  Overwriting an identifier that is already on the
    row is the de-duplicator's decision (FR-184), not this pass's: a company
    that already carries a legal id was keyed on it, and silently repointing
    it would move every fact that hangs from it.
    """
    if not legal_id:
        return False
    changed = execute(
        "UPDATE company "
        "   SET legal_id = ?, legal_id_type = ?, "
        "       vat_number = COALESCE(NULLIF(vat_number, ''), ?), "
        "       country = COALESCE(NULLIF(country, ''), ?), "
        "       jurisdiction = COALESCE(NULLIF(jurisdiction, ''), ?) "
        " WHERE id = ? AND (legal_id IS NULL OR legal_id = '')",
        (legal_id, legal_id_type, vat_number, country, country, company_id),
    )
    if sector_codes:
        execute(
            "UPDATE company SET sector_codes = ? "
            " WHERE id = ? AND (sector_codes IS NULL OR sector_codes IN ('', '[]'))",
            (to_json(sorted(set(sector_codes))), company_id),
        )
    return bool(changed)


def companies_for_registry(
    limit: int = 200,
    *,
    market: str = "BE",
    refresh: bool = False,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Employers this register could still answer for, widest blast radius first.

    Not the same queue as ``employer_resolution.companies_needing_verdict``,
    and deliberately so.  That one asks "who has no verdict from any rung";
    this one asks "whose legal entity have we not resolved", which is a
    different set - a company can carry a website verdict and still have no
    enterprise number, and the number is worth more to the de-duplicator
    (FR-184) than the verdict is to the badge.

    The market is the company's own country when it has one and otherwise the
    country its vacancies are in.  That matters more than it sounds: 1,337 of
    the corpus's companies carry a country on 3 of them, and filtering on
    ``company.country`` alone would ask the Belgian register about nobody.
    """
    moment = now or utcnow()
    params: list[Any] = []
    freshness = ""
    if not refresh:
        freshness = (
            " AND (i.company_id IS NULL"
            "      OR (i.expires_at IS NOT NULL AND i.expires_at <= ?))"
        )
        params.append(moment)
    having = ""
    if market:
        having = " HAVING UPPER(COALESCE(company_country, '')) = ?"
    sql = f"""
        SELECT co.id                AS company_id,
               co.name              AS company_name,
               co.domain            AS company_domain,
               co.legal_id          AS legal_id,
               COALESCE(NULLIF(co.country, ''), (
                   SELECT v2.country FROM vacancy v2
                    WHERE v2.company_id = co.id AND COALESCE(v2.country, '') <> ''
                    ORDER BY COALESCE(v2.posted_at, v2.collected_at) DESC LIMIT 1
               ))                   AS company_country,
               COUNT(DISTINCT v.id) AS vacancy_count
          FROM company co
          JOIN vacancy v ON v.company_id = co.id
          LEFT JOIN {TABLE} i ON i.company_id = co.id
         WHERE COALESCE(co.name, '') <> ''{freshness}
         GROUP BY co.id{having}
         ORDER BY vacancy_count DESC, co.name
         LIMIT ?
    """
    if market:
        params.append(str(market).upper()[:2])
    params.append(max(1, int(limit)))
    return query_all(sql, tuple(params))


def vacancy_places(company_id: str, limit: int = 12) -> list[str]:
    """Where this company's vacancies are, for the seat cross-check.

    Both the free-text location and the country are returned: the register's
    seat is a municipality, and a row that says only "BE" cannot confirm one -
    which is the honest outcome, not a reason to invent a match.
    """
    rows = query_all(
        "SELECT DISTINCT location, country FROM vacancy "
        " WHERE company_id = ? AND (location IS NOT NULL OR country IS NOT NULL) "
        " LIMIT ?",
        (company_id, max(1, int(limit))),
    )
    places: list[str] = []
    for row in rows:
        for value in (row.get("location"), row.get("country")):
            text = str(value or "").strip()
            if text and text not in places:
                places.append(text)
    return places


# ---------------------------------------------------------------------------
# Domains the gate had to take back
# ---------------------------------------------------------------------------


def derived_domains(
    limit: int = 1000,
    *,
    sources: tuple[str, ...] = ("derived_confirmed",),
) -> list[dict[str, Any]]:
    """Every company whose domain was *spelled* rather than published.

    The country is the company's own when it has one and otherwise the country
    its vacancies are in: 264 of the corpus's derived domains sit on companies
    with a NULL country, and the market is what decides whether a ``.com``
    page in Canadian French belongs to a Belgian company.
    """
    if not sources:
        return []
    marks = ", ".join("?" for _ in sources)
    return query_all(
        f"""
        SELECT c.id                AS company_id,
               c.name              AS company_name,
               c.domain            AS company_domain,
               r.domain            AS domain,
               r.domain_source     AS domain_source,
               r.email             AS email,
               r.status            AS status,
               COALESCE(NULLIF(c.country, ''), (
                   SELECT v.country FROM vacancy v
                    WHERE v.company_id = c.id AND COALESCE(v.country, '') <> ''
                    ORDER BY COALESCE(v.posted_at, v.collected_at) DESC LIMIT 1
               ))                  AS country,
               (SELECT COUNT(*) FROM vacancy v2 WHERE v2.company_id = c.id) AS vacancy_count,
               (SELECT i.legal_id FROM {TABLE} i WHERE i.company_id = c.id) AS legal_id
          FROM apply_contact_resolution r
          JOIN company c ON c.id = r.company_id
         WHERE COALESCE(r.domain, '') <> ''
           AND r.domain_source IN ({marks})
         ORDER BY vacancy_count DESC, c.name
         LIMIT ?
        """,
        (*sources, max(1, int(limit))),
    )


def revoke_domain(
    company_id: str | None,
    *,
    company_name: str,
    domain: str,
    reason: str,
    evidence: str = "",
    previous_source: str | None = None,
    remove_contacts: bool = True,
) -> dict[str, Any]:
    """Take a domain back from a company, and remember that we did (NFR-402).

    Everything that rests on the domain goes with it, in one transaction:

    * the addresses spelled on it, because an address at a namesake is a real
      mailbox at an unrelated organisation - keeping it against this company
      is both the CR-405 failure and a privacy defect (FR-306, RK-08);
    * the company's ``domain``, so no later pass reads it as established;
    * the FR-301 resolution row, which becomes *unreachable with a reason* -
      a finding the Apply Browser shows, not a gap that invites a re-derivation;
    * the FR-305 probe verdict, so the ladder does not confirm the same domain
      again the moment the probe cache expires.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {"domain": "", "contacts_removed": 0}
    now = utcnow()
    note = (
        "the confirmed domain did not survive the v2 identity gate "
        f"({reason}); see company_domain_revocation"
    )
    with write_tx() as conn:
        removed = 0
        if remove_contacts and company_id:
            cur = conn.execute(
                "DELETE FROM contact WHERE company_id = ? AND lower(email) LIKE ?",
                (company_id, f"%@{domain}"),
            )
            removed = int(cur.rowcount or 0)
        conn.execute(
            f"INSERT INTO {REVOCATION_TABLE} "
            "(id, company_id, company_name, domain, previous_source, reason, evidence, "
            " gate_version, contacts_removed, cleared_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'v2', ?, ?)",
            (
                new_id(), company_id, company_name[:200], domain, previous_source,
                reason, evidence[:500], removed, now,
            ),
        )
        if company_id:
            conn.execute(
                "UPDATE company SET domain = NULL WHERE id = ? AND lower(domain) = ?",
                (company_id, domain),
            )
            conn.execute(
                "UPDATE apply_contact_resolution "
                "   SET status = 'unreachable', contact_id = NULL, email = NULL, "
                "       domain = NULL, domain_source = NULL, method = NULL, "
                "       validation = NULL, is_generic = 0, reason = ?, resolved_at = ? "
                " WHERE company_id = ?",
                (note, now, company_id),
            )
        conn.execute(
            "UPDATE apply_domain_probe "
            "   SET outcome = 'rejected', evidence = ?, last_probed_at = ? "
            " WHERE domain = ?",
            (f"v2 identity gate: {reason} - {evidence}"[:400], now, domain),
        )
    log.info("Cleared %s from %s (%s)", domain, company_name, reason)
    return {"domain": domain, "contacts_removed": removed}


def revocations(limit: int = 200) -> list[dict[str, Any]]:
    return query_all(
        f"SELECT * FROM {REVOCATION_TABLE} ORDER BY cleared_at DESC LIMIT ?",
        (max(1, int(limit)),),
    )


def revocation_summary() -> dict[str, Any]:
    rows = query_all(
        f"SELECT reason, COUNT(*) AS n, SUM(contacts_removed) AS contacts "
        f"  FROM {REVOCATION_TABLE} GROUP BY reason ORDER BY n DESC"
    )
    return {
        "total": sum(int(row["n"]) for row in rows),
        "contacts_removed": sum(int(row["contacts"] or 0) for row in rows),
        "by_reason": {str(row["reason"]): int(row["n"]) for row in rows},
    }
