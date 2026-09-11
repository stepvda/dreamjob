"""SQL for enrichment findings, composite profiles and dream job models
(FR-121..FR-128, CR-408).

Every function here filters on ``job_seeker_id``: the three tables are private
(FR-101, FR-344) and a query without that filter would leak one job seeker's
profile into another's campaign.

Two neighbouring tables are read from here as well:

* ``profile_version`` - the composite builder needs its ``sections`` blob as
  input.  Reads only; the profile slice owns the writes.
* ``disclosure_flag`` / ``consent_record`` - used as a fallback when the
  profile and auth repositories are not importable (they belong to other
  slices and may lag behind this one).  Keeping the fallback here rather than
  inline in the pipeline is what keeps CR-408 true: no SQL outside
  ``db/repositories``.
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    execute,
    from_json,
    insert_row,
    insert_row_versioned,
    query_all,
    query_one,
    to_json,
    update_row,
    utcnow,
)

# Columns that hold JSON and are decoded on the way out.
_FINDING_JSON = ("extracted_facts", "identity_signals")
_COMPOSITE_JSON = (
    "career_trajectory",
    "core_competencies",
    "seniority",
    "adjacent_competencies",
    "domains",
    "achievements",
    "public_footprint",
    "inferred_preferences",
    "constraints",
    "evidence_refs",
)
_DREAM_JSON = (
    "target_roles",
    "role_families",
    "responsibilities",
    "company_characteristics",
    "culture_values",
    "deal_breakers",
    "implicit_preferences",
)


def _decode(row: dict | None, columns: tuple[str, ...]) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in columns:
        if col in out:
            out[col] = from_json(out[col], None)
    return out


# ---------------------------------------------------------------------------
# enrichment_finding (FR-122, FR-123, FR-124)
# ---------------------------------------------------------------------------


def list_findings(
    job_seeker_id: str,
    *,
    status: str | None = None,
    classification: str | None = None,
    include_rejected: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT * FROM enrichment_finding WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if classification:
        sql += " AND classification = ?"
        params.append(classification)
    if not include_rejected:
        sql += " AND rejected_permanently = 0"
    sql += " ORDER BY identity_score DESC, created_at DESC LIMIT ? OFFSET ?"
    params.extend((int(limit), int(offset)))
    return [_decode(r, _FINDING_JSON) or {} for r in query_all(sql, tuple(params))]


def get_finding(job_seeker_id: str, finding_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM enrichment_finding WHERE id = ? AND job_seeker_id = ?",
            (finding_id, job_seeker_id),
        ),
        _FINDING_JSON,
    )


def finding_by_url(job_seeker_id: str, url: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM enrichment_finding WHERE job_seeker_id = ? AND url = ?",
            (job_seeker_id, url),
        ),
        _FINDING_JSON,
    )


def rejected_urls(job_seeker_id: str) -> set[str]:
    """URLs the job seeker rejected for good (FR-124) - never re-proposed."""
    rows = query_all(
        "SELECT url FROM enrichment_finding "
        "WHERE job_seeker_id = ? AND rejected_permanently = 1",
        (job_seeker_id,),
    )
    return {r["url"] for r in rows}


def save_finding(
    job_seeker_id: str,
    *,
    url: str,
    title: str | None,
    extracted_facts: Any,
    identity_signals: Any,
    identity_score: float,
    classification: str,
    status: str = "pending",
) -> str | None:
    """Insert or refresh one finding.

    A permanently rejected URL is left untouched and ``None`` is returned
    (FR-124): re-running enrichment must not resurrect it.  For a finding the
    job seeker has already ruled on, the evidence is refreshed but the
    decision is kept.
    """
    existing = query_one(
        "SELECT id, status, rejected_permanently FROM enrichment_finding "
        "WHERE job_seeker_id = ? AND url = ?",
        (job_seeker_id, url),
    )
    if existing and existing["rejected_permanently"]:
        return None

    values = {
        "title": title,
        "extracted_facts": to_json(extracted_facts),
        "identity_signals": to_json(identity_signals),
        "identity_score": float(identity_score),
        "classification": classification,
    }
    if existing:
        if existing["status"] == "pending":
            values["status"] = status
        update_row("enrichment_finding", existing["id"], values)
        return existing["id"]

    return insert_row(
        "enrichment_finding",
        {
            "job_seeker_id": job_seeker_id,
            "url": url,
            "status": status,
            "rejected_permanently": 0,
            "created_at": utcnow(),
            **values,
        },
    )


def set_finding_status(
    job_seeker_id: str, finding_id: str, status: str, *, permanent: bool = False
) -> dict | None:
    """Record the job seeker's confirm/reject decision (FR-124)."""
    row = get_finding(job_seeker_id, finding_id)
    if row is None:
        return None
    update_row(
        "enrichment_finding",
        finding_id,
        {"status": status, "rejected_permanently": 1 if permanent else 0},
    )
    return get_finding(job_seeker_id, finding_id)


