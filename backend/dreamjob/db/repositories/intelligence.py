"""Dream-job intelligence, LinkedIn advice and event-radar SQL.

(FR-381, FR-382, FR-384, FR-443, FR-462, FR-101, FR-344, CR-408)

All SQL for this slice lives here.  The split that matters is the one the
schema makes: ``gap_analysis``, ``stepping_stone_path``, ``linkedin_advice``
and ``event_interest`` are **private** and every statement filters on
``job_seeker_id``; ``event`` is a **shared** knowledge-base table and carries
no link back to the job seeker who caused it to be collected (FR-344), so the
seeker's interest in an event is a separate private row rather than a column
on the event itself.
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
)

GAP_JSON_COLUMNS = ("gaps",)
STONE_JSON_COLUMNS = ("steps", "basis")
ADVICE_JSON_COLUMNS = ("headlines", "skills", "keywords", "featured", "notes", "corpus_summary")
EVENT_JSON_COLUMNS = ("speakers", "linked_company_ids", "topics", "attendees")


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


# ---------------------------------------------------------------------------
# FR-381: gap analysis
# ---------------------------------------------------------------------------


def save_gap_analysis(job_seeker_id: str, values: dict[str, Any]) -> str:
    """Replace this campaign's gap analysis with a freshly computed one.

    One row per (job seeker, campaign): the analysis is a reading of the market
    as it stands, not a history, and FR-381 links its gaps to the opportunities
    of that campaign.
    """
    campaign_id = values.get("campaign_id")
    existing = query_one(
        "SELECT id FROM gap_analysis WHERE job_seeker_id = ? AND IFNULL(campaign_id, '') = ?",
        (job_seeker_id, campaign_id or ""),
    )
    now = utcnow()
    payload = {**values, "job_seeker_id": job_seeker_id, "updated_at": now}
    if existing:
        update_row("gap_analysis", existing["id"], payload)
        return str(existing["id"])
    payload["created_at"] = now
    return insert_row("gap_analysis", payload)


def get_gap_analysis(job_seeker_id: str, campaign_id: str | None = None) -> dict | None:
    if campaign_id:
        row = query_one(
            "SELECT * FROM gap_analysis WHERE job_seeker_id = ? AND campaign_id = ?",
            (job_seeker_id, campaign_id),
        )
    else:
        row = query_one(
            "SELECT * FROM gap_analysis WHERE job_seeker_id = ? "
            "ORDER BY IFNULL(updated_at, created_at) DESC LIMIT 1",
            (job_seeker_id,),
        )
    return _decode(row, GAP_JSON_COLUMNS)


def list_gap_analyses(job_seeker_id: str, limit: int = 20) -> list[dict]:
    return _decode_all(
        query_all(
            "SELECT * FROM gap_analysis WHERE job_seeker_id = ? "
            "ORDER BY IFNULL(updated_at, created_at) DESC LIMIT ?",
            (job_seeker_id, int(limit)),
        ),
        GAP_JSON_COLUMNS,
    )


# ---------------------------------------------------------------------------
# FR-382: stepping-stone paths
# ---------------------------------------------------------------------------


def replace_stepping_stones(
    job_seeker_id: str, campaign_id: str | None, paths: list[dict[str, Any]]
) -> list[str]:
    """Write the proposed paths for one campaign, replacing the previous set."""
    execute(
        "DELETE FROM stepping_stone_path WHERE job_seeker_id = ? AND IFNULL(campaign_id, '') = ?",
        (job_seeker_id, campaign_id or ""),
    )
    now = utcnow()
    ids: list[str] = []
    for path in paths:
        ids.append(
            insert_row(
                "stepping_stone_path",
                {
                    **path,
                    "job_seeker_id": job_seeker_id,
                    "campaign_id": campaign_id,
                    "created_at": now,
                },
            )
        )
    return ids


def list_stepping_stones(job_seeker_id: str, campaign_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM stepping_stone_path WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY created_at ASC, name ASC"
    return _decode_all(query_all(sql, tuple(params)), STONE_JSON_COLUMNS)


# ---------------------------------------------------------------------------
# FR-381/FR-382 inputs: the campaign's own opportunities
# ---------------------------------------------------------------------------

_OPPORTUNITY_SELECT = """
    SELECT o.id, o.title, o.kind, o.function_family, o.seniority, o.description,
           o.company_id, o.vacancy_id, o.language, o.tags, o.score, o.score_profile_fit,
           o.score_dream_fit, o.score_directive_fit, o.score_company, o.score_reachability,
           o.dream_fit_detail, o.score_detail, o.user_status, o.pinned, o.selected,
           o.work_arrangement, o.country, o.location, o.created_at,
           o.required_skills, o.desirable_skills, o.source_url,
           c.name AS company_name, c.domain AS company_domain, c.country AS company_country,
           c.size_band AS company_size_band, c.stage AS company_stage,
           v.required_skills AS vacancy_required_skills,
           v.desirable_skills AS vacancy_desirable_skills,
           v.description AS vacancy_description,
           v.title AS vacancy_title,
           v.language AS vacancy_language,
           v.source_url AS vacancy_source_url
    FROM opportunity o
    LEFT JOIN company c ON c.id = o.company_id
    LEFT JOIN vacancy v ON v.id = o.vacancy_id
