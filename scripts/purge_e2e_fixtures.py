"""Purge the end-to-end fixture rows the e2e suites left in a development DB.

The e2e runs sign up as ``thibault.casteleyn+...@example.com``, create
``Havenstad Analytics (e2e NNNNNN)`` companies on ``havenstad-NNNNNN.example``
domains, fill them with contacts at those domains, resolve them reachable, and
send a handful of "applications" to ``*-example.com`` addresses.  Every one of
those rows pollutes a real count: the company/contact/coverage totals, the
outreach reports, the objection blocklist and the activity feeds all read the
same tables the fixtures write to.

This script removes exactly those rows and nothing else.  The roots are:

* companies whose ``name`` contains ``(e2e`` or whose ``domain`` ends in
  ``.example``;
* contacts whose company is one of those, or whose e-mail ends in ``.example``;
* ``apply_contact_resolution`` rows for those companies or with an
  ``.example`` e-mail/domain;
* ``contact_objection`` rows with an ``.example`` e-mail/domain;
* ``dispatch`` rows to an ``.example``/``-example.com`` recipient.

Everything else is reached only through those roots: the opportunities and
vacancies linked to the fixture companies, their packages, the dispatch rows
referenced by those packages, the pipeline cards on those packages, and the
child rows hanging off all of them.  A row that is not derived from a root -
a real company, its scraped vacancy, a real seeker's campaign - is never
touched; in particular the fixture job seekers and campaigns themselves are
left in place, as the task scopes the purge to the ``.example`` data.

Dry run is the default and prints the full per-table plan.  ``--apply`` copies
every row it is about to delete into ``zz_backup_e2e_fixtures_20260912_<table>``
(unless ``--no-keep-backup``) and performs every delete in one transaction, so
a failure halfway leaves the database untouched.  Apply refuses to overwrite
an existing backup table, which makes a second run after a rollback explicit.

    PYTHONPATH=backend .venv/bin/python scripts/purge_e2e_fixtures.py
    PYTHONPATH=backend .venv/bin/python scripts/purge_e2e_fixtures.py --apply

The revert block it prints restores every backup with ``INSERT OR REPLACE``.
``company_fts`` is the one backup with an extra ``_rowid`` column (the virtual
table has no primary key usable from SQL), so its revert restates the columns;
if that is ever inconvenient, ``knowledge.reindex_all()`` rebuilds it from
``company``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dreamjob.config import get_settings
from dreamjob.db.connection import read_tx, write_tx

BACKUP_STAMP = "20260912"
BACKUP_PREFIX = f"zz_backup_e2e_fixtures_{BACKUP_STAMP}"

COMPANY_PREDICATE = (
    "lower(COALESCE(name, '')) LIKE '%(e2e%' "
    "OR lower(COALESCE(domain, '')) LIKE '%.example'"
)
EMAIL_EXAMPLE = (
    "lower(COALESCE(email, '')) LIKE '%.example' "
    "OR lower(COALESCE(email, '')) LIKE '%@example.com'"
)
DOMAIN_EXAMPLE = (
    "lower(COALESCE(domain, '')) LIKE '%.example' "
    "OR lower(COALESCE(domain, '')) = 'example.com'"
)
RECIPIENT_EXAMPLE = (
    "lower(COALESCE(recipient_email, '')) LIKE '%.example' "
    "OR lower(COALESCE(recipient_email, '')) LIKE '%@example.com' "
    "OR lower(COALESCE(recipient_email, '')) LIKE '%-example.com'"
)

#: The order matters: children before the rows they point at.  ``key`` is the
#: column the backup and delete statements filter on; every other table uses
#: its primary key.
TABLE_ORDER: tuple[tuple[str, str], ...] = (
    ("provenance", "id"),
    ("reply_draft", "id"),
    ("pipeline_card_event", "id"),
    ("interview_appointment", "id"),
    ("negotiation_brief", "id"),
    ("mock_interview_session", "id"),
    ("manual_response", "id"),
    ("apply_selection", "id"),
    ("introduction_path", "id"),
    ("pipeline_card", "id"),
    ("dispatch", "id"),
    ("application_package", "id"),
    ("contact_objection", "email"),
    ("apply_contact_resolution", "company_id"),
    ("company_employer_kind", "id"),
    ("employer_resolution_signal", "id"),
    ("employer_resolution_attempt", "id"),
    ("competitor_link", "id"),
    ("company_registry_identity", "id"),
    ("company_domain_candidate", "id"),
    ("company_domain_resolution", "id"),
    ("company_domain_revocation", "id"),
    ("apply_domain_probe", "id"),
    ("financial_analysis", "id"),
    ("financial_year", "id"),
    ("hiring_signal", "id"),
    ("employer_review_summary", "id"),
    ("compensation_observation", "id"),
    ("employer_kind_correction", "id"),
    ("watchlist_entry", "id"),
    ("network_member", "id"),
    ("board_registry", "id"),
    ("company_group_link", "id"),
    ("company_fts", "company_id"),
    ("opportunity", "id"),
    ("vacancy", "id"),
    ("contact", "id"),
    ("company", "id"),
)

#: Tables whose delete key is not their ``id``: the lookup tables are keyed by
#: the company they describe (or by e-mail for the objection blocklist), and
#: the composite-key tables have no single column key at all, so they are
#: addressed by rowid.
KEY_BY_TABLE: dict[str, str] = {
    "contact_objection": "email",
    "apply_contact_resolution": "company_id",
    "company_employer_kind": "rowid",
    "employer_resolution_signal": "rowid",
    "employer_resolution_attempt": "rowid",
    "company_registry_identity": "rowid",
    "company_domain_candidate": "rowid",
    "company_domain_resolution": "rowid",
    "apply_domain_probe": "rowid",
    "company_fts": "company_id",
}


@dataclass
class TablePlan:
    table: str
    key: str
    ids: list[str]


@dataclass
class Plan:
    tables: list[TablePlan] = field(default_factory=list)

    def rows(self, table: str) -> int:
        return next((len(t.ids) for t in self.tables if t.table == table), 0)

    @property
    def total(self) -> int:
        return sum(len(t.ids) for t in self.tables)


def _fetch(conn: sqlite3.Connection, sql: str, params: list | tuple = ()) -> list[str]:
    return [str(row[0]) for row in conn.execute(sql, params) if row[0] is not None]


def _in(column: str, ids: list[str]) -> tuple[str, list[str]]:
    """``column IN (...)`` or the constant-false ``0`` for an empty set."""
    if not ids:
        return "0", []
    marks = ",".join("?" for _ in ids)
    return f"{column} IN ({marks})", list(ids)


def _or(*clauses: tuple[str, list[str]]) -> tuple[str, list[str]]:
    live = [(sql, params) for sql, params in clauses if sql != "0"]
    if not live:
        return "0", []
    return " OR ".join(f"({sql})" for sql, _ in live), [v for _, params in live for v in params]


def _select(conn: sqlite3.Connection, table: str, clause: tuple[str, list[str]]) -> list[str]:
    sql, params = clause
    if sql == "0":
        return []
    return _fetch(conn, f"SELECT DISTINCT id FROM {table} WHERE {sql}", params)


def _select_key(
    conn: sqlite3.Connection, table: str, key: str, clause: tuple[str, list[str]]
) -> list[str]:
    sql, params = clause
    if sql == "0":
        return []
    return _fetch(conn, f"SELECT DISTINCT {key} FROM {table} WHERE {sql}", params)


def classify(conn: sqlite3.Connection) -> Plan:
    """The exact rows the purge would delete, in delete order."""
    companies = _fetch(conn, f"SELECT id FROM company WHERE {COMPANY_PREDICATE}")
    contacts = _select(
        conn, "contact", _or(_in("company_id", companies), (EMAIL_EXAMPLE, []))
    )
    acr = _select_key(
        conn,
        "apply_contact_resolution",
        "company_id",
        _or(_in("company_id", companies), (EMAIL_EXAMPLE, []), (DOMAIN_EXAMPLE, [])),
    )
    objections = _select_key(
        conn,
        "contact_objection",
        "email",
        _or((EMAIL_EXAMPLE, []), (DOMAIN_EXAMPLE, [])),
    )
    vacancies = _select(conn, "vacancy", _in("company_id", companies))
    opportunities = _select(
        conn, "opportunity", _or(_in("company_id", companies), _in("vacancy_id", vacancies))
    )

    dispatch_roots = _select_key(conn, "dispatch", "id", (RECIPIENT_EXAMPLE, []))
    if dispatch_roots:
        marks = ",".join("?" for _ in dispatch_roots)
        dispatch_packages = _fetch(
            conn,
            "SELECT DISTINCT application_package_id FROM dispatch "
            f"WHERE id IN ({marks}) AND application_package_id IS NOT NULL",
            dispatch_roots,
        )
    else:
        dispatch_packages = []
    packages = _select(
        conn,
        "application_package",
        _or(
            _in("opportunity_id", opportunities),
            _in("contact_id", contacts),
            _in("id", dispatch_packages),
        ),
    )
    dispatches = _select_key(
        conn,
        "dispatch",
        "id",
        _or(_in("id", dispatch_roots), _in("application_package_id", packages)),
    )
    cards = _select(
        conn,
        "pipeline_card",
        _or(_in("opportunity_id", opportunities), _in("application_package_id", packages)),
    )

    children: dict[str, tuple[str, list[str]]] = {
        "reply_draft": _in("pipeline_card_id", cards),
        "pipeline_card_event": _in("pipeline_card_id", cards),
        "interview_appointment": _or(
            _in("pipeline_card_id", cards), _in("opportunity_id", opportunities)
        ),
        "negotiation_brief": _in("opportunity_id", opportunities),
        "mock_interview_session": _in("opportunity_id", opportunities),
        "manual_response": _in("opportunity_id", opportunities),
        "apply_selection": _in("opportunity_id", opportunities),
        "introduction_path": _or(
            _in("company_id", companies),
            _in("target_contact_id", contacts),
            _in("opportunity_id", opportunities),
        ),
        "company_employer_kind": _in("company_id", companies),
        "employer_resolution_signal": _in("company_id", companies),
        "employer_resolution_attempt": _in("company_id", companies),
        "competitor_link": _or(_in("company_id", companies), _in("peer_company_id", companies)),
        "company_registry_identity": _in("company_id", companies),
        "company_domain_candidate": _in("company_id", companies),
        "company_domain_resolution": _in("company_id", companies),
        "company_domain_revocation": _in("company_id", companies),
        "apply_domain_probe": _in("company_id", companies),
        "financial_analysis": _in("company_id", companies),
        "financial_year": _in("company_id", companies),
        "hiring_signal": _in("company_id", companies),
        "employer_review_summary": _in("company_id", companies),
        "compensation_observation": _in("company_id", companies),
        "employer_kind_correction": _in("company_id", companies),
        "watchlist_entry": _in("company_id", companies),
        "network_member": _in("company_id", companies),
        "board_registry": _in("company_id", companies),
        "company_group_link": _or(
            _in("parent_company_id", companies), _in("subsidiary_company_id", companies)
        ),
    }

    provenance_clauses: list[tuple[str, list[str]]] = []
    for entity_type, ids in (
        ("company", companies),
        ("contact", contacts),
        ("vacancy", vacancies),
        ("opportunity", opportunities),
        ("application_package", packages),
        ("dispatch", dispatches),
        ("pipeline_card", cards),
    ):
        if ids:
            marks = ",".join("?" for _ in ids)
            provenance_clauses.append(
                (f"entity_type = ? AND entity_id IN ({marks})", [entity_type, *ids])
            )
    provenance = _select(conn, "provenance", _or(*provenance_clauses))

    by_table: dict[str, list[str]] = {
        "provenance": provenance,
        "pipeline_card": cards,
        "dispatch": dispatches,
        "application_package": packages,
        "contact_objection": objections,
        "apply_contact_resolution": acr,
        "opportunity": opportunities,
        "vacancy": vacancies,
        "contact": contacts,
        "company": companies,
    }
    for table, clause in children.items():
        by_table[table] = _select_key(conn, table, KEY_BY_TABLE.get(table, "id"), clause)

    return Plan(
        tables=[
            TablePlan(table=table, key=KEY_BY_TABLE.get(table, key), ids=by_table.get(table, []))
            for table, key in TABLE_ORDER
        ]
    )


def _chunks(values: list[str], size: int = 400):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _backup_name(table: str) -> str:
    return f"{BACKUP_PREFIX}_{table}"


def _backup_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _backup_table(conn: sqlite3.Connection, tp: TablePlan) -> int:
    """Copy every row the delete is about to remove, chunk by chunk."""
    name = _backup_name(tp.table)
    select = (
        f"SELECT rowid AS _rowid, * FROM {tp.table}"
        if tp.table == "company_fts"
        else f"SELECT * FROM {tp.table}"
    )
    first = True
    for chunk in _chunks(tp.ids):
        marks = ",".join("?" for _ in chunk)
        where = f"{tp.key} IN ({marks})"
        if first:
            conn.execute(f'CREATE TABLE "{name}" AS {select} WHERE {where}', chunk)
            first = False
        else:
            conn.execute(f'INSERT INTO "{name}" {select} WHERE {where}', chunk)
    return int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])


def apply_plan(conn: sqlite3.Connection, plan: Plan, *, keep_backup: bool) -> dict[str, int]:
    backups: dict[str, int] = {}
    for tp in plan.tables:
        if not tp.ids:
            continue
        if keep_backup:
            backups[tp.table] = _backup_table(conn, tp)
        for chunk in _chunks(tp.ids):
            marks = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM {tp.table} WHERE {tp.key} IN ({marks})", chunk)
    return backups


def _print_revert(backups: dict[str, int]) -> None:
    if not backups:
        return
    lines = ["", "Revert SQL:", "BEGIN IMMEDIATE;"]
    for table in backups:
        name = _backup_name(table)
        if table == "company_fts":
            lines.append(
                "INSERT INTO company_fts(rowid, company_id, name, normalised_name, "
                "business_summary, products_services) SELECT _rowid, company_id, name, "
                f'normalised_name, business_summary, products_services FROM "{name}";'
            )
        else:
            lines.append(f'INSERT OR REPLACE INTO {table} SELECT * FROM "{name}";')
    lines.append("COMMIT;")
    print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="back the rows up and delete them; default is a dry run",
    )
    parser.add_argument(
        "--keep-backup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="copy every deleted row into a zz_backup table (default: on)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="database path (default: DREAMJOB_DB_PATH from .env / settings)",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else get_settings().abs_db_path
    print(f"Database: {db_path}")
    mode = "APPLY" if args.apply else "dry run (pass --apply to purge)"
    print(f"Mode: {mode}; backups: {'on' if args.keep_backup else 'OFF'}")

    with read_tx(db_path) as conn:
        plan = classify(conn)

    print()
    print(f"{'table':<32}{'rows':>8}  backup")
    print("-" * 70)
    for tp in plan.tables:
        if tp.ids:
            print(f"{tp.table:<32}{len(tp.ids):>8}  {_backup_name(tp.table)}")
    print("-" * 70)
    print(f"{'total':<32}{plan.total:>8}")
    print(
        "\nRoots: companies matching \"(e2e\" or .example domains; contacts, "
        "apply_contact_resolution, contact_objection and dispatch rows with "
        ".example / -example.com addresses.  Everything else in the plan is "
        "reached only through those roots; non-fixture rows are never touched."
    )

    if not args.apply:
        print("\nDry run only; nothing written. Re-run with --apply to purge.")
        return 0

    if not args.keep_backup:
        print("WARNING: --no-keep-backup deletes without a backup table.", file=sys.stderr)

    with read_tx(db_path) as conn:
        for tp in plan.tables:
            if tp.ids and args.keep_backup and _backup_exists(conn, _backup_name(tp.table)):
                print(
                    f"Backup table {_backup_name(tp.table)} already exists; refusing to "
                    "overwrite it. Rename or drop it first.",
                    file=sys.stderr,
                )
                return 2

    with write_tx(db_path) as conn:
        plan = classify(conn)
        backups = apply_plan(conn, plan, keep_backup=args.keep_backup)

    print()
    for table, count in backups.items():
        print(f"Backup {_backup_name(table)}: {count} row(s)")
    print(f"Deleted {plan.total} row(s) across {sum(1 for tp in plan.tables if tp.ids)} table(s).")
    _print_revert(backups)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
