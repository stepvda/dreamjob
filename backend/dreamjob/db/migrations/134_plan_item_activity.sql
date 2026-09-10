-- ===========================================================================
-- When each source last did something (FR-361)
--
-- Migration 130 gave a plan item the six answers it can end on.  Nothing said
-- *when*, so the collection screen could draw a progress bar and a ledger and
-- never a chronology - and a four-hour run with 6,524 sources in it looked, to
-- the person watching, like a number that occasionally moved.
--
-- Two nullable columns and one index.  ``activity_at`` is when the row last
-- changed what it was doing; ``activity_kind`` is which of the feed's names
-- that was.  Both are written by ``UPDATE``s the collection worker already
-- issues (the status write at the head of the page loop, the verdict write on
-- the way out), so the feed costs no extra transaction on a single-writer
-- SQLite (CR-408, NFR-102).
--
-- Why a kind of its own rather than reading ``status``/``outcome_state``: the
-- verdict columns are written once, in a settle pass that runs after the whole
-- plan has finished.  A source that finished at 15:02 does not get its verdict
-- until 18:30, so a feed built on those columns would be silent for the length
-- of a run and then print 6,524 lines at the end of it.
--
-- No backfill.  There is no timestamp anywhere in the schema to backfill from -
-- ``created_at`` is plan-generation time and is identical across every row of a
-- plan - so a campaign that ran before this migration shows its milestones and
-- nothing else, which is the truth about what was recorded.
-- ===========================================================================

ALTER TABLE source_plan_item ADD COLUMN activity_at   TEXT;
ALTER TABLE source_plan_item ADD COLUMN activity_kind TEXT;

CREATE INDEX idx_plan_activity ON source_plan_item(campaign_id, activity_at);
