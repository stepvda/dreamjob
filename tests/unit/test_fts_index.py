"""What one vacancy costs to index, and what the index owes the table (FR-345, NFR-103).

These tests exist because of one measurement.  A collection campaign of 2,685
plan items advanced six items in eleven minutes and wrote no vacancy at all in
a thirty-second sample.  Nothing had crashed and nothing was starved: the
slow-statement log named the statement, every two seconds, with the evidence
that it really was the statement's own cost::

    slow statement ms=1950.3 cpu_ms=1893.0 db_steps=420000 params=1
        sql=DELETE FROM vacancy_fts WHERE vacancy_id = ?

Both FTS tables were declared standalone with their key column UNINDEXED, so
the DELETE that ``_index_company`` and ``_index_vacancy`` issued before every
re-index had no index to seek on and SQLite scanned the whole table.  Writing
one row cost a pass over every row already written; filling the knowledge base
was therefore quadratic in the knowledge base, which is why a campaign got
slower the longer it ran.

Migration 133 fixes both, differently, and section 1 of this file is about the
property they now share rather than about either mechanism: **indexing one row
costs the same whatever the knowledge base already holds.**  A timing
threshold cannot pin that - it measures the machine, and this incident is a
whole file of evidence that wall clock cannot tell an expensive statement from
a loaded box.  What is measured instead is how many instructions SQLite's
virtual machine executes, through the same progress handler
``observability/db_logging.py`` already uses for ``db_steps``.  That count is a
property of the query plan and the data: the same number on a slow machine and
a fast one, and here the same number at two corpus sizes.

The rest of the file is the correctness the change buys and the correctness it
puts at risk.  ``vacancy_fts`` is now an external-content index: it keeps no
text of its own and addresses rows by ``vacancy.rowid``.  That is what removed
133 MB of duplicated corpus from a 617 MB file, and it is also a new way to be
wrong.  An entry the index fails to remove does not go dead - SQLite hands the
freed rowid to the next INSERT, so a deleted posting's words come back naming
whichever vacancy was written next.  Sections 3 and 4 pin that, and section 4
keeps its own control, because the FTS5 command that detects drift is not the
one the documentation makes obvious.

The last two tests are about the migration rather than the design it leaves
behind.  Every other test here starts from an empty database, where 133 has
almost nothing to do; the two statements that refill ``vacancy_fts`` and put
each ``company_fts`` entry back on its own rowid only do anything to a database
that already holds rows.  That is the database this migration was written for
and the only run of it that matters, so those two start at migration 132 with
rows indexed the old way and let the real migrator carry them across.

Everything here is offline and runs against a throw-away database.
"""

from __future__ import annotations

import random
import sqlite3
import statistics
from collections.abc import Callable, Iterator

import pytest
from dreamjob.config import get_settings
from dreamjob.db import migrator
from dreamjob.db.connection import (
    execute,
    get_connection,
    insert_row,
    query_all,
    query_one,
    write_tx,
)
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import knowledge as kb
from dreamjob.observability import db_logging

SMALL_CORPUS = 200
LARGE_CORPUS = 1_600
GROWTH_ALLOWANCE = 2.0
MINIMUM_STEPS = 40
#: The migration under test.  Read from the filename rather than hard-coded in
#: two places, so a renumbering cannot leave this file quietly testing nothing.
FTS_MIGRATION = "133"

STANDALONE_FTS = """
CREATE TABLE vacancy (
    id TEXT PRIMARY KEY, title TEXT, description TEXT, company_name_raw TEXT);
CREATE VIRTUAL TABLE vacancy_fts USING fts5(
    vacancy_id UNINDEXED, title, description, company_name_raw);
"""

DESCRIPTION = "Build streaming pipelines in Python and dbt for a retail data platform. " * 12


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    migrate()
    yield
    get_settings.cache_clear()


class StepMeter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.steps = 0

    def __enter__(self) -> StepMeter:
        self.steps = 0

        def tick() -> int:
            self.steps += 1
            return 0

        self._conn.set_progress_handler(tick, 1)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._conn.set_progress_handler(None, 0)
        db_logging.attach(self._conn)


def _seed_vacancies(n: int, tag: str) -> None:
    for i in range(n):
        kb.insert_vacancy({
            "title": f"Data Engineer {i}", "description": DESCRIPTION,
            "company_name_raw": f"Acme {i % 50} NV", "source_url": f"https://fts.test/{tag}/{i}",
            "dedup_key": f"{tag}-{i}", "source_adapter": "test.fts", "country": "BE"})


def _seed_companies(n: int, tag: str) -> None:
    for i in range(n):
        kb.insert_company({
            "name": f"Acme {tag} {i} NV", "normalised_name": f"acme {tag} {i}",
            "business_summary": "Logistics software for retail warehouses.", "country": "BE"})


