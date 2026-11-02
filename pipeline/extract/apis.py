"""The three public APIs. Parsing keeps numbers as Decimal (never float) - R7."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from .api_client import ApiClient, ExtractionError

FX_CHUNK_DAYS = 80        # stay safely below the 90-day point where some FX APIs aggregate
FX_MAX_GAP_DAYS = 6       # a daily ECB series never has a gap this long (Easter = 5 days); weekly data = 7


def _loads(text: str):
    return json.loads(text, parse_float=Decimal)


# ------------------------------------------------------------------ FX (Frankfurter)
def fetch_fx(client: ApiClient, base_url: str, start: date, end: date) -> pd.DataFrame:
    """Daily BRL->USD series covering [start, end]. Returns columns rate_date, brl_usd (Decimal).

    Requested in <= 80 day windows so we never receive weekly/monthly aggregated data, and the
    result is validated for gaps: a silently coarse series would corrupt R6.
    """
    rates: dict[date, Decimal] = {}
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=FX_CHUNK_DAYS - 1), end)
        name = f"fx_BRL_USD_{cur}_{chunk_end}"
        text = client.get_text(name, f"{base_url.rstrip('/')}/{cur}..{chunk_end}",
                               params={"from": "BRL", "to": "USD"})
        payload = _loads(text)
        chunk = payload.get("rates") if isinstance(payload, dict) else None
        if not chunk:
            raise ExtractionError(f"FX API returned no rates for {cur}..{chunk_end}: {text[:200]}")
        for d, quote in chunk.items():
            if "USD" not in quote:
                raise ExtractionError(f"FX API response for {d} has no USD quote")
            value = Decimal(quote["USD"])
            if value <= 0:
                raise ExtractionError(f"FX API returned non-positive rate for {d}")
            rates[date.fromisoformat(d)] = value
        cur = chunk_end + timedelta(days=1)

    df = pd.DataFrame(sorted(rates.items()), columns=["rate_date", "brl_usd"])
    ds = df["rate_date"].tolist()
    if ds[0] > start + timedelta(days=FX_MAX_GAP_DAYS):
        raise ExtractionError(f"FX series starts {ds[0]}, expected close to {start}")
    for a, b in zip(ds, ds[1:]):
        if (b - a).days > FX_MAX_GAP_DAYS:
            raise ExtractionError(f"FX series has a {(b - a).days}-day gap between {a} and {b} "
                                  "(response looks aggregated); refusing to continue")
    return df


# ------------------------------------------------------------------ holidays (Nager.Date)
def fetch_holidays(client: ApiClient, base_url: str, years: list[int]) -> pd.DataFrame:
    """Raw holiday rows for Brazil. Columns: date, local_name, name, global_flag, counties, types."""
    rows = []
    for year in years:
        text = client.get_text(f"holidays_BR_{year}", f"{base_url.rstrip('/')}/PublicHolidays/{year}/BR")
        payload = _loads(text)
        if not isinstance(payload, list) or not payload:
            raise ExtractionError(f"Holiday API returned no holidays for {year}")
        for h in payload:
            rows.append({
                "date": date.fromisoformat(h["date"]),
                "local_name": (h.get("localName") or h.get("name") or "").strip(),
                "name": (h.get("name") or "").strip(),
                "global_flag": bool(h.get("global", True)),
                "counties": list(h.get("counties") or []),   # e.g. ["BR-SP", "BR-RJ"]
                "types": list(h.get("types") or []),
            })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ IBGE states (BrasilAPI)
def fetch_states(client: ApiClient, url: str) -> pd.DataFrame:
    return states_from_text(client.get_text("brasilapi_ibge_uf", url))


def states_from_text(text: str) -> pd.DataFrame:
    payload = _loads(text)
    if not isinstance(payload, list) or len(payload) < 27:
        raise ExtractionError(f"IBGE states API returned {len(payload) if isinstance(payload, list) else 'invalid'} "
                              "entries, expected 27")
    rows = [{"state_code": s["sigla"].strip().upper(),
             "state_name": s["nome"].strip(),
             "region": s["regiao"]["nome"].strip()} for s in payload]
    return pd.DataFrame(rows).drop_duplicates("state_code").reset_index(drop=True)
