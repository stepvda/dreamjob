"""Inspect the SQLite database, then optionally maintain it - never more.

The development database is ~2 GiB and the machine is short on disk, so this
script answers three questions before anything touches a page:

* **Where did the space go?**  ``dbstat`` attributes every page to a table or
  index; the script reports the top consumers and the page/WAL/freelist
  totals behind the file size.
* **How much of ``opportunity`` is blob?**  The ranked list reads a handful of
  columns, but ``description``, ``rationale`` and the score/JSON detail columns
  carry most of the table's weight.  The retention report measures each large
  column, counts the rows old enough and unreferenced enough to consider, and
  estimates what a future retention pass could reclaim.  It deletes nothing.
* **Is ``VACUUM`` safe to suggest?**  ``VACUUM`` writes a second copy of the
  database before it swaps files in, so it needs roughly the database size in
  free disk.  The script reports the free space and *prints* the command;
  it never runs it, and it never runs on its own.

The default run is read-only.  ``--apply`` runs exactly two maintenance
statements - ``PRAGMA optimize`` and a WAL checkpoint (``PASSIVE`` unless
``--checkpoint-mode`` says otherwise) - and still only suggests ``VACUUM``.

    PYTHONPATH=backend .venv/bin/python scripts/db_maintenance.py
    PYTHONPATH=backend .venv/bin/python scripts/db_maintenance.py --apply

Keep the backend running if you like: ``PASSIVE`` never blocks a reader, and
``PRAGMA optimize`` only rewrites the planner's own statistics.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

from dreamjob.config import get_settings
from dreamjob.db.connection import read_tx, write_tx

#: Columns whose text dominates ``opportunity``.  Ordered as they are reported,
#: largest expected first; the length sums are what the report is built on.
OPPORTUNITY_BLOBS: tuple[str, ...] = (
    "description",
    "rationale",
    "score_detail",
    "dream_fit_detail",
    "speculative_rationale",
    "comp_sources",
    "required_skills",
    "desirable_skills",
    "employer_review_themes",
    "tags",
)

#: A row is "heavy" once its blob columns together exceed this.  The threshold
#: is a report bucket, not a retention rule.
HEAVY_BLOB_BYTES = 16 * 1024

#: "Old" for the retention report: untouched for this many days.
OLD_DAYS = 90

#: ``VACUUM`` needs about the database size again; keep a margin on top.
VACUUM_HEADROOM = 1.15

UNREFERENCED = (
    "o.user_status = 'new' AND o.selected = 0 AND o.pinned = 0 "
    "AND o.manual_rank IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM application_package p WHERE p.opportunity_id = o.id) "
    "AND NOT EXISTS (SELECT 1 FROM pipeline_card k WHERE k.opportunity_id = o.id) "
    "AND NOT EXISTS (SELECT 1 FROM apply_selection s WHERE s.opportunity_id = o.id) "
    "AND NOT EXISTS (SELECT 1 FROM interview_appointment a WHERE a.opportunity_id = o.id) "
    "AND NOT EXISTS (SELECT 1 FROM negotiation_brief b WHERE b.opportunity_id = o.id) "
    "AND NOT EXISTS (SELECT 1 FROM mock_interview_session m WHERE m.opportunity_id = o.id) "
    "AND NOT EXISTS (SELECT 1 FROM manual_response r WHERE r.opportunity_id = o.id) "
    "AND NOT EXISTS (SELECT 1 FROM introduction_path i WHERE i.opportunity_id = o.id)"
)

#: Sum of the blob columns, as a SQL expression, for the retention report.
BLOB_BYTES_SQL = " + ".join(f"LENGTH(COALESCE(o.{column}, ''))" for column in OPPORTUNITY_BLOBS)


def _human(bytes_: int) -> str:
    value = float(bytes_)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _pragma(conn: sqlite3.Connection, name: str):
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


def _dbstat_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT count(*) FROM dbstat LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def _space_report(conn: sqlite3.Connection, db_path: Path) -> dict[str, object]:
    page_size = int(_pragma(conn, "page_size") or 0)
    page_count = int(_pragma(conn, "page_count") or 0)
    freelist = int(_pragma(conn, "freelist_count") or 0)
    file_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    report: dict[str, object] = {
        "page_size": page_size,
        "page_count": page_count,
        "file_bytes": file_bytes,
        "freelist_bytes": freelist * page_size,
        "journal_mode": _pragma(conn, "journal_mode"),
        "auto_vacuum": _pragma(conn, "auto_vacuum"),
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "shm_bytes": shm_path.stat().st_size if shm_path.exists() else 0,
        "consumers": [],
    }
    if _dbstat_available(conn):
        report["consumers"] = [
            (row["name"], int(row["bytes"]), int(row["pages"]))
            for row in conn.execute(
                "SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages "
                "FROM dbstat GROUP BY name ORDER BY bytes DESC LIMIT 15"
            )
        ]
    return report


def _print_space(report: dict[str, object], db_path: Path, top: int) -> None:
    print("\nSpace")
    print(f"  database file      {db_path}  {_human(int(report['file_bytes']))}")
    print(
        f"  pages              {report['page_count']} x {_human(int(report['page_size']))}"
        f" = {_human(int(report['page_count']) * int(report['page_size']))}"
    )
    print(f"  freelist           {_human(int(report['freelist_bytes']))} (reusable in place)")
    print(
        f"  WAL / SHM          {_human(int(report['wal_bytes']))} / "
        f"{_human(int(report['shm_bytes']))}"
    )
    print(f"  journal_mode       {report['journal_mode']}")
    print(f"  auto_vacuum        {report['auto_vacuum']}")
    consumers = report.get("consumers") or []
    if not consumers:
        print("  top consumers      dbstat not available in this SQLite build")
        return
    print(f"\n  {'btree':<44}{'bytes':>12}{'pages':>10}")
    print("  " + "-" * 66)
    for name, bytes_, pages in consumers[:top]:
        print(f"  {str(name):<44}{_human(bytes_):>12}{pages:>10}")


def _retention_report(conn: sqlite3.Connection, top: int) -> dict[str, object]:
    total_rows = conn.execute("SELECT count(*) FROM opportunity").fetchone()[0]
    column_bytes = {
        column: int(
            conn.execute(
                f"SELECT COALESCE(SUM(LENGTH(COALESCE({column}, ''))), 0) FROM opportunity"
            ).fetchone()[0]
        )
        for column in OPPORTUNITY_BLOBS
    }
    heavy_rows = int(
        conn.execute(
            f"SELECT count(*) FROM opportunity o WHERE ({BLOB_BYTES_SQL}) >= ?",
            (HEAVY_BLOB_BYTES,),
        ).fetchone()[0]
    )
    top_rows = [
        {
            "id": row["id"],
            "bytes": int(row["bytes"]),
            "updated_at": row["updated_at"],
            "title": row["title"],
        }
        for row in conn.execute(
            f"SELECT o.id, o.title, o.updated_at, ({BLOB_BYTES_SQL}) AS bytes "
            "FROM opportunity o ORDER BY bytes DESC LIMIT ?",
            (top,),
        )
    ]
    old_sql = f"substr(o.updated_at, 1, 10) < date('now', '-{OLD_DAYS} days')"
    retention: dict[str, object] = {"total_rows": total_rows, "column_bytes": column_bytes,
                                     "heavy_rows": heavy_rows, "top_rows": top_rows}
    for label, predicate in (
        ("old", old_sql),
        ("unreferenced", UNREFERENCED),
        ("old and unreferenced", f"({old_sql}) AND ({UNREFERENCED})"),
    ):
        row = conn.execute(
            f"SELECT count(*) AS n, COALESCE(SUM({BLOB_BYTES_SQL}), 0) AS bytes "
            f"FROM opportunity o WHERE {predicate}"
        ).fetchone()
        retention[label] = {"rows": int(row["n"]), "bytes": int(row["bytes"])}
    return retention


def _print_retention(report: dict[str, object], top: int) -> None:
    print("\nOpportunity blob retention")
    print(f"  rows               {report['total_rows']}")
    print(f"  rows over {_human(HEAVY_BLOB_BYTES)} blob columns   {report['heavy_rows']}")
    print(f"\n  {'column':<26}{'text bytes':>14}{'of table':>10}")
    print("  " + "-" * 50)
    column_bytes = report["column_bytes"]
    total_blob = sum(column_bytes.values()) or 1
    for column, bytes_ in sorted(column_bytes.items(), key=lambda kv: -kv[1]):
        print(f"  {column:<26}{_human(bytes_):>14}{bytes_ / total_blob:>9.1%}")
    print(f"  {'blob columns total':<26}{_human(sum(column_bytes.values())):>14}")
    print(f"\n  {'bucket':<26}{'rows':>10}{'blob bytes':>16}")
    print("  " + "-" * 52)
    for label in ("old", "unreferenced", "old and unreferenced"):
        bucket = report[label]
        print(f"  {label:<26}{bucket['rows']:>10}{_human(bucket['bytes']):>16}")
    print(f"\n  top {top} rows by blob bytes:")
    for row in report["top_rows"][:top]:
        print(f"    {row['bytes']:>10} B  {row['updated_at']}  {str(row['title'])[:48]}  {row['id']}")


def _vacuum_suggestion(db_path: Path, report: dict[str, object]) -> str:
    file_bytes = int(report["file_bytes"])
    freelist_bytes = int(report["freelist_bytes"])
    free_bytes = shutil.disk_usage(db_path.parent).free
    needed = int(file_bytes * VACUUM_HEADROOM)
    verdict = (
        f"VACUUM would need about {_human(needed)} free and there is {_human(free_bytes)}"
    )
    if free_bytes >= needed:
        verdict += " - it could run, from a stopped backend:\n"
    else:
        verdict += " - not enough headroom right now.\n"
    return (
        f"{verdict}"
        f"      sqlite3 {db_path} 'VACUUM;'\n"
        f"  VACUUM rewrites the file and can reclaim the "
        f"{_human(freelist_bytes)} freelist plus fragmentation.  This script never runs it."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="run PRAGMA optimize and the WAL checkpoint; VACUUM is still only suggested",
    )
    parser.add_argument(
        "--checkpoint-mode",
        default="PASSIVE",
        choices=("PASSIVE", "FULL", "RESTART", "TRUNCATE"),
        help="WAL checkpoint mode used with --apply (default: PASSIVE, never blocks)",
    )
    parser.add_argument("--top", type=int, default=15, help="how many btrees/rows to list")
    parser.add_argument(
        "--db",
        default=None,
        help="database path (default: DREAMJOB_DB_PATH from .env / settings)",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else get_settings().abs_db_path
    print(f"Database: {db_path}")
    print(f"Mode: {'APPLY (optimize + checkpoint)' if args.apply else 'read-only (dry run)'}")

    with read_tx(db_path) as conn:
        space = _space_report(conn, db_path)
        retention = _retention_report(conn, args.top)

    _print_space(space, db_path, args.top)
    _print_retention(retention, args.top)
    print("\nVACUUM")
    print("  " + _vacuum_suggestion(db_path, space))

    if not args.apply:
        print(
            "\nRead-only run; PRAGMA optimize and the WAL checkpoint were NOT run. "
            "Pass --apply for those two."
        )
        return 0

    with write_tx(db_path) as conn:
        conn.execute("PRAGMA optimize")
    print("\nPRAGMA optimize: applied.")
    with read_tx(db_path) as conn:
        row = conn.execute(f"PRAGMA wal_checkpoint({args.checkpoint_mode})").fetchone()
    if row is None:
        print(f"WAL checkpoint ({args.checkpoint_mode}): returned no result")
    else:
        busy, log, checkpointed = (int(row[0]), int(row[1]), int(row[2]))
        print(
            f"WAL checkpoint ({args.checkpoint_mode}): busy={busy}, log={log} frames, "
            f"checkpointed={checkpointed}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
