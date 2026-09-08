"""Campaign-export SQL (FR-463, FR-101, FR-344, NFR-301, CR-408).

FR-463 is accepted on two properties, and both are decided by the shape of the
queries in this module rather than by anything downstream:

* **no data of any other job seeker.**  Every private read starts from
  ``job_seeker_id`` and, where a campaign is named, from that campaign.  The
  shared rows - companies, vacancies, contacts, financial years - are never
  queried by a filter of their own; they are fetched **by id, from ids reached
  through the seeker's own opportunities**, which is what makes the isolation
  a property of the query graph and not of a later filtering pass.
* **respects do-not-disclose flags.**  The flag paths come back with the
  export inputs (``disclosure_paths``) so the writer can apply them to profile
  content before it reaches the package (FR-106).
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    from_json,
    insert_row,
    query_all,
    query_one,
    utcnow,
)

COMPANY_JSON_COLUMNS = (
    "products_services", "markets", "sector_codes", "locations", "structure", "key_people",
    "reference_customers", "tech_stack", "values_culture", "news",
)
OPPORTUNITY_JSON_COLUMNS = (
    "required_skills", "desirable_skills", "comp_sources", "employer_review_themes",
    "dream_fit_detail", "score_detail", "tags",
)
PACKAGE_JSON_COLUMNS = ("consistency_report",)
CARD_JSON_COLUMNS = ("stage_dates", "variables")


def _decode(row: dict | None, json_columns: tuple[str, ...]) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for column in json_columns:
        if column in out:
            out[column] = from_json(out[column], None)
    return out


def _decode_all(rows: list[dict], json_columns: tuple[str, ...]) -> list[dict]:
    return [d for d in (_decode(r, json_columns) for r in rows) if d]


def _marks(values: list[str]) -> str:
    return ", ".join("?" for _ in values)


# ---------------------------------------------------------------------------
# Private roots (FR-101): nothing in the export is reached except from here
# ---------------------------------------------------------------------------


def seeker(job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT id, email, display_name, locale, created_at FROM job_seeker WHERE id = ?",
        (job_seeker_id,),
    )


def campaign(campaign_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM campaign WHERE id = ? AND job_seeker_id = ?",
            (campaign_id, job_seeker_id),
        ),
        ("caps", "reuse_report"),
    )


def directive_set(directive_set_id: str | None, job_seeker_id: str) -> dict | None:
    if not directive_set_id:
        return None
    return _decode(
        query_one(
            "SELECT * FROM directive_set WHERE id = ? AND job_seeker_id = ?",
            (directive_set_id, job_seeker_id),
        ),
        (
            "job_content", "company_type", "location", "work_arrangement", "compensation",
            "discretion_excluded_companies", "discretion_excluded_contacts",
        ),
    )


def profile_version(profile_version_id: str | None, job_seeker_id: str) -> dict | None:
    if profile_version_id:
        row = query_one(
            "SELECT * FROM profile_version WHERE id = ? AND job_seeker_id = ?",
            (profile_version_id, job_seeker_id),
        )
        if row:
            return _decode(row, ("sections",))
    return _decode(
        query_one(
            "SELECT * FROM profile_version WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (job_seeker_id,),
        ),
        ("sections",),
    )


def composite_profile(composite_id: str | None, job_seeker_id: str) -> dict | None:
    sql = "SELECT * FROM composite_profile WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if composite_id:
        sql += " AND id = ?"
        params.append(composite_id)
    sql += " ORDER BY version DESC LIMIT 1"
    return _decode(
        query_one(sql, tuple(params)),
        (
            "career_trajectory", "core_competencies", "adjacent_competencies", "seniority",
            "domains", "achievements", "public_footprint", "inferred_preferences",
            "constraints", "evidence_refs",
        ),
    )


def dream_job_model(model_id: str | None, job_seeker_id: str) -> dict | None:
    sql = "SELECT * FROM dream_job_model WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if model_id:
        sql += " AND id = ?"
        params.append(model_id)
    sql += " ORDER BY version DESC LIMIT 1"
    return _decode(
        query_one(sql, tuple(params)),
        (
            "target_roles", "role_families", "responsibilities", "company_characteristics",
            "culture_values", "deal_breakers", "implicit_preferences",
        ),
    )


def disclosure_paths(job_seeker_id: str) -> set[str]:
    """FR-106: fields the seeker never wants to appear in generated material."""
    return {
        r["field_path"]
        for r in query_all(
            "SELECT field_path FROM disclosure_flag WHERE job_seeker_id = ? "
            "AND do_not_disclose = 1",
            (job_seeker_id,),
        )
    }


def opportunities(job_seeker_id: str, campaign_id: str) -> list[dict]:
    """The ranked list, in the order the screen shows it (FR-283, FR-284)."""
    return _decode_all(
        query_all(
            """
            SELECT o.*, c.name AS company_name, c.domain AS company_domain,
                   v.source_url AS vacancy_source_url, v.posted_at AS vacancy_posted_at
            FROM opportunity o
            LEFT JOIN company c ON c.id = o.company_id
            LEFT JOIN vacancy v ON v.id = o.vacancy_id
            WHERE o.job_seeker_id = ? AND o.campaign_id = ?
            ORDER BY (o.manual_rank IS NULL), o.manual_rank ASC, o.pinned DESC,
                     IFNULL(o.score, 0) DESC, o.created_at ASC
            """,
            (job_seeker_id, campaign_id),
        ),
        OPPORTUNITY_JSON_COLUMNS,
    )


def application_packages(job_seeker_id: str, opportunity_ids: list[str]) -> list[dict]:
    """FR-321..331: the generated material, reached from this seeker's own list."""
    if not opportunity_ids:
        return []
    return _decode_all(
        query_all(
            f"SELECT * FROM application_package WHERE job_seeker_id = ? "
            f"AND opportunity_id IN ({_marks(opportunity_ids)}) ORDER BY created_at ASC",
            (job_seeker_id, *opportunity_ids),
        ),
        PACKAGE_JSON_COLUMNS,
    )


