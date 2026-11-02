"""End-to-end transform on the synthetic dataset (tests/synth.py). All expected numbers are hand-computed."""
from datetime import date
from decimal import Decimal as D

import pandas as pd
import pytest

from pipeline.transform.build import build
from pipeline.transform.common import DQLog
from tests.synth import load_internal, make_kaggle, make_reference


@pytest.fixture(scope="module")
def res():
    dq = DQLog()
    return build(make_kaggle(), load_internal(dq), make_reference(), date(2018, 2, 1), date(2018, 3, 31), dq)


def order(res, oid):
    return res.tables["fact_order"].set_index("order_id").loc[oid]


def item(res, oid, n=1):
    return res.tables["fact_order_item"].set_index(["order_id", "order_item_id"]).loc[(oid, n)]


def test_counts_and_r2_exclusions(res):
    assert res.stats["orders_in_range"] == 13
    assert res.stats["excluded_by_status"] == {"canceled": 1, "unavailable": 1}
    loaded = set(res.tables["fact_order"]["order_id"])
    assert "O3" not in loaded and "O4" not in loaded and "OJ1" not in loaded          # excluded / out of range
    assert set(res.tables["fact_order_item"]["order_id"]) <= loaded                   # no items of excluded orders
    assert not res.dq[(res.dq.rule_id == "R2")].empty
    assert order(res, "O5")["is_revenue_eligible"] == 0                               # shipped: loaded, flagged 0


def test_r1_customer_identity_and_home_state(res):
    dc = res.tables["dim_customer"].set_index("customer_unique_id")
    assert dc.loc["U1", "home_state"] == "SP"            # earliest order (Jan, SP), though O11 shipped to RJ
    assert order(res, "O11")["customer_state"] == "RJ"   # order-level state is the delivery state
    assert order(res, "O11")["customer_unique_id"] == "U1"
    assert str(dc.loc["U1", "first_order_ts"]) == "2018-01-10 10:00:00"   # earliest order is outside the range!


def test_r3_delivered_without_date(res):
    o6 = order(res, "O6")
    assert o6["is_revenue_eligible"] == 1
    assert pd.isna(o6["is_on_time"]) and pd.isna(o6["delivery_business_days"])
    assert res.tables["fact_order_seller"].set_index("order_id").loc["O6", "sla_result"] is pd.NA or \
        pd.isna(res.tables["fact_order_seller"].set_index("order_id").loc["O6", "sla_result"])
    assert ((res.dq.rule_id == "R3") & (res.dq.entity_id == "O6")).any()


def test_r4_r6_amounts_and_fx(res):
    i = item(res, "O1")
    assert i["gross_brl"] == D("120.00") and i["gross_usd"] == D("37.20") and i["fx_rate"] == D("0.31")
    o2 = order(res, "O2")
    assert o2["fx_rate_date"] == date(2018, 2, 9) and o2["gross_usd"] == D("91.20")       # approval NULL -> purchase date
    o10 = order(res, "O10")                                                                 # approved on a Saturday
    assert o10["fx_rate_date"] == date(2018, 3, 9) and o10["fx_rate"] == D("0.355")
    assert item(res, "O10")["gross_usd"] == D("355.00")


def test_r5_payments(res):
    o2 = order(res, "O2")
    assert o2["payments_total_brl"] == D("285.00") and o2["voucher_total_brl"] == D("85.00")
    assert o2["net_cash_paid_brl"] == D("200.00") and o2["payment_mismatch"] == 0
    assert order(res, "O7")["payment_mismatch"] == 1                    # 90.00 vs 100.00
    assert order(res, "O13")["payment_mismatch"] == 0                   # 0.50 difference tolerated
    assert ((res.dq.rule_id == "R5") & (res.dq.entity_id == "O7")).any()


def test_r8_categories(res):
    assert item(res, "O11", 1)["category_en"] == "home_comfort"          # patch beats Kaggle's home_comfort_2
    assert item(res, "O11", 2)["category_en"] == "uncategorized"
    assert item(res, "O5")["category_en"] == "unknown"
    assert item(res, "O10")["category_en"] == "pc_gamer"


def test_r9_r10_r11_delivery(res):
    assert order(res, "O1")["delivery_business_days"] == 4 and order(res, "O1")["is_on_time"] == 1
    o2 = order(res, "O2")
    assert o2["delivery_business_days"] == 1 and o2["is_on_time"] == 0    # RS: 13th national + 14th regional holiday
    sellers = res.tables["fact_order_seller"].set_index(["order_id", "seller_id"])
    assert sellers.loc[("O2", "S1"), "sla_result"] == "seller_late"       # carrier 02-12 > limit 02-10
    assert sellers.loc[("O2", "S2"), "sla_result"] == "carrier_late"      # shipped in time, delivered late
    assert sellers.loc[("O1", "S1"), "sla_result"] == "on_time"
    assert order(res, "O7")["delivery_business_days"] == 3                # Wed, Thu, Fri (Sat delivery not a business day)


