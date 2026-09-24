-- Adzump competitors: one row per (client, product, website).
--
-- A competitor's identity is its canonical website (https://host/path - project
-- pages on one developer site stay separate), else its name while no website is
-- known. V17 keyed rows by name only and stored two shapes in `url` (the curated
-- page URL vs the ads writer's bare host or "name:<slug>"), so the ads writer
-- created a second row under the vendor's page name ("Brigade Group") while the
-- curated "Brigade Avalon" row stayed pending.
--
-- Data: `url` moves to the one canonical form, website-less rows drop the
-- "name:" placeholder to NULL, orphaned host-keyed ads rows go, and duplicates
-- of one website collapse onto the row holding the most creatives (a deleted
-- row's creatives cascade). The next save re-stamps the curated name.

ALTER TABLE `adzump_competitors`
    MODIFY `url` VARCHAR(512) DEFAULT NULL
        COMMENT 'Canonical website (https://host/path); NULL while none is known - the name identifies the row then';

UPDATE `adzump_competitors` SET `url` = NULL WHERE `url` LIKE 'name:%';

UPDATE `adzump_competitors` SET `url` = CONCAT('https://', `url`)
    WHERE `url` IS NOT NULL AND `url` NOT LIKE 'http%';

-- The old ads writer's host-keyed rows (vendor page name, bare host) beside the
-- curated project page they were fetched for are orphans under the new identity.
DELETE `orphan` FROM `adzump_competitors` `orphan`
    JOIN `adzump_competitors` `page`
        ON `page`.`client_code` = `orphan`.`client_code`
       AND `page`.`product_id` = `orphan`.`product_id`
       AND `page`.`id` <> `orphan`.`id`
       AND `page`.`url` LIKE CONCAT(`orphan`.`url`, '/%')
    WHERE `orphan`.`creatives_fetched_at` IS NOT NULL;

DELETE `dup` FROM `adzump_competitors` `dup`
    JOIN `adzump_competitors` `keep`
        ON `keep`.`client_code` = `dup`.`client_code`
       AND `keep`.`product_id` = `dup`.`product_id`
       AND `keep`.`url` = `dup`.`url`
       AND `keep`.`id` <> `dup`.`id`
       AND (`keep`.`total_creatives` > `dup`.`total_creatives`
            OR (`keep`.`total_creatives` = `dup`.`total_creatives` AND `keep`.`id` < `dup`.`id`));

ALTER TABLE `adzump_competitors`
    ADD UNIQUE KEY `uq_adzump_competitors_website` (`client_code`, `product_id`, `url`);
