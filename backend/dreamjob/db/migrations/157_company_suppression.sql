-- ===========================================================================
-- Suppressing a company in the shared knowledge base (FR-341, NFR-302)
--
-- 'company' is shared property of every job seeker (FR-341) and carries no
-- owner to scope a delete to, so a bad record cannot be hard-deleted without
-- taking it away from everyone.  Suppression is the shared-KB answer: the row
-- stays, its evidence stays, and every browse, list and count stops showing
-- it.  An administrator can undo the decision, which is why an admin can
-- still open the detail page of a suppressed company.
--
--   suppressed          0/1 flag, indexed for the browse filters
--   suppressed_at       when the decision was taken
--   suppressed_reason   why, in the administrator's own words
-- ===========================================================================

ALTER TABLE company ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE company ADD COLUMN suppressed_at TEXT;
ALTER TABLE company ADD COLUMN suppressed_reason TEXT;

CREATE INDEX idx_company_suppressed ON company(suppressed) WHERE suppressed = 1;
