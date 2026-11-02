"""Load transformed tables into MySQL.

Idempotency / sub-range design
  * Everything for one run happens in ONE transaction (all-or-nothing).
  * Facts are REPLACED for the processed purchase-timestamp window only:
        DELETE ... WHERE purchase_ts >= :start AND purchase_ts < :end_exclusive  (children first)
    then re-inserted.  Running the same range twice therefore yields identical rows, and other
    months are never touched.
  * Dimensions / reference tables are UPSERTed (INSERT ... ON DUPLICATE KEY UPDATE) so a sub-range run
    never removes rows that facts of other months still reference.
  * seller_month_tier rows are replaced for the months in the window (R12 - see run.py cascade).
  * data_quality_issue rows are appended per run_id (history); summaries query by run_id.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"
CHUNK = 5000

# table -> (columns in DB order as produced by the transform, primary-key columns)
DIM_SPECS = {
    "ref_state": (["state_code", "state_name", "region"], ["state_code"]),
    "ref_fx_rate": (["rate_date", "brl_usd"], ["rate_date"]),
    "dim_date": (["date_value", "year", "month", "day", "year_month", "day_of_week", "is_weekend",
                  "is_national_holiday", "holiday_name"], ["date_value"]),
    "dim_customer": (["customer_unique_id", "home_state", "home_state_name", "home_region", "first_order_ts"],
                     ["customer_unique_id"]),
    "dim_seller": (["seller_id", "zip_prefix", "city", "state", "state_name", "region", "lat", "lng"],
                   ["seller_id"]),
    "dim_product": (["product_id", "category_pt", "category_en", "category_source"], ["product_id"]),
    "dim_campaign": (["campaign_id", "name", "start_ts", "end_ts", "window_days", "priority", "all_geo",
                      "all_categories"], ["campaign_id"]),
}
HOLIDAY_COLS = ["holiday_date", "state_code", "scope", "local_name", "name", "holiday_type"]
FACT_COLS = {
    "fact_order": ["order_id", "customer_unique_id", "customer_id", "order_status", "purchase_ts", "purchase_date",
                   "year_month", "approved_ts", "carrier_ts", "delivered_ts", "estimated_delivery_date",
                   "customer_zip_prefix", "customer_state", "customer_region", "is_revenue_eligible",
                   "is_on_time", "delivery_business_days", "items_count", "items_price_brl",
                   "items_freight_brl", "items_gross_brl", "gross_usd", "payments_total_brl",
                   "voucher_total_brl", "net_cash_paid_brl", "payment_mismatch", "fx_rate", "fx_rate_date",
                   "review_score"],
    "fact_order_item": ["order_id", "order_item_id", "product_id", "seller_id", "purchase_ts", "year_month",
                        "is_revenue_eligible", "customer_state", "customer_region", "category_en",
                        "shipping_limit_ts", "price_brl", "freight_brl", "gross_brl", "fx_rate", "fx_rate_date",
                        "gross_usd", "seller_tier", "commission_rate", "commission_rate_source",
                        "commission_brl", "commission_usd", "distance_km", "campaign_id"],
    "fact_order_seller": ["order_id", "seller_id", "purchase_ts", "year_month", "is_revenue_eligible",
                          "items_count", "price_total_brl", "earliest_shipping_limit_ts", "sla_result",
                          "review_score"],
}
TIER_COLS = ["seller_id", "year_month", "prev_month_revenue_brl", "tier"]


# ------------------------------------------------------------------------------ helpers
def _py(v):
    """Convert pandas/numpy scalars to plain Python values (None for any kind of NULL)."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (Decimal, str, bool, int, dt.date)) and not isinstance(v, pd.Timestamp):
        return v
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return None if np.isnan(v) else float(v)
    if isinstance(v, np.bool_):
        return int(v)
    if isinstance(v, float):
        return None if v != v else v
    return v


def records(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    """DataFrame -> list of dicts with python-native values / None."""
    arrays = [df[c].tolist() for c in cols]
    return [{c: _py(v) for c, v in zip(cols, row)} for row in zip(*arrays)]


def _exec_many(conn: Connection, sql: str, rows: list[dict]) -> int:
    for i in range(0, len(rows), CHUNK):
        conn.execute(text(sql), rows[i:i + CHUNK])
    return len(rows)


def _q(col: str) -> str:
    """Backtick-quote a column name (year_month is a reserved word in MySQL)."""
    return f"`{col}`"


def insert_rows(conn: Connection, table: str, cols: list[str], rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = f"INSERT INTO {table} ({', '.join(_q(c) for c in cols)}) VALUES ({', '.join(':' + c for c in cols)})"
    return _exec_many(conn, sql, rows)


def upsert_rows(conn: Connection, table: str, cols: list[str], pk: list[str], rows: list[dict]) -> int:
    if not rows:
        return 0
    upd = [c for c in cols if c not in pk] or pk[:1]
    sql = (f"INSERT INTO {table} ({', '.join(_q(c) for c in cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
           f"ON DUPLICATE KEY UPDATE {', '.join(f'{_q(c)} = VALUES({_q(c)})' for c in upd)}")
    return _exec_many(conn, sql, rows)


# ------------------------------------------------------------------------------ schema / run bookkeeping
def ensure_schema(engine: Engine) -> None:
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    with engine.begin() as conn:
        for stmt in (s.strip() for s in ddl.split(";\n")):
            body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip().rstrip(";")
            if body:
                conn.execute(text(body))


def start_run(engine: Engine, run_id: str, start: dt.date, end: dt.date, params: dict) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO pipeline_run (run_id, range_start, range_end, parameters, started_at, status) "
            "VALUES (:run_id, :s, :e, :params, :now, 'running')"),
            {"run_id": run_id, "s": start, "e": end, "params": json.dumps(params, default=str),
             "now": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)})


