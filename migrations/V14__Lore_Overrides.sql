-- Lore inherits the platform's override model.
--
-- An app owned by SYSTEM can be edited by CLIENTA. CLIENTA's users must see
-- SYSTEM's knowledge about that app, plus their own on top, and their writes
-- must never touch SYSTEM's rows.
--
-- So: an entry always belongs to the client that WROTE it (CLIENT_CODE, which
-- is always the logged-in user's client). An entry may additionally declare
-- that it overrides a base-client entry, via BASE_ENTRY_ID. Reads walk the
-- inheritance chain from security (`applications/internal/appInheritance`,
-- base-first) and let a later client's override shadow an earlier one.
--
-- Retiring an inherited entry from an overriding client writes a TOMBSTONE:
-- a row with STATUS='retired' and BASE_ENTRY_ID set. The base row is untouched
-- and other clients still see it.

ALTER TABLE `lore_entry`
    ADD COLUMN `BASE_ENTRY_ID` BIGINT UNSIGNED DEFAULT NULL
        COMMENT 'This entry overrides that base-client entry, for this client only'
        AFTER `SUPERSEDED_BY`,
    ADD INDEX `IDX_ENTRY_BASE` (`BASE_ENTRY_ID`);

-- An entry may override a given base entry at most once per client: a second
-- override would make "which one wins" ambiguous.
ALTER TABLE `lore_entry`
    ADD UNIQUE KEY `UQ_ENTRY_OVERRIDE` (`CLIENT_CODE`, `APP_CODE`, `BASE_ENTRY_ID`);
