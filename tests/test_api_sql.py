"""Runs every API query (api/queries.py) against SQLite loaded with the synthetic model and checks the numbers.
Also validates the Pydantic response models. (FastAPI itself is exercised in test_api_http.py when installed.)"""
import re

import pytest

from api import queries as Q
from tests.api_fixture import RUN_ID, sqlite_conn


@pytest.fixture(scope="module")
def con():
    c = sqlite_conn()
    yield c
    c.close()


def one(con, sql, **p):
    row = con.execute(sql, p).fetchone()
    return dict(row) if row else None


def many(con, sql, **p):
    return [dict(r) for r in con.execute(sql, p).fetchall()]


def test_all_queries_use_only_named_binds_no_interpolation():
    # every SQL constant must be free of %s / ? placeholders and f-string leftovers; KPI filter is a static fragment
    for name in ("ORDER_HEADER", "ORDER_ITEMS", "ORDER_SELLERS", "ORDER_DQ", "SELLER_ORDERS_AGG", "KPIS_MONTHLY",
                 "CAMPAIGN_BASELINE", "DQ_SUMMARY"):
        sql = getattr(Q, name)
        assert "%s" not in sql and "?" not in sql and "{" not in sql, name
    for sql in Q.TOP_SELLERS.values():
        assert "{" not in sql


def test_order_endpoints(con):
    h = one(con, Q.ORDER_HEADER, order_id="O2")
    assert h["customer_region"] == "Sul" and h["delivery_business_days"] == 1 and h["is_on_time"] == 0
    assert h["net_cash_paid_brl"] == 200.0 and h["fx_rate_date"] == "2018-02-09"
    assert len(many(con, Q.ORDER_ITEMS, order_id="O2")) == 2
    sellers = {r["seller_id"]: r["sla_result"] for r in many(con, Q.ORDER_SELLERS, order_id="O2")}
    assert sellers == {"S1": "seller_late", "S2": "carrier_late"}
    assert one(con, Q.ORDER_HEADER, order_id="O3") is None                   # canceled -> not loaded
    ex = one(con, Q.ORDER_EXCLUDED, order_id="O3")
    assert ex["issue_type"] == "excluded_status" and "canceled" in ex["details"]


def test_order_dq_issues_include_item_level_rows(con):
    rows = many(con, Q.ORDER_DQ, order_id="O12", item_prefix="O12#%", limit=100)
    assert {"R16"} <= {r["rule_id"] for r in rows}                           # item-level distance issue
    rows = many(con, Q.ORDER_DQ, order_id="O7", item_prefix="O7#%", limit=100)
    assert "payment_mismatch" in {r["issue_type"] for r in rows}
    assert many(con, Q.ORDER_DQ, order_id="O1", item_prefix="O1#%", limit=100) == []


def test_seller_scorecard(con):
    assert one(con, Q.SELLER_EXISTS, seller_id="S1")["state"] == "SP"
    assert one(con, Q.SELLER_EXISTS, seller_id="NOPE") is None
    t = one(con, Q.SELLER_TIER, seller_id="S1", ym="2018-02")
    assert t["tier"] == "Silver" and t["prev_month_revenue_brl"] == 6000.0
    items = one(con, Q.SELLER_ITEMS_AGG, seller_id="S1", ym="2018-02")
    assert items["item_count"] == 2 and round(items["gross_brl"], 2) == 175.0
    assert round(items["commission_brl"], 2) == 15.5 and round(items["commission_usd"], 2) == 4.84
    o = one(con, Q.SELLER_ORDERS_AGG, seller_id="S1", ym="2018-02")
    assert o["delivered_orders"] == 2 and o["on_time_pct"] == 50.0 and o["seller_late_count"] == 1
    assert o["avg_review_score"] == 3.0


def test_scorecard_for_seller_without_activity_returns_zeros(con):
    items = one(con, Q.SELLER_ITEMS_AGG, seller_id="S2", ym="2018-01")
    assert items["item_count"] == 0 and items["gross_usd"] == 0
    o = one(con, Q.SELLER_ORDERS_AGG, seller_id="S2", ym="2018-01")
    assert o["delivered_orders"] == 0 and o["on_time_pct"] is None and o["seller_late_count"] == 0