"""

_OPPORTUNITY_JSON = (
    "tags",
    "dream_fit_detail",
    "score_detail",
    "required_skills",
    "desirable_skills",
    "vacancy_required_skills",
    "vacancy_desirable_skills",
)


def campaign_opportunities(
    job_seeker_id: str, campaign_id: str | None = None, limit: int = 2000
) -> list[dict]:
    """Opportunities with the vacancy text the gap analysis reads (FR-381)."""
    sql = _OPPORTUNITY_SELECT + " WHERE o.job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND o.campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY IFNULL(o.score, 0) DESC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), _OPPORTUNITY_JSON)


def opportunity_tags(opportunity_id: str, job_seeker_id: str) -> list[str] | None:
    row = query_one(
        "SELECT tags FROM opportunity WHERE id = ? AND job_seeker_id = ?",
        (opportunity_id, job_seeker_id),
    )
    if row is None:
        return None
    return from_json(row["tags"], []) or []


def set_opportunity_tags(opportunity_id: str, job_seeker_id: str, tags: list[str]) -> int:
    """FR-382: write the destination / stepping-stone tag.

    Filtered on ``job_seeker_id`` in the statement itself so a tag can never be
    written onto another seeker's row (FR-101, FR-344).
    """
    return execute(
        "UPDATE opportunity SET tags = ?, updated_at = ? WHERE id = ? AND job_seeker_id = ?",
        (to_json(tags), utcnow(), opportunity_id, job_seeker_id),
    )


# ---------------------------------------------------------------------------
# Profile inputs shared by FR-381 and FR-443
# ---------------------------------------------------------------------------


def latest_composite(job_seeker_id: str, persona_id: str | None = None) -> dict | None:
    sql = "SELECT * FROM composite_profile WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if persona_id:
        sql += " AND persona_id = ?"
        params.append(persona_id)
    sql += " ORDER BY version DESC LIMIT 1"
    return query_one(sql, tuple(params))


def latest_dream_model(job_seeker_id: str, persona_id: str | None = None) -> dict | None:
    sql = "SELECT * FROM dream_job_model WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if persona_id:
        sql += " AND persona_id = ?"
        params.append(persona_id)
    sql += " ORDER BY version DESC LIMIT 1"
    return query_one(sql, tuple(params))


def latest_profile_version(job_seeker_id: str) -> dict | None:
    row = query_one(
        "SELECT * FROM profile_version WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
        (job_seeker_id,),
    )
    return _decode(row, ("sections",))


def profile_skills(job_seeker_id: str) -> list[dict]:
    return query_all(
        "SELECT raw_label, normalised_label, proficiency, years_experience, last_used_year "
        "FROM profile_skill WHERE job_seeker_id = ?",
        (job_seeker_id,),
    )


def evidence_items(job_seeker_id: str) -> list[dict]:
    return _decode_all(
        query_all(
            "SELECT id, kind, title, url, description, linked_skills, verification_status "
            "FROM evidence_item WHERE job_seeker_id = ? ORDER BY created_at DESC",
            (job_seeker_id,),
        ),
        ("linked_skills",),
    )


def get_campaign(campaign_id: str, job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM campaign WHERE id = ? AND job_seeker_id = ?", (campaign_id, job_seeker_id)
    )


def latest_campaign(job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM campaign WHERE job_seeker_id = ? ORDER BY created_at DESC LIMIT 1",
        (job_seeker_id,),
    )


def directive_set(job_seeker_id: str, directive_set_id: str | None) -> dict | None:
    """The campaign's directives, or the newest set when none is pinned (FR-385)."""
    if directive_set_id:
        row = query_one(
            "SELECT * FROM directive_set WHERE id = ? AND job_seeker_id = ?",
            (directive_set_id, job_seeker_id),
        )
        if row:
            return row
    return query_one(
        "SELECT * FROM directive_set WHERE job_seeker_id = ? ORDER BY created_at DESC LIMIT 1",
        (job_seeker_id,),
    )


