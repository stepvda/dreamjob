"""Hiring-contact SQL (FR-301..FR-306, FR-461, NFR-302, NFR-303, RK-08).

Three data domains meet in this module and the rules differ for each:

``contact`` (SHARED, restricted)
    Professional contact details of third parties.  FR-306 fixes the column
    set - name, role, company, professional e-mail, source, date - and
    :data:`MINIMAL_COLUMNS` is the gate every write passes through, so a caller
    that hands over a phone number, a personal address or a scraped biography
    simply loses it.  That gate is the RK-08 control.

``contact_objection`` (SHARED, permanent)
    NFR-302 blocks an address for good.  Because NFR-303 deletes the contact
    row when the campaign's retention deadline passes, the block cannot live on
    the contact row alone; it lives here and the migration's triggers re-apply
    it to any re-collected copy.  :func:`usable_contacts_for_company` and
    :func:`best_contacts` read the ``usable_contact`` view, so the generation
    slice inherits the block through its query rather than through the UI.

``network_member`` / ``introduction_path`` (PRIVATE)
    The job seeker's own network and the routes built from it.  Every statement
    filters on ``job_seeker_id`` (FR-101, FR-344).

NFR-303 retention is enforced by :func:`sweep_retention`, which deletes both
browser-collected contacts and network members whose ``retention_until`` has
passed.  It is a plain function so the monitoring scheduler can call it.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.db.connection import (
    execute,
    from_json,
    insert_row,
    query_all,
    query_one,
    to_json,
    update_row,
    upsert_row,
    utcnow,
    write_tx,
)

log = logging.getLogger(__name__)

#: FR-306 / RK-08 - the only columns a discovered contact may occupy.
#:
#: ``full_name``/``role_title``/``department`` are the person's function,
#: ``company_id`` the employer, ``email`` the professional address, the
#: ``email_*`` columns the FR-303/FR-304 method and verdict, ``source`` and
#: ``collected_at`` the provenance FR-306 asks for, and the retention trio the
#: NFR-303 scope.  Nothing else about a human being is stored.
MINIMAL_COLUMNS = frozenset(
    {
        "company_id",
        "full_name",
        "role_title",
        "department",
        "email",
        "email_source_method",
        "email_validation",
        "email_validation_detail",
        "email_validated_at",
        "email_uncertain",
        "is_generic_mailbox",
        "linkedin_url",
        "source",
        "access_method",
        "shareable",
        "owning_campaign_id",
        "retention_until",
        "collected_at",
        "confidence",
    }
)

#: Columns of ``network_member`` a caller may write (the rest are managed here).
_NETWORK_COLUMNS = frozenset(
    {
        "full_name",
        "headline",
        "role_title",
        "company_name",
        "company_id",
        "linkedin_url",
        "degree",
        "mutual_name",
        "mutual_linkedin",
        "schools",
        "employers",
        "communities",
        "connected_at",
        "last_interaction_at",
        "source",
        "access_method",
        "owning_campaign_id",
        "retention_until",
    }
)

_INTRO_COLUMNS = frozenset(
    {
        "opportunity_id",
        "company_id",
        "target_contact_id",
        "network_member_id",
        "intermediary_name",
        "intermediary_role",
        "intermediary_linkedin",
        "relationship",
        "degree",
        "strength",
        "relevance",
        "rationale",
        "message_draft",
        "message_subject",
        "message_generated_at",
        "status",
    }
)

_JSON_NETWORK_COLUMNS = ("schools", "employers", "communities")


def minimise(values: dict) -> dict:
    """Keep only the FR-306 columns.  Anything else is dropped, not stored."""
    dropped = sorted(set(values) - MINIMAL_COLUMNS - {"id"})
    if dropped:
        log.debug("FR-306: dropping non-minimal contact fields %s", dropped)
    return {k: v for k, v in values.items() if k in MINIMAL_COLUMNS}


def _decode_network(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for column in _JSON_NETWORK_COLUMNS:
        out[column] = from_json(out.get(column), []) or []
    return out


# ---------------------------------------------------------------------------
# Contacts (FR-301, FR-306)
# ---------------------------------------------------------------------------


def get_contact(contact_id: str) -> dict | None:
    return query_one("SELECT * FROM contact WHERE id = ?", (contact_id,))


def contact_owned_by(job_seeker_id: str, contact_id: str) -> bool:
    """True when the contact is this seeker's own or explicitly shared.

    A contact is campaign-scoped by default (NFR-303), so accepting an id from a
    request without this check would let one seeker make another's private
    address the recipient of their message.
    """
    row = query_one(
        "SELECT 1 FROM contact c LEFT JOIN campaign camp ON camp.id = c.owning_campaign_id "
        "WHERE c.id = ? AND (c.shareable = 1 OR camp.job_seeker_id = ?)",
        (contact_id, job_seeker_id),
    )
    return row is not None


def contacts_for_company(
    company_id: str, *, include_blocked: bool = False, job_seeker_id: str | None = None
) -> list[dict]:
    """Every stored contact of one company, newest first.

    ``job_seeker_id`` narrows campaign-scoped rows to the seeker's own
    campaigns.  Browser-collected contacts are private to the campaign that
    collected them (NFR-303); without this, a seeker with one opportunity at a
    company could read every other seeker's collected names and addresses.
    """
    table = "contact" if include_blocked else "usable_contact"
    sql = f"SELECT * FROM {table} WHERE company_id = ?"
    params: list[Any] = [company_id]
    if job_seeker_id:
        sql += (
            " AND (shareable = 1 OR owning_campaign_id IS NULL"
            " OR owning_campaign_id IN (SELECT id FROM campaign WHERE job_seeker_id = ?))"
        )
        params.append(job_seeker_id)
    sql += " ORDER BY confidence DESC, collected_at DESC"
    return query_all(sql, tuple(params))


def usable_contacts_for_company(
    company_id: str,
    *,
    campaign_id: str | None = None,
    job_seeker_id: str | None = None,
    require_email: bool = True,
) -> list[dict]:
    """The query the generation slice must use (NFR-302, NFR-303, FR-304).

    Reads the ``usable_contact`` view, so an objection blocks the address for
    every job seeker and an ``invalid`` verdict is never handed out - neither
    rule depends on the caller remembering it.  ``campaign_id`` additionally
    hides campaign-scoped rows belonging to somebody else's campaign, and
    ``job_seeker_id`` does the same for callers that know the seeker but not the
    campaign.
    """
    sql = "SELECT * FROM usable_contact WHERE company_id = ?"
    params: list[Any] = [company_id]
    if require_email:
        sql += " AND email IS NOT NULL AND email <> ''"
    sql += " AND (shareable = 1 OR owning_campaign_id IS NULL"
    if campaign_id:
        sql += " OR owning_campaign_id = ?"
        params.append(campaign_id)
    elif job_seeker_id:
        sql += (
            " OR owning_campaign_id IN (SELECT id FROM campaign WHERE job_seeker_id = ?)"
        )
        params.append(job_seeker_id)
    sql += ")"
    sql += " ORDER BY confidence DESC, collected_at DESC"
    return query_all(sql, tuple(params))


def contact_by_email(email: str) -> dict | None:
    return query_one("SELECT * FROM contact WHERE lower(email) = ? LIMIT 1", (email.lower(),))


def contact_by_linkedin(url: str) -> dict | None:
    return query_one("SELECT * FROM contact WHERE linkedin_url = ? LIMIT 1", (url,))


def known_addresses_on_domain(domain: str, limit: int = 200) -> list[dict]:
    """Named addresses on one domain - the evidence FR-303 infers a pattern from."""
    return query_all(
        "SELECT full_name, email, email_source_method FROM contact "
        "WHERE email LIKE ? AND full_name IS NOT NULL AND full_name <> '' "
        "ORDER BY collected_at DESC LIMIT ?",
        (f"%@{domain.lower()}", limit),
    )


# ---------------------------------------------------------------------------
# E-mail backfill for stored contacts that have none (FR-303, FR-304, NFR-303)
# ---------------------------------------------------------------------------
#
# The Contacts screen can hold a person - a hiring manager read off a team page,
# a name carried over from the company profile - long before it holds an
# address.  These three statements are the work list, its size and the guarded
# write the backfill pass needs.  The write is on ``contact`` rather than the
# ``usable_contact`` view because ``set_contact_email`` has to *set* an address
# on a row the view would hide (it has no e-mail, which is the whole point).

#: Orders for the missing-e-mail work list.  ``recent`` mirrors the browse
#: default; ``company`` groups one employer's nameless rows together so a single
#: domain resolution and site crawl serves all of its people (FR-305).
_MISSING_EMAIL_ORDERS: dict[str, str] = {
    "recent": "COALESCE(c.collected_at, '') DESC, c.id",
    "company": (
        "COALESCE(co.name, '') COLLATE NOCASE ASC, "
        "COALESCE(c.full_name, '') COLLATE NOCASE ASC, c.id"
    ),
}


def _missing_email_clauses(
    *, job_seeker_id: str | None, company_id: str | None
) -> tuple[list[str], list[Any]]:
    """The WHERE clause every missing-e-mail query shares (FR-344, NFR-303)."""
    clauses = ["(c.email IS NULL OR c.email = '')", "c.objected = 0"]
    params: list[Any] = []
    if company_id:
        clauses.append("c.company_id = ?")
        params.append(company_id)
    if job_seeker_id:
        # NFR-303 / FR-344, exactly as ``browse_contacts`` applies it: a
        # campaign-scoped row is visible to its own campaign only, shared rows
        # to everybody.
        clauses.append(
            "(c.shareable = 1 OR c.owning_campaign_id IS NULL"
            " OR c.owning_campaign_id IN (SELECT id FROM campaign WHERE job_seeker_id = ?))"
        )
        params.append(job_seeker_id)
    return clauses, params


def contacts_missing_email(
    limit: int = 500,
    *,
    job_seeker_id: str | None = None,
    company_id: str | None = None,
    order: str = "recent",
) -> list[dict]:
    """Stored contacts with no address yet, oldest selection first (FR-303).

    Reads ``contact`` directly rather than ``usable_contact``: the view hides a
    row precisely because it has no address, and this pass exists to fill that
    gap.  The objection flag and the seeker scope are therefore applied here, as
    they are in :func:`browse_contacts`.  The company fields travel with each
    row so the pass never has to re-read a company per person.
    """
    clauses, params = _missing_email_clauses(
        job_seeker_id=job_seeker_id, company_id=company_id
    )
    ordering = _MISSING_EMAIL_ORDERS.get(order, _MISSING_EMAIL_ORDERS["recent"])
    sql = (
        "SELECT c.*, co.name AS company_name, co.domain AS company_domain,"
        " co.careers_url AS company_careers_url, co.country AS company_country"
        " FROM contact c LEFT JOIN company co ON co.id = c.company_id"
        f" WHERE {' AND '.join(clauses)} ORDER BY {ordering} LIMIT ?"
    )
    return query_all(sql, (*params, int(limit)))


def contacts_missing_email_count(*, job_seeker_id: str | None = None) -> int:
    """How many contacts the backfill pass could still give an address (FR-303)."""
    clauses, params = _missing_email_clauses(job_seeker_id=job_seeker_id, company_id=None)
    row = query_one(
        f"SELECT COUNT(*) AS n FROM contact c WHERE {' AND '.join(clauses)}",
        tuple(params),
    )
    return int((row or {}).get("n") or 0)


def set_contact_email(
    contact_id: str,
    *,
    email: str,
    method: str,
    validation_result: str,
    validation_detail: dict | None = None,
    confidence: float | None = None,
) -> bool:
    """Store a newly found address on a contact that still has none (FR-303/304).

    The guard is in the statement, not in the caller: the row is updated only
    while it is still empty and not objected.  A concurrent pass that filled it
    first, or an NFR-302 objection that arrived in the meantime, therefore wins
    and this call is a no-op - which is what makes the whole pass idempotent and
    safe to resume.  Returns whether a row actually changed.

    A composed address (``pattern_inference``) is a hypothesis until FR-304 says
    ``valid``, so ``email_uncertain`` is set for every other verdict; a
    published or stated address is never uncertain.
    """
    address = (email or "").strip().lower()
    if not address:
        return False
    detail = validation_detail or {}
    uncertain = 1 if method == "pattern_inference" and validation_result != "valid" else 0
    values: dict[str, Any] = {
        "email": address,
        "email_source_method": method,
        "email_validation": validation_result,
        "email_validation_detail": to_json(detail) if detail else None,
        "email_validated_at": utcnow(),
        "is_generic_mailbox": 1 if detail.get("role") else 0,
        "email_uncertain": uncertain,
    }
    if confidence is not None:
        values["confidence"] = round(float(confidence), 3)
    payload = dict(values)
    payload["__id"] = contact_id
    sets = ", ".join(f"{k}=:{k}" for k in values)
    sql = (
        f"UPDATE contact SET {sets} WHERE id=:__id"
        " AND (email IS NULL OR email = '') AND objected = 0"
    )
    return execute(sql, payload) > 0


# ---------------------------------------------------------------------------
# Browsing the contact corpus (FR-301, FR-344, NFR-303, NFR-502)
# ---------------------------------------------------------------------------

#: Browsing orders.  ``recent`` is what the screen opens on; ``company`` groups
#: the addresses of one employer together so a reviewer can see the set.
_BROWSE_ORDERS: dict[str, str] = {
    "recent": "COALESCE(c.collected_at, '') DESC, c.confidence DESC, c.id",
    "name": "COALESCE(c.full_name, '') COLLATE NOCASE ASC, c.id",
    "company": (
        "COALESCE(co.name, '') COLLATE NOCASE ASC, "
        "COALESCE(c.full_name, '') COLLATE NOCASE ASC, c.id"
    ),
}


def _browse_filters(
    *,
    include_blocked: bool,
    job_seeker_id: str | None,
    q: str | None,
    company_id: str | None,
    validation: str | None,
    method: str | None,
    uncertain: int | None,
) -> tuple[list[str], list[Any]]:
    """The WHERE clauses every browse query shares, so list and facets agree."""
    clauses: list[str] = []
    params: list[Any] = []
    if not include_blocked and job_seeker_id:
        # NFR-303 / FR-344: a campaign-scoped row is visible to its own campaign
        # only; shared rows are visible to everybody.  ``usable_contact`` has
        # already removed objections (NFR-302) and invalid addresses (FR-304).
        clauses.append(
            "(c.shareable = 1 OR c.owning_campaign_id IS NULL"
            " OR c.owning_campaign_id IN (SELECT id FROM campaign WHERE job_seeker_id = ?))"
        )
        params.append(job_seeker_id)
    if q:
        like = f"%{q.strip()}%"
        clauses.append(
            "(c.full_name LIKE ? OR c.email LIKE ? OR COALESCE(co.name, '') LIKE ?)"
        )
        params += [like, like, like]
    if company_id:
        clauses.append("c.company_id = ?")
        params.append(company_id)
    if validation:
        clauses.append("COALESCE(c.email_validation, 'unknown') = ?")
        params.append(validation)
    if method:
        clauses.append("c.email_source_method = ?")
        params.append(method)
    if uncertain is not None:
        clauses.append("c.email_uncertain = ?")
        params.append(1 if int(uncertain) else 0)
    return clauses, params


def _browse_table(include_blocked: bool) -> str:
    return "contact" if include_blocked else "usable_contact"


def browse_contacts(
    *,
    q: str | None = None,
    company_id: str | None = None,
    validation: str | None = None,
    method: str | None = None,
    uncertain: int | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "recent",
    include_blocked: bool = False,
    job_seeker_id: str | None = None,
) -> tuple[list[dict], int]:
    """One page of the contact corpus, filtered, with its total (FR-301, NFR-502).

    ``include_blocked`` reads ``contact`` instead of the ``usable_contact``
    view, so an administrator can see objected (NFR-302) and ``invalid``
    (FR-304) rows; the router is where that is restricted to an administrator.
    Every other read goes through the view, which is what makes the exclusion
    impossible to forget.
    """
    table = _browse_table(include_blocked)
    clauses, params = _browse_filters(
        include_blocked=include_blocked,
        job_seeker_id=job_seeker_id,
        q=q,
        company_id=company_id,
        validation=validation,
        method=method,
        uncertain=uncertain,
    )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    base = f"FROM {table} c LEFT JOIN company co ON co.id = c.company_id{where}"
    total_row = query_one(f"SELECT COUNT(*) AS n {base}", tuple(params))
    total = int((total_row or {}).get("n") or 0)
    ordering = _BROWSE_ORDERS.get(order, _BROWSE_ORDERS["recent"])
    rows = query_all(
        f"SELECT c.*, co.name AS company_name {base} ORDER BY {ordering} LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    )
    return rows, total


def browse_facets(
    *,
    q: str | None = None,
    company_id: str | None = None,
    validation: str | None = None,
    method: str | None = None,
    uncertain: int | None = None,
    include_blocked: bool = False,
    job_seeker_id: str | None = None,
) -> dict[str, Any]:
    """Counts beside the browse list (FR-301).

    Each dimension is counted over the same filters *minus its own*, so a
    screen filtered to ``valid`` can still show how many risky and unknown
    addresses the same query would offer.
    """

    def _where(**overrides: Any) -> tuple[str, list[Any]]:
        values: dict[str, Any] = {
            "include_blocked": include_blocked,
            "job_seeker_id": job_seeker_id,
            "q": q,
            "company_id": company_id,
            "validation": validation,
            "method": method,
            "uncertain": uncertain,
        }
        values.update(overrides)
        clauses, params = _browse_filters(**values)
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    base_from = (
        f"FROM {_browse_table(include_blocked)} c "
        "LEFT JOIN company co ON co.id = c.company_id"
    )

    where, params = _where(validation=None)
    by_validation = {
        (row["bucket"] or "unknown"): int(row["n"])
        for row in query_all(
            "SELECT COALESCE(c.email_validation, 'unknown') AS bucket,"
            f" COUNT(*) AS n {base_from}{where} GROUP BY bucket",
            tuple(params),
        )
    }

    where, params = _where(method=None)
    by_method = {
        (row["bucket"] or "unknown"): int(row["n"])
        for row in query_all(
            "SELECT COALESCE(c.email_source_method, 'unknown') AS bucket,"
            f" COUNT(*) AS n {base_from}{where} GROUP BY bucket",
            tuple(params),
        )
    }

    where, params = _where(uncertain=None)
    row = query_one(
        f"SELECT SUM(CASE WHEN c.email_uncertain = 1 THEN 1 ELSE 0 END) AS n"
        f" {base_from}{where}",
        tuple(params),
    )
    return {
        "by_validation": by_validation,
        "by_method": by_method,
        "uncertain": int((row or {}).get("n") or 0),
    }


def upsert_contact(values: dict) -> tuple[str, bool]:
    """Insert or refresh one contact, minimised to FR-306.  Returns ``(id, created)``.

    Identity is the e-mail address where there is one, else the LinkedIn URL,
    else name plus company.  An existing objection is never overwritten: the
    row is left blocked (NFR-302) and its id returned so the caller can show
    why the contact is unavailable.
    """
    values = minimise(values)
    values.setdefault("collected_at", utcnow())
    if values.get("email"):
        values["email"] = str(values["email"]).strip().lower()

    # FR-303/FR-304: an address composed by pattern inference is a hypothesis
    # until a validation says otherwise.  A ``valid`` verdict clears the flag;
    # every other verdict - or none yet - leaves it set, and an explicit 1 is
    # respected.  An address that was published or stated rather than composed
    # is never uncertain, so the column is left alone for another method.
    if values.get("email_source_method") == "pattern_inference":
        if values.get("email_validation") == "valid":
            values["email_uncertain"] = 0
        elif values.get("email_uncertain") != 1:
            values["email_uncertain"] = 1

    existing: dict | None = None
    if values.get("email"):
        existing = contact_by_email(values["email"])
    if existing is None and values.get("linkedin_url"):
        existing = contact_by_linkedin(values["linkedin_url"])
    if existing is None and values.get("full_name") and values.get("company_id"):
        existing = query_one(
            "SELECT * FROM contact WHERE company_id = ? AND lower(full_name) = ? "
            "AND (email IS NULL OR email = '') LIMIT 1",
            (values["company_id"], values["full_name"].lower()),
        )

    if existing is None:
        return insert_row("contact", values), True

    if existing.get("objected"):
        log.info("Contact %s carries an objection; not refreshed (NFR-302)", existing["id"])
        return existing["id"], False

    merged = {k: v for k, v in values.items() if v not in (None, "", [], {})}
    merged["collected_at"] = utcnow()
    # NFR-303: a campaign-scoped row never widens its own scope on refresh.
    if existing.get("shareable") == 0:
        merged.pop("shareable", None)
    update_row("contact", existing["id"], merged)
    return existing["id"], False


def set_validation(contact_id: str, result: str, detail: dict | None = None) -> None:
    """Store the FR-304 verdict and keep FR-303 uncertainty in step.

    A composed address stops being uncertain only when the verdict is
    ``valid``; any other verdict - ``risky``, ``unknown`` or ``invalid`` -
    leaves the flag set.  The method is read from the row rather than imported
    from :mod:`dreamjob.pipeline.email_patterns`, which would be a circular
    import; ``'pattern_inference'`` is the literal FR-303 label either way.
    """
    row = query_one("SELECT email_source_method FROM contact WHERE id = ?", (contact_id,))
    method = (row or {}).get("email_source_method")
    uncertain = 1 if method == "pattern_inference" and result != "valid" else 0
    update_row(
        "contact",
        contact_id,
        {
            "email_validation": result,
            "email_validation_detail": to_json(detail) if detail else None,
            "email_validated_at": utcnow(),
            "email_uncertain": uncertain,
        },
    )


def set_confidence(contact_id: str, confidence: float) -> None:
    update_row("contact", contact_id, {"confidence": round(float(confidence), 3)})


def delete_contact(contact_id: str) -> int:
    return execute("DELETE FROM contact WHERE id = ?", (contact_id,))


# ---------------------------------------------------------------------------
# Objections (NFR-302)
# ---------------------------------------------------------------------------


def record_objection(
    email: str | None,
    *,
    linkedin_url: str | None = None,
    reason: str | None = None,
    source: str = "manual",
) -> dict:
    """Block a contact permanently.  Returns the stored objection.

    The address is the key because honouring the objection means recognising
    the address when it is collected again; no other detail about the person is
    kept (RK-08).  The migration's trigger flags every stored copy.
    """
    address = (email or "").strip().lower()
    if not address and not linkedin_url:
        raise ValueError("An objection needs an e-mail address or a LinkedIn URL")
    row = {
        "email": address or f"linkedin:{linkedin_url}",
        "domain": address.rsplit("@", 1)[-1] if "@" in address else None,
        "linkedin_url": linkedin_url,
        "reason": (reason or "")[:500] or None,
        "source": source,
        "objected_at": utcnow(),
    }
    upsert_row("contact_objection", row, ["email"])
    if linkedin_url:
        execute(
            "UPDATE contact SET objected = 1, objected_at = ? WHERE linkedin_url = ? "
            "AND objected = 0",
            (row["objected_at"], linkedin_url),
        )
    return row


def is_objected(email: str | None, linkedin_url: str | None = None) -> bool:
    """NFR-302: has this address (or person) objected?  Checked before every use."""
    if email:
        if query_one(
            "SELECT 1 AS hit FROM contact_objection WHERE email = ?", (email.strip().lower(),)
        ):
            return True
    if linkedin_url and query_one(
        "SELECT 1 AS hit FROM contact_objection WHERE linkedin_url = ?", (linkedin_url,)
    ):
        return True
    return False


def list_objections(limit: int = 500) -> list[dict]:
    return query_all(
        "SELECT * FROM contact_objection ORDER BY objected_at DESC LIMIT ?", (limit,)
    )


# ---------------------------------------------------------------------------
# Contact jobs (FR-185, FR-301, FR-303)
# ---------------------------------------------------------------------------


def find_reusable_contact_job(
    kind: str, job_seeker_id: str, scope: str, *, queued_marker: str
) -> dict | None:
    """The running or genuinely-queued contacts job for this seeker and scope.

    A job counts when it is ``running``, or ``pending`` carrying the runner's
    queued marker - ``jobs/runner.py`` writes that marker when it hands a job
    to the in-memory pool, so a queued job can be found again and reused rather
    than duplicated.  A bare ``pending`` row (no marker) is deliberately *not*
    reused: it is a job the old behaviour stranded, and the caller should start
    a fresh one instead.  The scope lives in the checkpoint options, which is
    JSON, so it is compared here after decoding rather than in SQL.
    """
    rows = query_all(
        "SELECT id, checkpoint FROM job_run "
        "WHERE kind = ? AND job_seeker_id = ? "
        "  AND (status = 'running' OR (status = 'pending' AND last_error = ?)) "
        "ORDER BY created_at DESC",
        (kind, job_seeker_id, queued_marker),
    )
    for row in rows:
        checkpoint = from_json(row.get("checkpoint"), {}) or {}
        options = checkpoint.get("options") or {}
        if options.get("scope") == scope:
            return row
    return None


# ---------------------------------------------------------------------------
# NFR-303 retention
# ---------------------------------------------------------------------------


def due_for_retention(now: str | None = None) -> dict[str, list[dict]]:
    moment = now or utcnow()
    return {
        "contacts": query_all(
            "SELECT id, company_id, owning_campaign_id, retention_until, access_method "
            "FROM contact WHERE retention_until IS NOT NULL AND retention_until <= ?",
            (moment,),
        ),
        "network_members": query_all(
            "SELECT id, job_seeker_id, owning_campaign_id, retention_until "
            "FROM network_member WHERE retention_until IS NOT NULL AND retention_until <= ?",
            (moment,),
        ),
    }


def sweep_retention(now: str | None = None) -> dict[str, Any]:
    """Delete third-party records past their retention deadline (NFR-303, RK-08).

    Contacts and network members are removed in one transaction so a crash
    cannot leave a half-swept campaign behind.  Objections are deliberately not
    swept: the block must outlive the data it blocks (NFR-302).
    """
    moment = now or utcnow()
    with write_tx() as conn:
        contacts = conn.execute(
            "DELETE FROM contact WHERE retention_until IS NOT NULL AND retention_until <= ?",
            (moment,),
        ).rowcount
        members = conn.execute(
            "DELETE FROM network_member WHERE retention_until IS NOT NULL "
            "AND retention_until <= ?",
            (moment,),
        ).rowcount
    return {"contacts": int(contacts or 0), "network_members": int(members or 0), "at": moment}


def expire_campaign_records(campaign_id: str, retention_until: str) -> int:
    """Stamp a campaign's browser-collected records with their deletion date."""
    touched = execute(
        "UPDATE contact SET retention_until = ? WHERE owning_campaign_id = ? AND shareable = 0",
        (retention_until, campaign_id),
    )
    touched += execute(
        "UPDATE network_member SET retention_until = ? WHERE owning_campaign_id = ?",
        (retention_until, campaign_id),
    )
    return touched


