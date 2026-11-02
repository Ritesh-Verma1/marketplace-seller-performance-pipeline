"""Builds a tiny SQLite copy of the model from the synthetic transform output, so the API SQL can be
verified without MySQL.  (Column names/semantics are identical to sql/schema.sql.)"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd

from pipeline.transform.build import build
from pipeline.transform.common import DQLog
from tests.synth import load_internal, make_kaggle, make_reference

RUN_ID = "11111111-1111-1111-1111-111111111111"
TABLES = ["fact_order", "fact_order_item", "fact_order_seller", "seller_month_tier", "dim_seller",
          "dim_campaign", "dim_campaign_target", "ref_state", "ref_holiday"]


def _plain(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        def conv(v):
            if v is None or v is pd.NA or v is pd.NaT or (isinstance(v, float) and v != v):
                return None
            if isinstance(v, np.bool_):
                return int(v)
            if isinstance(v, np.integer):
                return int(v)
            if isinstance(v, np.floating):
                return None if np.isnan(v) else float(v)
            if isinstance(v, Decimal):
                return float(v)
            if isinstance(v, pd.Timestamp):
                return v.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(v, datetime):
                return v.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(v, date):
                return v.isoformat()
            return v
        out[c] = pd.Series([conv(v) for v in out[c]], index=out.index, dtype=object)
    return out


def load_sqlite(con) -> None:
    """`con` is a sqlite3 connection OR a SQLAlchemy engine/connection accepted by DataFrame.to_sql."""
    dq = DQLog()
    res = build(make_kaggle(), load_internal(dq), make_reference(), date(2018, 2, 1), date(2018, 3, 31), dq)
    for t in TABLES:
        _plain(res.tables[t]).to_sql(t, con, index=False, if_exists="replace")
    issues = _plain(res.dq)
    issues.insert(0, "issue_id", range(1, len(issues) + 1))
    issues["run_id"] = RUN_ID
    issues.to_sql("data_quality_issue", con, index=False, if_exists="replace")
    pd.DataFrame([{"run_id": RUN_ID, "status": "success", "started_at": "2018-04-01 00:00:00",
                   "range_start": "2018-02-01", "range_end": "2018-03-31"}]).to_sql(
        "pipeline_run", con, index=False, if_exists="replace")


def sqlite_conn() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    load_sqlite(con)
    return con
