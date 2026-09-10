"""Campaign, plan and source-catalogue SQL (FR-161..166, FR-185, NFR-403).

Campaigns and their source plans are *private* data: every read here takes a
``job_seeker_id`` and filters on it, except the deliberately named
``*_any`` helpers, which background jobs use once the request that owns them
has already been authorised (FR-101, FR-344).
"""

from __future__ import annotations

from typing import Any

from dreamjob.db.connection import (
    execute,
    from_json,
    insert_row,
    query_all,
    query_one,
    update_row,
    utcnow,
)

CAMPAIGN_JSON_COLUMNS = ("caps", "reuse_report")
PLAN_JSON_COLUMNS = ("native_query", "caps")


def _decode(row: dict | None, json_columns: tuple[str, ...]) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in json_columns:
        if col in out:
            out[col] = from_json(out[col], None)
    return out


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def create_campaign(job_seeker_id: str, values: dict) -> str:
    payload = dict(values)
    payload["job_seeker_id"] = job_seeker_id
    payload.setdefault("status", "draft")
    payload.setdefault("created_at", utcnow())
    return insert_row("campaign", payload)


def get_campaign(campaign_id: str, job_seeker_id: str) -> dict | None:
    row = query_one(
        "SELECT * FROM campaign WHERE id = ? AND job_seeker_id = ?", (campaign_id, job_seeker_id)
    )
    return _decode(row, CAMPAIGN_JSON_COLUMNS)


def get_campaign_any(campaign_id: str) -> dict | None:
    """Background-job read: the request that scheduled the job authorised it."""
    row = query_one("SELECT * FROM campaign WHERE id = ?", (campaign_id,))
    return _decode(row, CAMPAIGN_JSON_COLUMNS)


def list_campaigns(job_seeker_id: str, limit: int = 100) -> list[dict]:
    rows = query_all(
        "SELECT * FROM campaign WHERE job_seeker_id = ? ORDER BY created_at DESC LIMIT ?",
        (job_seeker_id, limit),
    )
    return [_decode(r, CAMPAIGN_JSON_COLUMNS) or {} for r in rows]


def update_campaign(campaign_id: str, values: dict) -> None:
    update_row("campaign", campaign_id, values)


def set_stage(campaign_id: str, stage: str, status: str | None = None) -> None:
    values: dict[str, Any] = {"stage": stage}
    if status:
        values["status"] = status
        if status == "running":
            row = query_one("SELECT started_at FROM campaign WHERE id = ?", (campaign_id,))
            if row and not row["started_at"]:
                values["started_at"] = utcnow()
        if status in ("completed", "cancelled", "failed"):
            values["finished_at"] = utcnow()
    update_row("campaign", campaign_id, values)


def delete_campaign(campaign_id: str, job_seeker_id: str) -> int:
    return execute(
        "DELETE FROM campaign WHERE id = ? AND job_seeker_id = ?", (campaign_id, job_seeker_id)
    )


# ---------------------------------------------------------------------------
# Planning inputs (read-only views of other slices' private tables)
# ---------------------------------------------------------------------------


def load_planning_inputs(campaign: dict) -> dict:
    """Everything the planner grounds its queries in (FR-162).

    Falls back to the newest composite profile / dream job model when the
    campaign does not pin one, so a plan can be generated before those steps
    have been re-confirmed.
    """
    seeker = campaign["job_seeker_id"]
    directives = query_one(
        "SELECT * FROM directive_set WHERE id = ? AND job_seeker_id = ?",
        (campaign.get("directive_set_id"), seeker),
    )
    composite = None
    if campaign.get("composite_profile_id"):
        composite = query_one(
            "SELECT * FROM composite_profile WHERE id = ? AND job_seeker_id = ?",
            (campaign["composite_profile_id"], seeker),
        )
    if composite is None:
        composite = query_one(
            "SELECT * FROM composite_profile WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (seeker,),
        )
    dream = None
    if campaign.get("dream_job_model_id"):
        dream = query_one(
            "SELECT * FROM dream_job_model WHERE id = ? AND job_seeker_id = ?",
            (campaign["dream_job_model_id"], seeker),
        )
    if dream is None:
        dream = query_one(
            "SELECT * FROM dream_job_model WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
            (seeker,),
        )
    profile = query_one(
        "SELECT * FROM profile_version WHERE id = ? AND job_seeker_id = ?",
        (campaign.get("profile_version_id"), seeker),
    )
    return {
        "directives": directives,
        "composite_profile": composite,
        "dream_job_model": dream,
        "profile_version": profile,
        "do_not_disclose": do_not_disclose_paths(seeker),
        # The company inventory the company-scoped sources are planned from
        # (ATS boards, website crawls, registry filings).  Shared knowledge, not
        # this seeker's own data, so it carries no ``job_seeker_id`` (FR-344).
        "companies_in_scope": companies_in_scope(),
    }


