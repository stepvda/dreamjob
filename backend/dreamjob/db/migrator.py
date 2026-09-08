"""Versioned schema migrations (CR-408).

Migrations are plain ``.sql`` files named ``NNN_description.sql`` in
``db/migrations``.  They run once, in filename order, inside a transaction,
and are recorded in ``schema_migration``.  There is no down-migration: a
mistake is corrected by a new forward migration, which keeps the applied
history honest.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from dreamjob.db.connection import get_connection, utcnow, write_tx

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);
"""


def discover() -> list[tuple[str, str, Path]]:
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _NAME_RE.match(p.name)
        if not m:
            raise ValueError(f"Migration filename must be NNN_name.sql, got {p.name!r}")
        out.append((m.group(1), m.group(2), p))
    return out


def applied_versions(db_path: Path | None = None) -> dict[str, str]:
    conn = get_connection(db_path)
    conn.executescript(_BOOTSTRAP)
    cur = conn.execute("SELECT version, checksum FROM schema_migration")
    try:
        return {r["version"]: r["checksum"] for r in cur.fetchall()}
    finally:
        cur.close()


def migrate(db_path: Path | None = None) -> list[str]:
    """Apply pending migrations.  Returns the versions that were applied."""
    done = applied_versions(db_path)
    applied: list[str] = []
    for version, name, path in discover():
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
        if version in done:
            if done[version] != checksum:
                log.warning(
                    "Migration %s_%s changed after being applied (checksum %s -> %s). "
                    "Applied migrations must be immutable; add a new migration instead.",
                    version, name, done[version], checksum,
                )
            continue
        log.info("Applying migration %s_%s", version, name)
        with write_tx(db_path) as conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migration (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (version, name, checksum, utcnow()),
            )
        applied.append(version)
    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = migrate()
    print(f"Applied: {result or 'nothing (already up to date)'}")
