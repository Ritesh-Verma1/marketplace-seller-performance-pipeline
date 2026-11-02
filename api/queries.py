"""All SQL used by the API. Every statement is a constant with :named bind parameters
(no string concatenation of user input) and every aggregation happens inside the database.

The SQL is deliberately limited to the portable subset of MySQL 8 / SQLite (CASE, COALESCE, NULLIF, CTEs,
LIMIT/OFFSET) so it can be unit-tested without a MySQL server (tests/test_api_sql.py).
"""

HEALTH = "SELECT 1"

# ----------------------------------------------------------------------------- /orders/{order_id}
ORDER_HEADER = """
SELECT order_id, customer_unique_id, customer_id, order_status, purchase_ts, approved_ts, carrier_ts,
       delivered_ts, estimated_delivery_date, customer_state, customer_region, is_revenue_eligible,
       is_on_time, delivery_business_days, items_count, items_price_brl, items_freight_brl, items_gross_brl,
       gross_usd, payments_total_brl, voucher_total_brl, net_cash_paid_brl, payment_mismatch,
       fx_rate, fx_rate_date, review_score
FROM fact_order WHERE order_id = :order_id
"""
ORDER_ITEMS = """
SELECT order_item_id, product_id, seller_id, category_en, shipping_limit_ts, price_brl, freight_brl, gross_brl,
       gross_usd, seller_tier, commission_rate, commission_rate_source, commission_brl, commission_usd,
       distance_km, campaign_id
FROM fact_order_item WHERE order_id = :order_id ORDER BY order_item_id
"""
ORDER_SELLERS = """
SELECT seller_id, items_count, price_total_brl, earliest_shipping_limit_ts, sla_result, review_score
FROM fact_order_seller WHERE order_id = :order_id ORDER BY seller_id
"""
ORDER_DQ = """
SELECT rule_id, entity_type, entity_id, issue_type, details, run_id
FROM data_quality_issue
WHERE (entity_id = :order_id OR entity_id LIKE :item_prefix)
  AND run_id = (SELECT run_id FROM data_quality_issue
                WHERE entity_id = :order_id OR entity_id LIKE :item_prefix
                ORDER BY detected_at DESC, issue_id DESC LIMIT 1)
ORDER BY rule_id, issue_id
LIMIT :limit
"""
ORDER_EXCLUDED = """
SELECT issue_type, details FROM data_quality_issue
WHERE rule_id = 'R2' AND entity_type = 'order' AND entity_id = :order_id
ORDER BY issue_id DESC LIMIT 1
"""

# ----------------------------------------------------------------------------- /sellers/{id}/scorecard
SELLER_EXISTS = "SELECT seller_id, state, region FROM dim_seller WHERE seller_id = :seller_id"
SELLER_TIER = """
SELECT tier, prev_month_revenue_brl FROM seller_month_tier
WHERE seller_id = :seller_id AND `year_month` = :ym
"""
SELLER_ITEMS_AGG = """
SELECT COUNT(*) AS item_count,
       COALESCE(SUM(gross_brl), 0) AS gross_brl,
       COALESCE(SUM(gross_usd), 0) AS gross_usd,
       COALESCE(SUM(commission_brl), 0) AS commission_brl,
       COALESCE(SUM(commission_usd), 0) AS commission_usd
FROM fact_order_item
WHERE seller_id = :seller_id AND `year_month` = :ym AND is_revenue_eligible = 1
"""
SELLER_ORDERS_AGG = """
SELECT COUNT(*) AS delivered_orders,
       COALESCE(SUM(CASE WHEN sla_result IS NOT NULL THEN 1 ELSE 0 END), 0) AS sla_orders,
       COALESCE(SUM(CASE WHEN sla_result = 'on_time' THEN 1 ELSE 0 END), 0) AS on_time_orders,
       COALESCE(SUM(CASE WHEN sla_result = 'seller_late' THEN 1 ELSE 0 END), 0) AS seller_late_count,
       ROUND(100.0 * SUM(CASE WHEN sla_result = 'on_time' THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN sla_result IS NOT NULL THEN 1 ELSE 0 END), 0), 2) AS on_time_pct,
       ROUND(AVG(review_score), 2) AS avg_review_score
FROM fact_order_seller
WHERE seller_id = :seller_id AND `year_month` = :ym AND is_revenue_eligible = 1
"""

# ----------------------------------------------------------------------------- /sellers/top
TOP_SELLERS = {
    "commission_usd": """
        SELECT seller_id, SUM(commission_usd) AS metric_value, COUNT(*) AS basis_count
        FROM fact_order_item
        WHERE `year_month` = :ym AND is_revenue_eligible = 1 AND commission_usd IS NOT NULL
        GROUP BY seller_id
        ORDER BY metric_value DESC, seller_id ASC
        LIMIT :limit OFFSET :offset
    """,
    # only sellers with >= 10 delivered orders in the month are ranked (basis_count = delivered orders)
    "on_time_rate": """
        SELECT seller_id,
               ROUND(100.0 * SUM(CASE WHEN sla_result = 'on_time' THEN 1 ELSE 0 END)
                     / NULLIF(SUM(CASE WHEN sla_result IS NOT NULL THEN 1 ELSE 0 END), 0), 2) AS metric_value,
               COUNT(*) AS basis_count
        FROM fact_order_seller
        WHERE `year_month` = :ym AND is_revenue_eligible = 1
        GROUP BY seller_id
        HAVING COUNT(*) >= 10 AND SUM(CASE WHEN sla_result IS NOT NULL THEN 1 ELSE 0 END) > 0
        ORDER BY metric_value DESC, seller_id ASC
        LIMIT :limit OFFSET :offset
    """,
    "avg_review": """
        SELECT seller_id, ROUND(AVG(review_score), 2) AS metric_value, COUNT(*) AS basis_count
        FROM fact_order_seller
        WHERE `year_month` = :ym AND is_revenue_eligible = 1
        GROUP BY seller_id
        HAVING COUNT(*) >= 10 AND COUNT(review_score) > 0
        ORDER BY metric_value DESC, seller_id ASC
        LIMIT :limit OFFSET :offset
    """,
}