def test_top_sellers_commission_usd_ranking_and_pagination(con):
    rows = many(con, Q.TOP_SELLERS["commission_usd"], ym="2018-03", limit=10, offset=0)
    assert [r["seller_id"] for r in rows] == ["S3", "S1", "S2"]
    assert [round(r["metric_value"], 2) for r in rows] == [21.52, 11.63, 4.96]
    page2 = many(con, Q.TOP_SELLERS["commission_usd"], ym="2018-03", limit=1, offset=1)
    assert [r["seller_id"] for r in page2] == ["S1"]


def test_top_sellers_rate_metrics_require_10_delivered_orders(con):
    assert many(con, Q.TOP_SELLERS["on_time_rate"], ym="2018-03", limit=10, offset=0) == []
    assert many(con, Q.TOP_SELLERS["avg_review"], ym="2018-02", limit=10, offset=0) == []
    # sanity: the same statements do rank sellers once the threshold is met
    con.execute("CREATE TEMP TABLE t AS SELECT * FROM fact_order_seller")
    for n in range(12):
        con.execute("INSERT INTO fact_order_seller VALUES (?,?,?,?,1,1,10,NULL,?,5)",
                    (f"X{n}", "SX", "2018-03-02 10:00:00", "2018-03", "on_time" if n < 9 else "seller_late"))
    r = many(con, Q.TOP_SELLERS["on_time_rate"], ym="2018-03", limit=10, offset=0)
    assert r == [{"seller_id": "SX", "metric_value": 75.0, "basis_count": 12}]
    r = many(con, Q.TOP_SELLERS["avg_review"], ym="2018-03", limit=10, offset=0)
    assert r[0]["seller_id"] == "SX" and r[0]["metric_value"] == 5.0
    con.execute("DELETE FROM fact_order_seller WHERE seller_id = 'SX'")


def test_kpis_monthly(con):
    p = dict(start="2018-02", end="2018-03", region=None, limit=24, offset=0)
    rows = many(con, Q.KPIS_MONTHLY, **p)
    feb = rows[0]
    assert feb["year_month"] == "2018-02" and feb["orders"] == 4 and feb["unique_customers"] == 3   # R1: U1 has 2 orders
    assert round(feb["gross_usd"], 2) == 192.55 and feb["net_cash_paid_brl"] == 500.0
    assert feb["on_time_pct"] == 66.67 and feb["avg_delivery_business_days"] == 2.67
    assert one(con, Q.KPIS_MONTH_COUNT, **p)["total"] == 2
    assert len(many(con, Q.KPIS_MONTHLY, **{**p, "limit": 1, "offset": 1})) == 1


def test_kpis_region_filter(con):
    rows = many(con, Q.KPIS_MONTHLY, start="2018-02", end="2018-02", region="Sul", limit=24, offset=0)
    assert len(rows) == 1 and rows[0]["orders"] == 1 and round(rows[0]["gross_usd"], 2) == 91.20
    assert many(con, Q.KPIS_MONTHLY, start="2018-02", end="2018-02", region="Norte", limit=24, offset=0) == []
    assert {r["region"] for r in many(con, Q.REGIONS)} >= {"Sul", "Sudeste"}