# Columns of ``company`` that a plan item may be seeded from.  A collection
# seed is an identity plus a route, never a profile.
_COMPANY_SEED_COLUMNS = (
    "id", "name", "domain", "careers_url", "country", "jurisdiction",
    "legal_id", "legal_id_type", "vat_number", "ats_vendor", "ats_slug",
)


def companies_in_scope(countries: list[str] | None = None, limit: int = 200) -> list[dict]:
    """Knowledge-base companies this campaign may collect against (FR-162, FR-342).

    The planner needs an inventory before it can plan a company-scoped source:
    an ATS board needs a slug, a website crawl needs a home page, a registry
    needs a legal identifier.  Without this the planner had nothing to name and
    every one of those sources was planned with an empty query, which is why
    they all reported "done" having fetched nothing.
    """
    columns = ", ".join(_COMPANY_SEED_COLUMNS)
    sql = f"SELECT {columns} FROM company"
    params: list[Any] = []
    codes = [c.upper()[:2] for c in (countries or []) if c]
    if codes:
        marks = ", ".join("?" for _ in codes)
        sql += f" WHERE (country IS NULL OR country IN ({marks}))"
        params.extend(codes)
    sql += " ORDER BY COALESCE(refreshed_at, collected_at) DESC LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


def companies_with_ats_board(vendor: str | None = None, limit: int = 200) -> list[dict]:
    """Companies whose ATS board is already known (FR-162).

    ``website.crawl`` fills ``ats_vendor``/``ats_slug`` through ``detect_ats``;
    this is the read-back that lets the harvest stage use what discovery found,
    in the same run or in the next campaign.
    """
    columns = ", ".join(_COMPANY_SEED_COLUMNS)
    sql = (
        f"SELECT {columns} FROM company "
        "WHERE ats_slug IS NOT NULL AND TRIM(ats_slug) <> '' "
        "AND ats_vendor IS NOT NULL AND TRIM(ats_vendor) <> ''"
    )
    params: list[Any] = []
    if vendor:
        sql += " AND LOWER(ats_vendor) = ?"
        params.append(vendor.lower())
    sql += " ORDER BY COALESCE(refreshed_at, collected_at) DESC LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


def do_not_disclose_paths(job_seeker_id: str) -> set[str]:
    """FR-106: fields that must be stripped before anything leaves the machine."""
    rows = query_all(
        "SELECT field_path FROM disclosure_flag WHERE job_seeker_id = ? AND do_not_disclose = 1",
        (job_seeker_id,),
    )
    return {r["field_path"] for r in rows}


def has_consent(job_seeker_id: str, kind: str) -> bool:
    row = query_one(
        "SELECT granted FROM consent_record WHERE job_seeker_id = ? AND kind = ? "
        "ORDER BY granted_at DESC LIMIT 1",
        (job_seeker_id, kind),
    )
    return bool(row and row["granted"])


# ---------------------------------------------------------------------------
# Source catalogue (FR-161, FR-164, NFR-403)
# ---------------------------------------------------------------------------