def _vacancy_write_steps(tag: str, repeats: int = 5) -> float:
    conn = get_connection()
    with StepMeter(conn) as meter:
        for k in range(repeats):
            kb.insert_vacancy({
                "title": "Senior Streaming Architect", "description": DESCRIPTION,
                "company_name_raw": "Zeta NV", "source_url": f"https://fts.test/probe/{tag}/{k}",
                "dedup_key": f"probe-{tag}-{k}", "source_adapter": "test.fts", "country": "BE"})
    return meter.steps / repeats


def _vacancy_update_steps(vacancy_id: str, repeats: int = 5) -> float:
    conn = get_connection()
    with StepMeter(conn) as meter:
        for k in range(repeats):
            kb.update_vacancy(vacancy_id, {"title": f"Retitled {k}"})
    return meter.steps / repeats


def _company_write_steps(tag: str, repeats: int = 5) -> float:
    conn = get_connection()
    with StepMeter(conn) as meter:
        for k in range(repeats):
            kb.insert_company({
                "name": f"Probe {tag} {k} BV", "normalised_name": f"probe {tag} {k}",
                "business_summary": "Logistics software for retail warehouses.",
                "country": "BE"})
    return meter.steps / repeats


def test_the_step_meter_sees_the_scan_it_exists_to_catch() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(STANDALONE_FTS)

    def measure(corpus: int) -> float:
        conn.execute("DELETE FROM vacancy")
        conn.execute("DELETE FROM vacancy_fts")
        for i in range(corpus):
            row = (f"v{i}", f"Data Engineer {i}", DESCRIPTION, f"Acme {i % 50} NV")
            conn.execute("INSERT INTO vacancy VALUES (?, ?, ?, ?)", row)
            conn.execute(
                "INSERT INTO vacancy_fts (vacancy_id, title, description, "
                "company_name_raw) VALUES (?, ?, ?, ?)", row)
        conn.commit()
        meter = StepMeter(conn)
        with meter:
            for k in range(5):
                conn.execute("DELETE FROM vacancy_fts WHERE vacancy_id = ?", (f"probe{k}",))
                conn.execute(
                    "INSERT INTO vacancy_fts (vacancy_id, title, description, "
                    "company_name_raw) VALUES (?, ?, ?, ?)",
                    (f"probe{k}", "Senior Streaming Architect", DESCRIPTION, "Zeta NV"))
        return meter.steps / 5

    small, large = measure(SMALL_CORPUS), measure(LARGE_CORPUS)
    conn.close()
    print(f"\ncontrol: standalone {small:.0f} -> {large:.0f} steps")
    assert small > MINIMUM_STEPS
    assert large / small > (LARGE_CORPUS / SMALL_CORPUS) / 2


def test_writing_a_vacancy_costs_the_same_whatever_the_corpus_holds(db: None) -> None:
    _seed_vacancies(SMALL_CORPUS, "small")
    small = _vacancy_write_steps("small")
    _seed_vacancies(LARGE_CORPUS - SMALL_CORPUS, "large")
    large = _vacancy_write_steps("large")
    print(f"\nvacancy insert: {small:.0f} -> {large:.0f} steps")
    assert small > MINIMUM_STEPS
    assert query_one("SELECT COUNT(*) AS n FROM vacancy")["n"] >= LARGE_CORPUS
    assert large / small < GROWTH_ALLOWANCE


def test_re_indexing_a_vacancy_costs_the_same_whatever_the_corpus_holds(db: None) -> None:
    _seed_vacancies(SMALL_CORPUS, "small")
    victim = query_one("SELECT id FROM vacancy LIMIT 1")["id"]
    small = _vacancy_update_steps(victim)
    _seed_vacancies(LARGE_CORPUS - SMALL_CORPUS, "large")
    large = _vacancy_update_steps(victim)
    print(f"\nvacancy update: {small:.0f} -> {large:.0f} steps")
    assert small > MINIMUM_STEPS
    assert large / small < GROWTH_ALLOWANCE


def test_writing_a_company_costs_the_same_whatever_the_corpus_holds(db: None) -> None:
    _seed_companies(SMALL_CORPUS, "small")
    small = _company_write_steps("small")
    _seed_companies(LARGE_CORPUS - SMALL_CORPUS, "large")
    large = _company_write_steps("large")
    print(f"\ncompany insert: {small:.0f} -> {large:.0f} steps")
    assert small > MINIMUM_STEPS
    assert large / small < GROWTH_ALLOWANCE


def _median_steps(work: Callable[[int], None], repeats: int = 5) -> float:
    """Median virtual-machine steps over ``repeats`` runs of ``work``.

    The median, not one measurement and not the mean.  FTS5 merges its
    segments every so often, and the update that pays for the merge costs
    three to four times the others (229 steps against 814, measured).  A merge
    is real work the index owes, not a regression in the statement, and one
    landing on the wrong side of a comparison would fail a correct build.
    """
    conn = get_connection()
    runs = []
    for k in range(repeats):
        with StepMeter(conn) as meter:
            work(k)
        runs.append(meter.steps)
    return statistics.median(runs)


