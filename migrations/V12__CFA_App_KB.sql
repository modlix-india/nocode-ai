-- Per-app knowledge base for the customer-facing agent.
--
-- One row per (client_code, app_code, section, version) tuple. Latest version
-- per (client, app, section) is the live state; prior versions are the history.
--
-- `decisions_log` is the exception — its rows are append-only: each commit
-- becomes a NEW row with the next version number, never overwriting prior
-- ones. The full log is `SELECT * WHERE section='decisions_log' ORDER BY
-- version`.
--
-- Section model (mirrors what modlix-apps used as folders before retirement):
--   overview         — one-paragraph "what is this app", read every session
--   current_focus    — what's being worked on right now, read every session
--   inventory        — components / pages / storages / functions present
--   conventions      — app-specific naming / theming / binding patterns
--   roadmap          — what's next, what's blocked, who's doing what
--   decisions_log    — append-only record of decisions and reasoning
--
-- See app/agents/appbuilder/tools/kb_app.py for the propose-then-confirm
-- write flow used by the agent.

CREATE TABLE IF NOT EXISTS `cfa_app_kb` (
    `ID`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `CLIENT_CODE`   VARCHAR(64)     NOT NULL COMMENT 'Owning client (tenant) code',
    `APP_CODE`      VARCHAR(64)     NOT NULL COMMENT 'Target appCode being built',
    `SECTION`       VARCHAR(32)     NOT NULL COMMENT 'overview|current_focus|inventory|conventions|roadmap|decisions_log',
    `BODY`          MEDIUMTEXT      NOT NULL,
    `BODY_HASH`     CHAR(64)        NOT NULL COMMENT 'SHA-256 of BODY, for no-op detection on migration / promotion',
    `VERSION`       INT UNSIGNED    NOT NULL COMMENT 'Monotonic per (client, app, section)',
    `UPDATED_BY`    BIGINT UNSIGNED NOT NULL COMMENT 'userId from JWT at write time',
    `UPDATED_AT`    TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    `MESSAGE`       VARCHAR(512)    DEFAULT NULL COMMENT 'Commit-style note from the agent',
    PRIMARY KEY (`ID`),
    UNIQUE KEY `UQ_SECTION_VERSION` (`CLIENT_CODE`, `APP_CODE`, `SECTION`, `VERSION`),
    INDEX `IDX_CURRENT` (`CLIENT_CODE`, `APP_CODE`, `SECTION`, `VERSION` DESC),
    INDEX `IDX_USER` (`UPDATED_BY`),
    FULLTEXT KEY `FT_BODY` (`BODY`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