def dispatches(job_seeker_id: str, package_ids: list[str]) -> list[dict]:
    """FR-325..327: what was actually sent, and what came back."""
    if not package_ids:
        return []
    return _decode_all(
        query_all(
            f"SELECT * FROM dispatch WHERE job_seeker_id = ? "
            f"AND application_package_id IN ({_marks(package_ids)}) ORDER BY created_at ASC",
            (job_seeker_id, *package_ids),
        ),
        ("attachments",),
    )


def replies(job_seeker_id: str, dispatch_ids: list[str]) -> list[dict]:
    if not dispatch_ids:
        return []
    return _decode_all(
        query_all(
            f"SELECT * FROM incoming_reply WHERE job_seeker_id = ? "
            f"AND dispatch_id IN ({_marks(dispatch_ids)}) ORDER BY received_at ASC",
            (job_seeker_id, *dispatch_ids),
        ),
        ("extracted_slots",),
    )


def pipeline_cards(job_seeker_id: str, opportunity_ids: list[str]) -> list[dict]:
    if not opportunity_ids:
        return []
    return _decode_all(
        query_all(
            f"SELECT * FROM pipeline_card WHERE job_seeker_id = ? "
            f"AND opportunity_id IN ({_marks(opportunity_ids)}) ORDER BY created_at ASC",
            (job_seeker_id, *opportunity_ids),
        ),
        CARD_JSON_COLUMNS,
    )


def introduction_paths(job_seeker_id: str, opportunity_ids: list[str]) -> list[dict]:
    if not opportunity_ids:
        return []
    return query_all(
        f"SELECT * FROM introduction_path WHERE job_seeker_id = ? "
        f"AND opportunity_id IN ({_marks(opportunity_ids)}) ORDER BY strength DESC",
        (job_seeker_id, *opportunity_ids),
    )


def gap_analysis(job_seeker_id: str, campaign_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM gap_analysis WHERE job_seeker_id = ? AND campaign_id = ?",
            (job_seeker_id, campaign_id),
        ),
        ("gaps",),
    )


def stepping_stones(job_seeker_id: str, campaign_id: str) -> list[dict]:
    return _decode_all(
        query_all(
            "SELECT * FROM stepping_stone_path WHERE job_seeker_id = ? AND campaign_id = ? "
            "ORDER BY created_at ASC",
            (job_seeker_id, campaign_id),
        ),
        ("steps", "basis"),
    )


