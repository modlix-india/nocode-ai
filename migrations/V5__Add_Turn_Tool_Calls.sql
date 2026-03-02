-- V5__Add_Turn_Tool_Calls.sql
-- Store full tool call log per turn for training data and audit.

ALTER TABLE ai_session_history
    ADD COLUMN TOOL_CALLS_JSON LONGTEXT NULL AFTER ASSISTANT_SUMMARY;
