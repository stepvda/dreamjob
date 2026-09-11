"""Job seeker, session and consent storage (FR-101, FR-108, CR-410, NFR-301).

All SQL for the private account tables lives here (CR-408).  What a table
*is* decides how erasure (FR-108) and export (NFR-301) treat it, and that
decision is read from the schema itself rather than from a hand-written list:

* a table with a ``job_seeker_id`` column is private to one job seeker
  (:func:`private_tables`);
* a table with a ``campaign_id`` but no ``job_seeker_id`` is private through
  its campaign (:func:`campaign_scoped_tables`);
* a ``*_path`` column on a private table names a file on disk that goes with
  the row (:func:`private_file_columns`).

A migration written by another part of the system therefore cannot add a
private table that erasure and export silently miss - the failure mode a
hand-maintained list has.  ``PRIVATE_TABLES`` below stays as the curated
deletion order for the tables that existed when this module was written; the
discovery adds whatever else the schema now holds and orders children before
the rows they reference.

Shared knowledge-base rows (``company``, ``vacancy``, ``financial_year``,
``raw_document``, ``event`` ...) carry no ``job_seeker_id`` and are therefore
never picked up: FR-108 keeps the market data that the campaigns produced.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dreamjob.db.connection import (
    insert_row,
    query_all,
    query_one,
    to_json,
    update_row,
    utcnow,
    write_tx,
)

# Private tables in a foreign-key-safe deletion order: children before the
# rows they reference, ``job_seeker`` itself last (handled separately).
PRIVATE_TABLES: tuple[str, ...] = (
    "incoming_reply",
    "dispatch",
    "pipeline_card",
    "mock_interview_session",
    "negotiation_brief",
    "application_package",
    "introduction_path",
    "gap_analysis",
    "stepping_stone_path",
    "opportunity",
    "llm_call",
    "job_run",
    "campaign",
    "directive_set",
    "dream_job_model",
    "composite_profile",
    "enrichment_finding",
    "evidence_item",
    "profile_skill",
    "profile_conflict",
    "disclosure_flag",
    "profile_version",
    "persona",
    "scoring_weights",
    "watchlist_entry",
    "notification",
    "digest",
    "mail_account",
    "consent_record",
    "session",
    "audit_event",
)

# Private rows reached through the campaign rather than through job_seeker_id.
CAMPAIGN_SCOPED_TABLES: tuple[str, ...] = ("source_plan_item",)

# A ``*_path`` column normally names a file on disk; these are the exceptions,
# which address a place inside a JSON document instead (NFR-402).
_NOT_A_FILE_PATH: frozenset[str] = frozenset({"field_path"})

CONSENT_KINDS: tuple[str, ...] = ("llm_transfer", "linkedin_automation", "enrichment")


class EmailAlreadyRegistered(ValueError):
    """Raised when an account already exists for an address (job_seeker.email is UNIQUE)."""


# ---------------------------------------------------------------------------
# What the schema says is private (FR-108, NFR-301, FR-344)
# ---------------------------------------------------------------------------


def _table_columns() -> dict[str, dict[str, dict[str, Any]]]:
    """Every table with its columns, as SQLite reports them."""
    tables = query_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {
        row["name"]: {c["name"]: c for c in query_all(f'PRAGMA table_info("{row["name"]}")')}
        for row in tables
    }


def _referenced_tables(table: str) -> set[str]:
    return {r["table"] for r in query_all(f'PRAGMA foreign_key_list("{table}")')}


def private_tables() -> tuple[str, ...]:
    """Tables holding one job seeker's data, children before what they reference.

    Derived from the live schema so a table added by a later migration is
    erased (FR-108) and exported (NFR-301) without this module being edited.
    ``PRIVATE_TABLES`` supplies the order for the tables it names; anything
    else is appended alphabetically and then moved ahead of the tables it has
    a foreign key into, so the deletion order stays valid.
    """
    columns = _table_columns()
    discovered = {name for name, cols in columns.items() if "job_seeker_id" in cols}
    known = [t for t in PRIVATE_TABLES if t in discovered]
    ordered = known + sorted(discovered - set(known))
    parents = {t: (_referenced_tables(t) & discovered) - {t} for t in ordered}

    seen: set[str] = set()
    postorder: list[str] = []

    def visit(table: str) -> None:
        if table in seen:
            return
        seen.add(table)
        for parent in sorted(parents[table]):
            visit(parent)
        postorder.append(table)

    # Walking the hint order backwards and reversing the result keeps the
    # curated order for tables that do not reference each other.
    for table in reversed(ordered):
        visit(table)
    postorder.reverse()
    return tuple(postorder)


def campaign_scoped_tables() -> tuple[str, ...]:
    """Private tables reached only through the campaign that produced them."""
    columns = _table_columns()
    found = {
        name
        for name, cols in columns.items()
        if "campaign_id" in cols and "job_seeker_id" not in cols
    }
    known = [t for t in CAMPAIGN_SCOPED_TABLES if t in found]
    return tuple(known + sorted(found - set(known)))


def _campaign_linked_tables(private: tuple[str, ...]) -> set[str]:
    """Private tables whose job_seeker_id is optional but which name a campaign.

    A row orphaned from the job seeker but still attached to their campaign is
    theirs all the same, so erasure and export follow the campaign as well.
    """
    columns = _table_columns()
    return {
        t
        for t in private
        if "campaign_id" in columns.get(t, {})
        and not columns[t]["job_seeker_id"]["notnull"]
    }


def private_file_columns() -> tuple[tuple[str, str], ...]:
    """``(table, column)`` pairs naming files on disk that belong to a job seeker."""
    columns = _table_columns()
    return tuple(
        (table, column)
        for table in private_tables()
        for column in columns[table]
        if column.endswith("_path") and column not in _NOT_A_FILE_PATH
    )


# ---------------------------------------------------------------------------
# Accounts (FR-101)
# ---------------------------------------------------------------------------


def create_seeker(
    email: str,
    display_name: str,
    *,
    password_hash: str | None = None,
    is_admin: bool = False,
    locale: str = "en",
) -> str:
    """Create a job seeker and return its id.

    Used by registration, by ``scripts/bootstrap.py`` and by the tests; it is
    the only place a ``job_seeker`` row is created (FR-101).
    """
    now = utcnow()
    try:
        return insert_row(
            "job_seeker",
            {
                "email": email.strip().lower(),
                "display_name": display_name.strip(),
                "password_hash": password_hash,
                "locale": locale,
                "is_admin": 1 if is_admin else 0,
                "created_at": now,
                "updated_at": now,
            },
        )
    except sqlite3.IntegrityError as exc:  # UNIQUE(email)
        raise EmailAlreadyRegistered(email) from exc


def get_seeker(seeker_id: str) -> dict | None:
    return query_one("SELECT * FROM job_seeker WHERE id = ?", (seeker_id,))


def get_seeker_by_email(email: str) -> dict | None:
    return query_one("SELECT * FROM job_seeker WHERE email = ?", (email.strip().lower(),))


def count_seekers() -> int:
    row = query_one("SELECT COUNT(*) AS n FROM job_seeker")
    return int(row["n"]) if row else 0


def count_admins(*, enabled_only: bool = True) -> int:
    """How many administrators remain.

    The guard against removing the last administrator reads this: an
    installation with no admin cannot reach the screens that would appoint a
    new one, so the last one can never be demoted, disabled or deleted.
    """
    sql = "SELECT COUNT(*) AS n FROM job_seeker WHERE is_admin = 1"
    if enabled_only:
        sql += " AND disabled = 0"
    row = query_one(sql)
    return int(row["n"]) if row else 0


def list_seekers(query: str | None = None, *, include_disabled: bool = True) -> list[dict]:
    """Every account, newest first, with the fields the user list shows.

    ``query`` matches the e-mail or the display name, case-insensitively. The
    session count and the last sign-in come along so an operator can tell an
    account that is in use from one that never was.
    """
    sql = (
        "SELECT j.id, j.email, j.display_name, j.locale, j.is_admin, "
        "       j.disabled, j.disabled_at, j.last_login_at, j.created_at, j.updated_at, "
        "       (j.totp_secret_enc IS NOT NULL) AS mfa_enrolled, "
        "       (j.password_hash IS NULL) AS passwordless, "
        "       (SELECT COUNT(*) FROM session s "
        "        WHERE s.job_seeker_id = j.id AND s.expires_at > ?) AS active_sessions "
        "FROM job_seeker j"
    )
    params: list[Any] = [utcnow()]
    clauses: list[str] = []
    if query:
        like = f"%{query.strip().lower()}%"
        clauses.append("(LOWER(j.email) LIKE ? OR LOWER(j.display_name) LIKE ?)")
        params += [like, like]
    if not include_disabled:
        clauses.append("j.disabled = 0")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY j.disabled, j.is_admin DESC, j.created_at DESC"
    return query_all(sql, tuple(params))


def get_seeker_row(seeker_id: str) -> dict | None:
    """The full row, including the columns the public view hides."""
    return query_one("SELECT * FROM job_seeker WHERE id = ?", (seeker_id,))


def set_disabled(seeker_id: str, disabled: bool) -> None:
    update_seeker(
        seeker_id,
        {"disabled": 1 if disabled else 0, "disabled_at": utcnow() if disabled else None},
    )


def set_last_login(seeker_id: str) -> None:
    update_seeker(seeker_id, {"last_login_at": utcnow()})


def list_users_without_sessions(
    *, exclude_admins: bool = True, exclude_ids: tuple[str, ...] = ()
) -> list[dict]:
    """Accounts with no *open* session, for a bulk clean-up.

    "No session" means no unexpired row in ``session``, which is the same
    figure the user list displays - so an operator can see in the interface
    exactly which accounts a purge would remove before running it. The acting
    administrator is excluded by the caller, and admins are excluded by
    default because a dormant administrator is a legitimate thing to keep and
    removing the last one would lock the installation out.
    """
    sql = (
        "SELECT j.id, j.email, j.display_name, j.is_admin, j.disabled, j.created_at "
        "FROM job_seeker j WHERE NOT EXISTS ("
        "  SELECT 1 FROM session s WHERE s.job_seeker_id = j.id AND s.expires_at > ?"
        ")"
    )
    params: list[Any] = [utcnow()]
    if exclude_admins:
        sql += " AND j.is_admin = 0"
    if exclude_ids:
        marks = ",".join("?" for _ in exclude_ids)
        sql += f" AND j.id NOT IN ({marks})"
        params += list(exclude_ids)
    sql += " ORDER BY j.created_at DESC"
    return query_all(sql, tuple(params))



def update_seeker(seeker_id: str, values: dict[str, Any]) -> None:
    if not values:
        return
    update_row("job_seeker", seeker_id, {**values, "updated_at": utcnow()})


def set_password_hash(seeker_id: str, password_hash: str) -> None:
    update_seeker(seeker_id, {"password_hash": password_hash})


def set_totp_blob(seeker_id: str, blob: bytes | None) -> None:
    """Store (or clear) the encrypted TOTP envelope (NFR-201, NFR-202)."""
    update_seeker(seeker_id, {"totp_secret_enc": blob})


def set_admin(seeker_id: str, is_admin: bool) -> None:
    update_seeker(seeker_id, {"is_admin": 1 if is_admin else 0})


# ---------------------------------------------------------------------------
# Sessions (NFR-202)
# ---------------------------------------------------------------------------


def create_session(
    seeker_id: str, token_hash: str, client_binding: str | None, expires_at: str
) -> str:
    return insert_row(
        "session",
        {
            "job_seeker_id": seeker_id,
            "token_hash": token_hash,
            "client_binding": client_binding,
            "expires_at": expires_at,
            "created_at": utcnow(),
        },
    )


def session_by_hash(token_hash: str) -> dict | None:
    return query_one("SELECT * FROM session WHERE token_hash = ?", (token_hash,))


def delete_session(token_hash: str) -> int:
    with write_tx() as conn:
        return conn.execute("DELETE FROM session WHERE token_hash = ?", (token_hash,)).rowcount


def delete_sessions_for(seeker_id: str) -> int:
    """Invalidate every session of one job seeker (password change, MFA change)."""
    with write_tx() as conn:
        return conn.execute("DELETE FROM session WHERE job_seeker_id = ?", (seeker_id,)).rowcount


def list_sessions(seeker_id: str) -> list[dict]:
    return query_all(
        "SELECT id, client_binding, expires_at, created_at FROM session "
        "WHERE job_seeker_id = ? ORDER BY created_at DESC",
        (seeker_id,),
    )


def purge_expired_sessions() -> int:
    with write_tx() as conn:
        return conn.execute("DELETE FROM session WHERE expires_at <= ?", (utcnow(),)).rowcount


# ---------------------------------------------------------------------------
# Consent (CR-410, CR-401, FR-126)
# ---------------------------------------------------------------------------


def record_consent(seeker_id: str, kind: str, granted: bool, detail: str | None = None) -> str:
    """Append a consent decision.  History is kept; nothing is ever updated."""
    return insert_row(
        "consent_record",
        {
            "job_seeker_id": seeker_id,
            "kind": kind,
            "granted": 1 if granted else 0,
            "detail": detail,
            "granted_at": utcnow(),
        },
    )


def latest_consent(seeker_id: str, kind: str) -> dict | None:
    return query_one(
        "SELECT * FROM consent_record WHERE job_seeker_id = ? AND kind = ? "
        "ORDER BY granted_at DESC, rowid DESC LIMIT 1",
        (seeker_id, kind),
    )


def latest_consents(seeker_id: str) -> dict[str, dict]:
    rows = query_all(
        "SELECT * FROM consent_record WHERE job_seeker_id = ? ORDER BY granted_at, rowid",
        (seeker_id,),
    )
    return {r["kind"]: r for r in rows}  # later rows overwrite earlier ones


def consent_history(seeker_id: str) -> list[dict]:
    return query_all(
        "SELECT * FROM consent_record WHERE job_seeker_id = ? ORDER BY granted_at DESC, rowid DESC",
        (seeker_id,),
    )


def has_consent(seeker_id: str, kind: str) -> bool:
    """True when the most recent decision for ``kind`` granted consent (CR-410)."""
    row = latest_consent(seeker_id, kind)
    return bool(row and row["granted"])


# ---------------------------------------------------------------------------
# Export (NFR-301) and erasure (FR-108)
# ---------------------------------------------------------------------------


def _json_safe(row: dict) -> dict:
    """Make a row JSON-serialisable without leaking key material (NFR-201)."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            out[key] = f"<encrypted, {len(bytes(value))} bytes>"
        elif key == "password_hash" and value:
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


