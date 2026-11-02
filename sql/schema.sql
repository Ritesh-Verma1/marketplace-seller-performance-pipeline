-- Marketplace Seller Performance Pipeline : MySQL 8.x DDL
-- Idempotent (CREATE TABLE IF NOT EXISTS); executed automatically by the pipeline.
-- NOTE: keep statements separated by ";" at end of line and do not put ";" inside comments.
-- Money = DECIMAL(x,2), rates = DECIMAL(x,6). No FLOAT/DOUBLE anywhere (R7).

CREATE TABLE IF NOT EXISTS pipeline_run (
  run_id        CHAR(36)     NOT NULL,
  range_start   DATE         NULL,
  range_end     DATE         NULL,
  parameters    JSON         NOT NULL,
  started_at    DATETIME(3)  NOT NULL,
  finished_at   DATETIME(3)  NULL,
  status        VARCHAR(16)  NOT NULL,
  row_counts    JSON         NULL,
  error_message TEXT         NULL,
  PRIMARY KEY (run_id),
  KEY ix_run_status_started (status, started_at),
  KEY ix_run_range (range_start, range_end)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS data_quality_issue (
  issue_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  rule_id     VARCHAR(10)  NOT NULL,
  entity_type VARCHAR(30)  NOT NULL,
  entity_id   VARCHAR(80)  NOT NULL,
  issue_type  VARCHAR(60)  NOT NULL,
  details     VARCHAR(500) NULL,
  detected_at DATETIME(3)  NOT NULL,
  run_id      CHAR(36)     NOT NULL,
  PRIMARY KEY (issue_id),
  KEY ix_dq_run_rule (run_id, rule_id, issue_type),
  KEY ix_dq_entity (entity_type, entity_id),
  CONSTRAINT fk_dq_run FOREIGN KEY (run_id) REFERENCES pipeline_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- reference
CREATE TABLE IF NOT EXISTS ref_fx_rate (
  rate_date DATE          NOT NULL,
  brl_usd   DECIMAL(12,6) NOT NULL,
  PRIMARY KEY (rate_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ref_holiday (
  holiday_id   INT          NOT NULL AUTO_INCREMENT,
  holiday_date DATE         NOT NULL,
  state_code   VARCHAR(2)   NOT NULL DEFAULT '',   -- '' = national
  scope        VARCHAR(10)  NOT NULL,              -- national / regional
  local_name   VARCHAR(120) NOT NULL,
  name         VARCHAR(120) NULL,
  holiday_type VARCHAR(60)  NULL,
  PRIMARY KEY (holiday_id),
  UNIQUE KEY uq_holiday (holiday_date, state_code, local_name),
  KEY ix_holiday_date (holiday_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ref_state (
  state_code CHAR(2)      NOT NULL,
  state_name VARCHAR(60)  NOT NULL,
  region     VARCHAR(20)  NOT NULL,
  PRIMARY KEY (state_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ref_commission_rate (
  rate_id        INT          NOT NULL AUTO_INCREMENT,
  tier           VARCHAR(10)  NOT NULL,
  rate           DECIMAL(9,6) NOT NULL,
  effective_from DATE         NOT NULL,
  effective_to   DATE         NULL,                 -- NULL = open ended
  PRIMARY KEY (rate_id),
  KEY ix_rate_tier (tier, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ref_category_override (
  override_id    INT          NOT NULL AUTO_INCREMENT,
  category_en    VARCHAR(80)  NOT NULL,
  fixed_rate     DECIMAL(9,6) NOT NULL,
  effective_from DATE         NOT NULL,
  effective_to   DATE         NULL,
  notes          VARCHAR(200) NULL,
  PRIMARY KEY (override_id),
  KEY ix_override_cat (category_en, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- dimensions
CREATE TABLE IF NOT EXISTS dim_date (
  date_value          DATE        NOT NULL,
  year                SMALLINT    NOT NULL,
  month               TINYINT     NOT NULL,
  day                 TINYINT     NOT NULL,
  `year_month`          CHAR(7)     NOT NULL,
  day_of_week         TINYINT     NOT NULL,         -- 0 = Monday
  is_weekend          TINYINT     NOT NULL,
  is_national_holiday TINYINT     NOT NULL,
  holiday_name        VARCHAR(120) NULL,
  PRIMARY KEY (date_value),
  KEY ix_date_ym (`year_month`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dim_customer (
  customer_unique_id VARCHAR(32) NOT NULL,           -- R1
  home_state         CHAR(2)     NULL,               -- state on earliest order (R1)
  home_state_name    VARCHAR(60) NULL,
  home_region        VARCHAR(20) NULL,
  first_order_ts     DATETIME    NULL,
  PRIMARY KEY (customer_unique_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dim_seller (
  seller_id   VARCHAR(32)  NOT NULL,
  zip_prefix  CHAR(5)      NULL,
  city        VARCHAR(80)  NULL,
  state       CHAR(2)      NULL,
  state_name  VARCHAR(60)  NULL,
  region      VARCHAR(20)  NULL,
  lat         DECIMAL(9,6) NULL,
  lng         DECIMAL(9,6) NULL,
  PRIMARY KEY (seller_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dim_product (
  product_id      VARCHAR(32) NOT NULL,
  category_pt     VARCHAR(80) NULL,
  category_en     VARCHAR(80) NOT NULL,              -- R8 (unknown / uncategorized kept distinct)
  category_source VARCHAR(20) NOT NULL,              -- patch / kaggle / missing_translation / none
  PRIMARY KEY (product_id),
  KEY ix_product_cat (category_en)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dim_campaign (
  campaign_id    VARCHAR(20)  NOT NULL,
  name           VARCHAR(100) NOT NULL,
  start_ts       DATETIME     NOT NULL,              -- Sao Paulo local time, inclusive
  end_ts         DATETIME     NOT NULL,              -- Sao Paulo local time, inclusive
  window_days    INT          NOT NULL,
  priority       INT          NOT NULL,
  all_geo        TINYINT      NOT NULL,              -- states or regions = ALL
  all_categories TINYINT      NOT NULL,
  PRIMARY KEY (campaign_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dim_campaign_target (
  campaign_id  VARCHAR(20) NOT NULL,
  target_type  VARCHAR(10) NOT NULL,                 -- state / region / category
  target_value VARCHAR(80) NOT NULL,
  PRIMARY KEY (campaign_id, target_type, target_value),
  CONSTRAINT fk_target_campaign FOREIGN KEY (campaign_id) REFERENCES dim_campaign (campaign_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- R12 : tier of a seller for a calendar month (derived from previous month revenue)
CREATE TABLE IF NOT EXISTS seller_month_tier (
  seller_id               VARCHAR(32)   NOT NULL,
  `year_month`              CHAR(7)       NOT NULL,
  prev_month_revenue_brl  DECIMAL(14,2) NOT NULL,
  tier                    VARCHAR(10)   NOT NULL,
  PRIMARY KEY (seller_id, `year_month`),
  KEY ix_tier_month (`year_month`, tier),
  CONSTRAINT fk_tier_seller FOREIGN KEY (seller_id) REFERENCES dim_seller (seller_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- facts
CREATE TABLE IF NOT EXISTS fact_order (
  order_id                VARCHAR(32)   NOT NULL,
  customer_unique_id      VARCHAR(32)   NOT NULL,
  customer_id             VARCHAR(32)   NOT NULL,
  order_status            VARCHAR(20)   NOT NULL,
  purchase_ts             DATETIME      NOT NULL,
  purchase_date           DATE          NOT NULL,
  `year_month`              CHAR(7)       NOT NULL,
  approved_ts             DATETIME      NULL,
  carrier_ts              DATETIME      NULL,
  delivered_ts            DATETIME      NULL,
  estimated_delivery_date DATE          NULL,
  customer_zip_prefix     CHAR(5)       NULL,
  customer_state          CHAR(2)       NULL,        -- state of THIS order (delivery state)
  customer_region         VARCHAR(20)   NULL,
  is_revenue_eligible     TINYINT       NOT NULL,    -- R2
  is_on_time              TINYINT       NULL,        -- R10 (NULL when no delivery date, R3)
  delivery_business_days  INT           NULL,        -- R9
  items_count             INT           NOT NULL,
  items_price_brl         DECIMAL(14,2) NOT NULL,
  items_freight_brl       DECIMAL(14,2) NOT NULL,
  items_gross_brl         DECIMAL(14,2) NOT NULL,
  gross_usd               DECIMAL(14,2) NULL,        -- sum of item gross_usd
  payments_total_brl      DECIMAL(14,2) NOT NULL,
  voucher_total_brl       DECIMAL(14,2) NOT NULL,
  net_cash_paid_brl       DECIMAL(14,2) NOT NULL,    -- R5
  payment_mismatch        TINYINT       NOT NULL,
  fx_rate                 DECIMAL(12,6) NULL,        -- R6
  fx_rate_date            DATE          NULL,        -- R6 actual rate date used
  review_score            TINYINT       NULL,        -- R14
  PRIMARY KEY (order_id),
  KEY ix_order_purchase (purchase_ts),
  KEY ix_order_month_region (`year_month`, customer_region, is_revenue_eligible),
  KEY ix_order_customer (customer_unique_id),
  CONSTRAINT fk_order_customer FOREIGN KEY (customer_unique_id) REFERENCES dim_customer (customer_unique_id),
  CONSTRAINT fk_order_date FOREIGN KEY (purchase_date) REFERENCES dim_date (date_value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS fact_order_item (
  order_id               VARCHAR(32)   NOT NULL,
  order_item_id          INT           NOT NULL,
  product_id             VARCHAR(32)   NOT NULL,
  seller_id              VARCHAR(32)   NOT NULL,
  purchase_ts            DATETIME      NOT NULL,
  `year_month`             CHAR(7)       NOT NULL,
  is_revenue_eligible    TINYINT       NOT NULL,
  customer_state         CHAR(2)       NULL,
  customer_region        VARCHAR(20)   NULL,
  category_en            VARCHAR(80)   NOT NULL,
  shipping_limit_ts      DATETIME      NULL,
  price_brl              DECIMAL(12,2) NOT NULL,
  freight_brl            DECIMAL(12,2) NOT NULL,
  gross_brl              DECIMAL(12,2) NOT NULL,     -- R4 price + freight
  fx_rate                DECIMAL(12,6) NULL,
  fx_rate_date           DATE          NULL,
  gross_usd              DECIMAL(14,2) NULL,
  seller_tier            VARCHAR(10)   NULL,         -- R12
  commission_rate        DECIMAL(9,6)  NULL,         -- R13 (NULL = not eligible or no rate found)
  commission_rate_source VARCHAR(20)   NULL,         -- category_override / tier
  commission_brl         DECIMAL(14,2) NULL,
  commission_usd         DECIMAL(14,2) NULL,
  distance_km            DECIMAL(10,3) NULL,         -- R16
  campaign_id            VARCHAR(20)   NULL,         -- R18
  PRIMARY KEY (order_id, order_item_id),
  KEY ix_item_seller_month (seller_id, `year_month`, is_revenue_eligible),
  KEY ix_item_month (`year_month`, is_revenue_eligible),
  KEY ix_item_campaign (campaign_id, purchase_ts),
  KEY ix_item_purchase (purchase_ts, is_revenue_eligible),
  CONSTRAINT fk_item_order    FOREIGN KEY (order_id)    REFERENCES fact_order (order_id),
  CONSTRAINT fk_item_seller   FOREIGN KEY (seller_id)   REFERENCES dim_seller (seller_id),
  CONSTRAINT fk_item_product  FOREIGN KEY (product_id)  REFERENCES dim_product (product_id),
  CONSTRAINT fk_item_campaign FOREIGN KEY (campaign_id) REFERENCES dim_campaign (campaign_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- one row per seller within an order (R11 lateness, R14 review attribution)
CREATE TABLE IF NOT EXISTS fact_order_seller (
  order_id                  VARCHAR(32)   NOT NULL,
  seller_id                 VARCHAR(32)   NOT NULL,
  purchase_ts               DATETIME      NOT NULL,
  `year_month`                CHAR(7)       NOT NULL,
  is_revenue_eligible       TINYINT       NOT NULL,
  items_count               INT           NOT NULL,
  price_total_brl           DECIMAL(14,2) NOT NULL,
  earliest_shipping_limit_ts DATETIME     NULL,
  sla_result                VARCHAR(12)   NULL,      -- seller_late / carrier_late / on_time / NULL
  review_score              TINYINT       NULL,
  PRIMARY KEY (order_id, seller_id),
  KEY ix_os_seller_month (seller_id, `year_month`, is_revenue_eligible),
  KEY ix_os_purchase (purchase_ts),
  CONSTRAINT fk_os_order  FOREIGN KEY (order_id)  REFERENCES fact_order (order_id),
  CONSTRAINT fk_os_seller FOREIGN KEY (seller_id) REFERENCES dim_seller (seller_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
