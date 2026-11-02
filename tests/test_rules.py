"""Unit tests for the trickiest rules: R6, R9, R12, R13, R18 (+ R5, R8, R10/R11, R14, R15/R16)."""
from datetime import date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from pipeline.transform.common import DQLog, prev_month
from pipeline.transform.internal_files import Campaign
from pipeline.transform.rules_campaign import attribute_campaigns, latest_review_per_order
from pipeline.transform.rules_commission import (monthly_revenue_cents, resolve_commission_rates, seller_tiers,
                                                 tier_for_cents, tier_for_revenue)
from pipeline.transform.rules_delivery import business_days_between, holiday_calendar, on_time, sla_result
from pipeline.transform.rules_geo import haversine_km, zip_centroids
from pipeline.transform.rules_money import (attach_fx, flag_payment_mismatch, lookup_fx, payment_summary,
                                            resolve_categories)

D = Decimal


# =============================================================== R6 FX
class TestR6Fx:
    FX_DATES = [date(2018, 3, 8), date(2018, 3, 9), date(2018, 3, 12)]       # Thu, Fri, Mon
    FX_RATES = [D("0.30"), D("0.31"), D("0.32")]

    def test_exact_date(self):
        rates, used = lookup_fx([date(2018, 3, 9)], self.FX_DATES, self.FX_RATES)
        assert rates == [D("0.31")] and used == [date(2018, 3, 9)]

    def test_weekend_uses_previous_friday_never_next_monday(self):
        rates, used = lookup_fx([date(2018, 3, 10), date(2018, 3, 11)], self.FX_DATES, self.FX_RATES)
        assert used == [date(2018, 3, 9), date(2018, 3, 9)]
        assert rates == [D("0.31"), D("0.31")]          # NOT 0.32 (Monday) - never a later rate

    def test_before_first_rate_is_none(self):
        rates, used = lookup_fx([date(2018, 3, 1)], self.FX_DATES, self.FX_RATES)
        assert rates == [None] and used == [None]

    def test_null_approval_falls_back_to_purchase_date(self):
        orders = pd.DataFrame({
            "order_id": ["A", "B"],
            "purchase_ts": pd.to_datetime(["2018-03-08 10:00", "2018-03-10 10:00"]),
            "approved_ts": pd.to_datetime([None, "2018-03-11 08:00"]),
        })
        fx = pd.DataFrame({"rate_date": self.FX_DATES, "brl_usd": self.FX_RATES})
        dq = DQLog()
        out = attach_fx(orders, fx, dq)
        assert out.loc[0, "fx_rate_date"] == date(2018, 3, 8)       # purchase date used (approval NULL)
        assert out.loc[1, "fx_rate_date"] == date(2018, 3, 9)       # approval Sunday -> Friday
        assert dq.count("R6", "approved_at_null_used_purchase_date") == 1

    def test_missing_rate_is_logged_not_defaulted(self):
        orders = pd.DataFrame({"order_id": ["A"], "purchase_ts": pd.to_datetime(["2018-01-01"]),
                               "approved_ts": pd.to_datetime(["2018-01-01"])})
        fx = pd.DataFrame({"rate_date": self.FX_DATES, "brl_usd": self.FX_RATES})
        dq = DQLog()
        out = attach_fx(orders, fx, dq)
        assert out.loc[0, "fx_rate"] is None and dq.count("R6", "no_fx_rate_available") == 1