def test_a_vacancy_update_that_changes_nothing_searchable_does_no_index_work(db: None) -> None:
    """Two guards, and they fail differently (FR-345, NFR-103).

    ``AFTER UPDATE OF title, description, company_name_raw`` is what makes a
    re-collected posting that only moved ``confidence`` and ``collected_at``
    cost nothing: the trigger is never considered.  The ``WHEN`` clause is what
    makes the commoner case free - a collector that writes the whole row back
    every pass, description included, with the same text in it.

    Only the second needs a measurement to see, and it needs the right one.
    Deleting the ``WHEN`` clause leaves the column list doing its job, so the
    ``confidence`` case stays cheap and a comparison against it proves nothing;
    what changes is that re-writing the same description costs *exactly* what
    changing it costs - 223 steps against 223, measured - because the trigger
    re-tokenises text FTS5 already holds.  ``unchanged < loud`` is therefore
    the assertion that fails when the guard goes, and it fails against the
    pre-133 design too, where every update re-indexed the whole row (536
    against 536).
    """
    _seed_vacancies(20, "quiet")
    victim = query_one("SELECT id FROM vacancy LIMIT 1")["id"]
    kb.update_vacancy(victim, {"description": DESCRIPTION})
    quiet = _median_steps(lambda k: kb.update_vacancy(
        victim, {"confidence": 0.9 + k / 1000, "collected_at": "2026-09-10T00:00:00+00:00"}))
    unchanged = _median_steps(lambda k: kb.update_vacancy(victim, {"description": DESCRIPTION}))
    loud = _median_steps(lambda k: kb.update_vacancy(
        victim, {"description": DESCRIPTION + f" kayak{k}"}))
    print(f"\nquiet update {quiet:.0f} steps, same-text update {unchanged:.0f}, "
          f"changed-text update {loud:.0f}")
    assert quiet > MINIMUM_STEPS
    # The column list: an update that never mentions an indexed column costs a
    # fraction of one that rewrites the index, not the same thing minus noise.
    assert quiet * 2 < loud, (quiet, loud)
    # The WHEN clause: writing the same text back is not an index write.
    assert unchanged < loud, (unchanged, loud)


def test_the_index_does_not_keep_its_own_copy_of_every_vacancy(db: None) -> None:
    _seed_vacancies(20, "shape")
    names = [r["name"] for r in query_all(
        "SELECT name FROM sqlite_master WHERE name LIKE 'vacancy_fts%' ORDER BY name")]
    assert "vacancy_fts_content" not in names, names
    declaration = query_one("SELECT sql FROM sqlite_master WHERE name = 'vacancy_fts'")["sql"]
    assert "content='vacancy'" in declaration.replace('"', "'"), declaration


RANKED_CORPUS = [
    ("a", "Senior Data Engineer", "Build streaming pipelines in Python and dbt.", "Acme NV"),
    ("b", "Data Engineer", "Maintain batch pipelines. Some streaming.", "Globex"),
    ("c", "Streaming Platform Engineer",
     "Streaming, streaming and more streaming pipelines.", "Initech"),
    ("d", "Backend Engineer", "Python services, no pipelines here.", "Umbrella"),
    ("e", "Analytics Engineer", "dbt models over a warehouse.", "Hooli"),
]


def _seed_ranked() -> dict[str, str]:
    return {key: kb.insert_vacancy({
        "title": title, "description": desc, "company_name_raw": company, "dedup_key": key,
        "source_url": f"https://fts.test/{key}", "source_adapter": "test.fts", "country": "BE"})
        for key, title, desc, company in RANKED_CORPUS}


def _keys(rows: list[dict]) -> list[str]:
    return [r["dedup_key"] for r in rows]


def test_search_returns_the_same_rows_in_the_same_order_as_before(db: None) -> None:
    ids = _seed_ranked()
    hits = kb.search_vacancies("streaming")
    assert _keys(hits) == ["c", "b", "a"], _keys(hits)
    assert kb.count_vacancies("streaming") == 3
    assert [h["id"] for h in hits] == [ids["c"], ids["b"], ids["a"]]
    assert _keys(hits) != [k for k, *_ in RANKED_CORPUS if k in _keys(hits)]
    assert _keys(hits) != sorted(_keys(hits))
    assert _keys(kb.search_vacancies("stream")) == ["c", "b", "a"]
    assert _keys(kb.search_vacancies("dbt")) == ["e", "a"]
    assert kb.search_vacancies("underwater basket weaving") == []
    assert kb.count_vacancies("underwater basket weaving") == 0
    assert _keys(kb.search_vacancies("streaming", country="BE")) == ["c", "b", "a"]
    assert kb.search_vacancies("streaming", country="NL") == []
    assert _keys(kb.search_vacancies("streaming", limit=2)) == ["c", "b"]
    assert _keys(kb.search_vacancies("streaming", limit=2, offset=1)) == ["b", "a"]