def event_interests(job_seeker_id: str) -> list[dict]:
    return query_all(
        "SELECT i.status, i.note, i.created_at, e.name, e.starts_at, e.location, e.url "
        "FROM event_interest i JOIN event e ON e.id = i.event_id "
        "WHERE i.job_seeker_id = ? ORDER BY e.starts_at ASC",
        (job_seeker_id,),
    )


# ---------------------------------------------------------------------------
# Shared rows, reached by id only (FR-344)
# ---------------------------------------------------------------------------


def companies(company_ids: list[str]) -> list[dict]:
    if not company_ids:
        return []
    return _decode_all(
        query_all(
            f"SELECT * FROM company WHERE id IN ({_marks(company_ids)}) ORDER BY name",
            tuple(company_ids),
        ),
        COMPANY_JSON_COLUMNS,
    )


def vacancies(vacancy_ids: list[str]) -> list[dict]:
    if not vacancy_ids:
        return []
    return _decode_all(
        query_all(
            f"SELECT * FROM vacancy WHERE id IN ({_marks(vacancy_ids)})", tuple(vacancy_ids)
        ),
        ("required_skills", "desirable_skills"),
    )


def financial_years(company_ids: list[str]) -> list[dict]:
    if not company_ids:
        return []
    return _decode_all(
        query_all(
            f"SELECT * FROM financial_year WHERE company_id IN ({_marks(company_ids)}) "
            f"ORDER BY company_id, fiscal_year",
            tuple(company_ids),
        ),
        ("reconciliation_flags",),
    )


def financial_analyses(company_ids: list[str]) -> list[dict]:
    if not company_ids:
        return []
    return _decode_all(
        query_all(
            f"SELECT * FROM financial_analysis WHERE company_id IN ({_marks(company_ids)})",
            tuple(company_ids),
        ),
        ("years_covered",),
    )


def hiring_signals(company_ids: list[str], limit_per_company: int = 25) -> list[dict]:
    if not company_ids:
        return []
    rows = query_all(
        f"SELECT * FROM hiring_signal WHERE company_id IN ({_marks(company_ids)}) "
        f"ORDER BY company_id, IFNULL(occurred_at, collected_at) DESC",
        tuple(company_ids),
    )
    out: list[dict] = []
    seen: dict[str, int] = {}
    for row in rows:
        key = str(row["company_id"])
        if seen.get(key, 0) >= limit_per_company:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(row)
    return out


def contacts(company_ids: list[str], campaign_id: str | None = None) -> list[dict]:
    """NFR-302: professional contact details only, and never a blocked contact.

    Campaign-scoped contacts (NFR-303) are included only for the campaign that
    collected them, which is the campaign being exported.
    """
    if not company_ids:
        return []
    sql = (
        f"SELECT id, company_id, full_name, role_title, department, email, linkedin_url, "
        f"email_validation, is_generic_mailbox, source, collected_at "
        f"FROM contact WHERE company_id IN ({_marks(company_ids)}) AND objected = 0 "
        f"AND (shareable = 1"
    )
    params: list[Any] = list(company_ids)
    if campaign_id:
        sql += " OR owning_campaign_id = ?"
        params.append(campaign_id)
    sql += ") ORDER BY company_id, full_name"
    return query_all(sql, tuple(params))


# ---------------------------------------------------------------------------
# The export record (FR-463)
# ---------------------------------------------------------------------------


def record_export(job_seeker_id: str, values: dict[str, Any]) -> str:
    return insert_row(
        "campaign_export", {**values, "job_seeker_id": job_seeker_id, "created_at": utcnow()}
    )


def get_export(export_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM campaign_export WHERE id = ? AND job_seeker_id = ?",
            (export_id, job_seeker_id),
        ),
        ("manifest",),
    )


def list_exports(job_seeker_id: str, campaign_id: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM campaign_export WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), ("manifest",))


def record_audit(
    job_seeker_id: str, action: str, entity_id: str | None, detail: dict[str, Any]
) -> str:
    """NFR-702: an export leaves the machine, so it is an audited event."""
    return insert_row(
        "audit_event",
        {
            "job_seeker_id": job_seeker_id,
            "actor": job_seeker_id,
            "action": action,
            "entity_type": "campaign_export",
            "entity_id": entity_id,
            "detail": detail,
            "created_at": utcnow(),
        },
    )