# =============================================================== R9 business days
class TestR9BusinessDays:
    NATIONAL = {date(2018, 2, 13)}                     # Tue
    REGIONAL = {"RS": {date(2018, 2, 14)}}             # Wed, RS only

    def _bd(self, start, end, state):
        s, e = pd.Series([pd.Timestamp(start)]), pd.Series([pd.Timestamp(end)])
        return business_days_between(s, e, pd.Series([state]), self.NATIONAL, self.REGIONAL).iloc[0]

    def test_exclusive_of_start_inclusive_of_end(self):
        assert self._bd("2018-02-05", "2018-02-09", "SP") == 4        # Tue..Fri
        assert self._bd("2018-02-05", "2018-02-05", "SP") == 0        # same day

    def test_weekend_start_and_end(self):
        assert self._bd("2018-02-09", "2018-02-12", "SP") == 1        # Fri -> Mon: only Monday counts
        assert self._bd("2018-02-10", "2018-02-11", "SP") == 0        # Sat -> Sun

    def test_national_holiday_excluded(self):
        assert self._bd("2018-02-12", "2018-02-14", "SP") == 1        # Tue 13 is a holiday, Wed 14 counts

    def test_regional_holiday_only_for_that_state(self):
        assert self._bd("2018-02-12", "2018-02-14", "RS") == 0        # 13 national + 14 regional(RS)
        assert self._bd("2018-02-12", "2018-02-14", "SP") == 1

    def test_end_before_start_is_na(self):
        assert pd.isna(self._bd("2018-02-10", "2018-02-05", "SP"))

    def test_missing_dates_are_na(self):
        out = business_days_between(pd.Series([pd.NaT]), pd.Series([pd.Timestamp("2018-02-05")]),
                                    pd.Series(["SP"]), set(), {})
        assert pd.isna(out.iloc[0])

    def test_holiday_calendar_splits_national_regional_and_types(self):
        raw = pd.DataFrame([
            {"date": date(2018, 1, 1), "local_name": "A", "name": "A", "global_flag": True, "counties": [], "types": ["Public"]},
            {"date": date(2018, 7, 9), "local_name": "B", "name": "B", "global_flag": False,
             "counties": ["BR-SP"], "types": ["Public"]},
            {"date": date(2018, 3, 1), "local_name": "C", "name": "C", "global_flag": True, "counties": [], "types": ["Optional"]},
        ])
        national, regional, ref = holiday_calendar(raw)
        assert national == {date(2018, 1, 1)}
        assert regional == {"SP": {date(2018, 7, 9)}}
        assert set(ref["scope"]) == {"national", "regional"}            # Optional row ignored


# =============================================================== R10 / R11
class TestR10R11:
    def test_on_time_compares_dates_not_timestamps(self):
        out = on_time(pd.Series(pd.to_datetime(["2018-02-12 23:59:00"])),
                      pd.Series(pd.to_datetime(["2018-02-12 00:00:00"])))
        assert out.iloc[0] == 1          # same calendar day = on time even though the timestamp is later

    def test_late_when_one_day_after(self):
        out = on_time(pd.Series(pd.to_datetime(["2018-02-13 00:00:01"])),
                      pd.Series(pd.to_datetime(["2018-02-12 23:59:59"])))
        assert out.iloc[0] == 0

    def test_seller_vs_carrier_vs_on_time(self):
        carrier = pd.Series(pd.to_datetime(["2018-02-12", "2018-02-05", "2018-02-05", "2018-02-12"]))
        limit = pd.Series(pd.to_datetime(["2018-02-10", "2018-02-10", "2018-02-10", "2018-02-10"]))
        ontime = pd.Series([1, 0, 1, 0], dtype="Int64")
        flag = pd.Series([True, True, True, False])
        res = sla_result(carrier, limit, ontime, flag)
        assert list(res[:3]) == ["seller_late", "carrier_late", "on_time"]
        assert pd.isna(res[3])           # not SLA eligible (R3) -> NULL


# =============================================================== R12 tiers
class TestR12Tiers:
    @pytest.mark.parametrize("revenue,tier", [
        ("0", "Bronze"), ("4999.99", "Bronze"), ("5000.00", "Silver"), ("12000", "Silver"),
        ("20000.00", "Silver"), ("20000.01", "Gold"), ("150000", "Gold")])
    def test_thresholds(self, revenue, tier):
        assert tier_for_revenue(D(revenue)) == tier

    def test_cents_boundaries(self):
        assert tier_for_cents(499_999) == "Bronze" and tier_for_cents(500_000) == "Silver"
        assert tier_for_cents(2_000_000) == "Silver" and tier_for_cents(2_000_001) == "Gold"

    def test_prev_month_wraps_year(self):
        assert prev_month("2018-01") == "2017-12" and prev_month("2018-03") == "2018-02"

    def _source(self):
        orders = pd.DataFrame({
            "order_id": ["a", "b", "c", "d"],
            "order_status": ["delivered", "delivered", "canceled", "shipped"],
            "order_purchase_timestamp": ["2018-01-10 10:00:00", "2018-01-31 23:00:00",
                                         "2018-01-12 10:00:00", "2018-01-13 10:00:00"]})
        items = pd.DataFrame({"order_id": ["a", "b", "c", "d"], "seller_id": ["S1", "S1", "S1", "S1"],
                              "price": ["2500.00", "2500.00", "9999.00", "9999.00"]})
        return orders, items

    def test_only_delivered_orders_count_and_tier_uses_previous_month(self):
        orders, items = self._source()
        rev = monthly_revenue_cents(orders, items)
        assert rev.loc[0, "cents"] == 500_000                         # 2500 + 2500; canceled/shipped ignored
        t = seller_tiers(rev, ["S1", "S2"], ["2018-01", "2018-02", "2018-03"]).set_index(["seller_id", "year_month"])
        assert t.loc[("S1", "2018-02"), "tier"] == "Silver"            # exactly 5,000 in January -> Silver
        assert t.loc[("S1", "2018-01"), "tier"] == "Bronze"            # no December sales
        assert t.loc[("S1", "2018-03"), "tier"] == "Bronze"            # no February sales -> back to Bronze
        assert t.loc[("S2", "2018-02"), "tier"] == "Bronze"            # seller without sales

    def test_tier_for_range_start_uses_month_before_range(self):
        """Sub-range rerun (March only) must still see February: revenue is read from the FULL source."""
        orders, items = self._source()
        rev = monthly_revenue_cents(orders, items)
        t = seller_tiers(rev, ["S1"], ["2018-02"])                     # only February requested
        assert t.loc[0, "tier"] == "Silver"


