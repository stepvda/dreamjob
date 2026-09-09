"""The board registry as a live register, and the named-employer seed (FR-181, DR-101).

``tests/unit/test_board_registry.py`` covers the shipped file and the importer
that builds it. This covers what happens to a board *afterwards*: the liveness
this installation establishes with its own requests, the retirement of a board
that has stopped answering, and the import that must never overwrite either.

It also covers the employer seed, which exists because the shipped registry is
built from public URL indexes and is therefore shaped like them - Personio and
Recruitee SMEs, Teamtailor's Nordic base, and the Greenhouse/Ashby boards that
appear in Hacker News threads. The large employers a Benelux search is judged
on appear in none of those indexes, so no campaign could ever surface them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import board_registry as repo

SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "dreamjob" / "pipeline" / "data" / "employer_seed.json"
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


def test_upsert_normalises_the_vendor_and_needs_both_keys(db):
    repo.upsert({"vendor": "GreenHouse", "slug": "acme", "source": "commoncrawl"})
    assert repo.get("greenhouse", "acme") is not None
    # DR-101 keys on (ats_vendor, ats_slug); half a key is not a board.
    with pytest.raises(ValueError):
        repo.upsert({"vendor": "greenhouse", "slug": ""})
    with pytest.raises(ValueError):
        repo.upsert({"slug": "acme"})


def test_a_board_is_retired_only_on_repeated_failure(db):
    repo.upsert({"vendor": "greenhouse", "slug": "globex", "source": "wayback"})

    # One 404 is a blip - a renamed board, a transient 502 - not a dead board.
    repo.record_verification("greenhouse", "globex", http_status=404)
    row = repo.get("greenhouse", "globex")
    assert row["state"] == "unverified"
    assert row["consecutive_failures"] == 1

    repo.record_verification("greenhouse", "globex", http_status=404)
    repo.record_verification("greenhouse", "globex", http_status=404)
    assert repo.get("greenhouse", "globex")["state"] == "gone"

    # And a board that answers again is live again, with the count cleared.
    repo.record_verification("greenhouse", "globex", http_status=200, job_count=7)
    row = repo.get("greenhouse", "globex")
    assert row["state"] == "live"
    assert row["consecutive_failures"] == 0
    assert row["job_count"] == 7


def test_boards_prefers_verified_and_drops_the_retired(db):
    repo.upsert({"vendor": "lever", "slug": "never-asked", "source": "commoncrawl"})
    repo.upsert({"vendor": "lever", "slug": "alive", "source": "commoncrawl"})
    repo.upsert({"vendor": "lever", "slug": "gone", "source": "wayback"})
    repo.record_verification("lever", "alive", http_status=200)
    for _ in range(repo.RETIRE_AFTER_FAILURES):
        repo.record_verification("lever", "gone", http_status=404)

    planned = [b["slug"] for b in repo.boards("lever")]
    assert planned[0] == "alive", "a verified board is worth a request first"
    assert "never-asked" in planned, "never having asked is not evidence of death"
    assert "gone" not in planned, "a retired board must not cost another request"


def test_importing_the_shipped_file_never_overwrites_what_a_request_established(db):
    """The whole reason the table exists rather than only the file."""
    repo.upsert({"vendor": "greenhouse", "slug": "acme", "source": "commoncrawl"})
    repo.record_verification("greenhouse", "acme", http_status=200, job_count=42)

    result = repo.sync_from_file(
        [
            # The same board as the file ships it: never verified.
            {"vendor": "greenhouse", "slug": "acme", "name": "Acme", "last_verified": None},
            {"vendor": "ashby", "slug": "brand-new", "source": "hackernews"},
        ]
    )

    assert result == {"added": 1, "kept": 1}
    survived = repo.get("greenhouse", "acme")
    assert survived["state"] == "live"
    assert survived["job_count"] == 42
    assert survived["last_verified"] is not None
    assert repo.get("ashby", "brand-new")["state"] == "unverified"


def test_the_import_reads_the_shape_discovery_actually_produces(db):
    """The row shape differs by caller, and reading one of them reports success.

    ``discovery.load_board_registry`` normalises the vendor onto ``ats_vendor``
    - the column name a company row uses - while the importer and the raw file
    call it ``vendor``. An import that reads only ``vendor`` skips every row of
    the other shape and logs "0 imported", which looks exactly like a registry
    that was already up to date.
    """
    from dreamjob.pipeline.discovery import load_board_registry

    shipped = load_board_registry()
    assert shipped, "the shipped registry should not be empty"
    assert "ats_vendor" in shipped[0], "this test guards the shape it was written for"

    result = repo.sync_from_file(shipped)
    assert result["added"] == len(shipped)
    assert repo.count() == len(shipped)

    # Both spellings name the same board, so importing the other shape adds none.
    again = repo.sync_from_file(
        [{"vendor": r["ats_vendor"], "slug": r["slug"]} for r in shipped[:50]]
    )
    assert again == {"added": 0, "kept": 50}


def test_a_malformed_row_does_not_fail_the_import(db):
    result = repo.sync_from_file(
        [{"vendor": "lever", "slug": "good"}, {"vendor": "", "slug": ""}, {"nonsense": 1}]
    )
    assert result["added"] == 1
    assert repo.get("lever", "good") is not None


# ---------------------------------------------------------------------------
# The named-employer seed (FR-181)
# ---------------------------------------------------------------------------


def test_the_employer_seed_names_no_board_it_has_not_verified():
    """A guessed slug is a 404 that gets stored, re-probed and counted (§6.7).

    The seed is a list of employers, not of boards: every slug in the registry
    has to come from that employer's own careers page, confirmed by a request.
    """
    payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    employers = payload["employers"]
    assert len(employers) >= 50

    for row in employers:
        assert row.get("name"), row
        assert row.get("domain"), row
        assert "ats_slug" not in row, f"{row['name']}: the seed must not guess a board"
        assert "ats_vendor" not in row, f"{row['name']}: the seed must not guess a vendor"

    domains = [r["domain"] for r in employers]
    assert len(domains) == len(set(domains)), "a duplicate domain resolves the same board twice"

    # The gap this closes is the large IT employers the URL indexes never name.
    names = {r["name"] for r in employers}
    assert {"Accenture", "Capgemini", "Cegeka", "IBM"} <= names
