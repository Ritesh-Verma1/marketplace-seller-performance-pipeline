"""Transformation orchestrator: applies R1-R18 and returns modelled tables + the DQ log.

Pure pandas, no database or network access -> fully unit-testable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from .common import DQLog, ZERO, month_range_between, next_month, q_money, q_rate, to_decimal
from .internal_files import Campaign
from .rules_campaign import attribute_campaigns, latest_review_per_order
from .rules_commission import monthly_revenue_cents, resolve_commission_rates, seller_tiers
from .rules_delivery import business_days_between, holiday_calendar, on_time, sla_result
from .rules_geo import enrich_states, item_distances, zip5, zip_centroids
from .rules_money import attach_fx, flag_payment_mismatch, payment_summary, resolve_categories

log = logging.getLogger(__name__)

EXCLUDED_STATUSES = {"canceled", "unavailable"}     # R2
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class Internal:
    rates: pd.DataFrame
    overrides: pd.DataFrame
    patch: dict
    campaigns: list[Campaign]


@dataclass
class Reference:
    fx: pd.DataFrame            # rate_date, brl_usd (Decimal)
    holidays: pd.DataFrame      # raw Nager rows
    states: pd.DataFrame        # state_code, state_name, region


@dataclass
class TransformResult:
    tables: dict[str, pd.DataFrame]
    dq: pd.DataFrame
    stats: dict = field(default_factory=dict)


def _ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format=TS_FORMAT, errors="coerce")


def _sum_by(keys, values) -> dict:
    out: dict = {}
    for k, v in zip(keys, values):
        out[k] = out.get(k, ZERO) + v
    return out


def expected_tiers(kaggle: dict[str, pd.DataFrame], months: list[str]) -> pd.DataFrame:
    """R12 tiers straight from the source files, for the given months. Used by the loader's cascade check."""
    rev = monthly_revenue_cents(kaggle["orders"], kaggle["order_items"])
    sellers = sorted(set(kaggle["sellers"]["seller_id"]) | set(kaggle["order_items"]["seller_id"]))
    return seller_tiers(rev, sellers, months)


