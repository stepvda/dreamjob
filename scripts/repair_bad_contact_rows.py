"""Repair the contact-resolution rows the domain and extraction bugs produced.

The FR-301 ladder wrote several classes of dishonest row into
``apply_contact_resolution`` (ACR) that the fixed code no longer produces but
that are still in the live database.  This script counts them category by
category, and with ``--apply`` copies every row it is about to delete or
modify into ``zz_backup_repair_*_20260912`` tables before repairing anything,
all in a single transaction.

Categories:

a. **Cross-company resolutions.**  ``ACR.contact_id`` points at a ``contact``
   whose ``company_id`` differs from the resolution's.  The fixed code refuses
   to store such a link (:func:`dreamjob.pipeline.apply_contacts.resolve_company`,
   :func:`dreamjob.db.repositories.apply.record_resolution`); the rows are
   deleted so the ladder re-resolves the company.
b. **Foreign/aggregator domains.**  ``ACR.domain`` - or the registrable domain
   of ``ACR.email`` - is a job board, ATS vendor, hosting or social host, as
   rejected by the fixed code (``apply_contacts.AGGREGATOR_DOMAINS`` plus
   ``company_domains.NEVER_AN_EMPLOYER``).  Deleted.
c. **Junk domain fragments.**  ``ACR.domain`` (or the email's host) is one of
   ``apply_contacts.JUNK_DOMAINS`` - prose fragments such as ``any.in``.  The
   ACR row, plus any ``company_domain_resolution``/``company_domain_candidate``
   row carrying that junk domain, is deleted so the domain is re-derived.
d. **Reachable without a usable contact.**  ``ACR.status = 'reachable'`` for a
   company with no usable ``contact`` e-mail.  Deleted so the status cannot
   flatter the coverage counts.
e. **Duplicate contacts.**  For each ``(company_id, lower(email))`` with more
   than one ``contact`` row, the highest-confidence, most recent row is kept;
   references (``apply_contact_resolution.contact_id``,
   ``application_package.contact_id``, ``introduction_path.target_contact_id``)
   are repointed to it and the extras are deleted.

Rows whose domain or e-mail ends in ``.example`` are end-to-end fixtures and
are never touched.

Dry run (the default) only prints.  ``--apply`` writes:

    PYTHONPATH=backend .venv/bin/python scripts/repair_bad_contact_rows.py
    PYTHONPATH=backend .venv/bin/python scripts/repair_bad_contact_rows.py --apply

Every row the script deletes or modifies is copied into a backup table, and
the revert block it prints restores them with ``INSERT OR REPLACE`` (the
unique index is dropped first, because the duplicates return).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from dreamjob.adapters.website.crawler import registrable_domain
from dreamjob.config import get_settings
from dreamjob.db.connection import read_tx, write_tx
from dreamjob.pipeline.apply_contacts import AGGREGATOR_DOMAINS, JUNK_DOMAINS
from dreamjob.pipeline.company_domains import NEVER_AN_EMPLOYER

#: The fixed code's whole rejection set for a host: boards, vendors, hosting
#: and social hosts.  ``usable_employer_domain`` also rejects free mailbox
#: providers, but an address at one is still kept - only its *domain* is never
#: spelled on - so a freemail ACR domain is not this repair's business.
FOREIGN_DOMAINS = AGGREGATOR_DOMAINS | NEVER_AN_EMPLOYER

EXAMPLE_SUFFIX = ".example"

BACKUP_TABLES = {
    "acr": "zz_backup_repair_acr_20260912",
    "cdr": "zz_backup_repair_cdr_20260912",
    "cdc": "zz_backup_repair_cdc_20260912",
    "contact": "zz_backup_repair_contact_20260912",
    "package": "zz_backup_repair_package_20260912",
    "intro": "zz_backup_repair_intro_20260912",
}

REVERT_TABLES = {
    "acr": "apply_contact_resolution",
    "cdr": "company_domain_resolution",
    "cdc": "company_domain_candidate",
    "contact": "contact",
    "package": "application_package",
    "intro": "introduction_path",
}

UNIQUE_INDEX = "uq_contact_company_email"
UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_INDEX} "
    "ON contact(company_id, lower(email)) WHERE email IS NOT NULL AND email <> ''"
)

CONTACT_REFERENCES: tuple[tuple[str, str], ...] = (
    ("apply_contact_resolution", "contact_id"),
    ("application_package", "contact_id"),
    ("introduction_path", "target_contact_id"),
)


def _clean(value: str | None) -> str:
    return (value or "").strip().lower()


def _email_host(email: str | None) -> str:
    value = _clean(email)
    return value.rsplit("@", 1)[-1] if "@" in value else ""


def _is_example(*values: str | None) -> bool:
    return any(_clean(value).endswith(EXAMPLE_SUFFIX) for value in values)


def _is_foreign(host: str | None) -> bool:
    """Does the fixed code reject this host as an employer domain?"""
    value = _clean(host).lstrip(".")
    if not value or "." not in value:
        return False
    registrable = registrable_domain(value) or value
    return value in FOREIGN_DOMAINS or registrable in FOREIGN_DOMAINS


def _is_junk(host: str | None) -> bool:
    value = _clean(host).lstrip(".")
    if not value:
        return False
    return value in JUNK_DOMAINS or (registrable_domain(value) or value) in JUNK_DOMAINS


@dataclass
class Pair:
    """One ``(company_id, lower(email))`` group with more than one row."""

    company_id: str | None
    email: str
    kept: str
    extras: list[str]


@dataclass
class Plan:
    """What the repair would delete, modify and leave alone."""

    a: list[str] = field(default_factory=list)
    b: list[str] = field(default_factory=list)
    c: list[str] = field(default_factory=list)
    d: list[str] = field(default_factory=list)
    acr_status: dict[str, str] = field(default_factory=dict)
    raw: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    examples: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    extras: list[str] = field(default_factory=list)
    kept_of: dict[str, str] = field(default_factory=dict)
    pairs: list[Pair] = field(default_factory=list)
    refs: dict[str, dict[str, int]] = field(default_factory=dict)
    usable_companies_after: set[str] = field(default_factory=set)

    @property
    def acr_deleted(self) -> list[str]:
        return sorted(set(self.a) | set(self.b) | set(self.c) | set(self.d))


def _chunks(values: list, size: int = 400):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _contact_refs(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, int]]:
    refs: dict[str, dict[str, int]] = defaultdict(dict)
    for table, column in CONTACT_REFERENCES:
        for chunk in _chunks(list(ids)):
            placeholders = ",".join("?" for _ in chunk)
            sql = (
                f"SELECT {column}, count(*) FROM {table} "
                f"WHERE {column} IN ({placeholders}) GROUP BY {column}"
            )
            for contact_id, count in conn.execute(sql, chunk):
                refs[contact_id][table] = count
    return refs


def _duplicate_contacts(conn: sqlite3.Connection, plan: Plan) -> None:
    groups = conn.execute(
        """
        SELECT company_id, lower(email) AS lemail, count(*) AS n
        FROM contact
        WHERE COALESCE(email, '') <> ''
        GROUP BY company_id, lower(email)
        HAVING n > 1
        ORDER BY company_id, lemail
        """
    ).fetchall()
    if not groups:
        return
    group_rows: list[tuple[sqlite3.Row, list[sqlite3.Row]]] = []
    for group in groups:
        rows = conn.execute(
            "SELECT * FROM contact WHERE company_id IS ? AND lower(email) = ?",
            (group["company_id"], group["lemail"]),
        ).fetchall()
        if any(_is_example(row["email"]) for row in rows):
            plan.examples["e"] += 1
            continue
        group_rows.append((group, list(rows)))
    plan.refs = _contact_refs(
        conn, [row["id"] for _, rows in group_rows for row in rows]
    )
    for group, rows in group_rows:
        # Highest confidence first, then most recent, then the row references
        # already point at, then a stable id order.  Python's sort is stable,
        # so each later sort refines the previous tie-break.
        ordered = sorted(rows, key=lambda row: str(row["id"]))
        ordered.sort(
            key=lambda row: sum(plan.refs.get(row["id"], {}).values()), reverse=True
        )
        ordered.sort(key=lambda row: str(row["collected_at"] or ""), reverse=True)
        ordered.sort(key=lambda row: float(row["confidence"] or 0), reverse=True)
        kept, *extras = ordered
        pair = Pair(
            company_id=group["company_id"],
            email=group["lemail"],
            kept=kept["id"],
            extras=[extra["id"] for extra in extras],
        )
        plan.pairs.append(pair)
        for extra_id in pair.extras:
            plan.extras.append(extra_id)
            plan.kept_of[extra_id] = kept["id"]


def _usable_companies(conn: sqlite3.Connection, excluding: list[str]) -> set[str]:
    companies: set[str] = set()
    if excluding:
        for chunk in _chunks(list(excluding)):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT DISTINCT company_id FROM usable_contact "
                f"WHERE email IS NOT NULL AND email <> '' AND id NOT IN ({placeholders})",
                chunk,
            ).fetchall()
            companies.update(row[0] for row in rows if row[0])
        return companies
    rows = conn.execute(
        "SELECT DISTINCT company_id FROM usable_contact "
        "WHERE email IS NOT NULL AND email <> ''"
    ).fetchall()
    return {row[0] for row in rows if row[0]}


def classify(conn: sqlite3.Connection) -> Plan:
    plan = Plan()
    acr = conn.execute(
        """
        SELECT r.company_id, r.status, r.contact_id, r.domain, r.email,
               c.email AS contact_email, c.company_id AS contact_company_id
        FROM apply_contact_resolution r
        LEFT JOIN contact c ON c.id = r.contact_id
        """
    ).fetchall()
    a_ids: set[str] = set()
    b_ids: set[str] = set()
    c_ids: set[str] = set()
    for row in acr:
        company = row["company_id"]
        plan.acr_status[company] = row["status"]
        example = _is_example(row["domain"], row["email"], row["contact_email"])
        cross = bool(row["contact_id"]) and str(
            row["contact_company_id"] or ""
        ) != str(company or "")
        foreign = _is_foreign(row["domain"]) or _is_foreign(_email_host(row["email"]))
        junk = _is_junk(row["domain"]) or _is_junk(_email_host(row["email"]))
        if cross:
            plan.raw["a"] += 1
        if foreign:
            plan.raw["b"] += 1
        if junk:
            plan.raw["c"] += 1
        if example:
            if cross:
                plan.examples["a"] += 1
            if foreign:
                plan.examples["b"] += 1
            if junk:
                plan.examples["c"] += 1
            continue
        if cross:
            plan.a.append(company)
            a_ids.add(company)
        elif foreign:
            plan.b.append(company)
            b_ids.add(company)
        elif junk:
            plan.c.append(company)
            c_ids.add(company)

    _duplicate_contacts(conn, plan)
    plan.usable_companies_after = _usable_companies(conn, plan.extras)

    for row in acr:
        company = row["company_id"]
        if _is_example(row["domain"], row["email"], row["contact_email"]):
            continue
        if row["status"] != "reachable":
            continue
        if company in plan.usable_companies_after:
            continue
        plan.raw["d"] += 1
        if company not in a_ids and company not in b_ids and company not in c_ids:
            plan.d.append(company)
    return plan


def snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    statuses = dict(
        conn.execute(
            "SELECT status, count(*) FROM apply_contact_resolution GROUP BY status"
        ).fetchall()
    )
    total_companies = conn.execute("SELECT count(*) FROM company").fetchone()[0]
    with_email = conn.execute(
        "SELECT count(*) FROM (SELECT DISTINCT company_id FROM usable_contact "
        "WHERE email IS NOT NULL AND email <> '')"
    ).fetchone()[0]
    return {
        "acr_total": sum(statuses.values()),
        "acr_reachable": statuses.get("reachable", 0),
        "acr_unreachable": statuses.get("unreachable", 0),
        "companies_total": total_companies,
        "companies_with_email": with_email,
        "companies_without_email": total_companies - with_email,
        "contacts": conn.execute("SELECT count(*) FROM contact").fetchone()[0],
        "duplicate_pairs": conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM contact WHERE COALESCE(email, '') <> '' "
            "GROUP BY company_id, lower(email) HAVING count(*) > 1)"
        ).fetchone()[0],
    }


def _print_snapshot(label: str, data: dict[str, int]) -> None:
    print(
        f"{label}: ACR {data['acr_total']} "
        f"(reachable {data['acr_reachable']}, unreachable {data['acr_unreachable']}); "
        f"contacts {data['contacts']}; "
        f"companies {data['companies_total']} "
        f"(usable e-mail {data['companies_with_email']}, "
        f"without {data['companies_without_email']}); "
        f"duplicate (company, email) pairs {data['duplicate_pairs']}"
    )


def _print_plan(plan: Plan) -> None:
    d_overlap = plan.raw["d"] - len(plan.d)
    print()
    print("Category                                                      rows  note")
    print(
        f"a cross-company resolutions                                "
        f"{len(plan.a):6d}  {plan.examples['a']} e2e .example skipped"
    )
    print(
        f"b foreign/aggregator domains                               "
        f"{len(plan.b):6d}  raw {plan.raw['b']}, {plan.raw['b'] - len(plan.b)} also in a"
    )
    print(
        f"c junk domain fragments (ACR)                              "
        f"{len(plan.c):6d}  CDR/CDC rows for these domains also removed"
    )
    print(
        f"d reachable without a usable contact e-mail                "
        f"{len(plan.d):6d}  raw {plan.raw['d']}, {d_overlap} already in a/b/c"
    )
    print(
        f"e duplicate contacts: {len(plan.pairs)} pair(s)           "
        f"{len(plan.extras):6d}  {plan.examples['e']} e2e .example pairs skipped"
    )
    for pair in plan.pairs:
        print(f"    {pair.email} under {pair.company_id}: keep {pair.kept}, "
              f"delete {', '.join(pair.extras)}")


def _backup_by_keys(
    conn: sqlite3.Connection,
    source: str,
    backup: str,
    key_column: str,
    keys: list[str],
    *,
    delete: bool = False,
) -> int:
    conn.execute("CREATE TEMP TABLE _repair_keys (key TEXT)")
    conn.executemany("INSERT INTO _repair_keys VALUES (?)", [(key,) for key in keys])
    conn.execute(
        f'CREATE TABLE "{backup}" AS SELECT s.* FROM {source} s '
        f"JOIN _repair_keys k ON s.{key_column} IS k.key"
    )
    count = conn.execute(f'SELECT count(*) FROM "{backup}"').fetchone()[0]
    if delete:
        conn.execute(
            f"DELETE FROM {source} WHERE {key_column} IN (SELECT key FROM _repair_keys)"
        )
    conn.execute("DROP TABLE _repair_keys")
    return count


def _backup_by_domain(
    conn: sqlite3.Connection, source: str, backup: str, predicate: str, params: tuple
) -> int:
    conn.execute(f'CREATE TABLE "{backup}" AS SELECT * FROM {source} WHERE {predicate}', params)
    count = conn.execute(f'SELECT count(*) FROM "{backup}"').fetchone()[0]
    conn.execute(f"DELETE FROM {source} WHERE {predicate}", params)
    return count


def apply_plan(conn: sqlite3.Connection, plan: Plan) -> dict[str, int]:
    created: dict[str, int] = {}
    extras = sorted(set(plan.extras))
    deleted_ids = plan.acr_deleted

    repointed_acr: list[str] = []
    package_rows: list[str] = []
    intro_rows: list[str] = []
    if extras:
        for chunk in _chunks(extras):
            placeholders = ",".join("?" for _ in chunk)
            repointed_acr.extend(
                row[0]
                for row in conn.execute(
                    "SELECT company_id FROM apply_contact_resolution "
                    f"WHERE contact_id IN ({placeholders})",
                    chunk,
                )
            )
            package_rows.extend(
                row[0]
                for row in conn.execute(
                    "SELECT id FROM application_package "
                    f"WHERE contact_id IN ({placeholders})",
                    chunk,
                )
            )
            intro_rows.extend(
                row[0]
                for row in conn.execute(
                    "SELECT id FROM introduction_path "
                    f"WHERE target_contact_id IN ({placeholders})",
                    chunk,
                )
            )

    # Backups first, before any UPDATE or DELETE touches the source tables.
    acr_keys = sorted(set(deleted_ids) | set(repointed_acr))
    created["acr"] = _backup_by_keys(
        conn, "apply_contact_resolution", BACKUP_TABLES["acr"], "company_id", acr_keys
    )
    junk = sorted(JUNK_DOMAINS)
    placeholders = ",".join("?" for _ in junk)
    junk_predicate = f"domain IN ({placeholders})"
    created["cdr"] = _backup_by_domain(
        conn, "company_domain_resolution", BACKUP_TABLES["cdr"], junk_predicate, tuple(junk)
    )
    created["cdc"] = _backup_by_domain(
        conn, "company_domain_candidate", BACKUP_TABLES["cdc"], junk_predicate, tuple(junk)
    )
    if package_rows:
        created["package"] = _backup_by_keys(
            conn, "application_package", BACKUP_TABLES["package"], "id",
            sorted(set(package_rows)),
        )
    if intro_rows:
        created["intro"] = _backup_by_keys(
            conn, "introduction_path", BACKUP_TABLES["intro"], "id",
            sorted(set(intro_rows)),
        )
    if extras:
        created["contact"] = _backup_by_keys(
            conn, "contact", BACKUP_TABLES["contact"], "id", extras
        )

    # Deletes and repoints.
    for chunk in _chunks(deleted_ids):
        ids = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM apply_contact_resolution WHERE company_id IN ({ids})", chunk
        )
    conn.execute(f"DELETE FROM company_domain_resolution WHERE {junk_predicate}", tuple(junk))
    conn.execute(f"DELETE FROM company_domain_candidate WHERE {junk_predicate}", tuple(junk))
    if extras:
        for extra, kept in plan.kept_of.items():
            conn.execute(
                "UPDATE apply_contact_resolution SET contact_id = ? WHERE contact_id = ?",
                (kept, extra),
            )
            conn.execute(
                "UPDATE application_package SET contact_id = ? WHERE contact_id = ?",
                (kept, extra),
            )
            conn.execute(
                "UPDATE introduction_path SET target_contact_id = ? WHERE target_contact_id = ?",
                (kept, extra),
            )
        for chunk in _chunks(extras):
            ids = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM contact WHERE id IN ({ids})", chunk)
    return created


def _backup_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _try_unique_index() -> tuple[bool, str]:
    try:
        with write_tx() as conn:
            conn.execute(UNIQUE_INDEX_SQL)
    except sqlite3.Error as exc:  # duplicates left, or a lock we could not win
        return False, str(exc)
    return True, ""


def _print_revert(created: dict[str, int]) -> None:
    lines = ["Revert SQL:", "BEGIN IMMEDIATE;"]
    if "contact" in created:
        lines.append(f"DROP INDEX IF EXISTS {UNIQUE_INDEX};")
    for key in ("acr", "cdr", "cdc", "contact", "package", "intro"):
        if key in created:
            lines.append(
                f"INSERT OR REPLACE INTO {REVERT_TABLES[key]} "
                f"SELECT * FROM {BACKUP_TABLES[key]};"
            )
    lines.append("COMMIT;")
    print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="back up the affected rows and repair; default is a dry run",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="database path (default: DREAMJOB_DB_PATH from .env / settings)",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else get_settings().abs_db_path
    print(f"Database: {db_path}")
    print(f"Mode: {'APPLY' if args.apply else 'dry run (pass --apply to repair)'}")

    with read_tx(db_path) as conn:
        before = snapshot(conn)
        plan = classify(conn)

    _print_snapshot("Before", before)
    _print_plan(plan)

    if not args.apply:
        projected = dict(before)
        deleted_status = [plan.acr_status.get(cid, "") for cid in plan.acr_deleted]
        projected["acr_total"] = before["acr_total"] - len(plan.acr_deleted)
        projected["acr_reachable"] = before["acr_reachable"] - deleted_status.count("reachable")
        projected["acr_unreachable"] = (
            before["acr_unreachable"] - deleted_status.count("unreachable")
        )
        projected["contacts"] = before["contacts"] - len(set(plan.extras))
        projected["duplicate_pairs"] = before["duplicate_pairs"] - len(plan.pairs)
        projected["companies_with_email"] = len(plan.usable_companies_after)
        projected["companies_without_email"] = (
            before["companies_total"] - len(plan.usable_companies_after)
        )
        _print_snapshot("Projected after", projected)
        print("\nDry run only; nothing written. Re-run with --apply to repair.")
        return 0

    with read_tx(db_path) as conn:
        for backup in BACKUP_TABLES.values():
            if _backup_exists(conn, backup):
                print(
                    f"Backup table {backup} already exists; refusing to overwrite it. "
                    "Rename or drop it first.",
                    file=sys.stderr,
                )
                return 2

    with write_tx(db_path) as conn:
        plan = classify(conn)
        created = apply_plan(conn, plan)

    print()
    for key, backup in BACKUP_TABLES.items():
        if key in created:
            print(f"Backup {backup}: {created[key]} row(s)")
    deleted_status = [plan.acr_status.get(cid, "") for cid in plan.acr_deleted]
    print(
        f"Deleted ACR {len(plan.acr_deleted)} row(s) "
        f"(reachable {deleted_status.count('reachable')}, "
        f"unreachable {deleted_status.count('unreachable')}); "
        f"deleted contacts {len(set(plan.extras))} row(s)"
    )
    for pair in plan.pairs:
        print(
            f"Duplicate {pair.email} under {pair.company_id}: kept {pair.kept}, "
            f"deleted {', '.join(pair.extras)}"
        )

    with read_tx(db_path) as conn:
        after = snapshot(conn)
    _print_snapshot("After", after)

    ok, error = _try_unique_index()
    if ok:
        print(f"Unique index {UNIQUE_INDEX}: created (or already present).")
    else:
        print(f"Unique index {UNIQUE_INDEX}: NOT created: {error}")

    _print_revert(created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
