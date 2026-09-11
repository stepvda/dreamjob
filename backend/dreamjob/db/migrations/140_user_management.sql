-- ===========================================================================
-- User management (NFR-202, FR-101, FR-362)
--
-- The administration surface needs to suspend an account without deleting the
-- person's private space, and to show an operator when each account last
-- actually signed in.  Two columns carry that, plus the timestamp of the
-- suspension for the audit trail.
--
-- Nothing here is destructive: a disabled account keeps every row and file it
-- had, so re-enabling it restores the account exactly as it was. Deleting a
-- job seeker remains the existing FR-108 erasure path, which is deliberately
-- separate and irreversible.
-- ===========================================================================

ALTER TABLE job_seeker ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_seeker ADD COLUMN disabled_at TEXT;
ALTER TABLE job_seeker ADD COLUMN last_login_at TEXT;

-- The user list is searched by e-mail and by name, and is read on every visit
-- to the administration screen. A normal index on the e-mail column already
-- exists via the UNIQUE constraint; this one supports the ORDER BY the list
-- uses so the screen stays fast as accounts accumulate (NFR-101).
CREATE INDEX IF NOT EXISTS idx_seeker_last_login ON job_seeker(last_login_at DESC);