def directives_in_force(
    job_seeker_id: str, campaign_id: str | None = None
) -> tuple[dict | None, dict | None]:
    """The campaign in force and the directive set it runs under (FR-385).

    One resolution, used by everything that has to say whether this search is a
    discreet one: the named campaign, or the latest one, pins its directives,
    and without a campaign the newest set is what the job seeker is editing.
    The session-wide indicator in the application shell and the per-screen
    badges both come through here, so they cannot disagree.
    """
    campaign = (
        get_campaign(campaign_id, job_seeker_id) if campaign_id else latest_campaign(job_seeker_id)
    )
    return campaign, directive_set(job_seeker_id, (campaign or {}).get("directive_set_id"))


def get_company(company_id: str) -> dict | None:
    return query_one("SELECT * FROM company WHERE id = ?", (company_id,))


def company_vacancy_language_counts(company_id: str) -> list[dict]:
    return query_all(
        "SELECT language, COUNT(*) AS n FROM vacancy WHERE company_id = ? AND language IS NOT NULL "
        "GROUP BY language ORDER BY n DESC",
        (company_id,),
    )


# ---------------------------------------------------------------------------
# FR-443: LinkedIn profile advice
# ---------------------------------------------------------------------------


def save_advice(job_seeker_id: str, values: dict[str, Any]) -> str:
    now = utcnow()
    return insert_row(
        "linkedin_advice",
        {**values, "job_seeker_id": job_seeker_id, "created_at": now, "updated_at": now},
    )


def get_advice(advice_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM linkedin_advice WHERE id = ? AND job_seeker_id = ?",
            (advice_id, job_seeker_id),
        ),
        ADVICE_JSON_COLUMNS,
    )


def latest_advice(job_seeker_id: str, campaign_id: str | None = None) -> dict | None:
    sql = "SELECT * FROM linkedin_advice WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY created_at DESC LIMIT 1"
    return _decode(query_one(sql, tuple(params)), ADVICE_JSON_COLUMNS)


def list_advice(job_seeker_id: str, limit: int = 20) -> list[dict]:
    return _decode_all(
        query_all(
            "SELECT * FROM linkedin_advice WHERE job_seeker_id = ? ORDER BY created_at DESC "
            "LIMIT ?",
            (job_seeker_id, int(limit)),
        ),
        ADVICE_JSON_COLUMNS,
    )


def save_advice_edit(advice_id: str, job_seeker_id: str, text: str) -> dict | None:
    """FR-443: the seeker's own edit of the draft.  Nothing is sent to LinkedIn."""
    now = utcnow()
    execute(
        "UPDATE linkedin_advice SET edited_text = ?, edited_at = ?, updated_at = ? "
        "WHERE id = ? AND job_seeker_id = ?",
        (text, now, now, advice_id, job_seeker_id),
    )
    return get_advice(advice_id, job_seeker_id)


def delete_advice(advice_id: str, job_seeker_id: str) -> int:
    return execute(
        "DELETE FROM linkedin_advice WHERE id = ? AND job_seeker_id = ?",
        (advice_id, job_seeker_id),
    )


# ---------------------------------------------------------------------------
# FR-462: events (shared) and the seeker's interest in them (private)
# ---------------------------------------------------------------------------


def get_event(event_id: str) -> dict | None:
    return _decode(query_one("SELECT * FROM event WHERE id = ?", (event_id,)), EVENT_JSON_COLUMNS)


def upcoming_events(
    *, since: str, until: str | None = None, limit: int = 500, countries: list[str] | None = None
) -> list[dict]:
    """Shared events that have not happened yet, soonest first (FR-462)."""
    sql = "SELECT * FROM event WHERE IFNULL(starts_at, '') >= ?"
    params: list[Any] = [since]
    if until:
        sql += " AND IFNULL(starts_at, '') <= ?"
        params.append(until)
    if countries:
        marks = ", ".join("?" for _ in countries)
        sql += f" AND (country IS NULL OR UPPER(country) IN ({marks}))"
        params.extend(c.upper() for c in countries)
    sql += " ORDER BY starts_at ASC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), EVENT_JSON_COLUMNS)