def usable_findings(job_seeker_id: str) -> list[dict]:
    """Findings that may feed the composite profile (FR-124).

    Auto-merged confirmed findings plus anything the job seeker accepted.
    Pending 'probable'/'doubtful' findings are deliberately excluded.
    """
    rows = query_all(
        "SELECT * FROM enrichment_finding "
        "WHERE job_seeker_id = ? AND rejected_permanently = 0 "
        "AND (status = 'accepted' OR (status = 'pending' AND classification = 'confirmed')) "
        "ORDER BY identity_score DESC",
        (job_seeker_id,),
    )
    return [_decode(r, _FINDING_JSON) or {} for r in rows]


def delete_findings(job_seeker_id: str, *, keep_rejections: bool = True) -> int:
    sql = "DELETE FROM enrichment_finding WHERE job_seeker_id = ?"
    if keep_rejections:
        sql += " AND rejected_permanently = 0"
    return execute(sql, (job_seeker_id,))


# ---------------------------------------------------------------------------
# composite_profile (FR-121, FR-125)
# ---------------------------------------------------------------------------


def next_composite_version(job_seeker_id: str) -> int:
    row = query_one(
        "SELECT MAX(version) AS v FROM composite_profile WHERE job_seeker_id = ?",
        (job_seeker_id,),
    )
    return int(row["v"] or 0) + 1 if row else 1


def insert_composite(job_seeker_id: str, values: dict) -> str:
    # The version is allocated in the insert's own transaction: reading MAX and
    # inserting separately could collide on UNIQUE(job_seeker_id, version).
    return insert_row_versioned(
        "composite_profile",
        {"job_seeker_id": job_seeker_id, "created_at": utcnow(), **values},
        "SELECT MAX(version) FROM composite_profile WHERE job_seeker_id = ?",
        (job_seeker_id,),
    )


def get_composite(job_seeker_id: str, composite_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM composite_profile WHERE id = ? AND job_seeker_id = ?",
            (composite_id, job_seeker_id),
        ),
        _COMPOSITE_JSON,
    )


def latest_composite(job_seeker_id: str, persona_id: str | None = None) -> dict | None:
    sql = "SELECT * FROM composite_profile WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if persona_id:
        sql += " AND persona_id = ?"
        params.append(persona_id)
    sql += " ORDER BY version DESC LIMIT 1"
    return _decode(query_one(sql, tuple(params)), _COMPOSITE_JSON)


def list_composites(job_seeker_id: str) -> list[dict]:
    rows = query_all(
        "SELECT id, version, persona_id, profile_version_id, edited_by_user, created_at "
        "FROM composite_profile WHERE job_seeker_id = ? ORDER BY version DESC",
        (job_seeker_id,),
    )
    return rows


def update_composite(job_seeker_id: str, composite_id: str, values: dict) -> dict | None:
    """Apply the job seeker's edits (FR-125) and mark the row as user-edited."""
    if get_composite(job_seeker_id, composite_id) is None:
        return None
    payload = {k: (to_json(v) if isinstance(v, (dict, list)) else v) for k, v in values.items()}
    payload["edited_by_user"] = 1
    update_row("composite_profile", composite_id, payload)
    return get_composite(job_seeker_id, composite_id)


# ---------------------------------------------------------------------------
# dream_job_model (FR-128)
# ---------------------------------------------------------------------------


def next_dream_version(job_seeker_id: str) -> int:
    row = query_one(
        "SELECT MAX(version) AS v FROM dream_job_model WHERE job_seeker_id = ?",
        (job_seeker_id,),
    )
    return int(row["v"] or 0) + 1 if row else 1


def insert_dream_model(job_seeker_id: str, values: dict) -> str:
    return insert_row_versioned(
        "dream_job_model",
        {"job_seeker_id": job_seeker_id, "created_at": utcnow(), **values},
        "SELECT MAX(version) FROM dream_job_model WHERE job_seeker_id = ?",
        (job_seeker_id,),
    )