def test_every_hit_is_a_vacancy_that_actually_contains_the_word(db: None) -> None:
    _seed_ranked()
    for term in ("streaming", "pipelines", "dbt", "python", "warehouse", "kitchen"):
        expected = {key for key, title, desc, company in RANKED_CORPUS
                    if term in f"{title} {desc} {company}".lower()}
        assert set(_keys(kb.search_vacancies(term))) == expected, term


def _delete_vacancy(vacancy_id: str) -> None:
    execute("DELETE FROM vacancy WHERE id = ?", (vacancy_id,))


def test_deleting_a_vacancy_removes_it_from_the_index(db: None) -> None:
    ids = _seed_ranked()
    assert _keys(kb.search_vacancies("warehouse")) == ["e"]
    _delete_vacancy(ids["e"])
    assert kb.search_vacancies("warehouse") == []
    assert kb.count_vacancies("warehouse") == 0
    assert query_one(
        "SELECT COUNT(*) AS n FROM vacancy_fts WHERE vacancy_fts MATCH 'warehouse'")["n"] == 0


def test_a_reused_rowid_does_not_inherit_the_deleted_vacancys_words(db: None) -> None:
    ids = _seed_ranked()
    freed = query_one("SELECT rowid AS r FROM vacancy WHERE id = ?", (ids["e"],))["r"]
    _delete_vacancy(ids["e"])
    successor = kb.insert_vacancy({
        "title": "Chef de partie", "description": "Kitchen work, no software.",
        "company_name_raw": "Bistro", "dedup_key": "f", "source_url": "https://fts.test/f",
        "source_adapter": "test.fts"})
    reused = query_one("SELECT rowid AS r FROM vacancy WHERE id = ?", (successor,))["r"]
    print(f"\nfreed rowid {freed}, successor rowid {reused}")
    assert reused == freed, (reused, freed)
    assert kb.search_vacancies("warehouse") == [], _keys(kb.search_vacancies("warehouse"))
    assert _keys(kb.search_vacancies("kitchen")) == ["f"]


def _integrity_check(argument: int) -> None:
    execute("INSERT INTO vacancy_fts (vacancy_fts, rank) VALUES ('integrity-check', ?)",
            (argument,))


def test_the_index_still_matches_the_table_after_a_mixed_workload(db: None) -> None:
    _seed_ranked()
    _integrity_check(1)
    scratch = sqlite3.connect(":memory:")
    scratch.executescript(
        query_one("SELECT sql FROM sqlite_master WHERE name = 'vacancy'")["sql"] + ";"
        + query_one("SELECT sql FROM sqlite_master WHERE name = 'vacancy_fts'")["sql"] + ";")
    scratch.execute(
        "INSERT INTO vacancy (id, title, description, company_name_raw, collected_at) "
        "VALUES ('1', 'Engineer', 'streaming pipelines', 'Acme', '2026-09-10T00:00:00+00:00')")
    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        scratch.execute(
            "INSERT INTO vacancy_fts (vacancy_fts, rank) VALUES ('integrity-check', 1)")
    scratch.close()
    victim = query_one("SELECT id FROM vacancy WHERE dedup_key = 'd'")["id"]
    kb.update_vacancy(victim, {"title": "Staff Backend Engineer",
                               "description": "Go services now, still no pipelines."})
    _delete_vacancy(query_one("SELECT id FROM vacancy WHERE dedup_key = 'b'")["id"])
    kb.insert_vacancy({
        "title": "Site Reliability Engineer", "description": "Terraform and streaming.",
        "company_name_raw": "Vandelay", "dedup_key": "g", "source_url": "https://fts.test/g",
        "source_adapter": "test.fts"})
    _integrity_check(1)


def test_a_long_description_survives_being_re_indexed(db: None) -> None:
    long_description = "streaming " * 2_600 + " needleword"
    assert len(long_description) > 20_000
    vacancy_id = kb.insert_vacancy({
        "title": "A very long posting", "description": long_description,
        "company_name_raw": "Acme NV", "dedup_key": "long",
        "source_url": "https://fts.test/long", "source_adapter": "test.fts"})
    assert _keys(kb.search_vacancies("needleword")) == ["long"]
    kb.update_vacancy(vacancy_id, {"title": "A very long posting, retitled"})
    assert _keys(kb.search_vacancies("needleword")) == ["long"]
    _integrity_check(1)


def test_reindex_all_rebuilds_the_index_rather_than_emptying_it(db: None) -> None:
    _seed_ranked()
    kb.insert_company({"name": "Initech NV", "normalised_name": "initech",
                       "business_summary": "streaming platforms for retailers"})
    before = _keys(kb.search_vacancies("streaming"))
    assert before == ["c", "b", "a"]
    counts = kb.reindex_all()
    assert counts["vacancy"] == len(RANKED_CORPUS), counts
    assert counts["company"] == 1, counts
    assert _keys(kb.search_vacancies("streaming")) == before
    assert [c["name"] for c in kb.search_companies("retailers")] == ["Initech NV"]
    _integrity_check(1)


