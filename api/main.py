"""Read-only FastAPI service on top of the MySQL model.

Run:  uvicorn api.main:app --reload      (DATABASE_URL env var selects the database)
Docs: http://127.0.0.1:8000/docs
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import queries as Q
from .db import get_conn, get_engine
from .models import (CampaignPerformance, CampaignWindowStats, DQSummary, DQSummaryRow, DQIssueOut, FxOut,
                     HealthResponse, KpiMonth, KpiResponse, OrderItemOut, OrderOut, OrderSellerOut, Page,
                     SellerScorecard, TopSellerRow, TopSellersResponse)

app = FastAPI(title="Marketplace Seller Performance API", version="1.0.0",
              description="Read-only API over the seller performance pipeline (Finance / Ops dashboards).")

MAX_LIMIT = 100
MIN_DELIVERED_FOR_RATE_METRICS = 10          # /sellers/top: on_time_rate and avg_review
YM_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"       # YYYY-MM
YM6_PATTERN = r"^\d{4}(0[1-9]|1[0-2])$"       # YYYYMM


class Metric(str, Enum):
    commission_usd = "commission_usd"
    on_time_rate = "on_time_rate"
    avg_review = "avg_review"


def _all(conn: Connection, sql: str, **params) -> list[dict]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def _one(conn: Connection, sql: str, **params) -> Optional[dict]:
    row = conn.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def _pct_change(new, old) -> Optional[Decimal]:
    if old is None or Decimal(str(old)) == 0:
        return None
    return round((Decimal(str(new)) - Decimal(str(old))) / Decimal(str(old)) * 100, 2)


# ------------------------------------------------------------------------------ /health
@app.get("/health", response_model=HealthResponse, tags=["system"])
def health():
    try:
        with get_engine().connect() as conn:
            conn.execute(text(Q.HEALTH))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "database": f"down: {type(exc).__name__}"})
    return HealthResponse(status="ok", database="up")


# ------------------------------------------------------------------------------ /orders/{order_id}
@app.get("/orders/{order_id}", response_model=OrderOut, tags=["orders"])
def get_order(order_id: str, conn: Connection = Depends(get_conn)):
    o = _one(conn, Q.ORDER_HEADER, order_id=order_id)
    if o is None:
        excluded = _one(conn, Q.ORDER_EXCLUDED, order_id=order_id)
        if excluded:
            raise HTTPException(404, f"Order {order_id} exists in the source but was excluded by rule R2 "
                                     f"({excluded['details']}); it only appears in the data-quality report.")
        raise HTTPException(404, f"Order {order_id} not found")
    items = _all(conn, Q.ORDER_ITEMS, order_id=order_id)
    sellers = _all(conn, Q.ORDER_SELLERS, order_id=order_id)
    dq = _all(conn, Q.ORDER_DQ, order_id=order_id, item_prefix=f"{order_id}#%", limit=MAX_LIMIT)
    return OrderOut(
        **{k: v for k, v in o.items() if k not in ("fx_rate", "fx_rate_date", "customer_id")},
        fx=FxOut(rate_brl_usd=o["fx_rate"], rate_date_used=o["fx_rate_date"]),
        campaigns=sorted({i["campaign_id"] for i in items if i["campaign_id"]}),
        items=[OrderItemOut(**i) for i in items],
        sellers=[OrderSellerOut(**s) for s in sellers],
        data_quality_issues=[DQIssueOut(**d) for d in dq],
    )


# ------------------------------------------------------------------------------ /sellers/...
@app.get("/sellers/top", response_model=TopSellersResponse, tags=["sellers"])
def top_sellers(month: str = Query(..., pattern=YM6_PATTERN, description="YYYYMM"),
                metric: Metric = Query(Metric.commission_usd),
                limit: int = Query(10, ge=1, le=MAX_LIMIT),
                offset: int = Query(0, ge=0),
                conn: Connection = Depends(get_conn)):
    ym = f"{month[:4]}-{month[4:]}"
    rows = _all(conn, Q.TOP_SELLERS[metric.value], ym=ym, limit=limit, offset=offset)
    return TopSellersResponse(
        month=ym, metric=metric.value,
        min_delivered_orders_for_rate_metrics=MIN_DELIVERED_FOR_RATE_METRICS
        if metric != Metric.commission_usd else 0,
        page=Page(limit=limit, offset=offset),
        results=[TopSellerRow(rank=offset + n + 1, seller_id=r["seller_id"], value=r["metric_value"],
                              delivered_orders_or_items=r["basis_count"]) for n, r in enumerate(rows)])


@app.get("/sellers/{seller_id}/scorecard", response_model=SellerScorecard, tags=["sellers"])
def seller_scorecard(seller_id: str, month: str = Query(..., pattern=YM_PATTERN, description="YYYY-MM"),
                     conn: Connection = Depends(get_conn)):
    if _one(conn, Q.SELLER_EXISTS, seller_id=seller_id) is None:
        raise HTTPException(404, f"Seller {seller_id} not found")
    tier = _one(conn, Q.SELLER_TIER, seller_id=seller_id, ym=month)
    if tier is None:
        raise HTTPException(404, f"No data loaded for month {month}; run the pipeline for that month first")
    items = _one(conn, Q.SELLER_ITEMS_AGG, seller_id=seller_id, ym=month)
    orders = _one(conn, Q.SELLER_ORDERS_AGG, seller_id=seller_id, ym=month)
    return SellerScorecard(seller_id=seller_id, month=month, tier=tier["tier"],
                           prev_month_revenue_brl=tier["prev_month_revenue_brl"],
                           item_count=items["item_count"], gross_brl=items["gross_brl"],
                           gross_usd=items["gross_usd"], commission_brl=items["commission_brl"],
                           commission_usd=items["commission_usd"],
                           delivered_orders=orders["delivered_orders"], on_time_pct=orders["on_time_pct"],
                           seller_late_count=orders["seller_late_count"],
                           avg_review_score=orders["avg_review_score"])


# ------------------------------------------------------------------------------ /kpis/monthly
@app.get("/kpis/monthly", response_model=KpiResponse, tags=["kpis"])
def kpis_monthly(start: str = Query(..., pattern=YM_PATTERN, description="YYYY-MM"),
                 end: str = Query(..., pattern=YM_PATTERN, description="YYYY-MM"),
                 region: Optional[str] = Query(None, description="Norte, Nordeste, Centro-Oeste, Sudeste or Sul"),
                 limit: int = Query(24, ge=1, le=MAX_LIMIT),
                 offset: int = Query(0, ge=0),
                 conn: Connection = Depends(get_conn)):
    if start > end:
        raise HTTPException(422, "start must not be after end")
    canonical = None
    if region is not None:
        known = {r["region"].lower(): r["region"] for r in _all(conn, Q.REGIONS)}
        canonical = known.get(region.strip().lower())
        if canonical is None:
            raise HTTPException(422, f"Unknown region '{region}'. Valid: {sorted(known.values())}")
    params = dict(start=start, end=end, region=canonical)
    total = _one(conn, Q.KPIS_MONTH_COUNT, **params)["total"]
    rows = _all(conn, Q.KPIS_MONTHLY, limit=limit, offset=offset, **params)
    return KpiResponse(start=start, end=end, region=canonical, page=Page(limit=limit, offset=offset, total=total),
                       results=[KpiMonth(**r) for r in rows])


# ------------------------------------------------------------------------------ /campaigns/{id}/performance
def _stats(row: dict) -> CampaignWindowStats:
    orders, gross = row["orders"], Decimal(str(row["gross_usd"]))
    aov = (gross / orders).quantize(Decimal("0.01")) if orders else None
    return CampaignWindowStats(items=row["items"], orders=orders, gross_usd=gross, avg_order_value_usd=aov)


@app.get("/campaigns/{campaign_id}/performance", response_model=CampaignPerformance, tags=["campaigns"])
def campaign_performance(campaign_id: str, conn: Connection = Depends(get_conn)):
    c = _one(conn, Q.CAMPAIGN, cid=campaign_id)
    if c is None:
        raise HTTPException(404, f"Campaign {campaign_id} not found")
    start_ts, end_ts = _dt(c["start_ts"]), _dt(c["end_ts"])
    b_start, b_end = start_ts - timedelta(days=c["window_days"]), start_ts
    window = _one(conn, Q.CAMPAIGN_WINDOW, cid=campaign_id)
    baseline = _one(conn, Q.CAMPAIGN_BASELINE, cid=campaign_id, b_start=b_start, b_end=b_end,
                    all_geo=int(c["all_geo"]), all_categories=int(c["all_categories"]))
    camp, base = _stats(window), _stats(baseline)
    return CampaignPerformance(
        campaign_id=c["campaign_id"], name=c["name"], start=start_ts, end=end_ts, window_days=c["window_days"],
        priority=c["priority"], items_attributed_all_statuses=window["items_attributed"],
        campaign=camp, baseline_start=b_start, baseline_end_exclusive=b_end, baseline=base,
        gross_usd_change_pct=_pct_change(camp.gross_usd, base.gross_usd),
        orders_change_pct=_pct_change(camp.orders, base.orders),
        note="campaign = revenue-eligible items attributed to this campaign (R18, after priority resolution); "
             "baseline = revenue-eligible items in the campaign's own target scope during the same number of "
             "days immediately before it")


# ------------------------------------------------------------------------------ /data-quality/summary
@app.get("/data-quality/summary", response_model=DQSummary, tags=["data quality"])
def dq_summary(run_id: Optional[str] = Query(None, description="defaults to the latest successful run"),
               limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
               conn: Connection = Depends(get_conn)):
    if run_id is None:
        latest = _one(conn, Q.LATEST_RUN)
        if latest is None:
            raise HTTPException(404, "No successful pipeline run found")
        run_id = latest["run_id"]
    elif _one(conn, Q.RUN_EXISTS, run_id=run_id) is None:
        raise HTTPException(404, f"Run {run_id} not found")
    rows = _all(conn, Q.DQ_SUMMARY, run_id=run_id, limit=limit, offset=offset)
    total_issues = _one(conn, Q.DQ_TOTAL_ISSUES, run_id=run_id)["total_issues"]
    total_groups = _one(conn, Q.DQ_TOTAL_GROUPS, run_id=run_id)["total_groups"]
    return DQSummary(run_id=run_id, total_issues=total_issues,
                     page=Page(limit=limit, offset=offset, total=total_groups),
                     results=[DQSummaryRow(**r) for r in rows])
