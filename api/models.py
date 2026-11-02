"""Pydantic response models. Money is Decimal inside the app and rendered as a JSON number."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, PlainSerializer

Money = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")]
OptMoney = Annotated[Optional[Decimal],
                     PlainSerializer(lambda v: None if v is None else float(v), return_type=Optional[float],
                                     when_used="json")]


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(_Base):
    status: str
    database: str


class Page(_Base):
    limit: int
    offset: int
    total: Optional[int] = None


# ---------------------------------------------------------------- orders
class OrderItemOut(_Base):
    order_item_id: int
    product_id: str
    seller_id: str
    category_en: str
    shipping_limit_ts: Optional[datetime] = None
    price_brl: Money
    freight_brl: Money
    gross_brl: Money
    gross_usd: OptMoney = None
    seller_tier: Optional[str] = None
    commission_rate: OptMoney = None
    commission_rate_source: Optional[str] = None
    commission_brl: OptMoney = None
    commission_usd: OptMoney = None
    distance_km: OptMoney = None
    campaign_id: Optional[str] = None


class OrderSellerOut(_Base):
    seller_id: str
    items_count: int
    price_total_brl: Money
    earliest_shipping_limit_ts: Optional[datetime] = None
    sla_result: Optional[str] = None          # seller_late / carrier_late / on_time / null (no SLA, R3)
    review_score: Optional[int] = None


class DQIssueOut(_Base):
    rule_id: str
    entity_type: str
    entity_id: str
    issue_type: str
    details: Optional[str] = None
    run_id: str


class FxOut(_Base):
    rate_brl_usd: OptMoney = None
    rate_date_used: Optional[date] = None


class OrderOut(_Base):
    order_id: str
    customer_unique_id: str
    order_status: str
    purchase_ts: datetime
    approved_ts: Optional[datetime] = None
    carrier_ts: Optional[datetime] = None
    delivered_ts: Optional[datetime] = None
    estimated_delivery_date: Optional[date] = None
    customer_state: Optional[str] = None
    customer_region: Optional[str] = None
    is_revenue_eligible: bool
    is_on_time: Optional[bool] = None
    delivery_business_days: Optional[int] = None
    review_score: Optional[int] = None
    items_count: int
    items_price_brl: Money
    items_freight_brl: Money
    items_gross_brl: Money
    gross_usd: OptMoney = None
    payments_total_brl: Money
    voucher_total_brl: Money
    net_cash_paid_brl: Money
    payment_mismatch: bool
    fx: FxOut
    campaigns: list[str]
    items: list[OrderItemOut]
    sellers: list[OrderSellerOut]
    data_quality_issues: list[DQIssueOut]


# ---------------------------------------------------------------- sellers
class SellerScorecard(_Base):
    seller_id: str
    month: str
    tier: str
    prev_month_revenue_brl: Money
    item_count: int
    gross_brl: Money
    gross_usd: Money
    commission_brl: Money
    commission_usd: Money
    delivered_orders: int
    on_time_pct: OptMoney = None
    seller_late_count: int
    avg_review_score: OptMoney = None


class TopSellerRow(_Base):
    rank: int
    seller_id: str
    value: OptMoney
    delivered_orders_or_items: int


class TopSellersResponse(_Base):
    month: str
    metric: str
    min_delivered_orders_for_rate_metrics: int
    page: Page
    results: list[TopSellerRow]


# ---------------------------------------------------------------- KPIs
class KpiMonth(_Base):
    year_month: str
    orders: int
    unique_customers: int
    gross_usd: Money
    net_cash_paid_brl: Money
    on_time_pct: OptMoney = None
    avg_delivery_business_days: OptMoney = None


class KpiResponse(_Base):
    start: str
    end: str
    region: Optional[str] = None
    page: Page
    results: list[KpiMonth]


# ---------------------------------------------------------------- campaigns
class CampaignWindowStats(_Base):
    items: int
    orders: int
    gross_usd: Money
    avg_order_value_usd: OptMoney = None


class CampaignPerformance(_Base):
    campaign_id: str
    name: str
    start: datetime
    end: datetime
    window_days: int
    priority: int
    items_attributed_all_statuses: int
    campaign: CampaignWindowStats
    baseline_start: datetime
    baseline_end_exclusive: datetime
    baseline: CampaignWindowStats
    gross_usd_change_pct: OptMoney = None
    orders_change_pct: OptMoney = None
    note: str


# ---------------------------------------------------------------- data quality
class DQSummaryRow(_Base):
    rule_id: str
    issue_type: str
    issue_count: int


class DQSummary(_Base):
    run_id: str
    total_issues: int
    page: Page
    results: list[DQSummaryRow]
