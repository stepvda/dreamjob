"""Application package persistence and generation inputs (FR-321..331, NFR-206).

``application_package`` is a **private** table: every statement takes a
``job_seeker_id`` and filters on it (FR-101, FR-344).  The shared rows the
generators read - company, vacancy, financial_year, hiring_signal,
competitor_link, contact - are joined outwards from an opportunity the caller
has already proved they own, and carry no link back to a job seeker.

Two reads are worth naming:

``generation_inputs``
    One round trip that collects everything the four artefacts of FR-321 need,
    including the versions FR-331 makes the documents state: the profile
    version behind the campaign and the freshness stamp of the company record.

``provenance_corpus``
    The NFR-206 leak scan needs the set of material this job seeker's generated
    documents are *allowed* to contain: their own profile, their composite, the
    findings they accepted, and the company and vacancy this application is
    for.  Anything in a generated document that is not traceable to this set is
    unattributable, and that is what the scan reports.
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    execute,
    from_json,
    insert_row,
    query_all,
    query_one,
    to_json,
    update_row,
    utcnow,
    write_tx,
)
from dreamjob.db.repositories import contacts as contact_repo
from dreamjob.security import at_rest

#: Columns of ``application_package`` holding JSON, decoded on the way out.
JSON_COLUMNS = ("consistency_report", "generation_notes")


def _encode_sensitive(payload: dict[str, Any], job_seeker_id: str) -> dict[str, Any]:
    """JSON-encode the package's JSON columns, sealing the CV/motivation text.

    NFR-201 names generated CVs: ``generation_notes`` holds the tailored CV,
    motivation and briefing text, so it is sealed with the job seeker's key.
    ``consistency_report`` is a check result with no personal prose and stays a
    plain JSON column.  The read side unseals centrally.
    """
    report = payload.get("consistency_report")
    if isinstance(report, (dict, list)):
        payload["consistency_report"] = to_json(report)
    notes = payload.get("generation_notes")
    if isinstance(notes, (dict, list)):
        payload["generation_notes"] = at_rest.seal(
            to_json(notes), purpose="cv", scope=job_seeker_id
        )
    return payload

PACKAGE_STATUSES = frozenset({"draft", "approved", "discarded", "sent"})

_UPDATABLE = frozenset(
    {
        "contact_id", "language", "cv_template", "cv_docx_path", "cv_pdf_path",
        "briefing_pdf_path", "motivation_pdf_path", "email_subject", "email_body",
        "consistency_status", "consistency_report", "leak_scan_status", "status",
        "approved_at", "approved_by", "consistency_override",
        "profile_version_id", "company_snapshot_at",
        "generation_notes", "updated_at",
    }
)


def decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for column in JSON_COLUMNS:
        if column in out:
            out[column] = from_json(out[column], None)
    return out


# ---------------------------------------------------------------------------
# Packages (FR-321, FR-324)
# ---------------------------------------------------------------------------

_PACKAGE_SELECT = """
    SELECT p.*,
           o.title            AS opportunity_title,
           o.kind             AS opportunity_kind,
           o.campaign_id      AS campaign_id,
           o.company_id       AS company_id,
           o.vacancy_id       AS vacancy_id,
           o.application_channel AS application_channel,
           o.application_target  AS application_target,
           c.name             AS company_name,
           ct.full_name       AS contact_name,
           ct.email           AS contact_email,
           ct.role_title      AS contact_role,
           ct.objected        AS contact_objected
    FROM application_package p
    JOIN opportunity o ON o.id = p.opportunity_id
    LEFT JOIN company c ON c.id = o.company_id
    LEFT JOIN contact ct ON ct.id = p.contact_id
