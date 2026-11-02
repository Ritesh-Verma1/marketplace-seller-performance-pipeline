"""R9 business-day lead time, R10 on-time, R11 responsibility for lateness."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .common import DQLog

HOLIDAY_TYPES = {"Public"}   # Nager.Date 'types' counted as holidays; 'Optional', 'Bank' etc. are not


def holiday_calendar(holidays: pd.DataFrame, allowed_types: set[str] = HOLIDAY_TYPES):
    """Split the raw Nager rows into (national_dates, regional {state: dates}).

    A row with a non-empty `counties` list (e.g. ['BR-SP']) is regional and only applies to those
    states; a row without counties is national.  Rows whose `types` don't include an allowed type
    (e.g. only 'Optional') are ignored.  Returns the sets plus a flat DataFrame for ref_holiday."""
    national: set[date] = set()
    regional: dict[str, set[date]] = {}
    flat = []
    for r in holidays.itertuples(index=False):
        types = list(r.types) if r.types else []
        if types and not (set(types) & allowed_types):
            continue
        counties = [c for c in (r.counties or [])]
        if counties:
            for c in counties:
                st = str(c).split("-")[-1].strip().upper()
                regional.setdefault(st, set()).add(r.date)
                flat.append((r.date, st, "regional", r.local_name, r.name, ",".join(types)))
        else:
            national.add(r.date)
            flat.append((r.date, "", "national", r.local_name, r.name, ",".join(types)))
    ref = pd.DataFrame(flat, columns=["holiday_date", "state_code", "scope", "local_name", "name", "holiday_type"])
    ref = ref.drop_duplicates(["holiday_date", "state_code", "local_name"]).reset_index(drop=True)
    return national, regional, ref


def business_days_between(start: pd.Series, end: pd.Series, states: pd.Series,
                          national: set[date], regional: dict[str, set[date]]) -> pd.Series:
    """Number of business days in the half-open window (start, end]  -> EXCLUSIVE of the start day,
    INCLUSIVE of the end day (R9).  Same-day delivery = 0.  Business day = Mon-Fri and not a national
    holiday, and not a regional holiday of the customer's state.
    Rows where start/end is missing or end < start return <NA>."""
    s = pd.to_datetime(start).dt.normalize()
    e = pd.to_datetime(end).dt.normalize()
    result = pd.Series(pd.NA, index=start.index, dtype="Int64")
    valid = s.notna() & e.notna() & (e >= s)
    if not valid.any():
        return result
    st_arr = states.fillna("").str.upper()
    for st in st_arr[valid].unique():
        m = valid & (st_arr == st)
        hol = sorted(national | regional.get(st, set()))
        hol_arr = np.array(hol, dtype="datetime64[D]") if hol else np.array([], dtype="datetime64[D]")
        a = (s[m] + pd.Timedelta(days=1)).values.astype("datetime64[D]")
        b = (e[m] + pd.Timedelta(days=1)).values.astype("datetime64[D]")
        result.loc[m] = np.busday_count(a, b, holidays=hol_arr)
    return result


def on_time(delivered: pd.Series, estimated: pd.Series) -> pd.Series:
    """R10: delivered DATE <= estimated DATE (dates, not timestamps). <NA> if either missing."""
    d = pd.to_datetime(delivered).dt.normalize()
    e = pd.to_datetime(estimated).dt.normalize()
    out = pd.Series(pd.NA, index=delivered.index, dtype="Int64")
    ok = d.notna() & e.notna()
    out.loc[ok] = (d[ok] <= e[ok]).astype(int)
    return out


def sla_result(carrier_ts: pd.Series, earliest_limit: pd.Series, is_on_time: pd.Series,
               sla_eligible: pd.Series) -> pd.Series:
    """R11 per seller within an order.
       carrier handover after the seller's (earliest) shipping_limit_date -> seller_late
       else order late per R10                                              -> carrier_late
       else                                                                  -> on_time
    Only evaluated when the order is revenue eligible AND has a delivery date (R3)."""
    out = pd.Series(pd.NA, index=carrier_ts.index, dtype="object")
    seller_late = carrier_ts.notna() & earliest_limit.notna() & (carrier_ts > earliest_limit)
    carrier_late = ~seller_late & (is_on_time == 0)
    out[sla_eligible & seller_late] = "seller_late"
    out[sla_eligible & carrier_late.fillna(False)] = "carrier_late"
    out[sla_eligible & ~seller_late & (is_on_time == 1).fillna(False)] = "on_time"
    return out
