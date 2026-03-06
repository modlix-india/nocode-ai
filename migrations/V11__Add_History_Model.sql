-- Add MODEL column to ai_session_history to track which LLM model was used per turn
ALTER TABLE ai_session_history
    ADD COLUMN MODEL VARCHAR(64) NULL AFTER TOOL_CALLS_JSON;