def list_catalogue(enabled_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM source_catalogue"
    if enabled_only:
        sql += " WHERE enabled = 1 AND (requires_ack = 0 OR acknowledged_at IS NOT NULL)"
    sql += " ORDER BY source_type, adapter_key"
    rows = query_all(sql)
    for row in rows:
        row["coverage_countries"] = from_json(row.get("coverage_countries"), []) or []
        row["coverage_industries"] = from_json(row.get("coverage_industries"), []) or []
        row["query_capabilities"] = from_json(row.get("query_capabilities"), {}) or {}
    return rows


def get_catalogue_entry(adapter_key: str) -> dict | None:
    row = query_one("SELECT * FROM source_catalogue WHERE adapter_key = ?", (adapter_key,))
    if row:
        row["coverage_countries"] = from_json(row.get("coverage_countries"), []) or []
        row["coverage_industries"] = from_json(row.get("coverage_industries"), []) or []
        row["query_capabilities"] = from_json(row.get("query_capabilities"), {}) or {}
    return row


def record_extraction_rate(adapter_key: str, rate: float | None, had_success: bool) -> None:
    """NFR-403: keep a rolling extraction-success rate per adapter."""
    row = query_one(
        "SELECT extraction_success_rate FROM source_catalogue WHERE adapter_key = ?", (adapter_key,)
    )
    if row is None:
        return
    values: dict[str, Any] = {"updated_at": utcnow()}
    if rate is not None:
        previous = row["extraction_success_rate"]
        values["extraction_success_rate"] = (
            rate if previous is None else round(0.7 * float(previous) + 0.3 * rate, 4)
        )
    if had_success:
        values["last_success_at"] = utcnow()
    sets = ", ".join(f"{k} = :{k}" for k in values)
    values["__key"] = adapter_key
    execute(f"UPDATE source_catalogue SET {sets} WHERE adapter_key = :__key", values)


# ---------------------------------------------------------------------------
# Source plan items (FR-163, FR-166)
# ---------------------------------------------------------------------------


class UnknownSource(ValueError):
    """A plan item was written for a source this installation has never heard of.

    Raised by :func:`insert_plan_item` (FR-161, FR-164; plan item C5).
    """


#: Sources whose implementation is not a ``SourceAdapter``.  The FR-165
#: LinkedIn network strategy is planned as a plan item like every other source
#: but is executed by the browser job rather than by the collection spine, so
#: it is never in the adapter registry and the guard below must not mistake it
#: for a fixture.  The keys are written out rather than imported because this
#: module is on the write path of every plan item and ``dreamjob.browser``
#: pulls in the automation stack; ``tests/unit/test_plan_fixture_leak.py``
#: asserts they still match ``browser.linkedin.ADAPTER_KEY`` and
#: ``browser.glassdoor.ADAPTER_KEY``, so the shortcut cannot drift in silence.
BROWSER_STRATEGY_KEYS = frozenset({"linkedin_network", "glassdoor"})


def implemented_sources() -> set[str]:
    """Every source key this installation can actually execute (NFR-601, FR-165).

    Empty when no adapter module has been imported: the registry fills itself
    at import time, so an empty one means "nothing is knowable yet", never
    "nothing exists".  Callers treat the empty set as "do not judge".
    """
    from dreamjob.adapters.base import all_adapters  # noqa: PLC0415 - import cycle

    registered = set(all_adapters())
    return registered | set(BROWSER_STRATEGY_KEYS) if registered else set()


def refuse_unknown_source(adapter_key: str) -> None:
    """A plan may only name a source something knows about (FR-161, FR-164, C5).

    Two things can vouch for a source, and a plan item needs one of them:

    * an **implementation** - an adapter in the registry, or one of the browser
      strategies that are executed outside it;
    * a **catalogue row** - a source an administrator can see, acknowledge or
      switch off (FR-363).  A catalogued source whose adapter has gone is
      deliberately still plannable: collection settles it as ``skipped`` and
      ``admin.prune_unknown_sources`` removes the row at the next start-up
      unless a person decided to keep it.  Refusing it here would break a plan
      over a row that is already being handled honestly elsewhere.

    A key with neither is residue.  ``broken_board``, ``stub_board`` and eight
    ``spine.*`` stubs are unit-test adapters that reached the installed
    catalogue through ``sync_catalogue`` (C5); the catalogue rows were pruned,
    but the 104 plan items they had produced stayed behind in 32 campaigns
    (migration 132), naming sources that nothing implements and nothing
    catalogues: unrunnable, unexplainable, and 104 of the 538 outcomes the
    FR-185 dashboard had to account for.  A row like that must not be written
    in the first place.

    This is the second net, not the first: a stub registered by a test *is* an
    implementation as far as this function can see, so what keeps fixtures out
    of the installed database is ``tests/unit/conftest.py`` pointing the whole
    unit suite at a scratch file.  This one catches what gets past that.
    """
    known = implemented_sources()
    if not known or adapter_key in known:
        return
    if adapter_key and get_catalogue_entry(adapter_key) is not None:
        return
    raise UnknownSource(
        f"Nothing knows the source {adapter_key!r}: it has no adapter and no catalogue row, "
        f"so a plan item for it could never run and could never be explained (FR-164, C5). "
        f"Implemented sources: {', '.join(sorted(known))}."
    )


def insert_plan_item(campaign_id: str, values: dict, *, allow_unimplemented: bool = False) -> str:
    """Write one plan item, refusing a source nothing knows about (FR-163, FR-164).

    ``allow_unimplemented`` is the deliberate exception, and it is not a way
    round the guard: a plan outlives the code that ran it, so a *stored* item
    may legitimately name an adapter that has since been deleted or renamed -
    collection settles those as skipped - and the tests that cover that path
    have to be able to write one.  Nothing in the application passes it.
    """
    payload = dict(values)
    payload["campaign_id"] = campaign_id
    payload.setdefault("status", "planned")
    payload.setdefault("created_at", utcnow())
    if not allow_unimplemented:
        refuse_unknown_source(str(payload.get("adapter_key") or ""))
    return insert_row("source_plan_item", payload)


def list_plan_items(campaign_id: str, include_excluded: bool = True) -> list[dict]:
    """The campaign's plan, in the order collection must execute it (FR-181).

    Ordering is by *stage* first (see :func:`plan_item_stage`), because the plan
    is a dependency chain and not a flat list: registries, directories and job
    boards discover companies, the website crawl reads their careers pages and
    fills ``company.ats_vendor``/``ats_slug``, and only then can an ATS board be
    read.  Ordering by ``created_at, adapter_key`` alone ran that chain
    backwards - every row of a plan is written in the same second, so the
    tie-break was alphabetical and ``ats.*`` came first.
    """
    sql = "SELECT * FROM source_plan_item WHERE campaign_id = ?"
    if not include_excluded:
        sql += " AND excluded_by_user = 0"
    sql += " ORDER BY created_at, adapter_key"
    rows = [_decode(r, PLAN_JSON_COLUMNS) or {} for r in query_all(sql, (campaign_id,))]
    return sorted(rows, key=lambda r: (plan_item_stage(r), r.get("created_at") or "",
                                       r.get("adapter_key") or ""))


# Execution stages (FR-181).  Discovery first, deepening second, harvesting
# last, so a source that needs a company runs after the sources that find one.
STAGE_DISCOVER = 1
STAGE_DEEPEN = 2
STAGE_HARVEST = 3

STAGE_BY_SOURCE_TYPE: dict[str, int] = {
    "registry": STAGE_DISCOVER,
    "directory": STAGE_DISCOVER,
    "job_board": STAGE_DISCOVER,
    "compensation": STAGE_DISCOVER,
    "website": STAGE_DEEPEN,
    "news": STAGE_DEEPEN,
    "events": STAGE_DEEPEN,
    "linkedin": STAGE_DEEPEN,
    "ats": STAGE_HARVEST,
}

# Fall-back for rows planned before the planner recorded a stage of its own.
_STAGE_BY_PREFIX = (
    ("ats.", STAGE_HARVEST),
    ("website.", STAGE_DEEPEN),
    ("news.", STAGE_DEEPEN),
    ("events.", STAGE_DEEPEN),
    ("linkedin", STAGE_DEEPEN),
)


def plan_item_stage(item: dict) -> int:
    """Which collection stage this plan item belongs to (FR-181).

    The planner stamps ``caps["stage"]``; a row from an older plan is placed by
    its adapter key so that ordering never depends on a migration.
    """
    caps = item.get("caps")
    if isinstance(caps, dict):
        try:
            stage = int(caps.get("stage"))
        except (TypeError, ValueError):
            stage = 0
        if stage:
            return stage
    key = str(item.get("adapter_key") or "")
    for prefix, stage in _STAGE_BY_PREFIX:
        if key.startswith(prefix):
            return stage
    return STAGE_DISCOVER


def get_plan_item(plan_item_id: str, campaign_id: str | None = None) -> dict | None:
    if campaign_id:
        row = query_one(
            "SELECT * FROM source_plan_item WHERE id = ? AND campaign_id = ?",
            (plan_item_id, campaign_id),
        )
    else:
        row = query_one("SELECT * FROM source_plan_item WHERE id = ?", (plan_item_id,))
    return _decode(row, PLAN_JSON_COLUMNS)


def update_plan_item(plan_item_id: str, values: dict) -> None:
    update_row("source_plan_item", plan_item_id, values)


def bump_plan_item(
    plan_item_id: str, *, records: int = 0, errors: int = 0, last_error: str | None = None
) -> None:
    execute(
        "UPDATE source_plan_item SET records_collected = records_collected + ?, "
        "error_count = error_count + ?, last_error = COALESCE(?, last_error) WHERE id = ?",
        (records, errors, last_error[:2000] if last_error else None, plan_item_id),
    )


def delete_plan_items(campaign_id: str, keep_ids: list[str] | None = None) -> int:
    if keep_ids:
        marks = ", ".join("?" for _ in keep_ids)
        return execute(
            f"DELETE FROM source_plan_item WHERE campaign_id = ? AND id NOT IN ({marks})",
            (campaign_id, *keep_ids),
        )
    return execute("DELETE FROM source_plan_item WHERE campaign_id = ?", (campaign_id,))


def plan_items_with_provenance(campaign_id: str) -> set[str]:
    """Plan items a collected record still points at (FR-166).

    Deleting one of these would leave the record with a dangling provenance
    link, so re-planning keeps the row instead.
    """
    rows = query_all(
        "SELECT DISTINCT s.id AS id FROM source_plan_item s "
        "JOIN provenance p ON p.source_plan_item_id = s.id WHERE s.campaign_id = ?",
        (campaign_id,),
    )
    return {r["id"] for r in rows}


def reset_plan_progress(campaign_id: str) -> int:
    """NFR-603: re-running collection starts from a clean per-item counter.

    The activity stamps go with the counters.  Left behind, the second run's
    feed opens with the first run's chronology - lines dated hours before the
    job it claims to be reporting on (FR-361).
    """
    return execute(
        "UPDATE source_plan_item SET status = 'planned', records_collected = 0, "
        "error_count = 0, last_error = NULL, activity_at = NULL, activity_kind = NULL "
        "WHERE campaign_id = ? AND excluded_by_user = 0",
        (campaign_id,),
    )


# ---------------------------------------------------------------------------
# Dashboard (FR-185, FR-361)
# ---------------------------------------------------------------------------


def list_plan_activity(campaign_id: str, since: str, limit: int) -> list[dict]:
    """What each source of this campaign last did, newest first (FR-361).

    ``activity_at >= ''`` excludes NULL on its own, so the range stays pure and
    the index is used end to end.  ``>=`` and not ``>``: :func:`utcnow` is
    second-resolution and a wave settles a dozen sources inside one second, so
    an exclusive cursor drops most of them; the caller dedupes on the event id.
    """
    rows = query_all(
        "SELECT s.id AS plan_item_id, s.activity_at AS at, s.activity_kind AS kind, "
        "s.adapter_key AS adapter_key, s.native_query AS native_query, "
        "s.outcome_reason AS outcome_reason, s.last_error AS last_error, "
        "s.records_collected AS records_collected, s.error_count AS error_count, "
        "c.display_name AS display_name, c.source_type AS source_type "
        "FROM source_plan_item s "
        "LEFT JOIN source_catalogue c ON c.adapter_key = s.adapter_key "
        "WHERE s.campaign_id = ? AND s.activity_at >= ? "
        "ORDER BY s.activity_at DESC, s.id DESC LIMIT ?",
        (campaign_id, since or "", limit),
    )
    return [_decode(row, ("native_query",)) or {} for row in rows]


def list_campaign_audit(campaign_id: str, since: str, limit: int) -> list[dict]:
    """The campaign's own milestones (FR-361).

    ``detail`` is decoded here rather than by the caller, so that every JSON
    column this module hands out has been decoded in the same place.
    """
    rows = query_all(
        "SELECT id, action, detail, created_at AS at FROM audit_event "
        "WHERE entity_type = 'campaign' AND entity_id = ? AND created_at >= ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (campaign_id, since or "", limit),
    )
    return [_decode(row, ("detail",)) or {} for row in rows]


def llm_totals(campaign_id: str) -> dict:
    row = query_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens, "
        "COALESCE(SUM(cost_eur), 0) AS cost_eur FROM llm_call WHERE campaign_id = ?",
        (campaign_id,),
    )
    return dict(row or {"calls": 0, "tokens": 0, "cost_eur": 0.0})