def test_a_company_reindex_replaces_its_entry_rather_than_adding_one(db: None) -> None:
    company_id = kb.insert_company({
        "name": "Havenstad Analytics", "normalised_name": "havenstad analytics",
        "business_summary": "warehouse robotics", "country": "BE"})
    kb.insert_company({"name": "Other BV", "normalised_name": "other",
                       "business_summary": "warehouse robotics", "country": "BE"})
    kb.update_company(company_id, {"business_summary": "fleet orchestration software"})
    assert [c["id"] for c in kb.search_companies("orchestration")] == [company_id]
    assert kb.count_companies("robotics") == 1
    assert query_one(
        "SELECT COUNT(*) AS n FROM company_fts WHERE company_id = ?", (company_id,))["n"] == 1


# ---------------------------------------------------------------------------
# The migration itself, against a database that already holds rows.
#
# Every test above starts from an empty database, and on an empty database
# migration 133 has almost nothing to do: 'rebuild' rebuilds nothing and the
# company_fts refill copies no rows.  Both of those statements exist only for a
# database that is already full - which is the one this migration was written
# for, the 617 MB file with 35,159 vacancies and 4,156 companies in it, and the
# one it gets exactly one attempt at.  Deleting either statement leaves every
# other test in this file green, so neither is pinned by them.
#
# These two start from the schema as it stood at migration 132, with rows
# indexed the way knowledge.py indexed them then, and run the real migration
# through the real migrator.
# ---------------------------------------------------------------------------

OLD_VACANCY_INDEX = (
    "INSERT INTO vacancy_fts (vacancy_id, title, description, company_name_raw) "
    "VALUES (?, ?, ?, ?)"
)
OLD_COMPANY_INDEX = (
    "INSERT INTO company_fts (company_id, name, normalised_name, business_summary, "
    "products_services) VALUES (?, ?, ?, ?, '')"
)
#: One distinctive word per company, so that a company which loses its index
#: entry is named in the failure rather than counted in it.
COMPANY_WORDS = ["hydrofoil", "escalator", "trebuchet", "zeppelin", "funicular", "aqueduct"]
NEWCOMER_WORD = "kiteboard"
COLLECTED_AT = "2026-09-10T00:00:00+00:00"


@pytest.fixture()
def pre_133_db(tmp_path, monkeypatch) -> Iterator[None]:
    """A database migrated to 132 and no further, with 133 still pending."""
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    every = migrator.discover()
    earlier = [m for m in every if m[0] < FTS_MIGRATION]
    upto = [m for m in every if m[0] <= FTS_MIGRATION]
    assert len(upto) == len(earlier) + 1, [m[0] for m in every]
    monkeypatch.setattr(migrator, "discover", lambda: earlier)
    migrate()
    # 133 is the only pending migration inside the test.  Anything added after
    # it is not what this fixture is about, and letting it run would turn every
    # future migration into a change to these assertions - which is exactly what
    # the "133 is the last file" guard that used to sit here did.
    monkeypatch.setattr(migrator, "discover", lambda: upto)
    yield
    get_settings.cache_clear()


def _seed_the_pre_133_way() -> list[str]:
    """Write rows and index them the way knowledge.py did before migration 133.

    Returns the company ids in creation order.  Two of them are then
    re-collected the old way - DELETE the entry, INSERT a fresh one - which is
    what put a company's index entry under a rowid belonging to nobody.  FTS5
    hands an unkeyed INSERT ``max(rowid) + 1``, so a re-indexed company drifts
    off its own rowid and never comes back.  On the live database that had
    happened to 20 entries in every 30.
    """
    for key, title, desc, company in RANKED_CORPUS:
        vacancy_id = insert_row("vacancy", {
            "title": title, "description": desc, "company_name_raw": company,
            "dedup_key": key, "source_url": f"https://fts.test/pre/{key}",
            "source_adapter": "test.fts", "country": "BE", "collected_at": COLLECTED_AT})
        with write_tx() as conn:
            conn.execute(OLD_VACANCY_INDEX, (vacancy_id, title, desc, company))
    company_ids = []
    for i, word in enumerate(COMPANY_WORDS):
        company_id = insert_row("company", {
            "name": f"Havenstad {i} NV", "normalised_name": f"havenstad {i}",
            "business_summary": f"{word} maintenance", "country": "BE",
            "collected_at": COLLECTED_AT})
        with write_tx() as conn:
            conn.execute(OLD_COMPANY_INDEX,
                         (company_id, f"Havenstad {i} NV", f"havenstad {i}", f"{word} maintenance"))
        company_ids.append(company_id)
    for i in (0, 1):
        with write_tx() as conn:
            conn.execute("DELETE FROM company_fts WHERE company_id = ?", (company_ids[i],))
            conn.execute(OLD_COMPANY_INDEX, (
                company_ids[i], f"Havenstad {i} NV", f"havenstad {i}",
                f"{COMPANY_WORDS[i]} maintenance"))
    return company_ids