# =============================================================== R13 commission
class TestR13Commission:
    RATES = pd.DataFrame({
        "tier": ["Bronze", "Bronze", "Silver", "Silver", "Silver", "Gold", "Gold"],
        "rate": [D("0.15"), D("0.16"), D("0.12"), D("0.12"), D("0.11"), D("0.10"), D("0.09")],
        "effective_from": [date(2016, 1, 1), date(2018, 1, 1), date(2016, 1, 1), date(2018, 1, 1),
                           date(2018, 7, 1), date(2016, 1, 1), date(2018, 1, 1)],
        "effective_to": [date(2017, 12, 31), None, date(2017, 12, 31), date(2018, 6, 30), None,
                         date(2017, 12, 31), None]})
    OVERRIDES = pd.DataFrame({
        "category_en": ["telephony", "telephony", "computers"],
        "fixed_rate": [D("0.08"), D("0.07"), D("0.05")],
        "effective_from": [date(2016, 1, 1), date(2018, 1, 1), date(2017, 6, 1)],
        "effective_to": [date(2017, 12, 31), None, None]})

    def _rate(self, purchase, category, tier, eligible=True):
        items = pd.DataFrame({"order_id": ["o"], "order_item_id": [1], "purchase_date": [purchase],
                              "category_en": [category], "seller_tier": [tier], "eligible": [eligible]})
        dq = DQLog()
        out = resolve_commission_rates(items, self.RATES, self.OVERRIDES, dq)
        return out.loc[0, "commission_rate"], out.loc[0, "commission_rate_source"], dq

    def test_override_beats_tier(self):
        assert self._rate(date(2018, 3, 1), "telephony", "Gold")[:2] == (D("0.07"), "category_override")

    def test_override_dates_pick_the_right_version(self):
        assert self._rate(date(2017, 12, 31), "telephony", "Bronze")[0] == D("0.08")
        assert self._rate(date(2018, 1, 1), "telephony", "Bronze")[0] == D("0.07")

    def test_override_not_active_yet_falls_back_to_tier(self):
        assert self._rate(date(2017, 5, 31), "computers", "Bronze")[:2] == (D("0.15"), "tier")
        assert self._rate(date(2017, 6, 1), "computers", "Bronze")[:2] == (D("0.05"), "category_override")

    def test_tier_rate_versions(self):
        assert self._rate(date(2017, 6, 1), "toys", "Silver")[0] == D("0.12")
        assert self._rate(date(2018, 6, 30), "toys", "Silver")[0] == D("0.12")
        assert self._rate(date(2018, 7, 1), "toys", "Silver")[0] == D("0.11")
        assert self._rate(date(2018, 8, 1), "toys", "Gold")[0] == D("0.09")

    def test_not_eligible_gets_no_commission(self):
        assert self._rate(date(2018, 3, 1), "toys", "Bronze", eligible=False)[0] is None

    def test_no_rate_is_logged_never_defaulted(self):
        rate, src, dq = self._rate(date(2015, 1, 1), "toys", "Bronze")       # before any rate window
        assert rate is None and dq.count("R13", "no_commission_rate_found") == 1


# =============================================================== R18 campaigns
def _camp(cid, start, end, prio, states=(), regions=(), cats=(), all_geo=False, all_cats=False):
    return Campaign(cid, cid, start, end, prio, all_geo, frozenset(states), frozenset(regions), all_cats, frozenset(cats))