# ---------------------------------------------------------------------------
# Domain e-mail patterns (FR-303)
# ---------------------------------------------------------------------------


def get_pattern(domain: str) -> dict | None:
    row = query_one("SELECT * FROM email_pattern WHERE domain = ?", (domain.lower(),))
    if row is None:
        return None
    out = dict(row)
    out["alternatives"] = from_json(out.get("alternatives"), []) or []
    out["local_parts"] = from_json(out.get("local_parts"), []) or []
    return out


def save_pattern(domain: str, values: dict) -> None:
    payload = {
        "domain": domain.lower(),
        "pattern": values["pattern"],
        "confidence": round(float(values.get("confidence", 0.5)), 3),
        "sample_count": int(values.get("sample_count", 0)),
        "supporting": int(values.get("supporting", 0)),
        "alternatives": to_json(values.get("alternatives") or []),
        "local_parts": to_json(values.get("local_parts") or []),
        "source": values.get("source"),
        "inferred_at": utcnow(),
    }
    upsert_row("email_pattern", payload, ["domain"])


# ---------------------------------------------------------------------------
# Per-address validation cache and per-domain probe state (FR-304, FR-305)
# ---------------------------------------------------------------------------


def cached_validation(email: str, now: str | None = None) -> dict | None:
    row = query_one(
        "SELECT * FROM email_validation_cache WHERE email = ?", (email.strip().lower(),)
    )
    if row is None or row["expires_at"] <= (now or utcnow()):
        return None
    out = dict(row)
    out["detail"] = from_json(out.get("detail"), {}) or {}
    return out


