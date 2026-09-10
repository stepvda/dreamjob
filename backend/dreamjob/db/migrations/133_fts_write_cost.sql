-- ===========================================================================
-- 133: the full-text index stops costing a scan of itself on every write
-- (FR-345, NFR-101, NFR-103, CR-408)
--
-- **The measurement.**  A collection campaign of 2,685 plan items advanced six
-- items in eleven minutes and wrote no vacancy at all in a thirty-second
-- sample.  Nothing had crashed and nothing was starved.  The slow-statement
-- log named the statement, every two seconds:
--
--     slow statement ms=1950.3 db_steps=420000 params=1
--         sql=DELETE FROM vacancy_fts WHERE vacancy_id = ?
--
-- ``vacancy_fts`` was declared in 001_initial.sql as a standalone FTS5 table
-- whose key column is UNINDEXED::
--
--     CREATE VIRTUAL TABLE vacancy_fts USING fts5(
--         vacancy_id UNINDEXED, title, description, company_name_raw);
--
-- UNINDEXED means the value is stored in the content shadow table and put
-- through no index, so there is nothing for that WHERE clause to seek on.
-- EXPLAIN QUERY PLAN on this database said so plainly: ``SCAN vacancy_fts
-- VIRTUAL TABLE INDEX 0:``.  Every vacancy written re-read all 35,158 rows
-- already indexed - 160 MiB of shadow content - to find the one to remove.
-- The cost of writing the knowledge base was linear in the knowledge base, so
-- the cost of filling it was quadratic, which is why a campaign got slower the
-- longer it ran and why NFR-103's four-hour budget was never in reach.
--
-- It cost the disk too.  A standalone FTS5 table keeps every indexed value
-- verbatim in a ``_content`` shadow table, so the same bytes were stored
-- twice: 132.4 MiB of ``vacancy_fts_content`` text against the 132.5 MiB of
-- ``vacancy.title + description + company_name_raw`` it was copied from.  With
-- ``_data``, ``_docsize`` and ``_idx`` on top, ``vacancy_fts`` occupied
-- 240.3 MiB of a 616.9 MiB file - 39% of the database, none of it new
-- information.
--
-- **The fix for vacancy_fts: external content.**  ``content='vacancy'`` tells
-- FTS5 to keep no text of its own and to address rows by ``vacancy.rowid``.
-- Maintaining an entry becomes a rowid seek instead of a scan, and the second
-- copy of the corpus goes away.  Measured on a copy of this database: 2,143 ms
-- median per re-index before, 0.50 ms after; ``vacancy_fts`` down from
-- 240.3 MiB to 74.3 MiB.
--
-- Because an external-content table stores no text, it cannot subtract a row's
-- terms unless it is handed the exact values that were indexed - so
-- maintenance has to happen at the moment the base row changes, with both the
-- old and the new values in hand.  That is what a trigger is, and it is why
-- ``_index_vacancy`` is deleted from db/repositories/knowledge.py in the same
-- commit rather than rewritten: doing it in Python means UPDATE-then-re-read,
-- which quotes the *new* values back to FTS5 when removing the old entry and
-- corrupts the index on the spot (``database disk image is malformed``,
-- reproduced).  The triggers below cannot get that wrong; OLD and NEW are the
-- right values by construction.
--
-- **The fix for company_fts: a rowid key, and nothing else.**  company_fts has
-- the identical defect - ``company_id UNINDEXED``, the identical ``SCAN
-- company_fts VIRTUAL TABLE INDEX 0:`` plan, cost linear in the corpus
-- (measured on synthetic rows of the same shape: 6,674 virtual-machine steps
-- per re-index at 500 companies, 52,174 at 4,000).  It hurts less today only
-- because there are 4,156 companies of ~25 bytes of indexed text each rather
-- than 35,159 vacancies of ~3.9 KiB, and its shadow is 0.6 MiB rather than
-- 240 MiB.  Left alone it becomes the same incident later, at a worse moment.
--
-- It does not get the same fix, and the reason is worth stating rather than
-- discovering.  ``_index_company`` runs ``products_services`` through
-- ``_flatten``, which JSON-decodes the column and joins its values into prose;
-- the column holds a list of ``{name, description, confidence, source}``
-- objects.  An external-content table re-reads the base column as it stands,
-- so it would index the raw JSON and every company would match a search for
-- "description" or "confidence".  That is a change to what a company search
-- answers, and it has no business travelling inside a performance fix.
--
-- What removes the scan without touching a single indexed token is keying the
-- DELETE by ``rowid`` instead of by ``company_id``.  FTS5 does seek on rowid -
-- the plan becomes ``SCAN company_fts VIRTUAL TABLE INDEX 0:=`` and the cost
-- goes flat: 164 steps at 500 companies, 164 at 4,000.  company_fts therefore
-- stays exactly the table it was, declaration for declaration, and is only
-- rebuilt here so that each entry's rowid is the ``company.rowid`` of the
-- company it describes.  Without that rebuild the first keyed DELETE would
-- remove some other company's entry.
--
-- **Two things change that are not performance.**
--
-- ``_index_vacancy`` truncated the description to 20,000 characters before
-- indexing it.  External content indexes the column as it stands, so that
-- truncation goes.  Twelve of 35,159 vacancies are longer than that, 88 kB of
-- text between them; they become searchable to the end.  The truncation could
-- not be kept: FTS5 must be given back exactly what it was given, and
-- truncating on one side of that pair is the corruption described above.
--
-- The row counts settle.  ``vacancy`` holds 35,159 rows against
-- ``vacancy_fts``'s 35,158, and ``company`` 4,156 against ``company_fts``'s
-- 4,155 - one row each, written by tests/e2e/test_03_apply.py through
-- ``connection.insert_row``, which does not index.  Both rebuilds below take
-- their rows from the base table, so the counts come out equal afterwards.
-- That is the rebuild working, not rows appearing from nowhere.
--
-- **Re-runnable on purpose.**  ``migrator.migrate()`` wraps this in
-- ``write_tx`` and runs it with ``conn.executescript``, and executescript
-- commits the open transaction before it starts - so this script is not atomic
-- with the ``schema_migration`` row that records it.  A failure part way
-- through leaves the completed half committed and unrecorded, and the next
-- backend start runs the file again from the top.  Every statement here is
-- therefore written to survive being run twice.
--
-- **The file does not shrink by itself.**  This migration frees roughly 167
-- MiB to the free list; ``PRAGMA auto_vacuum`` is 0, so the file stays 617 MB
-- until somebody runs VACUUM.  VACUUM cannot run inside a transaction and
-- takes an exclusive lock for its whole 7-17 s, so it is not here.  Run it
-- against a stopped backend::
--
--     sqlite3 data/dreamjob.db "VACUUM;"
--
-- Measured: 617 MB -> 458 MB, 17 s.
--
-- **What renumbering rowids costs now, and the one thing that repairs it.**
-- After this migration *both* indexes are addressed by the base table's rowid:
-- ``vacancy_fts`` because that is what ``content_rowid`` means, and
-- ``company_fts`` because ``_index_company`` seeks on ``rowid`` instead of
-- scanning for ``company_id``.  Neither table is told when SQLite renumbers
-- the rowids of a table whose primary key is TEXT rather than INTEGER, so
-- anything that renumbers silently unpicks the correspondence they both rest
-- on.  Measured on 3.45.3: VACUUM did *not* renumber (0 of 17 rowids moved,
-- gaps and all), but a restore from ``.dump`` did - 15 of 17 - because the
-- dump carries the FTS shadow tables verbatim while the base tables are
-- re-inserted and take fresh rowids.
--
-- The damage is not a stale index, it is a wrong one, and it is silent.  On a
-- restored copy one ``update_company`` deleted the entry sitting at the rowid
-- the updated company now occupies - a *different* company, which vanished
-- from search - and left the updated company indexed twice.  Every company
-- written afterwards does it again.  ``vacancy_fts`` fails the same way, with
-- one posting's words answering for another.
--
-- ``INSERT INTO vacancy_fts (vacancy_fts) VALUES ('rebuild')`` repairs the
-- vacancy side, because an external-content rebuild re-reads ``vacancy`` and
-- re-keys every entry.  The obvious analogue on the other table does not:
-- ``INSERT INTO company_fts (company_fts) VALUES ('rebuild')`` is accepted
-- without complaint and repairs nothing (14 of 14 entries still misaligned in
-- the test above), because a standalone table rebuilds from its own shadow
-- copy and keeps the rowids it already had.  What repairs both is
-- ``repositories.knowledge.reindex_all()`` - it refills ``company_fts`` from
-- ``company`` keyed by ``company.rowid`` and issues the vacancy rebuild - so
-- after any restore from a dump, run it before the backend serves a search::
--
--     python3 -c "from dreamjob.db.repositories.knowledge import reindex_all; \
--         print(reindex_all())"
--
-- A binary ``.backup`` copies pages and preserves every rowid, so it needs
-- none of this; prefer it for backups (FR-345).
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. vacancy_fts becomes an external-content index over `vacancy`.
--    DROP takes the five shadow tables (_content, _data, _docsize, _idx,
--    _config) with it; there is nothing to clean up by hand.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS vacancy_fts;

