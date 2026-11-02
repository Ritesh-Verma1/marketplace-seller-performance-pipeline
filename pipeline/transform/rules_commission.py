"""R12 seller tier and R13 commission rate."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

from .common import DQLog, ZERO, prev_month, to_decimal

BRONZE_MAX_CENTS = 500_000      # below 5,000.00      -> Bronze
SILVER_MAX_CENTS = 2_000_000    # 5,000.00 .. 20,000.00 inclusive -> Silver ; above -> Gold


def tier_for_cents(cents: int) -> str:
    """R12 thresholds on integer cents (exact; no float comparison at 5,000 / 20,000)."""
    if cents < BRONZE_MAX_CENTS:
        return "Bronze"
    if cents <= SILVER_MAX_CENTS:
        return "Silver"
    return "Gold"


def tier_for_revenue(revenue: Decimal) -> str:
    return tier_for_cents(int((revenue * 100).to_integral_value()))


def monthly_revenue_cents(kaggle_orders: pd.DataFrame, kaggle_items: pd.DataFrame) -> pd.DataFrame:
    """Revenue-eligible (status = delivered) price total per seller and purchase month, taken from the
    FULL source - not limited to --start-date/--end-date - because a tier for month M needs month M-1
    even when M-1 is outside the processed range (R12, sub-range reruns).
    Returns columns seller_id, year_month, cents (int)."""
    o = kaggle_orders.loc[kaggle_orders["order_status"].str.strip().str.lower() == "delivered",
                          ["order_id", "order_purchase_timestamp"]]
    ts = pd.to_datetime(o["order_purchase_timestamp"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    o = o.assign(year_month=ts.dt.strftime("%Y-%m")).dropna(subset=["year_month"])
    it = kaggle_items[["order_id", "seller_id", "price"]].merge(o[["order_id", "year_month"]], on="order_id")
    it["cents"] = [int((Decimal(p) * 100).to_integral_value()) for p in it["price"]]
    return it.groupby(["seller_id", "year_month"], as_index=False)["cents"].sum()


def seller_tiers(revenue: pd.DataFrame, seller_ids: list[str], months: list[str]) -> pd.DataFrame:
    """Tier for every seller x month: based on the previous calendar month's revenue; no sales -> Bronze.
    Returns seller_id, year_month, prev_month_revenue_brl (Decimal), tier."""
    lookup = {(s, m): c for s, m, c in zip(revenue["seller_id"], revenue["year_month"], revenue["cents"])}
    rows = []
    for m in months:
        pm = prev_month(m)
        for s in seller_ids:
            cents = lookup.get((s, pm), 0)
            rows.append((s, m, Decimal(cents) / 100, tier_for_cents(cents)))
    return pd.DataFrame(rows, columns=["seller_id", "year_month", "prev_month_revenue_brl", "tier"])


# ----------------------------------------------------------------------------- R13
def resolve_commission_rates(items: pd.DataFrame, rates: pd.DataFrame, overrides: pd.DataFrame,
                             dq: DQLog) -> pd.DataFrame:
    """Adds commission_rate (Decimal | None) and commission_rate_source.

    `items` needs: order_id, order_item_id, purchase_date (date), category_en, seller_tier, eligible (bool).
    1. a category override active on the purchase date wins (fixed_rate)
    2. otherwise the tier rate active on the purchase date
    Items that are not revenue eligible get NULL (commission only on delivered orders, R2).
    No rate found -> NULL + DQ row (never a silent default)."""
    n = len(items)
    rate_arr = np.empty(n, dtype=object)
    rate_arr[:] = None
    src = np.empty(n, dtype=object)
    src[:] = None
    pdate = np.array(list(items["purchase_date"]), dtype="datetime64[D]")
    cat = items["category_en"].to_numpy(dtype=object)
    tier = items["seller_tier"].to_numpy(dtype=object)
    eligible = items["eligible"].to_numpy(dtype=bool)

    def window(row) -> np.ndarray:
        lo = np.datetime64(row.effective_from)
        m = pdate >= lo
        if row.effective_to is not None and not pd.isna(row.effective_to):
            m &= pdate <= np.datetime64(row.effective_to)
        return m

    # tier rates (windows were de-overlapped during cleaning)
    for r in rates.itertuples(index=False):
        m = window(r) & (tier == r.tier) & eligible
        rate_arr[m] = r.rate
        src[m] = "tier"
    # category overrides take precedence (applied last)
    for r in overrides.itertuples(index=False):
        m = window(r) & (cat == r.category_en) & eligible
        rate_arr[m] = r.fixed_rate
        src[m] = "category_override"

    out = items[["order_id", "order_item_id"]].copy()
    out["commission_rate"] = rate_arr
    out["commission_rate_source"] = src
    missing = eligible & np.array([x is None for x in rate_arr])
    if missing.any():
        sub = items[missing]
        dq.add_many("R13", "order_item", [f"{o}#{i}" for o, i in zip(sub["order_id"], sub["order_item_id"])],
                    "no_commission_rate_found",
                    [f"tier={t} category={c} purchase_date={d}; commission left NULL"
                     for t, c, d in zip(sub["seller_tier"], sub["category_en"], sub["purchase_date"])])
    return out
