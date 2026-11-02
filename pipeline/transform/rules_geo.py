"""R15 geolocation centroids, R16 haversine distance, R17 state -> region."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import DQLog

LAT_MIN, LAT_MAX = -33.75, 5.27
LNG_MIN, LNG_MAX = -73.99, -34.79
EARTH_RADIUS_KM = 6371.0088


def zip5(series: pd.Series) -> pd.Series:
    """Olist files drop leading zeros of zip prefixes in places; normalise to 5 digits everywhere."""
    return series.astype("string").str.strip().str.zfill(5)


def zip_centroids(geo: pd.DataFrame, dq: DQLog) -> pd.DataFrame:
    """R15: drop points outside Brazil's bounding box, THEN take the median lat/lng per zip prefix.
    Returns zip_prefix, lat, lng."""
    g = pd.DataFrame({
        "zip_prefix": zip5(geo["geolocation_zip_code_prefix"]),
        "lat": pd.to_numeric(geo["geolocation_lat"], errors="coerce"),
        "lng": pd.to_numeric(geo["geolocation_lng"], errors="coerce"),
    })
    unparseable = int(g[["lat", "lng"]].isna().any(axis=1).sum())
    inside = g["lat"].between(LAT_MIN, LAT_MAX) & g["lng"].between(LNG_MIN, LNG_MAX)
    removed = int((~inside).sum())
    dq.add("R15", "geolocation", "bbox_filter", "points_outside_brazil_removed",
           f"{removed} of {len(g)} geolocation rows removed (outside lat {LAT_MIN}..{LAT_MAX}, "
           f"lng {LNG_MIN}..{LNG_MAX}; {unparseable} unparseable)")
    cent = g[inside].groupby("zip_prefix", as_index=False)[["lat", "lng"]].median()
    return cent


def haversine_km(lat1, lng1, lat2, lng2) -> np.ndarray:
    lat1, lng1, lat2, lng2 = (np.radians(np.asarray(x, dtype=float)) for x in (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def item_distances(items: pd.DataFrame, centroids: pd.DataFrame, dq: DQLog) -> pd.Series:
    """R16: haversine km between seller and customer zip prefixes; NULL (+ DQ row) when either is unknown.
    `items` needs seller_zip, customer_zip, order_id, order_item_id."""
    c = centroids.set_index("zip_prefix")
    s_lat = items["seller_zip"].map(c["lat"])
    s_lng = items["seller_zip"].map(c["lng"])
    c_lat = items["customer_zip"].map(c["lat"])
    c_lng = items["customer_zip"].map(c["lng"])
    dist = pd.Series(haversine_km(s_lat, s_lng, c_lat, c_lng), index=items.index, dtype=float)
    missing = dist.isna()
    if missing.any():
        sub = items[missing]
        reason = [("seller zip %s" % s if pd.isna(sl) else "") + (" " if pd.isna(sl) and pd.isna(cl) else "") +
                  ("customer zip %s" % cz if pd.isna(cl) else "") + " has no coordinates"
                  for s, cz, sl, cl in zip(sub["seller_zip"], sub["customer_zip"], s_lat[missing], c_lat[missing])]
        dq.add_many("R16", "order_item", [f"{o}#{i}" for o, i in zip(sub["order_id"], sub["order_item_id"])],
                    "distance_not_computed", reason)
    return dist


def enrich_states(state_codes: pd.Series, states: pd.DataFrame) -> pd.DataFrame:
    """R17: state code -> state name + region (BrasilAPI)."""
    m = states.set_index("state_code")
    code = state_codes.astype("string").str.strip().str.upper()
    return pd.DataFrame({"state_name": code.map(m["state_name"]), "region": code.map(m["region"])},
                        index=state_codes.index)