CREATE VIRTUAL TABLE vacancy_fts USING fts5(
    title,
    description,
    company_name_raw,
    content='vacancy',
    content_rowid='rowid'
);

-- ---------------------------------------------------------------------------
-- 2. The triggers that keep it in step (FR-345).
--
--    AFTER UPDATE names its three columns so an update of `confidence` or
--    `collected_at` - which is what a re-collected posting mostly is - fires
--    nothing at all, and the WHEN guard catches the rest: a SET list that
--    mentions `description` but does not change it costs no re-tokenisation.
--    Measured through the repository: 82 virtual-machine steps for an update
--    of confidence and collected_at, 229 for one that changes the description.
--
--    Nothing in the product deletes a vacancy today.  The delete trigger is
--    here because the moment something does, an index that keeps no text of
--    its own does not merely go stale - SQLite hands the freed rowid to the
--    next INSERT, so the deleted posting's words come back naming whichever
--    vacancy was written next.  A wrong answer, not a missing one.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS vacancy_fts_after_insert;
CREATE TRIGGER vacancy_fts_after_insert AFTER INSERT ON vacancy
BEGIN
    INSERT INTO vacancy_fts (rowid, title, description, company_name_raw)
    VALUES (NEW.rowid, NEW.title, NEW.description, NEW.company_name_raw);
