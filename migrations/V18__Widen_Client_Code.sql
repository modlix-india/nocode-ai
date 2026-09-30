-- Client codes widened from CHAR(8) to CHAR(12), and the four VARCHAR(64) ones
-- brought back in line.
--
-- `ClientDAO.getValidClientCode` builds a code from at most the first five
-- characters of the client's name and appends a collision counter -- KAILA,
-- KAILA1, KAILA2 -- so a popular name stem eats the namespace one registration
-- at a time, and CHAR(8) left room for only three digits. Measured before the
-- change: 304 clients already sat at exactly eight characters, KAILA alone held
-- 294 of its 999, and the code after KAILA999 is nine characters, which strict
-- mode rejects outright with `ERROR 1406: Data too long`.
--
-- CHAR rather than VARCHAR, and 12 rather than 64: a client code is a short
-- uppercase token, and CHAR(64) in utf8mb4 would reserve 256 bytes per row
-- across ~70 columns to hold five to eight characters. Twelve gives the counter
-- seven digits, and stays under the thirteen-character limit in
-- `SecuredFileResourceService.checkReadAccessWithClientCode`, so no deploy
-- order between this and that parser fix can break secured file access.
--
-- The second half of this file is the interesting one. V1 of this service used
-- CHAR(8) like every other schema; V12 and V13 used VARCHAR(64), with no
-- comment saying why, in files where the column sits directly beside
-- `APP_CODE VARCHAR(64)`. It reads as the width having been copied from the
-- neighbouring column rather than chosen, and no row in any of the four has
-- ever held more than seven characters. Narrowing them to CHAR(12) is safe for
-- that reason, and leaves one width for this value everywhere instead of two.
--
-- App codes stay VARCHAR(64) and are deliberately untouched:
-- `AppDAO.generateAppCode` appends a random base36 suffix and regenerates on
-- collision rather than counting, real app codes already reach 27 characters,
-- and there is no ceiling to run into.

ALTER TABLE `ai_learning_feedback`
    MODIFY COLUMN `CLIENT_CODE` CHAR(12) NOT NULL;
ALTER TABLE `ai_learning_session_scores`
    MODIFY COLUMN `CLIENT_CODE` CHAR(12) NOT NULL;
ALTER TABLE `ai_tracking_sessions`
    MODIFY COLUMN `CLIENT_CODE` CHAR(12) NOT NULL;
ALTER TABLE `ai_tracking_token_usage`
    MODIFY COLUMN `CLIENT_CODE` CHAR(12) NOT NULL;

-- Were VARCHAR(64); see the note above.
ALTER TABLE `cfa_app_kb`
    MODIFY COLUMN `CLIENT_CODE` CHAR(12) NOT NULL COMMENT 'Owning client (tenant) code';
ALTER TABLE `lore_curation_run`
    MODIFY COLUMN `CLIENT_CODE` CHAR(12) NOT NULL;
ALTER TABLE `lore_entry`
    MODIFY COLUMN `CLIENT_CODE` CHAR(12) NOT NULL;
ALTER TABLE `lore_observation`
    MODIFY COLUMN `CLIENT_CODE` CHAR(12) NOT NULL COMMENT 'Owning client (tenant) code';
