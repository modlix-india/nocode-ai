-- Add TITLE column for session display in sidebar (ChatGPT-like UI)
ALTER TABLE ai_tracking_sessions
    ADD COLUMN TITLE VARCHAR(256) NULL AFTER APP_CODE;
