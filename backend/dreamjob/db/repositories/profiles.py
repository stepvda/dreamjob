"""Profile, skills, evidence and persona persistence (FR-101..109, FR-441, FR-442).

Every function here takes ``job_seeker_id`` first and every statement filters
on it: this module is the only place profile SQL is written, so tenant
isolation is checked in one file rather than argued about in twenty (FR-101,
FR-344, CR-408).

``do_not_disclose_paths`` is the FR-106 export that the document-generation and
LLM slices import; it returns the set of profile field paths that must never
reach generated CVs, motivation letters or emails.
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    execute,
    from_json,
    insert_row,
    new_id,
    query_all,
    query_one,
    to_json,
    update_row,
    utcnow,
    write_tx,
)

_JSON_COLUMNS = {
    "sections": dict,
    "directive_defaults": dict,
    "linked_skills": list,
    "linked_achievements": list,
    "evidence_refs": list,
}


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for column, empty in _JSON_COLUMNS.items():
        if column in out:
            out[column] = from_json(out[column], empty())
    return out


def _decode_all(rows: list[dict]) -> list[dict]:
    return [_decode(r) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Profile versions (FR-105, FR-109)
# ---------------------------------------------------------------------------


def latest_version(job_seeker_id: str, persona_id: str | None = None) -> dict | None:
    if persona_id:
        return _decode(
            query_one(
                "SELECT * FROM profile_version WHERE job_seeker_id = ? AND persona_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (job_seeker_id, persona_id),
            )
        )
    return _decode(
        query_one(
            "SELECT * FROM profile_version WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (job_seeker_id,),
        )
    )


def get_version(job_seeker_id: str, version_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM profile_version WHERE id = ? AND job_seeker_id = ?",
            (version_id, job_seeker_id),
        )
    )


def list_versions(job_seeker_id: str, limit: int = 100) -> list[dict]:
    return query_all(
        "SELECT id, version, source_note, photo_path, persona_id, created_at "
        "FROM profile_version WHERE job_seeker_id = ? ORDER BY version DESC LIMIT ?",
        (job_seeker_id, limit),
    )


def create_version(
    job_seeker_id: str,
    sections: dict[str, Any],
    *,
    source_note: str,
    dream_job_statement: str | None = None,
    photo_path: str | None = None,
    persona_id: str | None = None,
) -> dict:
    """Append a profile version.  FR-105: a save never overwrites history.

    The version number is allocated inside the write transaction so two
    concurrent saves cannot collide on ``UNIQUE (job_seeker_id, version)``.
    """
    row_id = new_id()
    with write_tx() as conn:
        cur = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM profile_version "
            "WHERE job_seeker_id = ?",
            (job_seeker_id,),
        )
        version = int(cur.fetchone()["next"])
        cur.close()
        conn.execute(
            "INSERT INTO profile_version "
            "(id, job_seeker_id, persona_id, version, sections, dream_job_statement, "
            " photo_path, source_note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id, job_seeker_id, persona_id, version, to_json(sections),
                dream_job_statement, photo_path, source_note, utcnow(),
            ),
        )
    return get_version(job_seeker_id, row_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Retained source documents (DR-102, FR-108)
# ---------------------------------------------------------------------------


def list_sources(job_seeker_id: str) -> list[dict]:
    return query_all(
        "SELECT * FROM profile_source WHERE job_seeker_id = ? ORDER BY kind",
        (job_seeker_id,),
    )


def record_source(job_seeker_id: str, record: dict[str, Any]) -> dict:
    """Register a retained original, replacing the previous one of its kind.

    The row is what makes the file visible to erasure (FR-108); the path
    itself is also carried in the profile version's metadata so a rebuild can
    read it without a second query.
    """
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO profile_source "
            "(id, job_seeker_id, kind, filename, file_path, sha256, byte_size, retained_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_seeker_id, kind) DO UPDATE SET "
            "filename = excluded.filename, file_path = excluded.file_path, "
            "sha256 = excluded.sha256, byte_size = excluded.byte_size, "
            "retained_at = excluded.retained_at",
            (
                new_id(), job_seeker_id, record["kind"], record["filename"], record["path"],
                record["sha256"], int(record["byte_size"]), record["retained_at"],
            ),
        )
    return query_one(  # type: ignore[return-value]
        "SELECT * FROM profile_source WHERE job_seeker_id = ? AND kind = ?",
        (job_seeker_id, record["kind"]),
    )


def delete_source(job_seeker_id: str, kind: str) -> int:
    return execute(
        "DELETE FROM profile_source WHERE job_seeker_id = ? AND kind = ?", (job_seeker_id, kind)
    )


# ---------------------------------------------------------------------------
# Do-not-disclose flags (FR-106)
# ---------------------------------------------------------------------------


def do_not_disclose_paths(job_seeker_id: str) -> set[str]:
    """Profile paths that must never appear in generated content (FR-106).

    Imported by the document-generation and LLM slices; the returned paths are
    dotted paths into ``profile_version.sections`` (``contact.photo``,
    ``experience.2.description``) and are matched case-insensitively.
    """
    rows = query_all(
        "SELECT field_path FROM disclosure_flag "
        "WHERE job_seeker_id = ? AND do_not_disclose = 1",
        (job_seeker_id,),
    )
    return {str(r["field_path"]) for r in rows}


def list_disclosure_flags(job_seeker_id: str) -> list[dict]:
    return query_all(
        "SELECT * FROM disclosure_flag WHERE job_seeker_id = ? ORDER BY field_path",
        (job_seeker_id,),
    )


def set_disclosure_flag(
    job_seeker_id: str,
    field_path: str,
    *,
    do_not_disclose: bool = True,
    reason: str | None = None,
) -> dict:
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO disclosure_flag "
            "(id, job_seeker_id, field_path, do_not_disclose, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_seeker_id, field_path) DO UPDATE SET "
            "do_not_disclose = excluded.do_not_disclose, reason = excluded.reason",
            (new_id(), job_seeker_id, field_path, int(do_not_disclose), reason, utcnow()),
        )
    return query_one(  # type: ignore[return-value]
        "SELECT * FROM disclosure_flag WHERE job_seeker_id = ? AND field_path = ?",
        (job_seeker_id, field_path),
    )


def delete_disclosure_flag(job_seeker_id: str, field_path: str) -> int:
    return execute(
        "DELETE FROM disclosure_flag WHERE job_seeker_id = ? AND field_path = ?",
        (job_seeker_id, field_path),
    )


def llm_transfer_consented(job_seeker_id: str) -> bool:
    """CR-410: profile data reaches a non-EU model only with explicit consent."""
    row = query_one(
        "SELECT granted FROM consent_record WHERE job_seeker_id = ? AND kind = 'llm_transfer' "
        "ORDER BY granted_at DESC LIMIT 1",
        (job_seeker_id,),
    )
    return bool(row and row["granted"])


# ---------------------------------------------------------------------------
# Merge conflicts (FR-103)
# ---------------------------------------------------------------------------


def replace_conflicts(
    job_seeker_id: str, profile_version_id: str, conflicts: list[dict]
) -> list[dict]:
    """Store the conflicts found for one version, preserving earlier decisions.

    A resolution the job seeker already made for the same field path is
    carried forward, so re-uploading a document does not ask the same question
    twice.
    """
    previous = {
        r["field_path"]: r
        for r in query_all(
            "SELECT field_path, resolution, resolved_value FROM profile_conflict "
            "WHERE job_seeker_id = ? AND resolution IS NOT NULL AND resolution != 'unresolved'",
            (job_seeker_id,),
        )
    }
    with write_tx() as conn:
        conn.execute(
            "DELETE FROM profile_conflict WHERE job_seeker_id = ? AND profile_version_id = ?",
            (job_seeker_id, profile_version_id),
        )
        for conflict in conflicts:
            earlier = previous.get(conflict["field_path"])
            conn.execute(
                "INSERT INTO profile_conflict "
                "(id, job_seeker_id, profile_version_id, field_path, value_linkedin, "
                " value_cv, resolution, resolved_value, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(), job_seeker_id, profile_version_id, conflict["field_path"],
                    conflict.get("value_linkedin"), conflict.get("value_cv"),
                    (earlier or {}).get("resolution") or "unresolved",
                    (earlier or {}).get("resolved_value"),
                    utcnow(),
                ),
            )
    return list_conflicts(job_seeker_id, profile_version_id=profile_version_id)


def list_conflicts(
    job_seeker_id: str,
    *,
    profile_version_id: str | None = None,
    unresolved_only: bool = False,
) -> list[dict]:
    sql = "SELECT * FROM profile_conflict WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if profile_version_id:
        sql += " AND profile_version_id = ?"
        params.append(profile_version_id)
    if unresolved_only:
        sql += " AND (resolution IS NULL OR resolution = 'unresolved')"
    return query_all(sql + " ORDER BY field_path", tuple(params))


def resolve_conflict(
    job_seeker_id: str, conflict_id: str, resolution: str, resolved_value: str | None
) -> dict | None:
    execute(
        "UPDATE profile_conflict SET resolution = ?, resolved_value = ? "
        "WHERE id = ? AND job_seeker_id = ?",
        (resolution, resolved_value, conflict_id, job_seeker_id),
    )
    return query_one(
        "SELECT * FROM profile_conflict WHERE id = ? AND job_seeker_id = ?",
        (conflict_id, job_seeker_id),
    )


# ---------------------------------------------------------------------------
# Skills (FR-107)
# ---------------------------------------------------------------------------


def replace_skills(job_seeker_id: str, profile_version_id: str, skills: list[dict]) -> list[dict]:
    with write_tx() as conn:
        conn.execute(
            "DELETE FROM profile_skill WHERE job_seeker_id = ? AND profile_version_id = ?",
            (job_seeker_id, profile_version_id),
        )
        for skill in skills:
            conn.execute(
                "INSERT INTO profile_skill "
                "(id, job_seeker_id, profile_version_id, raw_label, normalised_label, "
                " taxonomy, taxonomy_code, proficiency, years_experience, last_used_year, "
                " evidence_refs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(), job_seeker_id, profile_version_id, skill["raw_label"],
                    skill["normalised_label"], skill.get("taxonomy", "esco"),
                    skill.get("taxonomy_code"), skill.get("proficiency"),
                    skill.get("years_experience"), skill.get("last_used_year"),
                    to_json(skill.get("evidence_refs") or []),
                ),
            )
    return list_skills(job_seeker_id, profile_version_id=profile_version_id)


def list_skills(job_seeker_id: str, *, profile_version_id: str | None = None) -> list[dict]:
    if profile_version_id is None:
        latest = latest_version(job_seeker_id)
        if latest is None:
            return []
        profile_version_id = latest["id"]
    return _decode_all(
        query_all(
            "SELECT * FROM profile_skill WHERE job_seeker_id = ? AND profile_version_id = ? "
            "ORDER BY proficiency DESC, normalised_label",
            (job_seeker_id, profile_version_id),
        )
    )


def update_skill(job_seeker_id: str, skill_id: str, values: dict[str, Any]) -> dict | None:
    allowed = {
        k: v
        for k, v in values.items()
        if k in {"normalised_label", "proficiency", "years_experience", "last_used_year",
                 "taxonomy", "taxonomy_code", "evidence_refs"}
    }
    row = query_one(
        "SELECT id FROM profile_skill WHERE id = ? AND job_seeker_id = ?",
        (skill_id, job_seeker_id),
    )
    if row is None:
        return None
    if allowed:
        update_row("profile_skill", skill_id, allowed)
    return _decode(
        query_one(
            "SELECT * FROM profile_skill WHERE id = ? AND job_seeker_id = ?",
            (skill_id, job_seeker_id),
        )
    )


def delete_skill(job_seeker_id: str, skill_id: str) -> int:
    return execute(
        "DELETE FROM profile_skill WHERE id = ? AND job_seeker_id = ?", (skill_id, job_seeker_id)
    )


# ---------------------------------------------------------------------------
# Evidence items (FR-441)
# ---------------------------------------------------------------------------

EVIDENCE_KINDS = frozenset(
    {"repository", "publication", "talk", "case_study", "article", "reference", "certificate"}
)


def list_evidence(job_seeker_id: str, *, skill: str | None = None) -> list[dict]:
    rows = _decode_all(
        query_all(
            "SELECT * FROM evidence_item WHERE job_seeker_id = ? ORDER BY created_at DESC",
            (job_seeker_id,),
        )
    )
    if skill:
        needle = skill.lower()
        rows = [r for r in rows if any(needle == str(s).lower() for s in r["linked_skills"])]
    return rows


def get_evidence(job_seeker_id: str, evidence_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM evidence_item WHERE id = ? AND job_seeker_id = ?",
            (evidence_id, job_seeker_id),
        )
    )


def create_evidence(job_seeker_id: str, values: dict[str, Any]) -> dict:
    evidence_id = insert_row(
        "evidence_item",
        {
            "job_seeker_id": job_seeker_id,
            "kind": values["kind"],
            "title": values["title"],
            "url": values.get("url"),
            "file_path": values.get("file_path"),
            "description": values.get("description"),
            "linked_skills": values.get("linked_skills") or [],
            "linked_achievements": values.get("linked_achievements") or [],
            "verification_status": values.get("verification_status") or "unverified",
            "created_at": utcnow(),
        },
    )
    return get_evidence(job_seeker_id, evidence_id)  # type: ignore[return-value]


def update_evidence(job_seeker_id: str, evidence_id: str, values: dict[str, Any]) -> dict | None:
    if get_evidence(job_seeker_id, evidence_id) is None:
        return None
    allowed = {
        k: v
        for k, v in values.items()
        if k in {"kind", "title", "url", "file_path", "description", "linked_skills",
                 "linked_achievements", "verification_status"}
    }
    if allowed:
        update_row("evidence_item", evidence_id, allowed)
    return get_evidence(job_seeker_id, evidence_id)


def delete_evidence(job_seeker_id: str, evidence_id: str) -> int:
    return execute(
        "DELETE FROM evidence_item WHERE id = ? AND job_seeker_id = ?",
        (evidence_id, job_seeker_id),
    )


# ---------------------------------------------------------------------------
# Personas (FR-442)
# ---------------------------------------------------------------------------


def list_personas(job_seeker_id: str) -> list[dict]:
    return _decode_all(
        query_all(
            "SELECT * FROM persona WHERE job_seeker_id = ? ORDER BY is_default DESC, name",
            (job_seeker_id,),
        )
    )


def get_persona(job_seeker_id: str, persona_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM persona WHERE id = ? AND job_seeker_id = ?",
            (persona_id, job_seeker_id),
        )
    )


def default_persona(job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM persona WHERE job_seeker_id = ? AND is_default = 1 LIMIT 1",
            (job_seeker_id,),
        )
    )


def create_persona(job_seeker_id: str, values: dict[str, Any]) -> dict:
    """Create a persona.  The first one is the default (FR-442)."""
    existing = list_personas(job_seeker_id)
    is_default = bool(values.get("is_default")) or not existing
    persona_id = insert_row(
        "persona",
        {
            "job_seeker_id": job_seeker_id,
            "name": values["name"],
            "emphasis": values.get("emphasis"),
            "dream_job_statement": values.get("dream_job_statement"),
            "directive_defaults": values.get("directive_defaults") or {},
            "is_default": int(is_default),
            "created_at": utcnow(),
        },
    )
    if is_default:
        set_default_persona(job_seeker_id, persona_id)
    return get_persona(job_seeker_id, persona_id)  # type: ignore[return-value]


def update_persona(job_seeker_id: str, persona_id: str, values: dict[str, Any]) -> dict | None:
    if get_persona(job_seeker_id, persona_id) is None:
        return None
    allowed = {
        k: v
        for k, v in values.items()
        if k in {"name", "emphasis", "dream_job_statement", "directive_defaults"}
    }
    if allowed:
        update_row("persona", persona_id, allowed)
    if values.get("is_default"):
        set_default_persona(job_seeker_id, persona_id)
    return get_persona(job_seeker_id, persona_id)


def set_default_persona(job_seeker_id: str, persona_id: str) -> dict | None:
    """Exactly one persona is the default; flipping one clears the others."""
    if get_persona(job_seeker_id, persona_id) is None:
        return None
    with write_tx() as conn:
        conn.execute(
            "UPDATE persona SET is_default = 0 WHERE job_seeker_id = ? AND id != ?",
            (job_seeker_id, persona_id),
        )
        conn.execute(
            "UPDATE persona SET is_default = 1 WHERE job_seeker_id = ? AND id = ?",
            (job_seeker_id, persona_id),
        )
    return get_persona(job_seeker_id, persona_id)


def delete_persona(job_seeker_id: str, persona_id: str) -> int:
    persona = get_persona(job_seeker_id, persona_id)
    if persona is None:
        return 0
    deleted = execute(
        "DELETE FROM persona WHERE id = ? AND job_seeker_id = ?", (persona_id, job_seeker_id)
    )
    if persona.get("is_default"):
        remaining = list_personas(job_seeker_id)
        if remaining:
            set_default_persona(job_seeker_id, remaining[0]["id"])
    return deleted