def events_for_company(company_id: str, limit: int = 50) -> list[dict]:
    """Events linked to one company profile (FR-462)."""
    return _decode_all(
        query_all(
            "SELECT * FROM event WHERE IFNULL(linked_company_ids, '') LIKE ? "
            "ORDER BY starts_at ASC LIMIT ?",
            (f'%"{company_id}"%', int(limit)),
        ),
        EVENT_JSON_COLUMNS,
    )


def link_event_companies(event_id: str, company_ids: list[str]) -> None:
    update_row("event", event_id, {"linked_company_ids": company_ids, "refreshed_at": utcnow()})


def companies_by_ids(company_ids: list[str]) -> list[dict]:
    if not company_ids:
        return []
    marks = ", ".join("?" for _ in company_ids)
    return query_all(
        f"SELECT id, name, normalised_name, domain, country, key_people FROM company "
        f"WHERE id IN ({marks})",
        tuple(company_ids),
    )


def companies_by_names(names: list[str]) -> list[dict]:
    """Resolve organiser/speaker affiliations onto profiled companies (FR-462)."""
    cleaned = [n.strip().lower() for n in names if n and n.strip()]
    if not cleaned:
        return []
    marks = ", ".join("?" for _ in cleaned)
    return query_all(
        f"SELECT id, name, normalised_name, domain, country FROM company "
        f"WHERE lower(normalised_name) IN ({marks}) OR lower(name) IN ({marks})",
        (*cleaned, *cleaned),
    )


def upsert_interest(job_seeker_id: str, event_id: str, values: dict[str, Any]) -> str:
    now = utcnow()
    existing = query_one(
        "SELECT id FROM event_interest WHERE job_seeker_id = ? AND event_id = ?",
        (job_seeker_id, event_id),
    )
    if existing:
        update_row("event_interest", existing["id"], {**values, "updated_at": now})
        return str(existing["id"])
    return insert_row(
        "event_interest",
        {
            **values,
            "job_seeker_id": job_seeker_id,
            "event_id": event_id,
            "created_at": now,
            "updated_at": now,
        },
    )


def get_interest(job_seeker_id: str, event_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM event_interest WHERE job_seeker_id = ? AND event_id = ?",
        (job_seeker_id, event_id),
    )


def list_interests(job_seeker_id: str, *, status: str | None = None) -> list[dict]:
    sql = (
        "SELECT i.*, e.name AS event_name, e.starts_at, e.ends_at, e.location, e.url "
        "FROM event_interest i JOIN event e ON e.id = i.event_id WHERE i.job_seeker_id = ?"
    )
    params: list[Any] = [job_seeker_id]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    sql += " ORDER BY e.starts_at ASC"
    return query_all(sql, tuple(params))


def delete_interest(job_seeker_id: str, event_id: str) -> int:
    return execute(
        "DELETE FROM event_interest WHERE job_seeker_id = ? AND event_id = ?",
        (job_seeker_id, event_id),
    )


def campaign_company_ids(job_seeker_id: str, campaign_id: str | None = None) -> list[str]:
    """The target companies of a campaign - the ones the radar matches against."""
    sql = (
        "SELECT DISTINCT company_id FROM opportunity "
        "WHERE job_seeker_id = ? AND company_id IS NOT NULL"
    )
    params: list[Any] = [job_seeker_id]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    return [r["company_id"] for r in query_all(sql, tuple(params))]


def contacts_for_companies(company_ids: list[str]) -> list[dict]:
    if not company_ids:
        return []
    marks = ", ".join("?" for _ in company_ids)
    return query_all(
        f"SELECT id, company_id, full_name, role_title, email, linkedin_url, objected "
        f"FROM contact WHERE company_id IN ({marks})",
        tuple(company_ids),
    )


# ---------------------------------------------------------------------------
# Settings (FR-382's configurable threshold)
# ---------------------------------------------------------------------------


def get_setting(key: str) -> str | None:
    row = query_one("SELECT value FROM app_setting WHERE key = ?", (key,))
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    now = utcnow()
    if query_one("SELECT key FROM app_setting WHERE key = ?", (key,)):
        execute("UPDATE app_setting SET value = ?, updated_at = ? WHERE key = ?", (value, now, key))
        return
    execute(
        "INSERT INTO app_setting (key, value, updated_at) VALUES (?, ?, ?)", (key, value, now)
    )