def get_dream_model(job_seeker_id: str, model_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM dream_job_model WHERE id = ? AND job_seeker_id = ?",
            (model_id, job_seeker_id),
        ),
        _DREAM_JSON,
    )


def latest_dream_model(
    job_seeker_id: str, persona_id: str | None = None, *, confirmed_only: bool = False
) -> dict | None:
    """The row other slices read (planning, discovery, speculative, scoring)."""
    sql = "SELECT * FROM dream_job_model WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if persona_id:
        sql += " AND persona_id = ?"
        params.append(persona_id)
    if confirmed_only:
        sql += " AND confirmed_by_user = 1"
    sql += " ORDER BY version DESC LIMIT 1"
    return _decode(query_one(sql, tuple(params)), _DREAM_JSON)


def list_dream_models(job_seeker_id: str) -> list[dict]:
    return query_all(
        "SELECT id, version, persona_id, confirmed_by_user, created_at "
        "FROM dream_job_model WHERE job_seeker_id = ? ORDER BY version DESC",
        (job_seeker_id,),
    )


def update_dream_model(job_seeker_id: str, model_id: str, values: dict) -> dict | None:
    if get_dream_model(job_seeker_id, model_id) is None:
        return None
    payload = {k: (to_json(v) if isinstance(v, (dict, list)) else v) for k, v in values.items()}
    update_row("dream_job_model", model_id, payload)
    return get_dream_model(job_seeker_id, model_id)


def confirm_dream_model(job_seeker_id: str, model_id: str) -> dict | None:
    """FR-128: the model is shown for confirmation before it drives planning."""
    return update_dream_model(job_seeker_id, model_id, {"confirmed_by_user": 1})


# ---------------------------------------------------------------------------
# Neighbouring reads
# ---------------------------------------------------------------------------


def get_profile_version(job_seeker_id: str, profile_version_id: str) -> dict | None:
    row = query_one(
        "SELECT * FROM profile_version WHERE id = ? AND job_seeker_id = ?",
        (profile_version_id, job_seeker_id),
    )
    return _decode(row, ("sections",))


def latest_profile_version(job_seeker_id: str, persona_id: str | None = None) -> dict | None:
    sql = "SELECT * FROM profile_version WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if persona_id:
        sql += " AND persona_id = ?"
        params.append(persona_id)
    sql += " ORDER BY version DESC LIMIT 1"
    return _decode(query_one(sql, tuple(params)), ("sections",))


def seeker_display_name(job_seeker_id: str) -> str:
    """The job seeker's name, for the identity anchors (FR-122, FR-123).

    ``profile_version`` carries no name of its own, so the anchor builder falls
    back to the account name.  It lives here because the pipeline issues no SQL
    of its own (CR-408).
    """
    row = query_one("SELECT display_name FROM job_seeker WHERE id = ?", (job_seeker_id,))
    return str(row["display_name"]) if row and row["display_name"] else ""


def disclosure_paths(job_seeker_id: str) -> set[str]:
    """Fallback for ``profiles.do_not_disclose_paths`` (FR-106)."""
    rows = query_all(
        "SELECT field_path FROM disclosure_flag "
        "WHERE job_seeker_id = ? AND do_not_disclose = 1",
        (job_seeker_id,),
    )
    return {r["field_path"] for r in rows}


def consent_granted(job_seeker_id: str, kind: str) -> bool:
    """Fallback for ``auth_service.has_consent`` (CR-410)."""
    row = query_one(
        "SELECT granted FROM consent_record WHERE job_seeker_id = ? AND kind = ? "
        "ORDER BY granted_at DESC LIMIT 1",
        (job_seeker_id, kind),
    )
    return bool(row and row["granted"])


def enrichment_enabled(job_seeker_id: str) -> bool:
    """FR-126: the online-enrichment switch, stored as a consent decision.

    Enrichment is opt-in, matching how every other outbound decision in the
    application is recorded (``consent_record``, CR-410): no decision means no
    searching.  Switching it off writes an explicit ``granted = 0`` row, so a
    withdrawal stays visible in the audit trail rather than looking like a
    profile that was never asked.
    """
    return consent_granted(job_seeker_id, "enrichment")


def set_enrichment_enabled(job_seeker_id: str, enabled: bool, detail: str | None = None) -> None:
    insert_row(
        "consent_record",
        {
            "job_seeker_id": job_seeker_id,
            "kind": "enrichment",
            "granted": 1 if enabled else 0,
            "detail": detail or ("online enrichment enabled" if enabled else "disabled by user"),
            "granted_at": utcnow(),
        },
    )
