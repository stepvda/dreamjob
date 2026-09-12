-- ===========================================================================
-- What the plan screen has to say about the plan it is showing (FR-186)
--
-- The planner scales a plan down to fit the campaign's page cap and used to
-- say so in one log line.  One campaign asked for 1,505 pages under a 150-page
-- cap, was silently scaled to 150, and the user never saw that 90% of the
-- plan they approved would not be fetched.  The scale-down is a fact about
-- the plan, so it is stored with the campaign and rendered by the plan screen
-- and the run report:
--
--   {"page_budget": {"requested": 1505, "granted": 150, "max_pages": 150,
--                    "scaled_down": true},
--    "message": "The plan asked for 1,505 pages under a 150-page cap; ..."}
--
-- JSON so the notice grows without another migration.  NULL means the plan
-- needed no notice: it fit inside its cap.
-- ===========================================================================

ALTER TABLE campaign ADD COLUMN plan_notice TEXT;
