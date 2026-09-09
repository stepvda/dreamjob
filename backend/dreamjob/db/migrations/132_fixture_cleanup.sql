-- ===========================================================================
-- 132: the test fixtures that were left behind in campaign plans
-- (FR-161, FR-163, FR-164, FR-166, FR-185; docs/Data_Gathering_Plan.md item C5)
--
-- **What this migration removes, so the disappearing rows are no mystery.**
-- 104 ``source_plan_item`` rows, measured on this database 2026-09-09, whose
-- ``adapter_key`` names a stub that only ever existed inside the unit suite:
--
--     broken_board        32      spine.ats           5
--     stub_board          32      spine.board         5
--                                 spine.discovery     5
--                                 spine.empty         5
--                                 spine.greenhouse    5
--                                 spine.hostile       5
--                                 spine.refused       5
--                                 spine.silent        5
--
-- They sit in 32 campaigns, they have collected 0 records between them, no
-- ``provenance`` row points at any of them (checked before writing this), and
-- no ``job_run``, ``fetch_ledger`` or ``source_catalogue`` row names them.
-- Deleting them therefore loses no history: there is none.
--
-- **How they got here.**  C5, twice.  ``SourceAdapter.register`` writes every
-- adapter in the process-wide registry (NFR-601) into the catalogue, and a
-- full unit run has each module's stub adapters in that registry, so one
-- ``load_all()`` against the installed ``DREAMJOB_DB_PATH`` catalogued
-- ``stub_board`` and its siblings as real sources.  Source selection (FR-164)
-- cannot tell a catalogue row with no adapter from a real one, so every
-- campaign planned an item against each of them.  Migration-less cleanup and
-- ``admin.prune_unknown_sources`` have since removed the *catalogue* rows (0
-- remain), and ``tests/unit/conftest.py`` points the whole unit suite at a
-- scratch database so the catalogue cannot be polluted again - but the plan
-- items those rows produced were never cleaned up.  Each is an item a
-- campaign schedules, cannot run, and cannot explain: 104 of the 538 outcomes
-- the FR-185 dashboard had to account for, none of them actionable, all of
-- them standing between an operator and the failures that are real.
--
-- Ten of the suite's twenty stub keys reached this database.  What keeps the
-- other ten - and the next one - out is not this DELETE, which runs once, but
-- two standing rules: ``tests/unit/conftest.py`` now refuses to let a unit
-- test open a database under ``data/`` at all, and
-- ``db.repositories.campaigns.insert_plan_item`` refuses to write a plan item
-- for a source with neither an implementation nor a catalogue row - which is
-- what each of the 104 rows below had become.
--
-- **What this migration deliberately does not do.**  It does not touch the
-- 4,664 items settled "not started: max_pages (FR-186)", the 308 dead board
-- slugs or the 192 robots.txt refusals.  Those are outcomes of real sources
-- and are classified where outcomes belong, not deleted here.
-- ===========================================================================

-- 1. The fixture plan items (104 rows).
DELETE FROM source_plan_item
 WHERE adapter_key IN (
     'broken_board',
     'stub_board',
     'spine.ats',
     'spine.board',
     'spine.discovery',
     'spine.empty',
     'spine.greenhouse',
     'spine.hostile',
     'spine.refused',
     'spine.silent'
 );

-- 2. Their catalogue rows, if this installation still has any.  0 here on
--    2026-09-09 - the prune already took them - but a developer's database
--    that ran the suite before the conftest fix still holds them, and an
--    installation that never restarts would keep planning against them.
--    A row an administrator acknowledged is a decision and is left alone
--    (the same rule ``prune_unknown_sources`` follows).
DELETE FROM source_catalogue
 WHERE acknowledged_at IS NULL
   AND adapter_key IN (
     'broken_board',
     'stub_board',
     'spine.ats',
     'spine.board',
     'spine.discovery',
     'spine.empty',
     'spine.greenhouse',
     'spine.hostile',
     'spine.refused',
     'spine.silent'
 );

-- 3. The four items whose reason is now untrue.  27 rows carried
--    "no adapter registered for this source"; 23 of them were the fixtures
--    deleted above, and the remaining four - board.arbeitnow, board.actiris,
--    ats.workable and ats.teamtailor, one each, all in the one campaign
--    d6488182... which was cancelled 20 seconds after it was planned - name
--    adapters that ship today and did not when that plan ran.
--
--    Retired, not re-planned.  Re-planning is the planner's job and the user's
--    decision (FR-163), the campaign that owns these four is cancelled, and
--    resurrecting an item inside a finished campaign would put a source back
--    into a plan nobody approved.  What was wrong was the *label*: the row
--    says the source cannot be run when it can, which is exactly the kind of
--    stale reason that teaches an operator to stop reading them.  The row
--    keeps status 'skipped' - it had nothing to do - and gains a reason that
--    is still true tomorrow.
UPDATE source_plan_item
   SET last_error = 'skipped when this plan ran: no adapter for this source at the time. The adapter ships now - re-plan the campaign to collect it (FR-163).'
 WHERE last_error = 'no adapter registered for this source';