def cache_validation(email: str, result: str, detail: dict, expires_at: str) -> None:
    upsert_row(
        "email_validation_cache",
        {
            "email": email.strip().lower(),
            "result": result,
            "detail": to_json(detail),
            "checked_at": utcnow(),
            "expires_at": expires_at,
        },
        ["email"],
    )


def clear_validation_cache(email: str) -> int:
    return execute("DELETE FROM email_validation_cache WHERE email = ?", (email.strip().lower(),))


def domain_state(domain: str) -> dict | None:
    row = query_one("SELECT * FROM email_domain_state WHERE domain = ?", (domain.lower(),))
    if row is None:
        return None
    out = dict(row)
    out["mx_hosts"] = from_json(out.get("mx_hosts"), []) or []
    return out


def save_domain_state(domain: str, values: dict) -> None:
    payload = {k: v for k, v in values.items() if k != "domain"}
    if "mx_hosts" in payload:
        payload["mx_hosts"] = to_json(payload["mx_hosts"])
    payload["domain"] = domain.lower()
    payload["updated_at"] = utcnow()
    upsert_row("email_domain_state", payload, ["domain"])


# ---------------------------------------------------------------------------
# Network members (FR-302, FR-461)
# ---------------------------------------------------------------------------


def upsert_network_member(job_seeker_id: str, values: dict) -> tuple[str, bool]:
    """Store one person from the job seeker's network.  Returns ``(id, created)``."""
    payload = {k: v for k, v in values.items() if k in _NETWORK_COLUMNS}
    if not payload.get("full_name"):
        raise ValueError("A network member needs a name")
    payload["job_seeker_id"] = job_seeker_id
    payload.setdefault("collected_at", utcnow())

    existing: dict | None = None
    if payload.get("linkedin_url"):
        existing = query_one(
            "SELECT id FROM network_member WHERE job_seeker_id = ? AND linkedin_url = ?",
            (job_seeker_id, payload["linkedin_url"]),
        )
    if existing is None:
        existing = query_one(
            "SELECT id FROM network_member WHERE job_seeker_id = ? AND lower(full_name) = ? "
            "AND COALESCE(lower(company_name), '') = ?",
            (
                job_seeker_id,
                str(payload["full_name"]).lower(),
                str(payload.get("company_name") or "").lower(),
            ),
        )
    if existing is None:
        return insert_row("network_member", payload), True

    merged = {k: v for k, v in payload.items() if v not in (None, "", [], {})}
    merged.pop("job_seeker_id", None)
    update_row("network_member", existing["id"], merged)
    return existing["id"], False


