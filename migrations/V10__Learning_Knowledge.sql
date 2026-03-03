-- Extracted knowledge entries (patterns, pitfalls, examples, lessons)
CREATE TABLE IF NOT EXISTS `ai_learning_knowledge` (
    `ID`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `KNOWLEDGE_TYPE`    ENUM('PATTERN', 'PITFALL', 'EXAMPLE', 'LESSON') NOT NULL,
    `AGENT_NAME`        VARCHAR(64) NOT NULL,
    `CATEGORY`          VARCHAR(128) DEFAULT NULL COMMENT 'e.g. page_creation, styling, form_building',
    `TITLE`             VARCHAR(512) NOT NULL COMMENT 'Short description for retrieval',
    `CONTENT`           LONGTEXT NOT NULL COMMENT 'Full knowledge entry, prompt-injectable text',
    `SOURCE_SESSION_IDS` TEXT DEFAULT NULL COMMENT 'Comma-separated session IDs this was derived from',
    `TOOL_SEQUENCE_JSON` LONGTEXT DEFAULT NULL COMMENT 'JSON array of tool names in order',
    `RELEVANCE_SCORE`   FLOAT DEFAULT 1.0 COMMENT 'How useful this entry is (decayed over time)',
    `USE_COUNT`         INT UNSIGNED DEFAULT 0 COMMENT 'Times injected into a prompt',
    `POSITIVE_FEEDBACK_COUNT` INT UNSIGNED DEFAULT 0,
    `NEGATIVE_FEEDBACK_COUNT` INT UNSIGNED DEFAULT 0,
    `STATUS`            ENUM('ACTIVE', 'DEPRECATED', 'PENDING_REVIEW') DEFAULT 'ACTIVE',
    `CREATED_AT`        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `UPDATED_AT`        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`ID`),
    INDEX `IDX_AGENT_TYPE` (`AGENT_NAME`, `KNOWLEDGE_TYPE`),
    INDEX `IDX_STATUS` (`STATUS`),
    INDEX `IDX_RELEVANCE` (`RELEVANCE_SCORE` DESC),
    FULLTEXT INDEX `FT_TITLE_CONTENT` (`TITLE`, `CONTENT`)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
