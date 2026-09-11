"""Sensitive rows are encrypted at rest and transparent on read (NFR-201).

The profile and the generated CV/motivation text used to sit in plaintext in
``profile_version.sections`` and ``application_package.generation_notes`` even
though the envelope that could encrypt them existed.  These tests pin both
halves: the bytes on disk are sealed, and every reader still sees the plaintext
it saw before.
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db import connection as conn_mod
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import applications as app_repo
from dreamjob.db.repositories import profiles as profile_repo
from dreamjob.security import at_rest


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "at_rest.db"
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(path))
    monkeypatch.setenv(
        "DREAMJOB_MASTER_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    get_settings.cache_clear()
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))
    yield path
    get_settings.cache_clear()


def _raw(path: Path, sql: str, params: tuple = ()) -> dict:
    conn = conn_mod.get_connection(path)
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {
            "email": f"{secrets.token_hex(4)}@example.com",
            "display_name": "Encrypted",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def test_the_profile_is_sealed_on_disk_and_plain_on_read(db: Path) -> None:
    seeker = _seeker()
    version = profile_repo.create_version(
        seeker,
        {"contact": {"name": "Ada Lovelace"}, "summary": "Mathematician"},
        source_note="manual",
    )

    stored = _raw(db, "SELECT sections FROM profile_version WHERE id = ?", (version["id"],))
    assert stored["sections"].startswith(at_rest.PREFIX), "the profile must not be plaintext"

    read_back = profile_repo.get_version(seeker, version["id"])
    assert read_back is not None
    assert read_back["sections"]["contact"]["name"] == "Ada Lovelace"
    assert read_back["sections"]["summary"] == "Mathematician"


def test_generated_cv_text_is_sealed_on_disk_and_plain_on_read(db: Path) -> None:
    seeker = _seeker()
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker, "name": "Encryption test", "created_at": utcnow()},
    )
    profile_id = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker,
            "version": 1,
            "sections": "{}",
            "source_note": "manual",
            "created_at": utcnow(),
        },
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "name": "Encryption test",
            "created_at": utcnow(),
        },
    )
    opportunity_id = insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker,
            "campaign_id": campaign_id,
            "kind": "vacancy",
            "title": "Data Engineer",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    package_id = app_repo.create_package(
        seeker,
        opportunity_id,
        {"generation_notes": {"cv_document": {"summary": "Tailored CV text"}}},
    )

    stored = _raw(
        db, "SELECT generation_notes FROM application_package WHERE id = ?", (package_id,)
    )
    assert stored["generation_notes"].startswith(at_rest.PREFIX)

    package = app_repo.get_package(package_id, seeker)
    assert package is not None
    assert package["generation_notes"]["cv_document"]["summary"] == "Tailored CV text"


def test_a_value_written_before_encryption_still_reads(db: Path) -> None:
    """Legacy plaintext rows carry no marker and must pass through untouched."""
    seeker = _seeker()
    version_id = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker,
            "version": 1,
            "sections": '{"contact": {"name": "Legacy"}}',
            "source_note": "manual",
            "created_at": utcnow(),
        },
    )
    read_back = profile_repo.get_version(seeker, version_id)
    assert read_back is not None
    assert read_back["sections"]["contact"]["name"] == "Legacy"