def get_network_member(member_id: str, job_seeker_id: str) -> dict | None:
    return _decode_network(
        query_one(
            "SELECT * FROM network_member WHERE id = ? AND job_seeker_id = ?",
            (member_id, job_seeker_id),
        )
    )


def list_network(job_seeker_id: str, limit: int = 2000) -> list[dict]:
    rows = query_all(
        "SELECT * FROM network_member WHERE job_seeker_id = ? "
        "ORDER BY degree ASC, full_name COLLATE NOCASE ASC LIMIT ?",
        (job_seeker_id, limit),
    )
    return [m for m in (_decode_network(r) for r in rows) if m]


def network_at_company(
    job_seeker_id: str, *, company_id: str | None, company_name: str | None
) -> list[dict]:
    """People in the job seeker's network who work at the target company (FR-302)."""
    sql = "SELECT * FROM network_member WHERE job_seeker_id = ? AND ("
    params: list[Any] = [job_seeker_id]
    clauses = []
    if company_id:
        clauses.append("company_id = ?")
        params.append(company_id)
    if company_name:
        clauses.append("lower(company_name) = ?")
        params.append(company_name.lower())
        clauses.append("lower(company_name) LIKE ?")
        params.append(f"%{company_name.lower()}%")
    if not clauses:
        return []
    sql += " OR ".join(clauses) + ") ORDER BY degree ASC, full_name COLLATE NOCASE ASC"
    rows = query_all(sql, tuple(params))
    return [m for m in (_decode_network(r) for r in rows) if m]


