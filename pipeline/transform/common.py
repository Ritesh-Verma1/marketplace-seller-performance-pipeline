"""Shared helpers: Decimal money handling (R7), data-quality log."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

import pandas as pd

MONEY_Q = Decimal("0.01")
RATE_Q = Decimal("0.000001")
ZERO = Decimal("0")


def is_missing(x) -> bool:
    if x is None:
        return True
    try:
        return bool(pd.isna(x))
    except (TypeError, ValueError):
        return False


def to_decimal(x) -> Decimal | None:
    """Parse as Decimal from the exact text (never via float). None for missing."""
    if is_missing(x):
        return None
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x).strip())


def q_money(d: Decimal | None) -> Decimal | None:
    """Round ONLY at the final stored value (R7): 2 decimals for money."""
    return None if d is None else d.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def q_rate(d: Decimal | None) -> Decimal | None:
    """6 decimals for rates (R7)."""
    return None if d is None else d.quantize(RATE_Q, rounding=ROUND_HALF_UP)


def year_month(ts: pd.Series) -> pd.Series:
    return ts.dt.strftime("%Y-%m")


def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def month_range_between(start, end) -> list[str]:
    """['2017-01', '2017-02', ...] for every calendar month touched by [start, end]."""
    out, cur, last = [], f"{start.year}-{start.month:02d}", f"{end.year}-{end.month:02d}"
    while cur <= last:
        out.append(cur)
        cur = next_month(cur)
    return out


def next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


@dataclass
class DQLog:
    """Collects data-quality issues; written to data_quality_issue by the loader.
    Nothing in the pipeline is dropped or defaulted without a row here."""
    rows: list[tuple] = field(default_factory=list)

    def add(self, rule_id: str, entity_type: str, entity_id, issue_type: str, details: str = "") -> None:
        self.rows.append((rule_id, entity_type, str(entity_id), issue_type, str(details)[:500]))

    def add_many(self, rule_id: str, entity_type: str, ids: Iterable, issue_type: str,
                 details: str | Iterable[str] = "") -> None:
        ids = list(ids)
        if isinstance(details, str):
            details = [details] * len(ids)
        for i, d in zip(ids, details):
            self.add(rule_id, entity_type, i, issue_type, d)

    def to_frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.rows, columns=["rule_id", "entity_type", "entity_id", "issue_type", "details"])
        df["detected_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        return df

    def count(self, rule_id: str | None = None, issue_type: str | None = None) -> int:
        return sum(1 for r in self.rows
                   if (rule_id is None or r[0] == rule_id) and (issue_type is None or r[3] == issue_type))