END;

DROP TRIGGER IF EXISTS vacancy_fts_after_delete;
CREATE TRIGGER vacancy_fts_after_delete AFTER DELETE ON vacancy
BEGIN
    INSERT INTO vacancy_fts (vacancy_fts, rowid, title, description, company_name_raw)
    VALUES ('delete', OLD.rowid, OLD.title, OLD.description, OLD.company_name_raw);
END;

DROP TRIGGER IF EXISTS vacancy_fts_after_update;
CREATE TRIGGER vacancy_fts_after_update
AFTER UPDATE OF title, description, company_name_raw ON vacancy
WHEN OLD.title IS NOT NEW.title
  OR OLD.description IS NOT NEW.description
  OR OLD.company_name_raw IS NOT NEW.company_name_raw
BEGIN
    INSERT INTO vacancy_fts (vacancy_fts, rowid, title, description, company_name_raw)
    VALUES ('delete', OLD.rowid, OLD.title, OLD.description, OLD.company_name_raw);
    INSERT INTO vacancy_fts (rowid, title, description, company_name_raw)
    VALUES (NEW.rowid, NEW.title, NEW.description, NEW.company_name_raw);
END;

-- ---------------------------------------------------------------------------
-- 3. Fill it.  'rebuild' discards whatever is there and re-reads every row of
--    `vacancy` through the tokeniser: 7.3 s for 35,159 rows on this machine,
--    and the repair for any later drift.
-- ---------------------------------------------------------------------------
INSERT INTO vacancy_fts (vacancy_fts) VALUES ('rebuild');

-- ---------------------------------------------------------------------------
-- 4. company_fts keeps its declaration - same five columns, same tokeniser,
--    same `company_id` the two search queries join on - and is rebuilt only so
--    that each entry's rowid is the rowid of the company it describes.  That
--    is what lets `_index_company` seek instead of scan.  `products_services`
--    is flattened here the way `_flatten` flattens it in Python: leaf values
--    joined with spaces, keys dropped.  json_tree() raises on a column that is
--    not valid JSON, so the CASE guards it and falls back to the raw text,
--    which is also what `_flatten` does with a value that will not decode.
--    (The column is NULL for all 4,156 companies today; the expression is here
--    so that this migration is still correct the day it is not.)
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS company_fts;

CREATE VIRTUAL TABLE company_fts USING fts5(
    company_id UNINDEXED, name, normalised_name, business_summary, products_services
);

INSERT INTO company_fts (rowid, company_id, name, normalised_name, business_summary,
                         products_services)
SELECT
    c.rowid,
    c.id,
    COALESCE(c.name, ''),
    COALESCE(c.normalised_name, ''),
    COALESCE(c.business_summary, ''),
    CASE
        WHEN c.products_services IS NULL THEN ''
        WHEN json_valid(c.products_services) THEN COALESCE(
            (SELECT group_concat(t.value, ' ') FROM json_tree(c.products_services) t
             WHERE t.type IN ('text', 'integer', 'real')), '')
        ELSE c.products_services
    END
FROM company c;