def delete_network_member(member_id: str, job_seeker_id: str) -> int:
    return execute(
        "DELETE FROM network_member WHERE id = ? AND job_seeker_id = ?",
        (member_id, job_seeker_id),
    )


# ---------------------------------------------------------------------------
# Introduction paths (FR-302, FR-461)
# ---------------------------------------------------------------------------


def upsert_introduction_path(job_seeker_id: str, values: dict) -> tuple[str, bool]:
    payload = {k: v for k, v in values.items() if k in _INTRO_COLUMNS}
    payload["job_seeker_id"] = job_seeker_id
    payload["updated_at"] = utcnow()

    existing = None
    if payload.get("opportunity_id") and payload.get("network_member_id"):
        existing = query_one(
            "SELECT id, status FROM introduction_path WHERE job_seeker_id = ? "
            "AND opportunity_id = ? AND network_member_id = ?",
            (job_seeker_id, payload["opportunity_id"], payload["network_member_id"]),
        )
    if existing is None and payload.get("opportunity_id") and payload.get("intermediary_name"):
        existing = query_one(
            "SELECT id, status FROM introduction_path WHERE job_seeker_id = ? "
            "AND opportunity_id = ? AND lower(intermediary_name) = ?",
            (job_seeker_id, payload["opportunity_id"], payload["intermediary_name"].lower()),
        )
    if existing is None:
        payload.setdefault("created_at", utcnow())
        return insert_row("introduction_path", payload), True

    # A route the job seeker has already acted on keeps its status.
    if existing.get("status") not in (None, "proposed"):
        payload.pop("status", None)
    payload.pop("job_seeker_id", None)
    update_row("introduction_path", existing["id"], payload)
    return existing["id"], False