def export_private_data(seeker_id: str) -> dict[str, Any]:
    """Every private row for one job seeker, ready to serialise (NFR-301).

    Shared knowledge-base rows are not included: they carry no link back to
    the job seeker (FR-344) and are not their personal data.
    """
    seeker = get_seeker(seeker_id)
    export: dict[str, Any] = {
        "exported_at": utcnow(),
        "job_seeker": _json_safe(seeker) if seeker else None,
        "tables": {},
    }
    private = private_tables()
    campaign_linked = _campaign_linked_tables(private)
    for table in private:
        rows = query_all(
            f"SELECT * FROM {table} WHERE job_seeker_id = ? ORDER BY rowid", (seeker_id,)
        )
        if table in campaign_linked:
            rows += query_all(
                f"SELECT * FROM {table} WHERE job_seeker_id IS NULL AND campaign_id IN "
                "(SELECT id FROM campaign WHERE job_seeker_id = ?) ORDER BY rowid",
                (seeker_id,),
            )
        export["tables"][table] = [_json_safe(r) for r in rows]

    for table in campaign_scoped_tables():
        export["tables"][table] = [
            _json_safe(r)
            for r in query_all(
                f"SELECT * FROM {table} WHERE campaign_id IN "
                "(SELECT id FROM campaign WHERE job_seeker_id = ?) ORDER BY rowid",
                (seeker_id,),
            )
        ]

    # NFR-303: contacts collected by a browser run are owned by the campaign,
    # not by the shared knowledge base, so they are part of this export.
    export["tables"]["contact"] = [
        _json_safe(r)
        for r in query_all(
            "SELECT * FROM contact WHERE shareable = 0 AND owning_campaign_id IN "
            "(SELECT id FROM campaign WHERE job_seeker_id = ?)",
            (seeker_id,),
        )
    ]
    return export