class TestR18Campaigns:
    def _items(self, rows):
        return pd.DataFrame(rows, columns=["purchase_ts", "customer_state", "customer_region", "category_en"]).assign(
            purchase_ts=lambda d: pd.to_datetime(d["purchase_ts"]))

    def test_date_only_end_includes_the_whole_last_day(self):
        c = _camp("C", datetime(2018, 5, 1), datetime(2018, 5, 13, 23, 59, 59, 999999), 1, all_geo=True, all_cats=True)
        items = self._items([("2018-05-13 23:59:59", "SP", "Sudeste", "x"), ("2018-05-14 00:00:00", "SP", "Sudeste", "x")])
        assert list(attribute_campaigns(items, [c])) == ["C", None]

    def test_utc_window_converted_to_sao_paulo(self):
        from pipeline.transform.internal_files import load_campaigns
        import json, tempfile, pathlib
        raw = [{"campaign_id": "C003", "name": "Tech", "start": "2018-03-05T03:00:00Z",
                "end": "2018-03-12T02:59:59Z", "target_states": ["SP"], "categories": ["telephony"], "priority": 2}]
        p = pathlib.Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps(raw))
        c = load_campaigns(p, DQLog())[0]
        assert c.start_ts == datetime(2018, 3, 5, 0, 0, 0) and c.end_ts == datetime(2018, 3, 11, 23, 59, 59)
        items = self._items([("2018-03-05 00:00:00", "SP", "Sudeste", "telephony"),      # first second -> in
                             ("2018-03-04 23:59:59", "SP", "Sudeste", "telephony"),      # before -> out
                             ("2018-03-11 23:59:59", "SP", "Sudeste", "telephony"),      # last second -> in
                             ("2018-03-12 00:00:00", "SP", "Sudeste", "telephony")])     # after -> out
        assert list(attribute_campaigns(items, [c])) == ["C003", None, "C003", None]

    def test_state_region_and_category_matching(self):
        south = _camp("S", datetime(2018, 5, 7), datetime(2018, 5, 13, 23, 59, 59), 2, regions=["sul"], cats=["perfumery"])
        sp = _camp("P", datetime(2018, 5, 7), datetime(2018, 5, 13, 23, 59, 59), 2, states=["SP"], cats=["perfumery"])
        items = self._items([("2018-05-08 10:00:00", "RS", "Sul", "perfumery"),         # region match
                             ("2018-05-08 10:00:00", "SP", "Sudeste", "perfumery"),     # state match
                             ("2018-05-08 10:00:00", "RS", "Sul", "toys"),              # wrong category
                             ("2018-05-08 10:00:00", "BA", "Nordeste", "perfumery")])   # wrong geo
        assert list(attribute_campaigns(items, [south, sp])) == ["S", "P", None, None]

    def test_higher_priority_number_wins(self):
        low = _camp("LOW", datetime(2018, 2, 1), datetime(2018, 2, 28, 23, 59, 59), 1, all_geo=True, all_cats=True)
        high = _camp("HIGH", datetime(2018, 2, 1), datetime(2018, 2, 28, 23, 59, 59), 3, states=["SP"], all_cats=True)
        items = self._items([("2018-02-10 10:00:00", "SP", "Sudeste", "x"), ("2018-02-10 10:00:00", "RJ", "Sudeste", "x")])
        assert list(attribute_campaigns(items, [low, high])) == ["HIGH", "LOW"]

    def test_tie_on_priority_later_start_wins(self):
        """C004 (May 1-13, all states) vs C005 (May 7-13, Sul) both priority 2 -> C005 where both match."""
        c4 = _camp("C004", datetime(2018, 5, 1), datetime(2018, 5, 13, 23, 59, 59, 999999), 2, all_geo=True,
                   cats=["health_beauty", "perfumery", "watches_gifts"])
        c5 = _camp("C005", datetime(2018, 5, 7), datetime(2018, 5, 13, 23, 59, 59, 999999), 2, regions=["sul"],
                   cats=["health_beauty", "perfumery"])
        items = self._items([("2018-05-09 10:00:00", "RS", "Sul", "perfumery"),         # both match -> C005 (later start)
                             ("2018-05-09 10:00:00", "SP", "Sudeste", "perfumery"),     # only C004
                             ("2018-05-03 10:00:00", "RS", "Sul", "perfumery"),         # C005 not started -> C004
                             ("2018-05-09 10:00:00", "RS", "Sul", "watches_gifts")])    # C005 lacks category -> C004
        assert list(attribute_campaigns(items, [c4, c5])) == ["C005", "C004", "C004", "C004"]
        assert list(attribute_campaigns(items, [c5, c4])) == ["C005", "C004", "C004", "C004"]   # order independent