def collected_counts(campaign_id: str) -> dict[str, int]:
    rows = query_all(
        "SELECT p.entity_type AS entity_type, COUNT(DISTINCT p.entity_id) AS n FROM provenance p "
        "JOIN source_plan_item s ON s.id = p.source_plan_item_id "
        "WHERE s.campaign_id = ? GROUP BY p.entity_type",
        (campaign_id,),
    )
    return {r["entity_type"]: int(r["n"]) for r in rows}


def list_jobs(campaign_id: str, limit: int = 20) -> list[dict]:
    return query_all(
        "SELECT * FROM job_run WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
        (campaign_id, limit),
    )


def latest_job(campaign_id: str, kind: str | None = None) -> dict | None:
    if kind:
        return query_one(
            "SELECT * FROM job_run WHERE campaign_id = ? AND kind = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (campaign_id, kind),
        )
    return query_one(
        "SELECT * FROM job_run WHERE campaign_id = ? ORDER BY created_at DESC LIMIT 1",
        (campaign_id,),
    )


def record_audit(
    action: str,
    *,
    job_seeker_id: str | None = None,
    actor: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict | None = None,
) -> str:
    return insert_row(
        "audit_event",
        {
            "job_seeker_id": job_seeker_id,
            "actor": actor or "system",
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": detail,
            "created_at": utcnow(),
        },
    )
