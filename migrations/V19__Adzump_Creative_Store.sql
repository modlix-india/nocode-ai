-- Adzump creative store: products, competitors, creatives, and creative assets.
--
-- Moves the creative data out of Modlix schemaless storage into nocode-ai's own
-- MySQL so the Creatives page can filter/sort/paginate/count/facet with real SQL.
--
-- Shape:
--   adzump_products         one row per (client, product). Typed Product in `data`,
--                             queried scalars promoted to columns.
--   adzump_flows         one row per (client, product, session, flow). Universal
--                             per-flow session state (new_campaign: the campaign
--                             draft) so any flow resumes from the last point.
--   adzump_competitors      one row per (client, competitor, product). Identity +
--                             fetch-ledger. Upserted on competitor_key (accumulates).
--   adzump_creatives        one row per LOGICAL creative (an ad concept). Shared
--                             attributes only; no binary/asset fields. Competitor
--                             creatives are refreshed WHOLESALE per slice (no
--                             per-creative id), so there is no business-unique key -
--                             the writer's delete-then-insert transaction owns it.
--   adzump_creative_assets  one row per FILE. A creative has N slides (distinct
--                             pictures; a carousel has many), each slide rendered in
--                             M aspect ratios. slide_index groups ratio-variants of
--                             one picture; a different slide_index is a different
--                             picture. Essence is per slide (per distinct visual),
--                             stored once per slide.
--
-- Scope is a client_code column + WHERE, never a header. Bytes live in the Files
-- service; asset rows carry only the URL + facets. No data migration - tables start
-- empty and fetches repopulate them.