# =============================================================== R5 / R8 / R14 / R15 / R16
class TestOtherRules:
    def test_r5_payments_vouchers_and_mismatch_tolerance(self):
        pays = pd.DataFrame({"order_id": ["A", "A", "B"], "payment_type": ["credit_card", "voucher", "boleto"],
                             "payment_value": ["200.00", "85.00", "50.00"]})
        s = payment_summary(pays).set_index("order_id")
        assert s.loc["A", "payments_total_brl"] == D("285.00") and s.loc["A", "net_cash_paid_brl"] == D("200.00")
        orders = pd.DataFrame({"order_id": ["A", "B", "C"], "payments_total_brl": [D("285.00"), D("50.00"), D("0")],
                               "items_gross_brl": [D("285.00"), D("50.99"), D("10.00")]})
        dq = DQLog()
        flags = flag_payment_mismatch(orders, dq)
        assert list(flags) == [0, 0, 1]                      # diff 0.99 tolerated; no payments at all -> flagged

    def test_r5_exactly_one_real_is_not_flagged(self):
        orders = pd.DataFrame({"order_id": ["A", "B"], "payments_total_brl": [D("10.00"), D("10.00")],
                               "items_gross_brl": [D("11.00"), D("11.01")]})
        assert list(flag_payment_mismatch(orders, DQLog())) == [0, 1]       # > 1.00 only

    def test_r8_resolution_order_and_distinct_unknown_vs_uncategorized(self):
        products = pd.DataFrame({"product_id": ["1", "2", "3", "4", "5"],
                                 "product_category_name": ["casa_conforto_2", "pc_gamer", "telefonia",
                                                           "sem_traducao", None]})
        translation = pd.DataFrame({"product_category_name": ["casa_conforto_2", "telefonia"],
                                    "product_category_name_english": ["home_comfort_2", "telephony"]})
        patch = {"pc_gamer": "pc_gamer", "casa_conforto_2": "home_comfort"}
        out = resolve_categories(products, patch, translation, DQLog()).set_index("product_id")
        assert out.loc["1", "category_en"] == "home_comfort"               # patch wins over Kaggle
        assert out.loc["2", "category_en"] == "pc_gamer" and out.loc["2", "category_source"] == "patch"
        assert out.loc["3", "category_en"] == "telephony"
        assert out.loc["4", "category_en"] == "unknown"
        assert out.loc["5", "category_en"] == "uncategorized"
        assert out.loc["4", "category_en"] != out.loc["5", "category_en"]

    def test_r14_keeps_latest_answer_timestamp(self):
        reviews = pd.DataFrame({"review_id": ["a", "b", "c"], "order_id": ["O", "O", "P"],
                                "review_score": ["1", "5", "3"],
                                "review_creation_date": ["2018-01-01 00:00:00"] * 3,
                                "review_answer_timestamp": ["2018-01-05 10:00:00", "2018-01-04 10:00:00",
                                                            "2018-01-02 10:00:00"]})
        dq = DQLog()
        out = latest_review_per_order(reviews, {"O", "P"}, dq).set_index("order_id")
        assert out.loc["O", "review_score"] == 1            # answered latest (Jan 5), even though lower score
        assert out.loc["P", "review_score"] == 3
        assert dq.count("R14", "duplicate_review_dropped") == 1

    def test_r15_bbox_filter_then_median(self):
        geo = pd.DataFrame({"geolocation_zip_code_prefix": ["1001"] * 5,
                            "geolocation_lat": ["-23.0", "-24.0", "-25.0", "40.0", "0.0"],
                            "geolocation_lng": ["-46.0", "-47.0", "-48.0", "-3.7", "0.0"]})
        dq = DQLog()
        c = zip_centroids(geo, dq).iloc[0]
        assert c["zip_prefix"] == "01001"                    # leading zero restored
        assert c["lat"] == -24.0 and c["lng"] == -47.0       # median of the 3 in-Brazil points only

    def test_r16_haversine_known_distance(self):
        # Sao Paulo (-23.5505, -46.6333) -> Rio (-22.9068, -43.1729) is about 360 km
        d = haversine_km([-23.5505], [-46.6333], [-22.9068], [-43.1729])[0]
        assert 355 < d < 365
        assert haversine_km([-23.0], [-46.0], [-23.0], [-46.0])[0] == 0