def build(kaggle: dict[str, pd.DataFrame], internal: Internal, ref: Reference,
          start: date, end: date, dq: DQLog | None = None) -> TransformResult:
    dq = dq if dq is not None else DQLog()
    stats: dict = {}

    # ------------------------------------------------------------------ orders + customers (R1, R2, R3)
    o = kaggle["orders"].copy()
    o["order_status"] = o["order_status"].str.strip().str.lower()
    o["purchase_ts"] = _ts(o["order_purchase_timestamp"])
    o["approved_ts"] = _ts(o["order_approved_at"])
    o["carrier_ts"] = _ts(o["order_delivered_carrier_date"])
    o["delivered_ts"] = _ts(o["order_delivered_customer_date"])
    o["estimated_ts"] = _ts(o["order_estimated_delivery_date"])

    bad_purchase = o["purchase_ts"].isna()
    if bad_purchase.any():
        dq.add_many("R2", "order", o.loc[bad_purchase, "order_id"], "unparseable_purchase_timestamp",
                    "order_purchase_timestamp missing/invalid: order cannot be placed in any date range")

    cust = kaggle["customers"][["customer_id", "customer_unique_id", "customer_zip_code_prefix", "customer_state"]]
    o = o.merge(cust, on="customer_id", how="left", validate="m:1")
    o["customer_zip"] = zip5(o["customer_zip_code_prefix"])
    o["customer_state"] = o["customer_state"].str.strip().str.upper()

    # R1: home state = state on the customer's EARLIEST (non-excluded) order, over the full source so that
    # sub-range reruns give the same answer as a full run.
    eligible_for_home = o[~o["order_status"].isin(EXCLUDED_STATUSES) & o["customer_unique_id"].notna()
                          & o["purchase_ts"].notna()]
    first = eligible_for_home.sort_values(["purchase_ts", "order_id"]).drop_duplicates("customer_unique_id")
    home = first.set_index("customer_unique_id")[["customer_state", "purchase_ts"]]

    lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    r = o[o["purchase_ts"].ge(lo) & o["purchase_ts"].lt(hi)].copy()
    stats["orders_in_range"] = len(r)

    # R2: canceled / unavailable are excluded from everything except the DQ report
    excl = r["order_status"].isin(EXCLUDED_STATUSES)
    dq.add_many("R2", "order", r.loc[excl, "order_id"], "excluded_status",
                [f"status={s}" for s in r.loc[excl, "order_status"]])
    stats["excluded_by_status"] = r.loc[excl, "order_status"].value_counts().to_dict()
    r = r[~excl].copy()

    orphan = r["customer_unique_id"].isna()
    if orphan.any():
        dq.add_many("R1", "order", r.loc[orphan, "order_id"], "order_without_customer",
                    "customer_id not found in customers file; order not loaded")
        r = r[~orphan].copy()

    r["year_month"] = r["purchase_ts"].dt.strftime("%Y-%m")
    r["purchase_date"] = r["purchase_ts"].dt.date
    r["is_revenue_eligible"] = (r["order_status"] == "delivered").astype(int)    # R2
    elig = r["is_revenue_eligible"] == 1

    # R3: delivered but no delivery date -> still revenue eligible, but out of every SLA metric
    broken = elig & r["delivered_ts"].isna()
    if broken.any():
        dq.add_many("R3", "order", r.loc[broken, "order_id"], "delivered_without_delivery_date",
                    "status=delivered but order_delivered_customer_date is NULL: kept for revenue, excluded from SLA")

    # R17: region for the order's delivery state
    geo_cols = enrich_states(r["customer_state"], ref.states)
    r["customer_region"] = geo_cols["region"]
    no_region = r["customer_region"].isna()
    for st in r.loc[no_region, "customer_state"].fillna("<null>").unique():
        dq.add("R17", "state", st, "state_not_in_ibge_list", "customer state has no region in BrasilAPI data")

    # ------------------------------------------------------------------ FX (R6)
    r = attach_fx(r, ref.fx, dq)

    # ------------------------------------------------------------------ items (R4, R8, R12, R13, R16, R18)
    it = kaggle["order_items"]
    it = it[it["order_id"].isin(r["order_id"])].copy()
    it["order_item_id"] = it["order_item_id"].astype(int)
    it["price_brl"] = [to_decimal(x) for x in it["price"]]
    it["freight_brl"] = [to_decimal(x) for x in it["freight_value"]]
    it["shipping_limit_ts"] = _ts(it["shipping_limit_date"])
    it["gross_brl"] = [p + f for p, f in zip(it["price_brl"], it["freight_brl"])]          # R4

    no_items = ~r["order_id"].isin(it["order_id"])
    if no_items.any():
        dq.add_many("R4", "order", r.loc[no_items, "order_id"], "order_without_items",
                    "order has no rows in order_items; loaded with zero amounts")

    order_cols = r[["order_id", "purchase_ts", "year_month", "purchase_date", "is_revenue_eligible",
                    "customer_state", "customer_region", "customer_zip", "fx_rate", "fx_rate_date"]]
    it = it.merge(order_cols, on="order_id", how="inner", validate="m:1")
    it["gross_usd"] = [q_money(g * fx) if fx is not None else None for g, fx in zip(it["gross_brl"], it["fx_rate"])]

    # R8 categories
    products = resolve_categories(kaggle["products"], internal.patch, kaggle["translation"], dq)
    it = it.merge(products[["product_id", "category_en"]], on="product_id", how="left")
    no_prod = it["category_en"].isna()
    if no_prod.any():
        dq.add_many("R8", "order_item", [f"{o_}#{i}" for o_, i in zip(it.loc[no_prod, "order_id"],
                                                                     it.loc[no_prod, "order_item_id"])],
                    "product_not_in_products_file", "item product_id missing in products file -> 'uncategorized'")
        it.loc[no_prod, "category_en"] = "uncategorized"

    # sellers (+ R17 region, R15 centroid)
    sellers = kaggle["sellers"][["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"]].copy()
    sellers["seller_zip"] = zip5(sellers["seller_zip_code_prefix"])
    sellers["seller_state"] = sellers["seller_state"].str.strip().str.upper()
    unknown_sellers = sorted(set(it["seller_id"]) - set(sellers["seller_id"]))
    if unknown_sellers:
        dq.add_many("R17", "seller", unknown_sellers, "seller_not_in_sellers_file",
                    "stub dim_seller row created (state/zip unknown)")
        sellers = pd.concat([sellers, pd.DataFrame({"seller_id": unknown_sellers})], ignore_index=True)
    s_geo = enrich_states(sellers["seller_state"], ref.states)
    sellers["state_name"], sellers["region"] = s_geo["state_name"], s_geo["region"]
    centroids = zip_centroids(kaggle["geolocation"], dq)                                    # R15
    cent_idx = centroids.set_index("zip_prefix")
    sellers["lat"] = sellers["seller_zip"].map(cent_idx["lat"])
    sellers["lng"] = sellers["seller_zip"].map(cent_idx["lng"])

    # R12 tiers (source-wide revenue) -> per item
    months = month_range_between(start, end)
    rev = monthly_revenue_cents(kaggle["orders"], kaggle["order_items"])
    tiers = seller_tiers(rev, sorted(sellers["seller_id"]), months)
    it = it.merge(tiers[["seller_id", "year_month", "tier"]].rename(columns={"tier": "seller_tier"}),
                  on=["seller_id", "year_month"], how="left")

    # R13 commission
    it["eligible"] = it["is_revenue_eligible"] == 1
    rate_df = resolve_commission_rates(it, internal.rates, internal.overrides, dq)
    it = it.merge(rate_df, on=["order_id", "order_item_id"], how="left", validate="1:1")
    comm_brl, comm_usd, rate_q = [], [], []
    for price, rate, fx in zip(it["price_brl"], it["commission_rate"], it["fx_rate"]):
        if rate is None or pd.isna(rate):
            comm_brl.append(None); comm_usd.append(None); rate_q.append(None)
            continue
        raw = price * rate                                   # commission on price only, never freight
        comm_brl.append(q_money(raw))
        comm_usd.append(q_money(raw * fx) if fx is not None else None)
        rate_q.append(q_rate(rate))
    it["commission_brl"], it["commission_usd"], it["commission_rate"] = comm_brl, comm_usd, rate_q

    # R16 distance
    it = it.merge(sellers[["seller_id", "seller_zip"]], on="seller_id", how="left")
    dist = item_distances(it, centroids, dq)
    it["distance_km"] = [None if pd.isna(d) else Decimal(str(round(float(d), 3))) for d in dist]

    # R18 campaigns
    it["campaign_id"] = attribute_campaigns(it, internal.campaigns)

    # ------------------------------------------------------------------ order level
    # items rolled up
    cnt = it.groupby("order_id").size()
    price_sum = _sum_by(it["order_id"], it["price_brl"])
    fr_sum = _sum_by(it["order_id"], it["freight_brl"])
    gr_sum = _sum_by(it["order_id"], it["gross_brl"])
    usd_known = it[it["gross_usd"].notna()]
    usd_sum = _sum_by(usd_known["order_id"], usd_known["gross_usd"])
    missing_usd = set(it.loc[it["gross_usd"].isna(), "order_id"])
    r["items_count"] = r["order_id"].map(cnt).fillna(0).astype(int)
    r["items_price_brl"] = [price_sum.get(x, ZERO) for x in r["order_id"]]
    r["items_freight_brl"] = [fr_sum.get(x, ZERO) for x in r["order_id"]]
    r["items_gross_brl"] = [gr_sum.get(x, ZERO) for x in r["order_id"]]
    r["gross_usd"] = [None if (x in missing_usd) else usd_sum.get(x, ZERO) for x in r["order_id"]]

    # R5 payments
    pay = payment_summary(kaggle["payments"][kaggle["payments"]["order_id"].isin(r["order_id"])])
    r = r.merge(pay, on="order_id", how="left")
    for c in ("payments_total_brl", "voucher_total_brl", "net_cash_paid_brl"):
        r[c] = [ZERO if (x is None or (not isinstance(x, Decimal) and pd.isna(x))) else x for x in r[c]]
    r["payment_mismatch"] = flag_payment_mismatch(r, dq)

    # R14 reviews
    rev_df = latest_review_per_order(kaggle["reviews"], set(r["order_id"]), dq)
    r = r.merge(rev_df, on="order_id", how="left")
    r["review_score"] = r["review_score"].astype("Int64")

    # R9 / R10 delivery performance (revenue-eligible orders with a delivery date only - R3)
    national, regional, ref_holiday = holiday_calendar(ref.holidays)
    # NB: derive the mask from `r` itself - merges above reset r's index, so a mask computed earlier would misalign.
    sla_ok = (r["is_revenue_eligible"] == 1) & r["delivered_ts"].notna()
    approval_basis = r["approved_ts"].where(r["approved_ts"].notna(), r["purchase_ts"])
    bd = business_days_between(approval_basis.where(sla_ok), r["delivered_ts"].where(sla_ok),
                               r["customer_state"], national, regional)
    before = sla_ok & bd.isna()
    if before.any():
        dq.add_many("R9", "order", r.loc[before, "order_id"], "delivered_before_approval",
                    "delivery date is before approval date: business days left NULL")
    r["delivery_business_days"] = bd
    r["is_on_time"] = on_time(r["delivered_ts"].where(sla_ok), r["estimated_ts"])

    # R11 per seller within order
    grp = it.groupby(["order_id", "seller_id"], as_index=False).agg(
        items_count=("order_item_id", "size"), earliest_shipping_limit_ts=("shipping_limit_ts", "min"))
    ps = _sum_by(zip(it["order_id"], it["seller_id"]), it["price_brl"])
    grp["price_total_brl"] = [ps[(a, b)] for a, b in zip(grp["order_id"], grp["seller_id"])]
    ocols = r[["order_id", "purchase_ts", "year_month", "is_revenue_eligible", "carrier_ts", "delivered_ts",
               "is_on_time", "review_score"]]
    os_ = grp.merge(ocols, on="order_id", how="left", validate="m:1")
    sla_flag = (os_["is_revenue_eligible"] == 1) & os_["delivered_ts"].notna()
    os_["sla_result"] = sla_result(os_["carrier_ts"], os_["earliest_shipping_limit_ts"], os_["is_on_time"], sla_flag)
    no_carrier = sla_flag & os_["carrier_ts"].isna()
    if no_carrier.any():
        dq.add_many("R11", "order", os_.loc[no_carrier, "order_id"], "missing_carrier_date",
                    "delivered order without carrier handover date: seller_late cannot be tested")
    no_limit = os_["earliest_shipping_limit_ts"].isna() & sla_flag
    if no_limit.any():
        dq.add_many("R11", "order", os_.loc[no_limit, "order_id"], "missing_shipping_limit_date", "")

    # ------------------------------------------------------------------ dimensions
    states = ref.states.set_index("state_code")
    home_df = home.loc[home.index.intersection(r["customer_unique_id"].unique())]
    dim_customer = pd.DataFrame({
        "customer_unique_id": home_df.index,
        "home_state": home_df["customer_state"].to_numpy(),
        "home_state_name": home_df["customer_state"].map(states["state_name"]).to_numpy(),
        "home_region": home_df["customer_state"].map(states["region"]).to_numpy(),
        "first_order_ts": home_df["purchase_ts"].to_numpy(),
    })
    dim_seller = sellers[["seller_id", "seller_zip", "seller_city", "seller_state", "state_name", "region",
                          "lat", "lng"]].rename(columns={"seller_zip": "zip_prefix", "seller_city": "city",
                                                         "seller_state": "state"})
    used_products = pd.DataFrame({"product_id": sorted(set(it["product_id"]))})
    dim_product = used_products.merge(products, on="product_id", how="left")
    stub = dim_product["category_en"].isna()
    if stub.any():
        dim_product.loc[stub, ["category_en", "category_source"]] = ["uncategorized", "none"]

    years = sorted({d.year for d in r["purchase_date"]} | {t.year for t in r["delivered_ts"].dropna()}
                   | {t.year for t in r["estimated_ts"].dropna()})
    dim_date = _dim_date(years, national, ref_holiday)

    camp_rows, tgt_rows = [], []
    for c in internal.campaigns:
        camp_rows.append((c.campaign_id, c.name, c.start_ts, c.end_ts, c.window_days, c.priority,
                          int(c.all_geo), int(c.all_categories)))
        tgt_rows += [(c.campaign_id, "state", v) for v in sorted(c.states)]
        tgt_rows += [(c.campaign_id, "region", v) for v in sorted(c.regions)]
        tgt_rows += [(c.campaign_id, "category", v) for v in sorted(c.categories)]
    dim_campaign = pd.DataFrame(camp_rows, columns=["campaign_id", "name", "start_ts", "end_ts", "window_days",
                                                     "priority", "all_geo", "all_categories"])
    dim_campaign_target = pd.DataFrame(tgt_rows, columns=["campaign_id", "target_type", "target_value"])

    # ------------------------------------------------------------------ holiday API findings (R9 documentation)
    reg = ref_holiday[ref_holiday["scope"] == "regional"]
    dq.add("R9", "holiday_api", "summary", "info_holidays_found",
           f"national={int((ref_holiday['scope'] == 'national').sum())} regional_rows={len(reg)} "
           f"regional_states={sorted(reg['state_code'].unique())} (types counted: Public)")

    # ------------------------------------------------------------------ final shaping
    fact_order = r[["order_id", "customer_unique_id", "customer_id", "order_status", "purchase_ts", "purchase_date",
                    "year_month", "approved_ts", "carrier_ts", "delivered_ts", "estimated_ts", "customer_zip",
                    "customer_state", "customer_region", "is_revenue_eligible", "is_on_time",
                    "delivery_business_days", "items_count", "items_price_brl", "items_freight_brl",
                    "items_gross_brl", "gross_usd", "payments_total_brl", "voucher_total_brl",
                    "net_cash_paid_brl", "payment_mismatch", "fx_rate", "fx_rate_date", "review_score"]].rename(
        columns={"estimated_ts": "estimated_delivery_date", "customer_zip": "customer_zip_prefix"})
    fact_order["estimated_delivery_date"] = fact_order["estimated_delivery_date"].dt.date
    fact_order_item = it[["order_id", "order_item_id", "product_id", "seller_id", "purchase_ts", "year_month",
                          "is_revenue_eligible", "customer_state", "customer_region", "category_en",
                          "shipping_limit_ts", "price_brl", "freight_brl", "gross_brl", "fx_rate", "fx_rate_date",
                          "gross_usd", "seller_tier", "commission_rate", "commission_rate_source",
                          "commission_brl", "commission_usd", "distance_km", "campaign_id"]]
    fact_order_seller = os_[["order_id", "seller_id", "purchase_ts", "year_month", "is_revenue_eligible",
                             "items_count", "price_total_brl", "earliest_shipping_limit_ts", "sla_result",
                             "review_score"]]

    stats["orders_loaded"] = len(fact_order)
    stats["items_loaded"] = len(fact_order_item)
    return TransformResult(
        tables={
            "dim_date": dim_date, "dim_customer": dim_customer, "dim_seller": dim_seller,
            "dim_product": dim_product, "dim_campaign": dim_campaign,
            "dim_campaign_target": dim_campaign_target, "seller_month_tier": tiers,
            "fact_order": fact_order, "fact_order_item": fact_order_item,
            "fact_order_seller": fact_order_seller,
            "ref_fx_rate": ref.fx, "ref_holiday": ref_holiday, "ref_state": ref.states,
            "ref_commission_rate": internal.rates, "ref_category_override": internal.overrides,
        },
        dq=dq.to_frame(),
        stats=stats,
    )


def _dim_date(years: list[int], national: set[date], ref_holiday: pd.DataFrame) -> pd.DataFrame:
    if not years:
        return pd.DataFrame(columns=["date_value", "year", "month", "day", "year_month", "day_of_week",
                                     "is_weekend", "is_national_holiday", "holiday_name"])
    days = pd.date_range(f"{min(years)}-01-01", f"{max(years)}-12-31", freq="D")
    names = {}
    for d, n in zip(ref_holiday.loc[ref_holiday["scope"] == "national", "holiday_date"],
                    ref_holiday.loc[ref_holiday["scope"] == "national", "local_name"]):
        names[d] = n
    df = pd.DataFrame({"date_value": days.date})
    df["year"], df["month"], df["day"] = days.year, days.month, days.day
    df["year_month"] = days.strftime("%Y-%m")
    df["day_of_week"] = days.dayofweek
    df["is_weekend"] = (days.dayofweek >= 5).astype(int)
    df["is_national_holiday"] = [int(d in national) for d in df["date_value"]]
    df["holiday_name"] = [names.get(d) for d in df["date_value"]]
    return df