-- ── Products ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `adzump_products` (
    `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `client_code`  CHAR(12)     NOT NULL COMMENT 'Owning client (tenant) code',
    `url`          VARCHAR(512) NOT NULL COMMENT 'Product primary_url; stable upsert key',
    `name`         VARCHAR(255) DEFAULT NULL COMMENT 'Business / product name',
    `scale`        VARCHAR(32)  DEFAULT NULL COMMENT 'local|regional|national|international',
    `category`     VARCHAR(64)  DEFAULT NULL COMMENT 'Taxonomy vertical; the competitor-gate yardstick',
    `country_code` CHAR(2)      DEFAULT NULL COMMENT 'ISO-3166 alpha-2, from place',
    `summary`      TEXT         DEFAULT NULL COMMENT 'One-line product summary',
    `data`         JSON         NOT NULL COMMENT 'Full typed Product; the columns above are projections of it',
    `created_by`   BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `updated_by`   BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `created_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_adzump_products` (`client_code`, `url`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Flows ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `adzump_flows` (
    `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `client_code`  CHAR(12)     NOT NULL COMMENT 'Owning client (tenant) code',
    `product_id`   BIGINT UNSIGNED NOT NULL COMMENT 'FK adzump_products.id - the product the flow works on',
    `session_id`   VARCHAR(64)  NOT NULL DEFAULT '' COMMENT 'Chat session that owns this run',
    `flow`         VARCHAR(32)  NOT NULL DEFAULT 'new_campaign' COMMENT 'Which flow this state belongs to',
    `status`       VARCHAR(32)  NOT NULL DEFAULT 'draft'
                    COMMENT 'Flow outcome state (new_campaign: draft|launched). Mirrors the flag, never asserts it',
    `data`         JSON         NOT NULL COMMENT 'The flow''s accumulated state; for new_campaign the campaign draft',
    `created_by`   BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `updated_by`   BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `created_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_adzump_flows` (`client_code`, `product_id`, `session_id`, `flow`),
    KEY `k_adzump_flows_latest` (`client_code`, `product_id`, `flow`, `updated_at`),
    CONSTRAINT `fk_adzump_flows_product` FOREIGN KEY (`product_id`)
        REFERENCES `adzump_products` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Competitors ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `adzump_competitors` (
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `client_code`       CHAR(12)     NOT NULL COMMENT 'Owning client (tenant) code',
    `product_id`        BIGINT UNSIGNED NOT NULL COMMENT 'FK adzump_products.id',
    `name`              VARCHAR(255) NOT NULL COMMENT 'Competitor name; stable upsert key (url is not always found)',
    `url`               VARCHAR(255) DEFAULT NULL COMMENT 'Competitor normalized host; NULL when none was found',
    `logo_url`          TEXT         DEFAULT NULL COMMENT 'Brand logo, Files service asset',
    `location`          VARCHAR(255) DEFAULT NULL COMMENT 'Competitor location',
    `pricing`           VARCHAR(255) DEFAULT NULL COMMENT 'Pricing summary',
    `searched_names`    JSON         DEFAULT NULL COMMENT 'Ad-library names already searched for this record; an uncovered name triggers a fresh search + merge',
    `creatives_fetched_at` TIMESTAMP NULL DEFAULT NULL COMMENT 'When creatives were last fetched; drives is_stale',
    `creative_status`   ENUM('pending','ok','empty','error') NOT NULL DEFAULT 'pending'
                         COMMENT 'pending until the first creatives fetch; empty is a no-ads outcome; error stays retryable',
    `fetched_creatives` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Ads discovered in the last fetch',
    `dropped_creatives` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Removed by verify + gate',
    `total_creatives`   INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Kept',
    `active_creatives`  INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Kept and currently running',
    `created_by`        BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `updated_by`        BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `created_at`        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_adzump_competitors` (`client_code`, `name`, `product_id`),
    CONSTRAINT `fk_adzump_competitors_product` FOREIGN KEY (`product_id`)
        REFERENCES `adzump_products` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Creatives (logical) ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `adzump_creatives` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `client_code`     CHAR(12)     NOT NULL COMMENT 'Owning client (tenant) code, denormalized from parent for scope filtering',
    `competitor_id`   BIGINT UNSIGNED DEFAULT NULL COMMENT 'FK adzump_competitors.id; NULL for generated / uploaded',
    `product_id`      BIGINT UNSIGNED NOT NULL COMMENT 'FK adzump_products.id',
    `format`          ENUM('single','carousel','video','collection') NOT NULL DEFAULT 'single' COMMENT 'Ad shape',
    `source_type`     ENUM('competitor','generated','uploaded') NOT NULL COMMENT 'Origin of the creative',
    `is_public`       TINYINT(1)   NOT NULL DEFAULT 0 COMMENT 'Surfaced in the shared Explore library',
    `is_active`       TINYINT(1)   DEFAULT NULL COMMENT 'Currently running (competitor)',
    `days_running`    INT          DEFAULT NULL COMMENT 'Days live (competitor)',
    `reference_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Times remixed; Explore ranking',
    `content`         JSON         NOT NULL COMMENT 'Ad-level metadata: creativeId, headline, primaryText, cta, landingUrl, metrics',
    `created_by`      BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `updated_by`      BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'userId from JWT, 0 for system',
    `created_at`      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `k_adzump_creatives_client_src` (`client_code`, `source_type`),
    KEY `k_adzump_creatives_comp`   (`competitor_id`),
    KEY `k_adzump_creatives_prod`   (`product_id`),
    KEY `k_adzump_creatives_active` (`client_code`, `is_active`),
    KEY `k_adzump_creatives_days`   (`client_code`, `days_running`),
    KEY `k_adzump_creatives_public` (`is_public`, `reference_count`),
    CONSTRAINT `fk_adzump_creatives_competitor` FOREIGN KEY (`competitor_id`) REFERENCES `adzump_competitors` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_adzump_creatives_product`    FOREIGN KEY (`product_id`)    REFERENCES `adzump_products` (`id`)    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Creative assets (one row per file) ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS `adzump_creative_assets` (
    `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `creative_id`  BIGINT UNSIGNED NOT NULL COMMENT 'FK adzump_creatives.id',
    `slide_index`      INT          NOT NULL DEFAULT 0 COMMENT 'Distinct picture; ratio-variants of one picture share it',
    `aspect_ratio`     ENUM('1:1','4:5','9:16','16:9','1.91:1','2:3','other') NOT NULL DEFAULT 'other'
                        COMMENT "'other' = a competitor ratio outside the standard set; width/height hold the real value",
    `media_type`       ENUM('image','video') NOT NULL DEFAULT 'image',
    `file_url`         TEXT         NOT NULL COMMENT 'Rendered file, Files service asset',
    `thumbnail_url`    TEXT         DEFAULT NULL COMMENT 'Video preview still; NULL for images (file_url is the thumbnail)',
    `width`            INT          DEFAULT NULL,
    `height`           INT          DEFAULT NULL,
    `duration_seconds` FLOAT        DEFAULT NULL COMMENT 'Video duration',
    `content_hash`     CHAR(64)     DEFAULT NULL COMMENT 'md5 of the file bytes; exact-dup key',
    `perceptual_hash`  CHAR(64)     DEFAULT NULL COMMENT 'DCT hash; near-dup key',
    `essence`          JSON         DEFAULT NULL COMMENT 'Per-slide analysis; identical across a slide ratio-variants, stored once per slide',
    `created_at`       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_adzump_creative_assets` (`creative_id`, `slide_index`, `aspect_ratio`),
    KEY `k_adzump_creative_assets_chash` (`content_hash`),
    KEY `k_adzump_creative_assets_phash` (`perceptual_hash`),
    CONSTRAINT `fk_adzump_creative_assets_creative` FOREIGN KEY (`creative_id`)
        REFERENCES `adzump_creatives` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