def finish_run(engine: Engine, run_id: str, status: str, row_counts: dict | None = None,
               error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE pipeline_run SET status = :status, finished_at = :now, row_counts = :rc, error_message = :err "
            "WHERE run_id = :run_id"),
            {"status": status, "now": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
             "rc": json.dumps(row_counts) if row_counts is not None else None,
             "err": (error or None) and error[:4000], "run_id": run_id})


# ------------------------------------------------------------------------------ main load
def load_result(engine: Engine, tables: dict[str, pd.DataFrame], dq: pd.DataFrame, run_id: str,
                start: dt.date, end: dt.date) -> dict[str, int]:
    """Replace the [start, end] window atomically. Returns rows written per table."""
    lo = dt.datetime.combine(start, dt.time.min)
    hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min)
    counts: dict[str, int] = {}
    months = sorted(tables["seller_month_tier"]["year_month"].unique())

    with engine.begin() as conn:
        # 1. remove the window's facts (children first because of foreign keys)
        for t in ("fact_order_item", "fact_order_seller", "fact_order"):
            res = conn.execute(text(f"DELETE FROM {t} WHERE purchase_ts >= :lo AND purchase_ts < :hi"),
                               {"lo": lo, "hi": hi})
            log.info("deleted %d rows from %s for %s..%s", res.rowcount, t, start, end)
        if months:
            conn.execute(text("DELETE FROM seller_month_tier WHERE `year_month` IN (%s)" %
                              ", ".join(f":m{i}" for i in range(len(months)))),
                         {f"m{i}": m for i, m in enumerate(months)})

        # 2. upsert reference + dimensions (never deleted: other months' facts point at them)
        for t, (cols, pk) in DIM_SPECS.items():
            counts[t] = upsert_rows(conn, t, cols, pk, records(tables[t], cols))
        counts["ref_holiday"] = upsert_rows(conn, "ref_holiday", HOLIDAY_COLS,
                                            ["holiday_date", "state_code", "local_name"],
                                            records(tables["ref_holiday"], HOLIDAY_COLS))
        # small, fully-owned reference tables: replace
        conn.execute(text("DELETE FROM ref_commission_rate"))
        counts["ref_commission_rate"] = insert_rows(
            conn, "ref_commission_rate", ["tier", "rate", "effective_from", "effective_to"],
            records(tables["ref_commission_rate"], ["tier", "rate", "effective_from", "effective_to"]))
        conn.execute(text("DELETE FROM ref_category_override"))
        counts["ref_category_override"] = insert_rows(
            conn, "ref_category_override", ["category_en", "fixed_rate", "effective_from", "effective_to", "notes"],
            records(tables["ref_category_override"],
                    ["category_en", "fixed_rate", "effective_from", "effective_to", "notes"]))
        conn.execute(text("DELETE FROM dim_campaign_target"))
        counts["dim_campaign_target"] = insert_rows(
            conn, "dim_campaign_target", ["campaign_id", "target_type", "target_value"],
            records(tables["dim_campaign_target"], ["campaign_id", "target_type", "target_value"]))

        # 3. tiers then facts (parents before children)
        counts["seller_month_tier"] = insert_rows(conn, "seller_month_tier", TIER_COLS,
                                                  records(tables["seller_month_tier"], TIER_COLS))
        for t in ("fact_order", "fact_order_seller", "fact_order_item"):
            counts[t] = insert_rows(conn, t, FACT_COLS[t], records(tables[t], FACT_COLS[t]))

        # 4. data quality issues of this run
        dq_cols = ["rule_id", "entity_type", "entity_id", "issue_type", "details", "detected_at"]
        rows = records(dq, dq_cols)
        for r_ in rows:
            r_["run_id"] = run_id
        counts["data_quality_issue"] = insert_rows(conn, "data_quality_issue", dq_cols + ["run_id"], rows)
    return counts


# ------------------------------------------------------------------------------ cascade support (R12)
def loaded_months(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT DISTINCT `year_month` FROM fact_order"))}


def stored_tiers(engine: Engine, month: str) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT seller_id, tier FROM seller_month_tier WHERE `year_month` = :m"),
                            {"m": month}).all()
    return {r[0]: r[1] for r in rows}
