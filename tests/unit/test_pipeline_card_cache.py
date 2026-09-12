"""The memo in front of ``pipeline_cards.active_seeker_ids`` (FR-403).

The query behind it is a four-way UNION over ``campaign``, ``watchlist_entry``,
``pipeline_card`` and ``opportunity``.  On the development database it was
measured at up to 23.8 s and 735k steps per call, and the follow-up sweep and
the digest both ask it on every scheduler tick, so the same answer was paid for
twice per tick and again every tick while nothing had changed.

The properties being pinned down:

* a second call within the TTL is served from memory and identical to the
  first;
* the callers rebuild ``since`` on every tick, so a call with the same *date*
  but a few seconds later is still a cache hit - otherwise the memo would
  never hit in production;
* every write to one of the four tables clears the memo, so a new campaign is
  visible at once;
* a write from another process (which cannot clear this cache) is bounded by
  the TTL rather than by hope.
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = ("DREAMJOB_DATA_DIR", "DREAMJOB_DB_PATH", "DREAMJOB_MASTER_KEY", "DREAMJOB_ENV")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_ENV"] = "development"
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate
    from dreamjob.db.repositories import pipeline_cards as repo

    migrate()
    repo.invalidate_active_seeker_cache()
    yield
    repo.invalidate_active_seeker_cache()

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def _seed_seekers(*emails: str) -> list[tuple[str, dict]]:
    """One seeker with a directive set and a profile version per address."""
    from dreamjob.db.connection import insert_row, utcnow

    seeded: list[tuple[str, dict]] = []
    for email in emails:
        seeker_id = insert_row(
            "job_seeker",
            {
                "email": email,
                "display_name": "Cache Test",
                "locale": "en",
                "created_at": utcnow(),
                "updated_at": utcnow(),
            },
        )
        directive_id = insert_row(
            "directive_set",
            {"job_seeker_id": seeker_id, "name": "Default", "created_at": utcnow()},
        )
        profile_id = insert_row(
            "profile_version",
            {
                "job_seeker_id": seeker_id,
                "version": 1,
                "sections": "{}",
                "created_at": utcnow(),
            },
        )
        seeded.append(
            (seeker_id, {"directive_set_id": directive_id, "profile_version_id": profile_id})
        )
    return seeded


def _seed_campaign(seeker_id: str, required: dict, *, status: str = "running") -> str:
    from dreamjob.db.connection import insert_row, utcnow

    return insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "name": "Cache",
            "status": status,
            "created_at": utcnow(),
            **required,
        },
    )


def _counting_queries(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every statement ``active_seeker_ids`` actually sends."""
    from dreamjob.db.repositories import pipeline_cards as repo

    calls: list[str] = []
    real = repo.query_all

    def counting(sql: str, params: tuple = (), db_path: Path | None = None):  # noqa: ANN202
        calls.append(sql)
        return real(sql, params, db_path)

    monkeypatch.setattr(repo, "query_all", counting)
    return calls


def test_the_second_call_within_the_ttl_does_not_hit_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dreamjob.db.repositories import pipeline_cards as repo

    (seeker_id, required), = _seed_seekers("cache-one@example.test")
    _seed_campaign(seeker_id, required, status="running")
    calls = _counting_queries(monkeypatch)

    first = repo.active_seeker_ids("2026-01-01T10:00:00+00:00")
    # Same date, eleven seconds later: this is exactly what the scheduler
    # hands over on the next tick.
    second = repo.active_seeker_ids("2026-01-01T10:00:11+00:00")

    assert first == second == [seeker_id]
    assert len(calls) == 1, "the second call went back to the database"


def test_a_campaign_write_invalidates_the_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dreamjob.db.repositories import campaigns
    from dreamjob.db.repositories import pipeline_cards as repo

    (first_seeker, required), (second_seeker, second_required) = _seed_seekers(
        "cache-first@example.test", "cache-second@example.test"
    )
    _seed_campaign(first_seeker, required, status="running")
    calls = _counting_queries(monkeypatch)

    assert repo.active_seeker_ids("2026-01-01T00:00:00+00:00") == [first_seeker]

    campaigns.create_campaign(second_seeker, {"name": "Second", "status": "planned", **second_required})

    assert sorted(repo.active_seeker_ids("2026-01-01T00:00:00+00:00")) == sorted(
        [first_seeker, second_seeker]
    )
    assert len(calls) == 2, "the write did not clear the memo"


def test_a_write_from_another_process_is_bounded_by_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dreamjob.db.repositories import pipeline_cards as repo

    (first_seeker, required), (second_seeker, second_required) = _seed_seekers(
        "cache-clock-first@example.test", "cache-clock-second@example.test"
    )
    _seed_campaign(first_seeker, required, status="running")
    calls = _counting_queries(monkeypatch)
    clock = [10_000.0]
    monkeypatch.setattr(repo.time, "monotonic", lambda: clock[0])

    assert repo.active_seeker_ids("2026-01-01T00:00:00+00:00") == [first_seeker]

    # Another process writes a campaign this process cannot see or clear.
    _seed_campaign(second_seeker, second_required, status="planned")
    assert repo.active_seeker_ids("2026-01-01T00:00:00+00:00") == [first_seeker]

    clock[0] += repo.ACTIVE_SEEKER_CACHE_TTL_SECONDS + 1
    assert sorted(repo.active_seeker_ids("2026-01-01T00:00:00+00:00")) == sorted(
        [first_seeker, second_seeker]
    )
    assert len(calls) == 2
