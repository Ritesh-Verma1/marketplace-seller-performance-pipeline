"""Read the Kaggle CSVs / internal files and persist the raw layer (as received)."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .api_client import ExtractionError

log = logging.getLogger(__name__)

KAGGLE_FILES = {
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "geolocation": "olist_geolocation_dataset.csv",
    "translation": "product_category_name_translation.csv",
}
INTERNAL_FILES = [
    "commission_rates.csv",
    "category_commission_overrides.csv",
    "category_translation_patch.csv",
    "campaigns.json",
]


def read_csv_raw(path: Path) -> pd.DataFrame:
    """Everything as string: no type coercion happens before the business rules.
    Only a truly empty field is NULL (so e.g. the text 'NA' is not silently turned into NaN)."""
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")


def read_kaggle(input_dir: Path) -> dict[str, pd.DataFrame]:
    input_dir = Path(input_dir)
    missing = [f for f in KAGGLE_FILES.values() if not (input_dir / f).exists()]
    if missing:
        raise ExtractionError(
            f"Missing Kaggle files in '{input_dir}': {', '.join(missing)}. Download the Olist dataset "
            "(https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) and set --input-dir / "
            "PIPELINE_INPUT_DIR.")
    out = {}
    for key, fname in KAGGLE_FILES.items():
        out[key] = read_csv_raw(input_dir / fname)
        log.info("read %-28s %9d rows", fname, len(out[key]))
    return out


def check_internal(internal_dir: Path) -> None:
    missing = [f for f in INTERNAL_FILES if not (Path(internal_dir) / f).exists()]
    if missing:
        raise ExtractionError(f"Missing internal files in '{internal_dir}': {', '.join(missing)}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def snapshot_raw(input_dir: Path, internal_dir: Path, raw_dir: Path,
                 kaggle: dict[str, pd.DataFrame]) -> Path:
    """Copy source files untouched into <raw_dir>/{kaggle,internal}/ and write a manifest
    (sha256, bytes, row counts). API responses are already stored in <raw_dir>/api/ by ApiClient."""
    raw_dir = Path(raw_dir)
    manifest = {"snapshot_at": datetime.now(timezone.utc).isoformat(), "files": {}}
    for sub, src_dir, names in (
        ("kaggle", Path(input_dir), list(KAGGLE_FILES.values())),
        ("internal", Path(internal_dir), INTERNAL_FILES),
    ):
        (raw_dir / sub).mkdir(parents=True, exist_ok=True)
        for name in names:
            src, dst = src_dir / name, raw_dir / sub / name
            digest = _sha256(src)
            if not dst.exists() or _sha256(dst) != digest:
                shutil.copy2(src, dst)
            entry = {"sha256": digest, "bytes": src.stat().st_size}
            key = next((k for k, v in KAGGLE_FILES.items() if v == name), None)
            if sub == "kaggle" and key:
                entry["rows"] = len(kaggle[key])
            manifest["files"][f"{sub}/{name}"] = entry
    path = raw_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path