def _drifted_entries() -> int:
    return query_one(
        "SELECT COUNT(*) AS n FROM company_fts f JOIN company c ON c.id = f.company_id "
        "WHERE f.rowid <> c.rowid")["n"]


def test_the_migration_carries_an_already_indexed_database_across(pre_133_db: None) -> None:
    """133 against rows that are already there, which is the only run that counts.

    Two statements are on trial.  ``'rebuild'`` is the one that refills
    ``vacancy_fts`` after the old standalone table is dropped: without it the
    new index is created empty and every search in the product answers nothing,
    silently, because an empty index is not a corrupt one.

    The ``company_fts`` refill is the one that puts each entry back under the
    rowid of the company it describes.  ``_index_company`` now deletes by
    rowid, so an entry sitting on somebody else's rowid is not merely stale -
    the next company written to that rowid deletes the wrong company out of the
    index.  That is what the seeding here arranges: the sixth company's entry
    is under rowid 7 because it was re-collected once, and the seventh company
    to be written takes rowid 7.
    """
    company_ids = _seed_the_pre_133_way()
    assert _drifted_entries() == 2, "the fixture no longer reproduces the drift it is about"

    assert migrate() == [FTS_MIGRATION]

    # The vacancy index came across, rows and ranking both.
    assert _keys(kb.search_vacancies("streaming")) == ["c", "b", "a"]
    assert kb.count_vacancies("streaming") == 3
    assert query_one("SELECT COUNT(*) AS n FROM vacancy_fts")["n"] == len(RANKED_CORPUS)
    _integrity_check(1)

    # Every company entry is back on its own rowid, exactly once.
    assert _drifted_entries() == 0
    assert query_one("SELECT COUNT(*) AS n FROM company_fts")["n"] == len(COMPANY_WORDS)
    for word in COMPANY_WORDS:
        assert kb.count_companies(word) == 1, word

    # The consequence, not just the arithmetic: writing the next company must
    # not evict the one whose entry used to sit on that rowid.
    newcomer = kb.insert_company({
        "name": "Nieuwe Haven BV", "normalised_name": "nieuwe haven",
        "business_summary": f"{NEWCOMER_WORD} rentals", "country": "BE"})
    assert [c["id"] for c in kb.search_companies(NEWCOMER_WORD)] == [newcomer]
    for word, company_id in zip(COMPANY_WORDS, company_ids, strict=True):
        assert [c["id"] for c in kb.search_companies(word)] == [company_id], word
    assert _drifted_entries() == 0


def test_the_migration_survives_being_run_a_second_time(pre_133_db: None) -> None:
    """``executescript`` commits before it starts, so a half-run 133 is not recorded.

    ``migrator.migrate()`` wraps the file in ``write_tx`` and hands it to
    ``conn.executescript``, which commits the open transaction first - so the
    ``schema_migration`` row that records this migration is not written in the
    same transaction as its effects.  A failure part way through leaves the
    finished half committed and unrecorded, and the next backend start runs the
    file again from the top.  The file says it is written to survive that; this
    is the test that it is.
    """
    company_ids = _seed_the_pre_133_way()
    assert migrate() == [FTS_MIGRATION]
    path = next(p for version, _name, p in migrator.discover() if version == FTS_MIGRATION)

    with write_tx() as conn:
        conn.executescript(path.read_text(encoding="utf-8"))

    assert _keys(kb.search_vacancies("streaming")) == ["c", "b", "a"]
    assert query_one("SELECT COUNT(*) AS n FROM vacancy_fts")["n"] == len(RANKED_CORPUS)
    assert query_one("SELECT COUNT(*) AS n FROM company_fts")["n"] == len(company_ids)
    assert _drifted_entries() == 0
    _integrity_check(1)
    assert migrate() == []


#: The query ``search_vacancies`` issued before migration 133, kept verbatim so
#: the comparison below is against the shipped statement rather than a
#: paraphrase of it.
PRE_133_SEARCH = (
    "SELECT v.id, bm25(vacancy_fts) AS rank FROM vacancy_fts f "
    "JOIN vacancy v ON v.id = f.vacancy_id WHERE vacancy_fts MATCH ? ORDER BY rank"
)
#: Enough shared vocabulary that most terms hit a few hundred rows and plenty
#: of them score identically - a corpus of distinct documents would rank in one
#: obvious order and prove nothing about ties.
CORPUS_WORDS = (
    "python", "sql", "warehouse", "engineer", "senior", "data", "platform", "kubernetes",
    "analyst", "remote", "amsterdam", "berlin", "logistics", "machine", "learning",
    "pipeline", "etl", "cloud", "security", "team",
)
CORPUS_TERMS = ("python", "data engineer", "senior data", "warehouse", "kubernetes cloud",
                "machine learning pipeline", "amsterdam", "etl", "team", "eng")


