-- Explicit user feedback on agent responses (thumbs up/down, corrections)
CREATE TABLE IF NOT EXISTS `ai_learning_feedback` (
    `ID`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `SESSION_ID`        VARCHAR(64) NOT NULL,
    `TURN_NUMBER`       INT UNSIGNED NOT NULL COMMENT 'Which turn is being rated',
    `CLIENT_CODE`       CHAR(8) NOT NULL,
    `USER_ID`           BIGINT UNSIGNED NOT NULL,
    `AGENT_NAME`        VARCHAR(64) DEFAULT NULL,
    `RATING`            TINYINT NOT NULL COMMENT '-1=thumbs down, 0=neutral, 1=thumbs up',
    `FEEDBACK_TEXT`     TEXT DEFAULT NULL COMMENT 'Optional free-text correction or explanation',
    `FEEDBACK_TYPE`     ENUM('RATING', 'CORRECTION', 'RETRY', 'UNDO', 'ABANDONMENT') DEFAULT 'RATING',
    `USER_INSTRUCTION`  TEXT DEFAULT NULL COMMENT 'Denormalized from ai_session_history',
    `ASSISTANT_SUMMARY` TEXT DEFAULT NULL COMMENT 'Denormalized from ai_session_history',
    `TOOL_CALLS_JSON`   LONGTEXT DEFAULT NULL COMMENT 'Denormalized from ai_session_history',
    `CREATED_AT`        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`ID`),
    INDEX `IDX_SESSION` (`SESSION_ID`),
    INDEX `IDX_RATING` (`RATING`),
    INDEX `IDX_AGENT_CREATED` (`AGENT_NAME`, `CREATED_AT`),
    CONSTRAINT `FK_FEEDBACK_SESSION` FOREIGN KEY (`SESSION_ID`)
        REFERENCES `ai_tracking_sessions` (`SESSION_ID`) ON DELETE CASCADE
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Aggregated tool error patterns for pitfall detection
CREATE TABLE IF NOT EXISTS `ai_learning_tool_errors` (
    `ID`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `AGENT_NAME`        VARCHAR(64) NOT NULL,
    `TOOL_NAME`         VARCHAR(128) NOT NULL,
    `ERROR_PATTERN`     VARCHAR(512) NOT NULL COMMENT 'Normalized error message pattern',
    `OCCURRENCE_COUNT`  INT UNSIGNED DEFAULT 1,
    `LAST_SEEN_AT`      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `EXAMPLE_INPUT_JSON` TEXT DEFAULT NULL COMMENT 'Example input that caused this error',
    `RESOLUTION`        TEXT DEFAULT NULL COMMENT 'Known fix or workaround',
    `STATUS`            ENUM('ACTIVE', 'RESOLVED', 'IGNORED') DEFAULT 'ACTIVE',
    `CREATED_AT`        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `UPDATED_AT`        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`ID`),
    UNIQUE KEY `UK_TOOL_ERROR` (`AGENT_NAME`, `TOOL_NAME`, `ERROR_PATTERN`),
    INDEX `IDX_COUNT` (`OCCURRENCE_COUNT` DESC)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