def private_file_paths(seeker_id: str) -> list[str]:
    """Paths of files on disk that belong to this job seeker (FR-108)."""
    paths: list[str] = []
    for table, column in private_file_columns():
        rows = query_all(
            f"SELECT {column} AS p FROM {table} WHERE job_seeker_id = ? AND {column} IS NOT NULL",
            (seeker_id,),
        )
        paths.extend(r["p"] for r in rows if r["p"])
    return paths


def erase_seeker(seeker_id: str) -> dict[str, int]:
    """Delete every private row for one job seeker (FR-108, NFR-301).

    Runs as a single transaction over the explicit table list so the result is
    a per-table count the caller can show and audit.  Shared knowledge-base
    rows are untouched; campaign-scoped contacts (NFR-303) are not shared and
    go with the campaign that collected them.
    """
    counts: dict[str, int] = {}
    private = private_tables()
    campaign_linked = _campaign_linked_tables(private)
    scoped = campaign_scoped_tables()
    with write_tx() as conn:
        counts["contact"] = conn.execute(
            "DELETE FROM contact WHERE shareable = 0 AND owning_campaign_id IN "
            "(SELECT id FROM campaign WHERE job_seeker_id = ?)",
            (seeker_id,),
        ).rowcount
        for table in scoped:
            counts[table] = conn.execute(
                f"DELETE FROM {table} WHERE campaign_id IN "
                "(SELECT id FROM campaign WHERE job_seeker_id = ?)",
                (seeker_id,),
            ).rowcount
        for table in private:
            deleted = conn.execute(
                f"DELETE FROM {table} WHERE job_seeker_id = ?", (seeker_id,)
            ).rowcount
            if table in campaign_linked:
                deleted += conn.execute(
                    f"DELETE FROM {table} WHERE job_seeker_id IS NULL AND campaign_id IN "
                    "(SELECT id FROM campaign WHERE job_seeker_id = ?)",
                    (seeker_id,),
                ).rowcount
            counts[table] = deleted
        counts["job_seeker"] = conn.execute(
            "DELETE FROM job_seeker WHERE id = ?", (seeker_id,)
        ).rowcount
    return counts


def record_erasure_marker(counts: dict[str, int], seeker_ref: str) -> str:
    """Leave an anonymous trace that an erasure happened (FR-108, NFR-702).

    The audit rows of the erased job seeker are deleted with the rest of their
    data, so this one row - carrying a one-way reference instead of the id -
    is what keeps the trail honest about the deletion itself.
    """
    return insert_row(
        "audit_event",
        {
            "job_seeker_id": None,
            "actor": "system",
            "action": "job_seeker.erased",
            "entity_type": "job_seeker",
            "entity_id": None,
            "detail": to_json({"seeker_ref": seeker_ref, "rows_deleted": counts}),
            "created_at": utcnow(),
        },
    )