def _seed_a_ranked_corpus(n: int = 200) -> None:
    """Write `n` vacancies and index them the pre-133 way, re-collecting some.

    Every third row is re-indexed the way ``_index_vacancy`` re-indexed it -
    DELETE the entry, INSERT a fresh one - because that is what moved an entry
    to ``max(rowid) + 1`` and put the old index's rows in re-collection order
    rather than table order.  The corpus exists to make that difference show
    up; a corpus written once would hide it.

    Descriptions stay well under 20,000 characters on purpose.  Past that the
    old index truncated and the new one does not, which is a deliberate change
    to what is searchable (see migration 133) and would turn an equivalence
    test into an assertion that the change never happened.
    """
    rng = random.Random(20260910)
    written: list[tuple[str, str, str, str]] = []
    for i in range(n):
        title = " ".join(rng.sample(CORPUS_WORDS, 4))
        description = " ".join(rng.choice(CORPUS_WORDS) for _ in range(rng.randint(20, 120)))
        company = f"{rng.choice(CORPUS_WORDS).title()} {i % 40} NV"
        vacancy_id = insert_row("vacancy", {
            "title": title, "description": description, "company_name_raw": company,
            "dedup_key": f"corpus-{i}", "source_url": f"https://fts.test/corpus/{i}",
            "source_adapter": "test.fts", "country": "BE" if i % 2 else "NL",
            "collected_at": COLLECTED_AT})
        with write_tx() as conn:
            conn.execute(OLD_VACANCY_INDEX, (vacancy_id, title, description, company))
        written.append((vacancy_id, title, description, company))
    # The re-collection is a later pass, not a second write inside the first
    # one: an entry re-indexed straight after its own INSERT still lands in
    # table order, and it is coming back *out* of order that the old index did.
    for vacancy_id, title, description, company in written[::3]:
        with write_tx() as conn:
            conn.execute("DELETE FROM vacancy_fts WHERE vacancy_id = ?", (vacancy_id,))
            conn.execute(OLD_VACANCY_INDEX, (vacancy_id, title, description, company))


def test_the_new_query_shape_ranks_a_real_corpus_exactly_as_the_old_one_did(
        pre_133_db: None) -> None:
    """The same rows, the same scores, and order that moves only inside a tie (FR-345).

    Migration 133 changes three things a search result could notice at once:
    the indexed table loses its ``vacancy_id`` column, so bm25() now averages
    three columns where it averaged four; the join goes from ``v.id
    = f.vacancy_id`` to ``v.rowid = f.rowid``; and the entries are addressed by
    ``vacancy.rowid`` instead of by arrival order in the index.  The other tests
    in this file pin the ranking of five hand-written rows, which is small
    enough that all three could be wrong together and still come out in the
    order somebody expected.

    This one runs both statements over the same two hundred rows and diffs
    them.  The scores come out bit-identical, which is the answer to the bm25
    question: FTS5 records no tokens for an UNINDEXED column, so dropping it
    changes neither the document lengths nor the average the formula divides
    by.  What does move is order within an exact tie, and it moves because the
    old index had stopped being in table order - every re-collected vacancy was
    sitting at the end of it.  The new index is in table order and stays there,
    so the tie-break is now stable across a re-collection instead of depending
    on when a posting was last written.  That is a difference worth stating
    rather than papering over, so the assertion allows it and only it: any
    position where the two disagree must carry the same score.
    """
    _seed_a_ranked_corpus()
    before = {term: [(r["id"], r["rank"]) for r in query_all(
        PRE_133_SEARCH, (kb.fts_query(term),))] for term in CORPUS_TERMS}
    assert all(len(rows) > 20 for rows in before.values()), \
        {t: len(r) for t, r in before.items()}

    assert migrate() == [FTS_MIGRATION]

    moved = 0
    for term in CORPUS_TERMS:
        old = before[term]
        new = [(r["id"], r["rank"]) for r in kb.search_vacancies(term, limit=10_000)]
        assert {i for i, _ in new} == {i for i, _ in old}, term
        assert [r for _, r in new] == [r for _, r in old], f"bm25 moved for {term!r}"
        assert kb.count_vacancies(term) == len(old), term
        for (old_id, old_rank), (new_id, new_rank) in zip(old, new, strict=True):
            if old_id != new_id:
                moved += 1
                assert old_rank == new_rank, (term, old_id, new_id, old_rank, new_rank)
        # Paging over the whole result set still visits every row exactly once.
        paged: list[str] = []
        for offset in range(0, len(new), 7):
            paged += [r["id"] for r in kb.search_vacancies(term, limit=7, offset=offset)]
        assert sorted(paged) == sorted(i for i, _ in new), term
        # The filters narrow the same set they narrowed before.
        for country in ("BE", "NL"):
            expected = {r["id"] for r in query_all(
                "SELECT v.id FROM vacancy v WHERE v.country = ? AND v.id IN "
                "(" + ",".join("?" * len(old)) + ")", (country, *[i for i, _ in old]))}
            got = {r["id"] for r in kb.search_vacancies(term, country=country, limit=10_000)}
            assert got == expected, (term, country)
            assert kb.count_vacancies(term, country=country) == len(expected), (term, country)
    assert moved > 0, (
        "no result moved at all, so the tie-tolerance above proved nothing - "
        "the corpus has stopped producing the equal scores it is built for")