def test_r12_tier_uses_previous_month_even_outside_range(res):
    assert item(res, "O1")["seller_tier"] == "Silver"        # S1 sold 6,000 in Jan (outside the processed range)
    assert item(res, "O2", 2)["seller_tier"] == "Bronze"      # S2 had no sales in Jan
    assert item(res, "O8")["seller_tier"] == "Bronze"         # S1 sold only 150 in Feb -> back to Bronze in March
    t = res.tables["seller_month_tier"].set_index(["seller_id", "year_month"])
    assert t.loc[("S1", "2018-02"), "tier"] == "Silver" and t.loc[("S1", "2018-02"), "prev_month_revenue_brl"] == D("6000.00")


def test_r13_commission(res):
    i1 = item(res, "O1")                                           # Silver 2018 rate, price only (not freight)
    assert i1["commission_rate"] == D("0.12") and i1["commission_brl"] == D("12.00") and i1["commission_usd"] == D("3.72")
    assert i1["commission_rate_source"] == "tier"
    i2 = item(res, "O2", 1)                                        # telephony override 2018 beats Silver
    assert i2["commission_rate"] == D("0.07") and i2["commission_rate_source"] == "category_override"
    assert i2["commission_brl"] == D("3.50") and i2["commission_usd"] == D("1.12")
    assert item(res, "O2", 2)["commission_brl"] == D("32.00")      # Bronze 2018 = 0.16
    assert pd.isna(item(res, "O5")["commission_rate"]) and pd.isna(item(res, "O5")["commission_brl"])  # not delivered
    assert item(res, "O10")["commission_brl"] == D("60.00")        # pc_gamer 6 %


def test_r14_review(res):
    assert order(res, "O7")["review_score"] == 5          # latest answer timestamp wins
    sellers = res.tables["fact_order_seller"].set_index(["order_id", "seller_id"])
    assert sellers.loc[("O2", "S1"), "review_score"] == 2 and sellers.loc[("O2", "S2"), "review_score"] == 2  # every seller


def test_r15_r16_distance(res):
    assert 3 < float(item(res, "O1")["distance_km"]) < 4                     # same city
    assert 1100 < float(item(res, "O2", 2)["distance_km"]) < 1150            # Rio -> Porto Alegre area
    assert pd.isna(item(res, "O12")["distance_km"])                          # customer zip 99999 has no coordinates
    assert ((res.dq.rule_id == "R16") & res.dq.entity_id.str.startswith("O12#")).any()
    assert ((res.dq.rule_id == "R15") & (res.dq.issue_type == "points_outside_brazil_removed")).any()


def test_r17_regions(res):
    assert order(res, "O2")["customer_region"] == "Sul" and order(res, "O8")["customer_region"] == "Sudeste"
    assert res.tables["dim_seller"].set_index("seller_id").loc["S3", "region"] == "Sul"


def test_r18_campaigns(res):
    assert item(res, "O1")["campaign_id"] == "C002"        # SP + Feb + stationery
    assert item(res, "O8")["campaign_id"] == "C003"        # MG + Tech Week + telephony
    assert item(res, "O13")["campaign_id"] == "C003"       # 2018-03-11 23:59:30 local, last minute of the window
    assert pd.isna(item(res, "O9")["campaign_id"])         # 2018-03-12 00:00:10 local, window already closed
    assert pd.isna(item(res, "O2", 1)["campaign_id"])      # RS customer: not a Tech Week state


def test_r7_money_is_decimal_and_rounded_to_cents(res):
    for col in ("price_brl", "gross_usd", "commission_brl", "commission_usd"):
        vals = [v for v in res.tables["fact_order_item"][col] if v is not None and not pd.isna(v)]
        assert vals and all(isinstance(v, D) for v in vals)
        assert all(v == v.quantize(D("0.01")) for v in vals if col != "price_brl")


def test_every_internal_file_problem_is_logged(res):
    internal = res.dq[res.dq.entity_type == "internal_file"]
    kinds = set(internal.issue_type)
    assert {"normalized_value", "open_ended_range", "rejected_impossible_range"} <= kinds


def test_subrange_gives_same_rows_as_full_range():
    """Re-running only March must reproduce exactly the March rows of a Feb-Mar run (R12 / idempotency)."""
    full = build(make_kaggle(), load_internal(), make_reference(), date(2018, 2, 1), date(2018, 3, 31))
    march = build(make_kaggle(), load_internal(), make_reference(), date(2018, 3, 1), date(2018, 3, 31))
    cols = ["order_id", "order_item_id", "seller_tier", "commission_rate", "commission_brl", "commission_usd",
            "gross_usd", "campaign_id", "distance_km"]
    a = full.tables["fact_order_item"]
    a = a[a["year_month"] == "2018-03"][cols].sort_values(["order_id", "order_item_id"]).reset_index(drop=True)
    b = march.tables["fact_order_item"][cols].sort_values(["order_id", "order_item_id"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    assert set(march.tables["fact_order"]["year_month"]) == {"2018-03"}
