-- Session-level outcome scores computed post-session
CREATE TABLE IF NOT EXISTS `ai_learning_session_scores` (
    `ID`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `SESSION_ID`        VARCHAR(64) NOT NULL,
    `AGENT_NAME`        VARCHAR(64) DEFAULT NULL,
    `CLIENT_CODE`       CHAR(8) NOT NULL,
    `SUCCESS_SCORE`     FLOAT DEFAULT NULL COMMENT '0.0 to 1.0 composite success metric',
    `USER_SATISFACTION` FLOAT DEFAULT NULL COMMENT 'Average user rating for session',
    `TOOL_ERROR_RATE`   FLOAT DEFAULT NULL COMMENT 'Fraction of tool calls that failed',
    `TURN_COUNT`        INT UNSIGNED DEFAULT 0,
    `TOOL_CALL_COUNT`   INT UNSIGNED DEFAULT 0,
    `RETRY_COUNT`       INT UNSIGNED DEFAULT 0 COMMENT 'Times user retried same request',
    `UNDO_COUNT`        INT UNSIGNED DEFAULT 0 COMMENT 'Times user undid agent work',
    `ABANDONED`         TINYINT(1) DEFAULT 0 COMMENT 'User abandoned mid-session',
    `TOTAL_TOKENS`      BIGINT UNSIGNED DEFAULT 0,
    `TOTAL_LATENCY_MS`  BIGINT UNSIGNED DEFAULT 0,
    `SCORE_VERSION`     VARCHAR(16) DEFAULT 'v1' COMMENT 'Scoring algorithm version',
    `COMPUTED_AT`       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`ID`),
    UNIQUE KEY `UK_SESSION_SCORE` (`SESSION_ID`, `SCORE_VERSION`),
    INDEX `IDX_SUCCESS` (`SUCCESS_SCORE`),
    INDEX `IDX_AGENT` (`AGENT_NAME`),
    CONSTRAINT `FK_SCORE_SESSION` FOREIGN KEY (`SESSION_ID`)
        REFERENCES `ai_tracking_sessions` (`SESSION_ID`) ON DELETE CASCADE
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
