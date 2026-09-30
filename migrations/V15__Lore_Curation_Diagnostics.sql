-- Lore curation diagnostics.
--
-- Why this exists: for the first seven curation runs on this instance every
-- row read ENTRIES_ADDED=0 with ERROR NULL, and there was no way to tell
-- whether the model returned an empty list, returned prose we could not
-- parse, returned nothing at all, or returned operations that were all
-- rejected. Those are four different bugs. The actual cause turned out to be
-- the fourth column below: the model spent its entire output budget on
-- reasoning and emitted no content, which the old schema could not show.
--
-- Note on the OBS_RENDERED column: a pass marks observations curated, but the
-- render budget means not all of them were ever shown to the model. Recording
-- both numbers is what makes that visible.

ALTER TABLE `lore_curation_run`
    ADD COLUMN `OBS_RENDERED` INT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'How many of OBS_CONSIDERED actually fitted the render budget'
        AFTER `OBS_CONSIDERED`,
    ADD COLUMN `OPS_RETURNED` INT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'Operations the model proposed, before validation',
    ADD COLUMN `ENTRIES_REJECTED` INT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'Operations refused by apply_operations',
    ADD COLUMN `ENTRIES_CONTRADICTED` INT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'Contradiction links written instead of a supersede',
    ADD COLUMN `RESPONSE_CHARS` INT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'Length of the model content. 0 with a long run means an empty response',
    ADD COLUMN `REASONING_CHARS` INT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'Length of reasoning_content, where the provider reports it',
    ADD COLUMN `STOP_REASON` VARCHAR(32) DEFAULT NULL
        COMMENT 'Provider finish_reason. length here means the budget was exhausted',
    ADD COLUMN `MODEL` VARCHAR(64) DEFAULT NULL
        COMMENT 'The model that ran this pass',
    ADD COLUMN `ATTEMPTS` TINYINT UNSIGNED NOT NULL DEFAULT 1
        COMMENT 'Model calls made, including one repair retry',
    ADD COLUMN `RAW_RESPONSE` MEDIUMTEXT DEFAULT NULL
        COMMENT 'Redacted model response, only when LORE_KEEP_RAW_RESPONSE is on';

ALTER TABLE `lore_observation`
    ADD COLUMN `CURATION_ATTEMPTS` INT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'Passes that showed this row to the model without it yielding an entry'
        AFTER `CURATED_AT`,
    ADD INDEX `IDX_OBS_ATTEMPTS` (`CLIENT_CODE`, `APP_CODE`, `CURATED_AT`, `CURATION_ATTEMPTS`);
