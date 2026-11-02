"""R4 item amounts, R5 payment reconciliation, R6 FX conversion, R8 categories."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

from .common import DQLog, ZERO, is_missing, to_decimal

PAYMENT_TOLERANCE = Decimal("1.00")


# ----------------------------------------------------------------------------- R6
def lookup_fx(dates: list[date], fx_dates: list[date], fx_rates: list[Decimal]):
    """For every date return (rate, fx_rate_date) = the rate of that date, or of the most recent
    EARLIER date that has one (weekends/holidays). Never uses a later date (R6).
    Returns (None, None) when the date is before the first available rate."""
    fd = np.array(fx_dates, dtype="datetime64[D]")
    q = np.array(dates, dtype="datetime64[D]")
    idx = np.searchsorted(fd, q, side="right") - 1          # last fx date <= q
    rates, used = [], []
    for i in idx:
        if i < 0:
            rates.append(None)
            used.append(None)
        else:
            rates.append(fx_rates[i])
            used.append(fx_dates[i])
    return rates, used


def attach_fx(orders: pd.DataFrame, fx: pd.DataFrame, dq: DQLog) -> pd.DataFrame:
    """Adds fx_basis_date, fx_rate, fx_rate_date to an orders frame.
    Basis date = date of order_approved_at; falls back to purchase date when NULL (R6)."""
    out = orders.copy()
    approved = out["approved_ts"]
    basis = approved.where(approved.notna(), out["purchase_ts"]).dt.date
    fallback = approved.isna()
    if fallback.any():
        dq.add_many("R6", "order", out.loc[fallback, "order_id"], "approved_at_null_used_purchase_date",
                    "order_approved_at is NULL; FX date taken from order_purchase_timestamp")
    out["fx_basis_date"] = basis
    rates, used = lookup_fx(list(basis), list(fx["rate_date"]), list(fx["brl_usd"]))
    out["fx_rate"] = pd.Series(rates, index=out.index, dtype=object)
    out["fx_rate_date"] = pd.Series(used, index=out.index, dtype=object)
    miss = out["fx_rate"].isna()
    if miss.any():
        dq.add_many("R6", "order", out.loc[miss, "order_id"], "no_fx_rate_available",
                    [f"no BRL->USD rate on or before {d}" for d in out.loc[miss, "fx_basis_date"]])
    return out


# ----------------------------------------------------------------------------- R5
def payment_summary(payments: pd.DataFrame) -> pd.DataFrame:
    """Per order: payments_total, voucher_total, net_cash_paid (= total excluding vouchers)."""
    total: dict[str, Decimal] = {}
    voucher: dict[str, Decimal] = {}
    for oid, ptype, val in zip(payments["order_id"], payments["payment_type"], payments["payment_value"]):
        v = to_decimal(val)
        if v is None:
            continue
        total[oid] = total.get(oid, ZERO) + v
        if str(ptype).strip().lower() == "voucher":
            voucher[oid] = voucher.get(oid, ZERO) + v
    df = pd.DataFrame({"order_id": list(total)})
    df["payments_total_brl"] = [total[o] for o in df["order_id"]]
    df["voucher_total_brl"] = [voucher.get(o, ZERO) for o in df["order_id"]]
    df["net_cash_paid_brl"] = [t - voucher.get(o, ZERO) for o, t in zip(df["order_id"], df["payments_total_brl"])]
    return df


def flag_payment_mismatch(orders: pd.DataFrame, dq: DQLog) -> pd.Series:
    """|sum(payment_value) - sum(item gross)| > 1.00 BRL -> payment_mismatch (R5). Kept, not dropped."""
    diff = [abs(p - g) for p, g in zip(orders["payments_total_brl"], orders["items_gross_brl"])]
    flag = pd.Series([d > PAYMENT_TOLERANCE for d in diff], index=orders.index)
    if flag.any():
        sub = orders[flag]
        dq.add_many("R5", "order", sub["order_id"], "payment_mismatch",
                    [f"payments={p} items_gross={g} diff={abs(p - g)}"
                     for p, g in zip(sub["payments_total_brl"], sub["items_gross_brl"])])
    return flag.astype(int)


# ----------------------------------------------------------------------------- R8
def resolve_categories(products: pd.DataFrame, patch: dict[str, str], translation: pd.DataFrame,
                       dq: DQLog) -> pd.DataFrame:
    """category_en resolution order (R8):
       1 patch file (wins)  2 Kaggle translation  3 Portuguese name w/o translation -> 'unknown'
       4 no category at all -> 'uncategorized'.   'unknown' and 'uncategorized' stay distinct."""
    tr = {}
    for pt, en in zip(translation["product_category_name"], translation["product_category_name_english"]):
        if not is_missing(pt) and not is_missing(en):
            tr[str(pt).strip().lower()] = str(en).strip().lower()

    out = products[["product_id", "product_category_name"]].copy()
    cat_en, source = [], []
    unknown_seen: set[str] = set()
    for pt in out["product_category_name"]:
        if is_missing(pt) or str(pt).strip() == "":
            cat_en.append("uncategorized"); source.append("none")
            continue
        key = str(pt).strip().lower()
        if key in patch:
            cat_en.append(patch[key]); source.append("patch")
        elif key in tr:
            cat_en.append(tr[key]); source.append("kaggle")
        else:
            cat_en.append("unknown"); source.append("missing_translation")
            unknown_seen.add(key)
    out["category_pt"] = out["product_category_name"].where(out["product_category_name"].notna(), None)
    out["category_en"] = cat_en
    out["category_source"] = source
    for key in sorted(unknown_seen):
        dq.add("R8", "category", key, "category_without_translation",
               "Portuguese category has no translation in patch or Kaggle file -> 'unknown'")
    n_uncat = int((out["category_source"] == "none").sum())
    if n_uncat:
        dq.add("R8", "product", "*", "products_without_category",
               f"{n_uncat} products have no category -> 'uncategorized'")
    return out[["product_id", "category_pt", "category_en", "category_source"]]