def test_campaign_performance_queries(con):
    c3 = one(con, Q.CAMPAIGN, cid="C003")
    assert c3["window_days"] == 7 and c3["start_ts"] == "2018-03-05 00:00:00" and c3["end_ts"] == "2018-03-11 23:59:59"
    targets = {(t["target_type"], t["target_value"]) for t in many(con, Q.CAMPAIGN_TARGETS, cid="C003")}
    assert ("state", "SP") in targets and ("category", "telephony") in targets
    w = one(con, Q.CAMPAIGN_WINDOW, cid="C003")
    assert w["items"] == 2 and w["orders"] == 2 and round(w["gross_usd"], 2) == 139.05          # O8 110.25 + O13 28.80
    base = one(con, Q.CAMPAIGN_BASELINE, cid="C003", b_start="2018-02-01 00:00:00", b_end="2018-03-05 00:00:00",
               all_geo=0, all_categories=0)
    assert base == {"items": 1, "orders": 1, "gross_usd": 34.0}                                   # O7: SP + computers_accessories
    c1 = one(con, Q.CAMPAIGN_WINDOW, cid="C001")
    assert c1["items"] == 0 and c1["gross_usd"] == 0                                              # nothing in Nov 2017 here
    # region-targeted campaign: baseline must be usable (C005 targets region 'sul')
    c5 = one(con, Q.CAMPAIGN, cid="C005")
    assert c5["all_geo"] == 0
    one(con, Q.CAMPAIGN_BASELINE, cid="C005", b_start="2018-02-01 00:00:00", b_end="2018-03-01 00:00:00",
        all_geo=0, all_categories=0)
    assert one(con, Q.CAMPAIGN, cid="NOPE") is None


def test_data_quality_summary(con):
    assert one(con, Q.LATEST_RUN)["run_id"] == RUN_ID
    assert one(con, Q.RUN_EXISTS, run_id=RUN_ID)["status"] == "success"
    assert one(con, Q.RUN_EXISTS, run_id="nope") is None
    rows = many(con, Q.DQ_SUMMARY, run_id=RUN_ID, limit=100, offset=0)
    got = {(r["rule_id"], r["issue_type"]): r["issue_count"] for r in rows}
    assert got[("R2", "excluded_status")] == 2 and got[("R5", "payment_mismatch")] == 1
    rule_numbers = [int(r["rule_id"][1:]) for r in rows]
    assert rule_numbers == sorted(rule_numbers)                                  # R2 before R10+ (numeric order)
    total = one(con, Q.DQ_TOTAL_ISSUES, run_id=RUN_ID)["total_issues"]
    assert total == sum(r["issue_count"] for r in rows)
    assert one(con, Q.DQ_TOTAL_GROUPS, run_id=RUN_ID)["total_groups"] == len(rows)
    assert len(many(con, Q.DQ_SUMMARY, run_id=RUN_ID, limit=2, offset=0)) == 2


# ----------------------------------------------------------------------------- Pydantic models
def test_response_models_validate_and_serialise_money_as_numbers(con):
    from api.models import (CampaignWindowStats, DQSummaryRow, KpiMonth, OrderItemOut, SellerScorecard)
    k = KpiMonth(**many(con, Q.KPIS_MONTHLY, start="2018-02", end="2018-02", region=None, limit=1, offset=0)[0])
    dumped = k.model_dump(mode="json")
    assert isinstance(dumped["gross_usd"], float) and dumped["orders"] == 4
    item = OrderItemOut(**many(con, Q.ORDER_ITEMS, order_id="O1")[0])
    assert item.model_dump(mode="json")["commission_brl"] == 12.0
    items = one(con, Q.SELLER_ITEMS_AGG, seller_id="S1", ym="2018-02")
    o = one(con, Q.SELLER_ORDERS_AGG, seller_id="S1", ym="2018-02")
    sc = SellerScorecard(seller_id="S1", month="2018-02", tier="Silver", prev_month_revenue_brl=6000,
                         item_count=items["item_count"], gross_brl=items["gross_brl"], gross_usd=items["gross_usd"],
                         commission_brl=items["commission_brl"], commission_usd=items["commission_usd"],
                         delivered_orders=o["delivered_orders"], on_time_pct=o["on_time_pct"],
                         seller_late_count=o["seller_late_count"], avg_review_score=o["avg_review_score"])
    assert sc.model_dump(mode="json")["on_time_pct"] == 50.0
    assert CampaignWindowStats(items=0, orders=0, gross_usd=0).avg_order_value_usd is None
    assert DQSummaryRow(rule_id="R2", issue_type="x", issue_count=1)