# ----------------------------------------------------------------------------- /kpis/monthly
KPI_FILTER = """
  is_revenue_eligible = 1 AND `year_month` >= :start AND `year_month` <= :end
  AND (:region IS NULL OR customer_region = :region)
"""
KPIS_MONTHLY = f"""
WITH o AS (
  SELECT `year_month`,
         COUNT(*) AS orders,
         COUNT(DISTINCT customer_unique_id) AS unique_customers,
         COALESCE(SUM(net_cash_paid_brl), 0) AS net_cash_paid_brl,
         ROUND(100.0 * SUM(CASE WHEN is_on_time = 1 THEN 1 ELSE 0 END)
               / NULLIF(SUM(CASE WHEN is_on_time IS NOT NULL THEN 1 ELSE 0 END), 0), 2) AS on_time_pct,
         ROUND(AVG(delivery_business_days), 2) AS avg_delivery_business_days
  FROM fact_order
  WHERE {KPI_FILTER}
  GROUP BY `year_month`
), i AS (
  SELECT `year_month`, SUM(gross_usd) AS gross_usd
  FROM fact_order_item
  WHERE {KPI_FILTER}
  GROUP BY `year_month`
)
SELECT o.`year_month`, o.orders, o.unique_customers, COALESCE(i.gross_usd, 0) AS gross_usd,
       o.net_cash_paid_brl, o.on_time_pct, o.avg_delivery_business_days
FROM o LEFT JOIN i ON i.`year_month` = o.`year_month`
ORDER BY o.`year_month`
LIMIT :limit OFFSET :offset
"""
KPIS_MONTH_COUNT = f"""
SELECT COUNT(DISTINCT `year_month`) AS total FROM fact_order WHERE {KPI_FILTER}
"""
REGIONS = "SELECT DISTINCT region FROM ref_state ORDER BY region"

# ----------------------------------------------------------------------------- /campaigns/{id}/performance
CAMPAIGN = """
SELECT campaign_id, name, start_ts, end_ts, window_days, priority, all_geo, all_categories
FROM dim_campaign WHERE campaign_id = :cid
"""
CAMPAIGN_TARGETS = "SELECT target_type, target_value FROM dim_campaign_target WHERE campaign_id = :cid"

CAMPAIGN_WINDOW = """
SELECT COUNT(*) AS items_attributed,
       COALESCE(SUM(CASE WHEN is_revenue_eligible = 1 THEN 1 ELSE 0 END), 0) AS items,
       COUNT(DISTINCT CASE WHEN is_revenue_eligible = 1 THEN order_id END) AS orders,
       COALESCE(SUM(CASE WHEN is_revenue_eligible = 1 THEN gross_usd ELSE 0 END), 0) AS gross_usd
FROM fact_order_item
WHERE campaign_id = :cid
"""
# Baseline: the same number of days immediately before the campaign, restricted to the campaign's own
# target scope (states/regions and categories) so we compare like with like. Revenue-eligible items only.
CAMPAIGN_BASELINE = """
SELECT COUNT(*) AS items,
       COUNT(DISTINCT order_id) AS orders,
       COALESCE(SUM(gross_usd), 0) AS gross_usd
FROM fact_order_item
WHERE is_revenue_eligible = 1
  AND purchase_ts >= :b_start AND purchase_ts < :b_end
  AND (:all_geo = 1
       OR customer_state IN (SELECT target_value FROM dim_campaign_target
                             WHERE campaign_id = :cid AND target_type = 'state')
       OR LOWER(customer_region) IN (SELECT target_value FROM dim_campaign_target
                                     WHERE campaign_id = :cid AND target_type = 'region'))
  AND (:all_categories = 1
       OR category_en IN (SELECT target_value FROM dim_campaign_target
                          WHERE campaign_id = :cid AND target_type = 'category'))
"""

# ----------------------------------------------------------------------------- /data-quality/summary
LATEST_RUN = "SELECT run_id FROM pipeline_run WHERE status = 'success' ORDER BY started_at DESC LIMIT 1"
RUN_EXISTS = "SELECT run_id, status, range_start, range_end FROM pipeline_run WHERE run_id = :run_id"
DQ_SUMMARY = """
SELECT rule_id, issue_type, COUNT(*) AS issue_count
FROM data_quality_issue
WHERE run_id = :run_id
GROUP BY rule_id, issue_type
ORDER BY LENGTH(rule_id), rule_id, issue_type
LIMIT :limit OFFSET :offset
"""
DQ_TOTAL_ISSUES = "SELECT COUNT(*) AS total_issues FROM data_quality_issue WHERE run_id = :run_id"
DQ_TOTAL_GROUPS = """
SELECT COUNT(*) AS total_groups FROM (
  SELECT rule_id, issue_type FROM data_quality_issue WHERE run_id = :run_id GROUP BY rule_id, issue_type
) g
"""
