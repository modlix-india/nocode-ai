-- V2__Add_Object_And_Agent_Name.sql
-- Replace PAGE_NAME with OBJECT_NAME and add AGENT_NAME to ai_tracking_sessions
-- OBJECT_NAME: The name of the object being tracked (e.g., page name, function name)
-- AGENT_NAME: The type of agent creating the session (e.g., PageAgent, FunctionAgent)

ALTER TABLE `ai_tracking_sessions`
    DROP COLUMN `PAGE_NAME`,
    ADD COLUMN `OBJECT_NAME` VARCHAR(256) DEFAULT NULL COMMENT 'Name of the object being tracked (page name, function name, etc.)' AFTER `USER_ID`,
    ADD COLUMN `AGENT_NAME` VARCHAR(64) DEFAULT NULL COMMENT 'Type of agent (PageAgent, FunctionAgent, etc.)' AFTER `OBJECT_NAME`,
    ADD INDEX `IDX_OBJECT_NAME` (`OBJECT_NAME`),
    ADD INDEX `IDX_AGENT_NAME` (`AGENT_NAME`);
