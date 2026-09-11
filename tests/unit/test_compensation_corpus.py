"""The collected postings become a market pay observation (FR-264).

``compensation_observation`` was empty while the corpus held thousands of stated
ranges, so every opportunity was priced off the built-in prior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db import connection as conn_mod
from dreamjob.db.connection import insert_row, query_all, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.pipeline import compensation_corpus


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "comp.db"
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(path))
    get_settings.cache_clear()
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))
    yield path
    get_settings.cache_clear()


def _vacancy(minimum: float, maximum: float, **overrides) -> None:
    row = {
        "title": "Data Engineer",
        "function_family": "data & analytics",
        "seniority": "senior",
        "country": "BE",
        "salary_currency": "EUR",
        "salary_min": minimum,
        "salary_max": maximum,
        "source_adapter": "ats.greenhouse",
        "collected_at": utcnow(),
    }
    row.update(overrides)
    insert_row("vacancy", row)


def test_priced_postings_become_one_observation_per_segment(db: Path) -> None:
    for low, high in [(60_000, 70_000), (65_000, 75_000), (70_000, 80_000),
                      (75_000, 85_000), (80_000, 90_000), (85_000, 95_000)]:
        _vacancy(low, high)

    result = compensation_corpus.mine_posted_ranges()
    assert result["segments"] == 1

    rows = query_all(
        "SELECT * FROM compensation_observation WHERE source = ?",
        (compensation_corpus.SOURCE,),
    )
    assert len(rows) == 1
    observation = rows[0]
    assert observation["function_family"] == "data & analytics"
    assert observation["seniority"] == "senior"
    assert observation["country"] == "BE"
    assert observation["sample_size"] == 6
    assert observation["amount_min"] == 60_000
    assert observation["amount_max"] == 95_000
    # Midpoints 65k..90k, so the median sits between the third and fourth.
    assert 72_000 <= observation["amount_median"] <= 78_000


def test_a_segment_below_the_minimum_sample_is_not_published(db: Path) -> None:
    _vacancy(60_000, 70_000)
    _vacancy(65_000, 75_000)
    result = compensation_corpus.mine_posted_ranges(min_sample=5)
    assert result["segments"] == 0


def test_a_seniority_less_posting_is_not_published(db: Path) -> None:
    """The blend looks up by family and seniority; a row without one is dead."""
    for _ in range(6):
        _vacancy(60_000, 70_000, seniority=None)
    assert compensation_corpus.mine_posted_ranges()["segments"] == 0


def test_non_eur_postings_are_left_out(db: Path) -> None:
    for _ in range(6):
        _vacancy(60_000, 70_000, salary_currency="USD")
    assert compensation_corpus.mine_posted_ranges()["segments"] == 0


def test_the_mining_is_idempotent(db: Path) -> None:
    for _ in range(6):
        _vacancy(60_000, 70_000)
    compensation_corpus.mine_posted_ranges()
    compensation_corpus.mine_posted_ranges()
    rows = query_all(
        "SELECT COUNT(*) AS n FROM compensation_observation WHERE source = ?",
        (compensation_corpus.SOURCE,),
    )
    assert rows[0]["n"] == 1
