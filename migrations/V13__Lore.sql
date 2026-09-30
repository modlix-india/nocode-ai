-- Lore: curated, growing knowledge about each application we build.
--
-- Two layers, deliberately separated:
--
--   lore_observation  raw facts, append-only, cheap to write, written by
--                       anything that watches the app (agent turns, definition
--                       edits, inventory snapshots, docs, people). Nobody reads
--                       these directly for answers.
--
--   lore_entry        curated knowledge, one durable statement per row, with
--                       provenance back to the observations that produced it.
--                       This is what gets read.
--
-- The curator (app/services/lore/curator.py) is the only thing that turns the
-- first into the second. Everything else reads entries.
--
-- Relationship to cfa_app_kb (V12): that table is six hand-written narrative
-- sections per app, authored by the agent on request. Lore is the automatic
-- layer around it: it accumulates without being asked, and it reads app_kb as
-- one of its sources. Lore never writes app_kb.
--
-- Scope key is (CLIENT_CODE, APP_CODE) everywhere, same as app_kb.

-- ── Layer 1: observations ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `lore_observation` (
    `ID`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `CLIENT_CODE`   VARCHAR(64)     NOT NULL COMMENT 'Owning client (tenant) code',
    `APP_CODE`      VARCHAR(64)     NOT NULL COMMENT 'App this observation is about',
    `KIND`          VARCHAR(24)     NOT NULL COMMENT 'chat|edit|inventory|doc|manual|run|review',
    `SOURCE`        VARCHAR(160)    NOT NULL COMMENT 'Where it came from, e.g. appbuilderv4:session:abc123',
    `SUBJECT`       VARCHAR(160)    NOT NULL DEFAULT 'app' COMMENT 'What it is about: app | page:jobsToday | storage:job',
    `BODY`          MEDIUMTEXT      NOT NULL COMMENT 'The raw observed text',
    `META`          JSON            DEFAULT NULL COMMENT 'Free-form structured context from the source',
    `FINGERPRINT`   CHAR(64)        NOT NULL COMMENT 'SHA-256 of (kind|subject|normalised body) — repeat sightings collapse',
    `SEEN_COUNT`    INT UNSIGNED    NOT NULL DEFAULT 1 COMMENT 'How many times this exact fact has been observed',
    `OBSERVED_BY`   BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `OBSERVED_AT`   TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `LAST_SEEN_AT`  TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `CURATED_AT`    TIMESTAMP       NULL DEFAULT NULL COMMENT 'NULL until the curator has processed it',
    PRIMARY KEY (`ID`),
    UNIQUE KEY `UQ_OBS_FINGERPRINT` (`CLIENT_CODE`, `APP_CODE`, `FINGERPRINT`),
    INDEX `IDX_OBS_PENDING` (`CLIENT_CODE`, `APP_CODE`, `CURATED_AT`, `ID`),
    INDEX `IDX_OBS_SUBJECT` (`CLIENT_CODE`, `APP_CODE`, `SUBJECT`),
    INDEX `IDX_OBS_SOURCE` (`SOURCE`),
    FULLTEXT KEY `FT_OBS_BODY` (`BODY`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Layer 2: curated entries ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `lore_entry` (
    `ID`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `CLIENT_CODE`       VARCHAR(64)     NOT NULL,
    `APP_CODE`          VARCHAR(64)     NOT NULL,
    `KIND`              VARCHAR(24)     NOT NULL COMMENT 'purpose|decision|convention|constraint|integration|glossary|gotcha|howto|owner|status',
    `SUBJECT`           VARCHAR(160)    NOT NULL DEFAULT 'app',
    `TITLE`             VARCHAR(240)    NOT NULL COMMENT 'One line, readable on its own',
    `BODY`              MEDIUMTEXT      NOT NULL COMMENT 'The knowledge itself, markdown',
    `TAGS`              JSON            DEFAULT NULL,
    `CONFIDENCE`        TINYINT UNSIGNED NOT NULL DEFAULT 50 COMMENT '0-100 at LAST_CONFIRMED_AT; decayed by kind half-life at read time',
    `STATUS`            VARCHAR(16)     NOT NULL DEFAULT 'active' COMMENT 'active|superseded|retired|draft',
    `SUPERSEDED_BY`     BIGINT UNSIGNED DEFAULT NULL COMMENT 'The entry that replaced this one',
    `SOURCE_COUNT`      INT UNSIGNED    NOT NULL DEFAULT 1 COMMENT 'Distinct observations backing this entry',
    `BODY_HASH`         CHAR(64)        NOT NULL,
    `VERSION`           INT UNSIGNED    NOT NULL DEFAULT 1,
    `PINNED`            TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'A human said this is true; the curator may not revise or retire it',
    `CREATED_BY`        BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `UPDATED_BY`        BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `FIRST_SEEN_AT`     TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `LAST_CONFIRMED_AT` TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `UPDATED_AT`        TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`ID`),
    UNIQUE KEY `UQ_ENTRY_BODY` (`CLIENT_CODE`, `APP_CODE`, `KIND`, `BODY_HASH`),
    INDEX `IDX_ENTRY_LIVE` (`CLIENT_CODE`, `APP_CODE`, `STATUS`, `KIND`),
    INDEX `IDX_ENTRY_SUBJECT` (`CLIENT_CODE`, `APP_CODE`, `SUBJECT`, `STATUS`),
    INDEX `IDX_ENTRY_FRESH` (`CLIENT_CODE`, `APP_CODE`, `LAST_CONFIRMED_AT`),
    FULLTEXT KEY `FT_ENTRY` (`TITLE`, `BODY`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Provenance: which observations produced / confirmed which entry.
CREATE TABLE IF NOT EXISTS `lore_entry_source` (
    `ENTRY_ID`       BIGINT UNSIGNED NOT NULL,
    `OBSERVATION_ID` BIGINT UNSIGNED NOT NULL,
    `LINKED_AT`      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`ENTRY_ID`, `OBSERVATION_ID`),
    INDEX `IDX_SRC_OBS` (`OBSERVATION_ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Previous bodies of a revised entry. Every revise writes the OLD row here first.
CREATE TABLE IF NOT EXISTS `lore_entry_history` (
    `ID`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `ENTRY_ID`   BIGINT UNSIGNED NOT NULL,
    `VERSION`    INT UNSIGNED    NOT NULL,
    `TITLE`      VARCHAR(240)    NOT NULL,
    `BODY`       MEDIUMTEXT      NOT NULL,
    `BODY_HASH`  CHAR(64)        NOT NULL,
    `CONFIDENCE` TINYINT UNSIGNED NOT NULL,
    `STATUS`     VARCHAR(16)     NOT NULL,
    `CHANGED_BY` BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `CHANGED_AT` TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `MESSAGE`    VARCHAR(512)    DEFAULT NULL,
    PRIMARY KEY (`ID`),
    UNIQUE KEY `UQ_HIST_VERSION` (`ENTRY_ID`, `VERSION`),
    INDEX `IDX_HIST_ENTRY` (`ENTRY_ID`, `VERSION` DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Entry-to-entry relations. `supersedes` is written by the curator; the rest
-- are for navigation ("what else touches this decision").
CREATE TABLE IF NOT EXISTS `lore_link` (
    `FROM_ID`   BIGINT UNSIGNED NOT NULL,
    `TO_ID`     BIGINT UNSIGNED NOT NULL,
    `REL`       VARCHAR(24)     NOT NULL COMMENT 'supersedes|relates_to|contradicts|depends_on|example_of',
    `CREATED_AT` TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`FROM_ID`, `TO_ID`, `REL`),
    INDEX `IDX_LINK_TO` (`TO_ID`, `REL`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One row per curator pass. Doubles as the concurrency lock: a pass whose
-- FINISHED_AT is NULL and STARTED_AT is recent blocks a second pass.
CREATE TABLE IF NOT EXISTS `lore_curation_run` (
    `ID`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `CLIENT_CODE`    VARCHAR(64)     NOT NULL,
    `APP_CODE`       VARCHAR(64)     NOT NULL,
    `TRIGGER_SOURCE` VARCHAR(64)     NOT NULL DEFAULT 'manual',
    `OBS_CONSIDERED` INT UNSIGNED    NOT NULL DEFAULT 0,
    `ENTRIES_ADDED`  INT UNSIGNED    NOT NULL DEFAULT 0,
    `ENTRIES_REVISED` INT UNSIGNED   NOT NULL DEFAULT 0,
    `ENTRIES_CONFIRMED` INT UNSIGNED NOT NULL DEFAULT 0,
    `ENTRIES_RETIRED` INT UNSIGNED   NOT NULL DEFAULT 0,
    `STARTED_AT`     TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `FINISHED_AT`    TIMESTAMP       NULL DEFAULT NULL,
    `ERROR`          VARCHAR(1024)   DEFAULT NULL,
    PRIMARY KEY (`ID`),
    INDEX `IDX_RUN_APP` (`CLIENT_CODE`, `APP_CODE`, `STARTED_AT` DESC),
    INDEX `IDX_RUN_OPEN` (`CLIENT_CODE`, `APP_CODE`, `FINISHED_AT`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
