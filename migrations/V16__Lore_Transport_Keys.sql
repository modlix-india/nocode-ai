-- Lore transport: a stable identity for an entry across environments.
--
-- Committed seed files and cross-env promotion both need to answer "is this
-- the same claim as the one already here?" BODY_HASH cannot: it is the dedupe
-- key and it changes on every body edit, so matching on it would import an
-- edited entry as a brand new row and leave the stale one standing.
--
-- SEED_KEY is that identity. SEED_SOURCE records which file a row came from,
-- without which a sync-mode import cannot tell a seeded row from one a person
-- or the curator wrote, and is therefore unsafe to run.
--
-- The unique key makes "one row per key per client per app" a database
-- invariant, so a buggy importer cannot leave two rows for one claim.

ALTER TABLE `lore_entry`
    ADD COLUMN `SEED_KEY` VARCHAR(120) DEFAULT NULL
        COMMENT 'Stable cross-environment identity for this claim'
        AFTER `BASE_ENTRY_ID`,
    ADD COLUMN `SEED_SOURCE` VARCHAR(120) DEFAULT NULL
        COMMENT 'Which transport document this row came from, e.g. seed:leadzump/v1'
        AFTER `SEED_KEY`,
    ADD UNIQUE KEY `UQ_ENTRY_SEED_KEY` (`CLIENT_CODE`, `APP_CODE`, `SEED_KEY`),
    ADD INDEX `IDX_ENTRY_SEED_SOURCE` (`CLIENT_CODE`, `APP_CODE`, `SEED_SOURCE`);