# ---------------------------------------------------------------------------
# 6. What the rowid key costs, and the only thing that pays it back
# ---------------------------------------------------------------------------
# Both indexes are now addressed by their base table's rowid: vacancy_fts
# because that is what content_rowid means, company_fts because _index_company
# seeks on rowid rather than scanning for company_id.  Nothing in the product
# breaks that correspondence.  A restore from ``.dump`` does, because the dump
# carries the FTS shadow tables verbatim while the base tables are re-inserted
# and take fresh rowids - measured against a real dump/restore of a migrated
# database, 15 of 17 company rowids and 15 of 17 vacancy rowids moved.  These
# two tests are the control and the repair: the first is here so nobody deletes
# the second believing the hazard is theoretical.


def _restore_renumbering(table: str) -> None:
    """Renumber ``table`` the way a restore from ``.dump`` renumbers it.

    A dump replays the base rows as plain INSERTs, so the restored table gets
    rowids 1..N in the order it was dumped and every gap left by a delete
    closes up.  Nothing tells either index, which is the whole point: this is
    done with an UPDATE of ``rowid`` alone, and ``vacancy_fts``'s trigger names
    ``title, description, company_name_raw``, so it does not fire here either.
    """
    old = [r["rowid"] for r in query_all(f"SELECT rowid FROM {table} ORDER BY rowid")]
    with write_tx() as conn:
        conn.execute(f"UPDATE {table} SET rowid = rowid + 1000000")
        for new, was in enumerate(old, start=1):
            conn.execute(f"UPDATE {table} SET rowid = ? WHERE rowid = ?", (new, was + 1000000))


def _seed_named_companies() -> list[str]:
    return [
        kb.insert_company({
            "name": f"{word.title()} Werken NV", "normalised_name": f"{word} werken",
            "business_summary": f"{word} maintenance for ports", "country": "BE"})
        for word in COMPANY_WORDS
    ]


def test_a_restore_that_renumbers_rowids_leaves_both_indexes_answering_wrongly(
    db: None,
) -> None:
    """The control.  Renumbering is silent, and what follows it is not stale, it is wrong."""
    company_ids = _seed_named_companies()
    _seed_ranked()
    _delete_vacancy(query_one("SELECT id FROM vacancy WHERE dedup_key = 'b'")["id"])
    execute("DELETE FROM company WHERE id = ?", (company_ids[1],))

    _restore_renumbering("company")
    _restore_renumbering("vacancy")

    # The vacancy index now answers for its neighbour: same number of hits,
    # different postings.  Nothing raised, and no search reported an error.
    assert _keys(kb.search_vacancies("streaming")) != ["c", "a"]
    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        _integrity_check(1)

    # The company index still answers correctly - the search joins on
    # company_id, not rowid - so the drift is invisible until something writes.
    assert _drifted_entries() > 0
    for word, company_id in zip(COMPANY_WORDS[2:], company_ids[2:], strict=True):
        assert [c["id"] for c in kb.search_companies(word)] == [company_id], word

    # One write is all it takes, and it evicts somebody else.
    kb.update_company(company_ids[-1], {"business_summary": f"{COMPANY_WORDS[-1]} dredging"})
    evicted = [w for w, cid in zip(COMPANY_WORDS[2:], company_ids[2:], strict=True)
               if [c["id"] for c in kb.search_companies(w)] != [cid]]
    assert evicted, "the rowid-keyed DELETE no longer evicts a neighbour; delete the repair test too"


def test_reindex_all_is_the_repair_for_renumbered_rowids(db: None) -> None:
    """And ``'rebuild'`` on company_fts is not, however much it looks like it."""
    company_ids = _seed_named_companies()
    vacancy_ids = _seed_ranked()
    _delete_vacancy(vacancy_ids["b"])
    execute("DELETE FROM company WHERE id = ?", (company_ids[1],))
    survivors = dict(zip(COMPANY_WORDS[2:], company_ids[2:], strict=True))

    _restore_renumbering("company")
    _restore_renumbering("vacancy")
    drifted = _drifted_entries()
    assert drifted > 0

    # The obvious analogue of the vacancy repair.  FTS5 accepts it and it
    # repairs nothing: a standalone table rebuilds from its own shadow copy,
    # which is where the wrong rowids live.
    execute("INSERT INTO company_fts (company_fts) VALUES ('rebuild')")
    assert _drifted_entries() == drifted

    assert kb.reindex_all() == {"company": len(survivors) + 1, "vacancy": len(vacancy_ids) - 1}

    assert _drifted_entries() == 0
    _integrity_check(1)
    assert _keys(kb.search_vacancies("streaming")) == ["c", "a"]
    assert kb.count_vacancies("streaming") == 2
    for word, company_id in survivors.items():
        assert [c["id"] for c in kb.search_companies(word)] == [company_id], word
    assert kb.search_companies(COMPANY_WORDS[1]) == []
