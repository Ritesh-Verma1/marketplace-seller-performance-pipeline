"""R18 campaign attribution and R14 review de-duplication."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import DQLog
from .internal_files import Campaign


def attribute_campaigns(items: pd.DataFrame, campaigns: list[Campaign]) -> pd.Series:
    """Campaign id per item (None when nothing matches).

    An item matches a campaign when ALL hold:
      * purchase_ts inside [start_ts, end_ts] (Sao Paulo local; date-only end = whole last day)
      * customer state in target_states OR customer region in target_regions (ALL = any)
      * item English category in categories (ALL = any)
    Several matches -> highest priority number wins; tie -> the campaign with the later start.
    `items` needs: purchase_ts, customer_state, customer_region, category_en."""
    result = np.empty(len(items), dtype=object)
    result[:] = None
    ts = items["purchase_ts"].to_numpy()
    state = items["customer_state"].fillna("").str.upper().to_numpy(dtype=object)
    region = items["customer_region"].fillna("").str.lower().to_numpy(dtype=object)
    cat = items["category_en"].fillna("").str.lower().to_numpy(dtype=object)

    ordered = sorted(campaigns, key=lambda c: (c.priority, c.start_ts, c.campaign_id), reverse=True)
    unassigned = np.ones(len(items), dtype=bool)
    for c in ordered:
        in_window = (ts >= np.datetime64(c.start_ts)) & (ts <= np.datetime64(c.end_ts))
        geo = np.ones(len(items), dtype=bool) if c.all_geo else (
            np.isin(state, list(c.states)) | np.isin(region, list(c.regions)))
        cats = np.ones(len(items), dtype=bool) if c.all_categories else np.isin(cat, list(c.categories))
        match = in_window & geo & cats & unassigned
        result[match] = c.campaign_id
        unassigned &= ~match
    return pd.Series(result, index=items.index, dtype=object)


def latest_review_per_order(reviews: pd.DataFrame, order_ids: set[str], dq: DQLog) -> pd.DataFrame:
    """R14: keep the review with the latest review_answer_timestamp per order.
    Ties broken deterministically by review_creation_date then review_id. Dropped reviews are logged.
    Returns order_id, review_score (int)."""
    r = reviews[reviews["order_id"].isin(order_ids)].copy()
    r["answer_ts"] = pd.to_datetime(r["review_answer_timestamp"], errors="coerce")
    r["create_ts"] = pd.to_datetime(r["review_creation_date"], errors="coerce")
    r["score"] = pd.to_numeric(r["review_score"], errors="coerce")
    bad = r["score"].isna() | ~r["score"].between(1, 5)
    if bad.any():
        dq.add_many("R14", "order", r.loc[bad, "order_id"], "invalid_review_score",
                    [f"review {i} has score {s!r}" for i, s in zip(r.loc[bad, "review_id"], r.loc[bad, "review_score"])])
        r = r[~bad]
    r = r.sort_values(["order_id", "answer_ts", "create_ts", "review_id"], na_position="first")
    dup = r.duplicated("order_id", keep="last")
    if dup.any():
        dq.add_many("R14", "order", r.loc[dup, "order_id"], "duplicate_review_dropped",
                    [f"older review {i} dropped; latest answer timestamp kept" for i in r.loc[dup, "review_id"]])
    kept = r[~dup]
    return pd.DataFrame({"order_id": kept["order_id"].to_numpy(), "review_score": kept["score"].astype(int).to_numpy()})
