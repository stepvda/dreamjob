-- ===========================================================================
-- Two lookups that were scanning (NFR-101, NFR-102)
--
-- 1. ``contact`` by address.  ``insert or find`` during contact discovery
--    looks the address up with ``WHERE lower(email) = ?`` (repositories
--    ``contacts.py``, ``dispatch.py``), and ``idx_contact_email`` indexes the
--    raw column - so the planner cannot use it for the ``lower()`` expression
--    and scans the table instead.  SQLite indexes the expression itself since
--    3.9, so the query's own text becomes seekable.
--
-- 2. ``provenance`` by entity and field.  The profile view and the crawl
--    inventory read one company's provenance with
--    ``entity_type = ? AND entity_id = ?``, ordered by ``created_at`` (or
--    filtered by ``field_path``), and ``idx_prov_entity`` stops at the leading
--    pair - so the sort, the prefix filter and ``MAX(created_at)`` all ran
--    outside the index.  This index carries the whole key plus the ordering
--    column, and supersedes the two-column index for these reads.
--
-- Deliberately *not* indexed: ``company`` scans on
-- ``news LIKE ? OR reference_customers LIKE ? OR business_summary LIKE ?``.
-- Each pattern has a leading ``%`` and the three predicates are OR-ed, so no
-- B-tree can seek them; a trigram/FTS index is a different design with its own
-- write cost, and it is not cheap enough to smuggle into an index migration.
--
-- ``IF NOT EXISTS`` because a forward migration must be safe to re-run if a
-- process died between the DDL and the ``schema_migration`` row.
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_contact_email_lower
    ON contact(lower(email));

CREATE INDEX IF NOT EXISTS idx_prov_entity_field
    ON provenance(entity_type, entity_id, field_path, created_at);
