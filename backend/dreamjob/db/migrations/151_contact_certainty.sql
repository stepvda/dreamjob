-- ===========================================================================
-- Persist e-mail uncertainty on hiring contacts (FR-301, FR-303, FR-304)
--
-- FR-303 pattern inference composes an address from the domain's convention
-- rather than reading one somebody published.  FR-304 validation is what turns
-- that guess into a fact, and until it comes back ``valid`` the address is a
-- hypothesis.  The distinction was already visible in the row (method plus
-- verdict) but not queryable, so the Contacts screen could not say "this address
-- is a guess" or filter on it.  ``email_uncertain`` makes the uncertainty a
-- stored, indexed property.
--
-- A ``valid`` verdict clears it (FR-304); any other verdict - ``risky``,
-- ``unknown`` or ``invalid`` - leaves it set.  Addresses that were published or
-- stated rather than composed are never uncertain, whatever their verdict.
-- ===========================================================================

ALTER TABLE contact ADD COLUMN email_uncertain INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_contact_uncertain ON contact(email_uncertain);

-- Existing rows composed by inference and not since proven valid.
UPDATE contact
   SET email_uncertain = 1
 WHERE email_source_method = 'pattern_inference'
   AND COALESCE(email_validation, 'unknown') <> 'valid';
