-- ===========================================================================
-- Placing every opportunity on the map automatically (FR-144, FR-283)
--
-- The radius filter measures from opportunity.latitude/longitude, and nothing
-- filled them in unless someone pressed "Place them on the map" - which on a
-- running installation nobody had, so every radius search returned only the
-- remote roles.  Placement is now part of adding an opportunity: synthesis
-- places it from what is already known, and a background job resolves the
-- rest through the geocoder (pipeline/locate.py).
--
--   opportunity.location_precision  where the coordinates came from:
--                                   source | address | locality | employer_site
--   opportunity.located_at          when placement last decided this row; NULL
--                                   means "not yet", which is the job's queue.
--                                   A decided row with no coordinates names no
--                                   place that can be measured from.
--
--   geo_place                       one answer per place string or employer
--                                   address, shared by every seeker: where a
--                                   town is is not personal, and asking the
--                                   geocoder once is what its usage policy
--                                   asks of us.  A NULL position is a recorded
--                                   "no such place", retried after a while.
-- ===========================================================================

ALTER TABLE opportunity ADD COLUMN location_precision TEXT;
ALTER TABLE opportunity ADD COLUMN located_at TEXT;

CREATE INDEX idx_opportunity_unlocated ON opportunity(created_at) WHERE located_at IS NULL;

CREATE TABLE geo_place (
    query_key     TEXT PRIMARY KEY,
    latitude      REAL,
    longitude     REAL,
    place_type    TEXT,
    place_rank    INTEGER,
    country_code  TEXT,
    display_name  TEXT,
    resolved_at   TEXT NOT NULL,
    CHECK ((latitude IS NULL) = (longitude IS NULL))
);
