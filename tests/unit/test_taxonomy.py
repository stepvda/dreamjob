"""Source vocabularies fold onto the canonical FR-261 fields (FR-261, CR-405).

Personio's ``experienced`` (13,533 rows), Recruitee's ``mid_level`` and the two
spellings of entry level were stored as though they were career stages the
ranking model understood.  These tests pin the mapping, the fallback for an
unrecognised word, and the backfill.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db import connection as conn_mod
from dreamjob.db.connection import insert_row, query_one, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.pipeline import opportunities, taxonomy


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "taxonomy.db"
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(path))
    get_settings.cache_clear()
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))
    yield path
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("experienced", "medior"),
        ("mid_level", "medior"),
        ("mid-level", "medior"),
        ("entry_level", "junior"),
        ("entry-level", "junior"),
        ("student_college", "intern"),
        ("student_school", "intern"),
        ("senior", "senior"),
        ("Senior Manager", "director"),
        ("Executive", "director"),
        ("C-Level", "c_level"),
        ("", None),
        ("wizard", None),
    ],
)
def test_seniority_aliases_fold(source: str, expected: str | None) -> None:
    assert taxonomy.canonical_seniority(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Engineering", "software engineering"),
        ("engineering", "software engineering"),
        ("IT", "it operations"),
        ("Technology", "software engineering"),
        ("Commercial", "sales & business development"),
        ("Marketing", "marketing & communications"),
        ("Data", "data & analytics"),
        ("", None),
        ("Facilities", None),
    ],
)
def test_function_aliases_fold(source: str, expected: str | None) -> None:
    assert taxonomy.canonical_function_family(source) == expected


def test_a_normalised_vacancy_uses_the_canonical_fields() -> None:
    record = opportunities.normalise_vacancy(
        {
            "id": "v1",
            "title": "Platform Engineer",
            "description": "Build things.",
            "function_family": "Engineering",
            "seniority": "experienced",
        }
    )
    assert record["function_family"] == "software engineering"
    assert record["seniority"] == "medior"


def test_an_unknown_source_label_falls_back_to_the_text() -> None:
    """A vendor word we do not know is not a licence to drop the inference."""
    record = opportunities.normalise_vacancy(
        {
            "id": "v2",
            "title": "Senior Data Engineer",
            "description": "",
            "function_family": "Platform Ninja",
            "seniority": "Ninja",
        }
    )
    assert record["function_family"] == "data & analytics"
    assert record["seniority"] == "senior"


def test_backfill_rewrites_a_vacancy_and_its_opportunity(db: Path) -> None:
    seeker = insert_row(
        "job_seeker",
        {
            "email": "tax@example.com",
            "display_name": "Tax",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker, "name": "d", "created_at": utcnow()},
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
            "name": "c",
            "created_at": utcnow(),
        },
    )
    vacancy_id = insert_row(
        "vacancy",
        {
            "title": "Data Engineer",
            "function_family": "Engineering",
            "seniority": "experienced",
            "source_adapter": "ats.personio",
            "collected_at": utcnow(),
        },
    )
    opportunity_id = insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker,
            "campaign_id": campaign_id,
            "vacancy_id": vacancy_id,
            "kind": "vacancy",
            "title": "Data Engineer",
            "function_family": "Engineering",
            "seniority": "experienced",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )

    changed = taxonomy.backfill_vacancies()

    assert changed["vacancies"] == 1
    assert changed["opportunities"] == 1
    assert query_one("SELECT seniority FROM vacancy WHERE id = ?", (vacancy_id,))[
        "seniority"
    ] == "medior"
    assert query_one("SELECT function_family FROM opportunity WHERE id = ?", (opportunity_id,))[
        "function_family"
    ] == "software engineering"
