-- Links to the files a session was given or produced.
--
-- The BYTES live in the files service (Cloudflare R2, mirrored by
-- `files.files_file_system`). This table only records WHERE they went and for
-- HOW LONG, so a reopened chat can render what the user attached instead of
-- showing nothing.
--
-- Before this, an attachment existed only as base64 in the request body and in
-- the in-memory conversation: `build_image_blocks` compressed it for the model,
-- `_drop_old_images` stripped it from history after about three turns, and
-- `_restore_conversation_history` rebuilt a resumed session as plain text. The
-- image was gone and there was nothing left to point at.
--
-- A separate table rather than a column on `ai_session_history`, for three
-- reasons that each stand alone:
--
--   * That row is UPSERTED, and its update list has already destroyed data once
--     (a cancelled turn wrote NULL over TOOL_CALLS_JSON because the assignment
--     was unguarded). Every column added there needs its own COALESCE guard and
--     getting one wrong loses data on the cancel path.
--   * Attachments are known at ROUTE time, before the history row exists -- it
--     is created by the first incremental save inside the agent loop. Writing
--     them into that row means a second upsert racing the real one.
--   * A generated image arrives mid-turn from the tool layer, which has no
--     business touching the turn's history row.
--
-- KIND is what keeps retention away from generated work: 'chat' files carry a
-- lifetime, 'generated' files never do. See EXPIRES_AFTER_MINUTES below.

CREATE TABLE IF NOT EXISTS `ai_session_attachment` (
    `ID`                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `SESSION_ID`            VARCHAR(64)     NOT NULL,
    `TURN_NUMBER`           INT UNSIGNED    NOT NULL,
    `KIND`                  VARCHAR(16)     NOT NULL DEFAULT 'chat' COMMENT 'chat = the user attached it, generated = a tool made it',
    `ATTACHMENT_TYPE`       VARCHAR(16)     NOT NULL DEFAULT 'image' COMMENT 'image|file, as the client sent it',
    `NAME`                  VARCHAR(255)    NOT NULL COMMENT 'Original file name, sanitised',
    `MIME_TYPE`             VARCHAR(127)    DEFAULT NULL,
    `STORE`                 VARCHAR(16)     NOT NULL DEFAULT 'secured' COMMENT 'secured|static, which files-service store holds it',
    -- Client-qualified path, exactly as FileDetail.filePath returns it. This is
    -- the only handle anything has for deleting the file later, which is why
    -- the WhatsApp equivalent keeps it among the four fields it retains.
    --
    -- 512 rather than 768 because this column is half of a composite UNIQUE
    -- key: InnoDB allows 3072 bytes and utf8mb4 costs 4 per character, so
    -- SESSION_ID(64) + FILE_PATH(768) = 3328 is refused outright. At 512 the
    -- key is 2304 bytes. Real paths run to about 170 characters
    -- (/_withInClient/aichat/{app}/{session}/t{n}/{uuid}-{name}), so the
    -- ceiling is nowhere near.
    `FILE_PATH`             VARCHAR(512)    NOT NULL,
    -- Root-relative URL the browser can fetch, as FileDetail.url returns it.
    `FILE_URL`              VARCHAR(768)    NOT NULL,
    `SIZE_BYTES`            BIGINT UNSIGNED DEFAULT NULL,
    -- Mirrors files_file_system.EXPIRES_AFTER_MINUTES, deliberately: same name,
    -- same units, same meaning. NULL is not "expire by default" and not
    -- "expire immediately", it is NEVER -- in both tables.
    --
    -- The files service measures the lifetime from its own UPDATED_AT, not from
    -- UPLOADED_AT here. The two agree only because nothing overwrites these
    -- files: every name carries a uuid prefix, so `override` is never needed
    -- and no re-write ever resets the file's clock without resetting ours.
    `EXPIRES_AFTER_MINUTES` INT UNSIGNED    DEFAULT NULL,
    `UPLOADED_AT`           TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`ID`),
    INDEX `IDX_ATT_SESSION_TURN` (`SESSION_ID`, `TURN_NUMBER`),
    UNIQUE KEY `UQ_ATT_SESSION_PATH` (`SESSION_ID`, `FILE_PATH`),
    CONSTRAINT `FK_ATT_SESSION` FOREIGN KEY (`SESSION_ID`)
        REFERENCES `ai_tracking_sessions` (`SESSION_ID`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