"""


def create_package(job_seeker_id: str, opportunity_id: str, values: dict[str, Any]) -> str:
    payload = {k: v for k, v in values.items() if k in _UPDATABLE}
    payload.update(
        {
            "job_seeker_id": job_seeker_id,
            "opportunity_id": opportunity_id,
            "status": payload.get("status") or "draft",
            "consistency_status": payload.get("consistency_status") or "not_run",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
    )
    return insert_row("application_package", _encode_sensitive(payload, job_seeker_id))


def get_package(package_id: str, job_seeker_id: str) -> dict | None:
    return decode(
        query_one(
            _PACKAGE_SELECT + " WHERE p.id = ? AND p.job_seeker_id = ?",
            (package_id, job_seeker_id),
        )
    )


def packages_by_ids(package_ids: list[str], job_seeker_id: str) -> list[dict]:
    if not package_ids:
        return []
    marks = ", ".join("?" for _ in package_ids)
    rows = query_all(
        _PACKAGE_SELECT + f" WHERE p.job_seeker_id = ? AND p.id IN ({marks})",
        (job_seeker_id, *package_ids),
    )
    by_id = {r["id"]: decode(r) for r in rows}
    return [by_id[i] for i in package_ids if i in by_id]


def latest_for_opportunity(job_seeker_id: str, opportunity_id: str) -> dict | None:
    """The live package for an opportunity; a discarded one never counts."""
    return decode(
        query_one(
            _PACKAGE_SELECT
            + " WHERE p.job_seeker_id = ? AND p.opportunity_id = ? AND p.status != 'discarded'"
            + " ORDER BY p.created_at DESC LIMIT 1",
            (job_seeker_id, opportunity_id),
        )
    )


def list_packages(
    job_seeker_id: str,
    *,
    campaign_id: str | None = None,
    status: str | None = None,
    opportunity_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    sql = _PACKAGE_SELECT + " WHERE p.job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND o.campaign_id = ?"
        params.append(campaign_id)
    if status:
        sql += " AND p.status = ?"
        params.append(status)
    if opportunity_id:
        sql += " AND p.opportunity_id = ?"
        params.append(opportunity_id)
    sql += " ORDER BY p.updated_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [decode(r) for r in query_all(sql, tuple(params))]  # type: ignore[misc]


def count_packages(job_seeker_id: str, *, campaign_id: str | None = None) -> dict[str, int]:
    sql = (
        "SELECT p.status AS status, COUNT(*) AS n FROM application_package p "
        "JOIN opportunity o ON o.id = p.opportunity_id WHERE p.job_seeker_id = ?"
    )
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND o.campaign_id = ?"
        params.append(campaign_id)
    rows = query_all(sql + " GROUP BY p.status", tuple(params))
    return {r["status"]: int(r["n"]) for r in rows}


def update_package(package_id: str, job_seeker_id: str, values: dict[str, Any]) -> dict | None:
    if query_one(
        "SELECT id FROM application_package WHERE id = ? AND job_seeker_id = ?",
        (package_id, job_seeker_id),
    ) is None:
        return None
    payload = {k: v for k, v in values.items() if k in _UPDATABLE}
    if not payload:
        return get_package(package_id, job_seeker_id)
    payload = _encode_sensitive(payload, job_seeker_id)
    payload["updated_at"] = utcnow()
    update_row("application_package", package_id, payload)
    return get_package(package_id, job_seeker_id)


def approve_packages(
    job_seeker_id: str, package_ids: list[str], actor: str, *, override_reason: str | None = None
) -> list[str]:
    """Mark packages approved for dispatch (FR-324, NFR-702 audit trail).

    ``override_reason`` is stored on the row, not only in the audit trail, so
    the send paths can tell an overridden FR-322 failure from one nobody has
    accepted (migration 146).
    """
    if not package_ids:
        return []
    stamp = utcnow()
    approved: list[str] = []
    with write_tx() as conn:
        for package_id in package_ids:
            cur = conn.execute(
                "UPDATE application_package SET status = 'approved', approved_at = ?, "
                "approved_by = ?, consistency_override = ?, updated_at = ? "
                "WHERE id = ? AND job_seeker_id = ? AND status = 'draft'",
                (stamp, actor, override_reason, stamp, package_id, job_seeker_id),
            )
            if cur.rowcount:
                approved.append(package_id)
    return approved


def discard_package(package_id: str, job_seeker_id: str) -> int:
    return execute(
        "UPDATE application_package SET status = 'discarded', updated_at = ? "
        "WHERE id = ? AND job_seeker_id = ? AND status != 'sent'",
        (utcnow(), package_id, job_seeker_id),
    )


def delete_package(package_id: str, job_seeker_id: str) -> int:
    return execute(
        "DELETE FROM application_package WHERE id = ? AND job_seeker_id = ? AND status != 'sent'",
        (package_id, job_seeker_id),
    )


def has_dispatch(package_id: str) -> bool:
    row = query_one(
        "SELECT COUNT(*) AS n FROM dispatch WHERE application_package_id = ?", (package_id,)
    )
    return bool(row and int(row["n"]))


def record_audit(
    job_seeker_id: str,
    action: str,
    *,
    actor: str | None = None,
    entity_type: str = "application_package",
    entity_id: str | None = None,
    detail: dict | None = None,
) -> str:
    """NFR-702: who approved what, and with which summary (FR-324)."""
    return insert_row(
        "audit_event",
        {
            "job_seeker_id": job_seeker_id,
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": to_json(detail or {}),
            "created_at": utcnow(),
        },
    )


# ---------------------------------------------------------------------------
# Opportunity selection (FR-321: "for each selected opportunity")
# ---------------------------------------------------------------------------


def selected_opportunity_ids(job_seeker_id: str, campaign_id: str | None = None) -> list[str]:
    sql = "SELECT id FROM opportunity WHERE job_seeker_id = ? AND selected = 1"
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY (manual_rank IS NULL), manual_rank, pinned DESC, score DESC"
    return [r["id"] for r in query_all(sql, tuple(params))]


def get_opportunity(opportunity_id: str, job_seeker_id: str) -> dict | None:
    row = query_one(
        "SELECT * FROM opportunity WHERE id = ? AND job_seeker_id = ?",
        (opportunity_id, job_seeker_id),
    )
    if row is None:
        return None
    out = dict(row)
    for column in ("required_skills", "desirable_skills", "tags", "dream_fit_detail",
                   "comp_sources", "employer_review_themes"):
        if column in out:
            out[column] = from_json(out[column], None)
    return out


# ---------------------------------------------------------------------------
# Generation inputs (FR-321, FR-329, FR-330, FR-331)
# ---------------------------------------------------------------------------


def seeker(job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT id, email, display_name, locale FROM job_seeker WHERE id = ?", (job_seeker_id,)
    )


def disclosure_paths(job_seeker_id: str) -> set[str]:
    """FR-106 field paths that must never reach generated content."""
    rows = query_all(
        "SELECT field_path FROM disclosure_flag WHERE job_seeker_id = ? AND do_not_disclose = 1",
        (job_seeker_id,),
    )
    return {str(r["field_path"]) for r in rows}


def profile_version(job_seeker_id: str, profile_version_id: str | None = None) -> dict | None:
    if profile_version_id:
        row = query_one(
            "SELECT * FROM profile_version WHERE id = ? AND job_seeker_id = ?",
            (profile_version_id, job_seeker_id),
        )
    else:
        row = query_one(
            "SELECT * FROM profile_version WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (job_seeker_id,),
        )
    if row is None:
        return None
    out = dict(row)
    out["sections"] = from_json(out.get("sections"), {})
    return out


def composite_profile(job_seeker_id: str, composite_id: str | None = None) -> dict | None:
    if composite_id:
        row = query_one(
            "SELECT * FROM composite_profile WHERE id = ? AND job_seeker_id = ?",
            (composite_id, job_seeker_id),
        )
    else:
        row = query_one(
            "SELECT * FROM composite_profile WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (job_seeker_id,),
        )
    if row is None:
        return None
    out = dict(row)
    for column in ("career_trajectory", "core_competencies", "adjacent_competencies",
                   "seniority", "domains", "achievements", "public_footprint",
                   "inferred_preferences", "constraints", "evidence_refs"):
        if column in out:
            out[column] = from_json(out[column], None)
    return out


def dream_job_model(job_seeker_id: str, model_id: str | None = None) -> dict | None:
    if model_id:
        row = query_one(
            "SELECT * FROM dream_job_model WHERE id = ? AND job_seeker_id = ?",
            (model_id, job_seeker_id),
        )
    else:
        row = query_one(
            "SELECT * FROM dream_job_model WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (job_seeker_id,),
        )
    if row is None:
        return None
    out = dict(row)
    for column in ("target_roles", "role_families", "responsibilities",
                   "company_characteristics", "culture_values", "deal_breakers",
                   "implicit_preferences"):
        if column in out:
            out[column] = from_json(out[column], None)
    return out


def profile_skills(job_seeker_id: str, profile_version_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT raw_label, normalised_label, proficiency, years_experience, last_used_year "
        "FROM profile_skill WHERE job_seeker_id = ?"
    )
    params: list[Any] = [job_seeker_id]
    if profile_version_id:
        sql += " AND profile_version_id = ?"
        params.append(profile_version_id)
    return query_all(sql + " ORDER BY proficiency DESC, normalised_label", tuple(params))


def evidence_items(job_seeker_id: str) -> list[dict]:
    rows = query_all(
        "SELECT kind, title, url, description, linked_skills FROM evidence_item "
        "WHERE job_seeker_id = ? ORDER BY created_at DESC LIMIT 200",
        (job_seeker_id,),
    )
    for row in rows:
        row["linked_skills"] = from_json(row.get("linked_skills"), [])
    return rows


def accepted_findings(job_seeker_id: str) -> list[dict]:
    rows = query_all(
        "SELECT url, title, extracted_facts FROM enrichment_finding "
        "WHERE job_seeker_id = ? AND status = 'accepted' LIMIT 200",
        (job_seeker_id,),
    )
    for row in rows:
        row["extracted_facts"] = from_json(row.get("extracted_facts"), {})
    return rows


def campaign_of(opportunity: dict) -> dict | None:
    if not opportunity.get("campaign_id"):
        return None
    return query_one(
        "SELECT id, name, profile_version_id, composite_profile_id, dream_job_model_id, "
        "directive_set_id, persona_id FROM campaign WHERE id = ?",
        (opportunity["campaign_id"],),
    )


def company(company_id: str | None) -> dict | None:
    if not company_id:
        return None
    row = query_one("SELECT * FROM company WHERE id = ?", (company_id,))
    if row is None:
        return None
    out = dict(row)
    for column in ("products_services", "markets", "sector_codes", "locations", "structure",
                   "key_people", "reference_customers", "tech_stack", "values_culture", "news"):
        if column in out:
            out[column] = from_json(out[column], None)
    return out


def vacancy(vacancy_id: str | None) -> dict | None:
    if not vacancy_id:
        return None
    row = query_one("SELECT * FROM vacancy WHERE id = ?", (vacancy_id,))
    if row is None:
        return None
    out = dict(row)
    for column in ("required_skills", "desirable_skills"):
        if column in out:
            out[column] = from_json(out[column], None)
    return out


def financial_years(company_id: str | None, limit: int = 5) -> list[dict]:
    """The five-year series FR-329 charts, newest first."""
    if not company_id:
        return []
    return query_all(
        "SELECT * FROM financial_year WHERE company_id = ? ORDER BY fiscal_year DESC LIMIT ?",
        (company_id, limit),
    )


def financial_analysis(company_id: str | None) -> dict | None:
    if not company_id:
        return None
    row = query_one("SELECT * FROM financial_analysis WHERE company_id = ?", (company_id,))
    if row is None:
        return None
    out = dict(row)
    out["years_covered"] = from_json(out.get("years_covered"), [])
    return out


def hiring_signals(company_id: str | None, limit: int = 25) -> list[dict]:
    if not company_id:
        return []
    return query_all(
        "SELECT * FROM hiring_signal WHERE company_id = ? "
        "ORDER BY COALESCE(occurred_at, collected_at) DESC LIMIT ?",
        (company_id, limit),
    )


def competitors(company_id: str | None, limit: int = 20) -> list[dict]:
    if not company_id:
        return []
    return query_all(
        """
        SELECT l.peer_name, l.basis, l.strength,
               c.name AS peer_company_name, c.size_band, c.stage, c.country,
               c.business_summary
        FROM competitor_link l
        LEFT JOIN company c ON c.id = l.peer_company_id
        WHERE l.company_id = ?
        ORDER BY l.strength DESC LIMIT ?
        """,
        (company_id, limit),
    )


def contacts_for_company(
    company_id: str | None, *, job_seeker_id: str | None = None
) -> list[dict]:
    """NFR-302/FR-344: an objecting contact is never offered as a recipient,
    and a campaign-scoped row belonging to another seeker is invisible.

    The stored flag is filtered in SQL and the shared block list is consulted
    through ``contacts.is_objected`` as well, so an address blocked after the
    row was written - or one whose row the trigger has not reached - cannot be
    handed to the generation slice as a recipient.
    """
    if not company_id:
        return []
    sql = "SELECT * FROM contact WHERE company_id = ? AND objected = 0"
    params: list[Any] = [company_id]
    if job_seeker_id:
        sql += (
            " AND (shareable = 1 OR owning_campaign_id IS NULL"
            " OR owning_campaign_id IN (SELECT id FROM campaign WHERE job_seeker_id = ?))"
        )
        params.append(job_seeker_id)
    sql += " ORDER BY is_generic_mailbox ASC, confidence DESC"
    rows = query_all(sql, tuple(params))
    return [
        row
        for row in rows
        if not contact_repo.is_objected(row.get("email"), row.get("linkedin_url"))
    ]


def get_contact(contact_id: str | None) -> dict | None:
    if not contact_id:
        return None
    return query_one("SELECT * FROM contact WHERE id = ?", (contact_id,))


def introduction_paths(job_seeker_id: str, opportunity_id: str) -> list[dict]:
    return query_all(
        "SELECT * FROM introduction_path WHERE job_seeker_id = ? AND opportunity_id = ? "
        "AND status != 'rejected' ORDER BY strength DESC",
        (job_seeker_id, opportunity_id),
    )


def generation_inputs(job_seeker_id: str, opportunity_id: str) -> dict[str, Any] | None:
    """Everything the four FR-321 artefacts are built from, in one read."""
    opportunity = get_opportunity(opportunity_id, job_seeker_id)
    if opportunity is None:
        return None
    campaign = campaign_of(opportunity)
    version = profile_version(job_seeker_id, (campaign or {}).get("profile_version_id"))
    company_row = company(opportunity.get("company_id"))
    return {
        "seeker": seeker(job_seeker_id),
        "opportunity": opportunity,
        "campaign": campaign,
        "company": company_row,
        "vacancy": vacancy(opportunity.get("vacancy_id")),
        "profile_version": version,
        "composite": composite_profile(job_seeker_id, (campaign or {}).get("composite_profile_id")),
        "dream_job": dream_job_model(job_seeker_id, (campaign or {}).get("dream_job_model_id")),
        "skills": profile_skills(job_seeker_id, (version or {}).get("id")),
        "evidence": evidence_items(job_seeker_id),
        "financial_years": financial_years(opportunity.get("company_id")),
        "financial_analysis": financial_analysis(opportunity.get("company_id")),
        "hiring_signals": hiring_signals(opportunity.get("company_id")),
        "competitors": competitors(opportunity.get("company_id")),
        "contacts": contacts_for_company(
            opportunity.get("company_id"), job_seeker_id=job_seeker_id
        ),
        "introduction_paths": introduction_paths(job_seeker_id, opportunity_id),
        "do_not_disclose": disclosure_paths(job_seeker_id),
        "company_snapshot_at": (company_row or {}).get("refreshed_at")
        or (company_row or {}).get("collected_at"),
    }


def provenance_corpus(job_seeker_id: str, opportunity_id: str) -> dict[str, Any]:
    """The material a generated document for this seeker may legitimately contain.

    NFR-206: anything in a generated CV or email that cannot be traced back to
    this set came from somewhere it should not have - another job seeker's
    profile, or scraped material about an unrelated company.

    It is built from :func:`generation_inputs`, so the scan sees the *pinned*
    profile version the documents were generated from.  Reading the latest
    version instead meant a re-uploaded profile made the CV's own contact
    details untraceable, which the scan then reported as a high leak - a
    non-overridable block on a package that had done nothing wrong.
    """
    inputs = generation_inputs(job_seeker_id, opportunity_id)
    if inputs is None:
        return {}
    return {
        "seeker": inputs["seeker"],
        "profile_version": inputs["profile_version"],
        "composite": inputs["composite"],
        "dream_job": inputs["dream_job"],
        "skills": inputs["skills"],
        "evidence": inputs["evidence"],
        "findings": accepted_findings(job_seeker_id),
        "opportunity": inputs["opportunity"],
        "company": inputs["company"],
        "vacancy": inputs["vacancy"],
        "contacts": inputs["contacts"],
    }