def get_introduction_path(path_id: str, job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM introduction_path WHERE id = ? AND job_seeker_id = ?",
        (path_id, job_seeker_id),
    )


def introduction_paths(
    job_seeker_id: str,
    *,
    opportunity_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    sql = "SELECT * FROM introduction_path WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if opportunity_id:
        sql += " AND opportunity_id = ?"
        params.append(opportunity_id)
    if company_id:
        sql += " AND company_id = ?"
        params.append(company_id)
    # FR-461 ranks on strength *and* relevance; the stored order is the same
    # combination the pipeline ranks on, so the list never reorders itself.
    sql += (
        " ORDER BY (0.6 * COALESCE(strength, 0) + 0.4 * COALESCE(relevance, 0)) DESC, "
        "COALESCE(strength, 0) DESC LIMIT ?"
    )
    params.append(limit)
    return query_all(sql, tuple(params))


def set_path_status(path_id: str, job_seeker_id: str, status: str) -> dict | None:
    if not get_introduction_path(path_id, job_seeker_id):
        return None
    update_row("introduction_path", path_id, {"status": status, "updated_at": utcnow()})
    return get_introduction_path(path_id, job_seeker_id)


def save_path_message(path_id: str, subject: str | None, body: str) -> None:
    update_row(
        "introduction_path",
        path_id,
        {
            "message_subject": subject,
            "message_draft": body,
            "message_generated_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def delete_paths_for_opportunity(job_seeker_id: str, opportunity_id: str) -> int:
    """Drop proposed routes before a rebuild; accepted or sent routes survive."""
    return execute(
        "DELETE FROM introduction_path WHERE job_seeker_id = ? AND opportunity_id = ? "
        "AND status = 'proposed'",
        (job_seeker_id, opportunity_id),
    )


# ---------------------------------------------------------------------------
# Reads the discovery pipeline needs (opportunity, company, profile)
# ---------------------------------------------------------------------------


def opportunity_context(opportunity_id: str, job_seeker_id: str) -> dict | None:
    """One opportunity with the company fields contact discovery reads (FR-301)."""
    row = query_one(
        """
        SELECT o.id, o.job_seeker_id, o.campaign_id, o.company_id, o.vacancy_id, o.kind,
               o.title, o.function_family, o.seniority, o.language,
               c.name AS company_name, c.domain AS company_domain,
               c.careers_url AS company_careers_url, c.country AS company_country,
               c.structure AS company_structure, c.key_people AS company_key_people,
               c.size_band AS company_size_band,
               v.application_channel AS vacancy_application_channel,
               v.application_target AS vacancy_application_target,
               v.source_url AS vacancy_source_url
        FROM opportunity o
        LEFT JOIN company c ON c.id = o.company_id
        LEFT JOIN vacancy v ON v.id = o.vacancy_id
        WHERE o.id = ? AND o.job_seeker_id = ?
        """,
        (opportunity_id, job_seeker_id),
    )
    if row is None:
        return None
    out = dict(row)
    out["company_structure"] = from_json(out.get("company_structure"), {}) or {}
    out["company_key_people"] = from_json(out.get("company_key_people"), []) or []
    return out


def selected_opportunities(job_seeker_id: str, campaign_id: str, limit: int = 200) -> list[dict]:
    """FR-301 runs "for each selected opportunity" (FR-284)."""
    return query_all(
        "SELECT id, company_id, title, function_family FROM opportunity "
        "WHERE job_seeker_id = ? AND campaign_id = ? AND selected = 1 "
        "ORDER BY COALESCE(score, 0) DESC LIMIT ?",
        (job_seeker_id, campaign_id, limit),
    )


def get_company(company_id: str) -> dict | None:
    row = query_one("SELECT * FROM company WHERE id = ?", (company_id,))
    if row is None:
        return None
    out = dict(row)
    out["structure"] = from_json(out.get("structure"), {}) or {}
    out["key_people"] = from_json(out.get("key_people"), []) or []
    return out


def latest_profile_sections(job_seeker_id: str) -> dict:
    """Employers and schools of the job seeker - the alumni join key (FR-461)."""
    row = query_one(
        "SELECT sections, job_seeker_id FROM profile_version WHERE job_seeker_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (job_seeker_id,),
    )
    return from_json((row or {}).get("sections"), {}) or {}


def campaign_end(campaign_id: str) -> str | None:
    row = query_one(
        "SELECT COALESCE(finished_at, started_at) AS ends_at FROM campaign WHERE id = ?",
        (campaign_id,),
    )
    return (row or {}).get("ends_at")


def display_name(job_seeker_id: str) -> str:
    row = query_one("SELECT display_name FROM job_seeker WHERE id = ?", (job_seeker_id,))
    return (row or {}).get("display_name") or ""
