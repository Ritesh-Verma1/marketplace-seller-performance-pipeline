"""One-command pipeline:

    python -m pipeline.run --start-date 2017-01-01 --end-date 2018-08-31

extract -> transform (R1-R18) -> load into MySQL -> cascade check (R12) -> pipeline_run bookkeeping.
"""
from __future__ import annotations

import argparse
import calendar
import logging
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .config import Settings
from .extract.api_client import ApiClient, ExtractionError
from .extract.apis import fetch_fx, fetch_holidays, fetch_states, states_from_text
from .extract.files import check_internal, read_kaggle, snapshot_raw
from .transform import internal_files as inf
from .transform.build import EXCLUDED_STATUSES, Internal, Reference, TransformResult, build, expected_tiers
from .transform.common import DQLog, next_month

log = logging.getLogger("pipeline")


# ------------------------------------------------------------------------------ extraction scope
def scope_dates(kaggle: dict[str, pd.DataFrame], start: date, end: date):
    """From the orders that will actually be processed, work out which FX dates and holiday years the API
    calls must cover (data driven: no hard-coded 2016-2018)."""
    o = kaggle["orders"]
    fmt = "%Y-%m-%d %H:%M:%S"
    purchase = pd.to_datetime(o["order_purchase_timestamp"], format=fmt, errors="coerce")
    m = purchase.ge(pd.Timestamp(start)) & purchase.lt(pd.Timestamp(end) + pd.Timedelta(days=1))
    m &= ~o["order_status"].str.strip().str.lower().isin(EXCLUDED_STATUSES)
    if not m.any():
        raise ExtractionError(f"No orders with purchase timestamp between {start} and {end} in the input files.")
    approved = pd.to_datetime(o["order_approved_at"], format=fmt, errors="coerce")
    basis = approved.where(approved.notna(), purchase)[m]
    delivered = pd.to_datetime(o["order_delivered_customer_date"], format=fmt, errors="coerce")[m]
    estimated = pd.to_datetime(o["order_estimated_delivery_date"], format=fmt, errors="coerce")[m]
    fx_start = basis.min().date() - timedelta(days=14)     # look-back so weekends/holidays can fall back (R6)
    fx_end = basis.max().date()
    years_src = [purchase[m].min(), purchase[m].max(), delivered.min(), delivered.max(),
                 estimated.min(), estimated.max()]
    years = [t.year for t in years_src if pd.notna(t)]
    return fx_start, fx_end, list(range(min(years), max(years) + 1))


def _states(client, settings: Settings):
    """IBGE states from the API; only if STATES_FILE is set, from a local copy (explicit, logged fallback)."""
    path = os.environ.get("STATES_FILE")
    if not path:
        return fetch_states(client, settings.states_url)
    log.warning("STATES_FILE is set: using local IBGE states file %s instead of the API", path)
    return states_from_text(Path(path).read_text(encoding="utf-8"))


def fetch_reference(settings: Settings, kaggle, start: date, end: date) -> Reference:
    fx_start, fx_end, years = scope_dates(kaggle, start, end)
    client = ApiClient(settings.raw_dir / "api", timeout=settings.http_timeout, attempts=settings.http_attempts,
                       backoff_base=settings.backoff_base, refresh=settings.refresh_cache)
    log.info("fetching FX %s..%s, holidays %s, IBGE states", fx_start, fx_end, years)
    return Reference(
        fx=fetch_fx(client, settings.fx_base_url, fx_start, fx_end),
        holidays=fetch_holidays(client, settings.holidays_base_url, years),
        states=_states(client, settings),
    )


def clean_internal(internal_dir: Path, dq: DQLog) -> Internal:
    d = Path(internal_dir)
    return Internal(
        rates=inf.load_commission_rates(d / "commission_rates.csv", dq),
        overrides=inf.load_category_overrides(d / "category_commission_overrides.csv", dq),
        patch=inf.load_translation_patch(d / "category_translation_patch.csv", dq),
        campaigns=inf.load_campaigns(d / "campaigns.json", dq),
    )


# ------------------------------------------------------------------------------ one range
def process_range(settings: Settings, kaggle, start: date, end: date, engine, run_id: str,
                  first_step: bool, dump_dir: Path | None) -> tuple[TransformResult, dict]:
    ref = fetch_reference(settings, kaggle, start, end)       # fails loudly if any API is down
    dq_internal = DQLog()
    internal = clean_internal(settings.internal_dir, dq_internal)
    # internal-file / holiday findings are recorded once per run, not again for every cascade month
    result = build(kaggle, internal, ref, start, end, dq_internal if first_step else _drop_internal(dq_internal))
    log.info("transformed %s..%s: %s", start, end, result.stats)
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)
        for name, df in {**result.tables, "data_quality_issue": result.dq}.items():
            df.to_csv(dump_dir / f"{name}.csv", index=False)
    counts: dict = {}
    if engine is not None:
        from .load.loader import load_result
        counts = load_result(engine, result.tables, result.dq, run_id, start, end)
        log.info("loaded %s..%s: %s", start, end, counts)
    return result, counts


def _drop_internal(dq: DQLog) -> DQLog:
    return DQLog()


# ------------------------------------------------------------------------------ R12 cascade
def cascade_next_months(settings: Settings, kaggle, engine, end: date, run_id: str, dump_dir) -> dict:
    """Re-running month M can change the tiers of month M+1 (tier = f(previous month revenue)).
    If M+1 is already loaded and its stored tiers no longer match what the source implies, re-process M+1
    (and keep going while the next month is stale too)."""
    from .load.loader import loaded_months, stored_tiers
    totals: dict = {}
    nxt = next_month(f"{end.year}-{end.month:02d}")
    while nxt in loaded_months(engine):
        exp_df = expected_tiers(kaggle, [nxt])
        exp = dict(zip(exp_df["seller_id"], exp_df["tier"]))
        stored = stored_tiers(engine, nxt)
        stale = [s for s in set(exp) | set(stored) if exp.get(s, "Bronze") != stored.get(s, "Bronze")]
        if not stale:
            log.info("cascade: tiers of %s are still consistent", nxt)
            break
        log.warning("cascade: %d seller tiers of %s changed -> re-processing that month", len(stale), nxt)
        y, m = int(nxt[:4]), int(nxt[5:7])
        _, counts = process_range(settings, kaggle, date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1]),
                                  engine, run_id, False, dump_dir)
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        nxt = next_month(nxt)
    return totals


# ------------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pipeline.run", description="Marketplace seller performance pipeline")
    p.add_argument("--start-date", required=True, type=date.fromisoformat, help="first purchase date (YYYY-MM-DD)")
    p.add_argument("--end-date", "--enddate", dest="end_date", required=True, type=date.fromisoformat,
                   help="last purchase date, inclusive (YYYY-MM-DD)")
    p.add_argument("--input-dir", help="folder with the Olist CSVs (env PIPELINE_INPUT_DIR, default data/kaggle)")
    p.add_argument("--internal-dir", help="folder with the internal files (default data/internal)")
    p.add_argument("--raw-dir", help="raw layer folder (default data/raw)")
    p.add_argument("--database-url", help="SQLAlchemy URL (env DATABASE_URL)")
    p.add_argument("--refresh-cache", action="store_true", help="ignore cached API responses and call the APIs again")
    p.add_argument("--states-file", help="local IBGE states JSON used INSTEAD of the API (fallback when it is down)")
    p.add_argument("--no-cascade", action="store_true", help="skip the R12 next-month staleness check")
    p.add_argument("--dry-run", action="store_true", help="extract + transform only; do not touch MySQL")
    p.add_argument("--dump-dir", help="also write every transformed table as CSV into this folder")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.start_date > args.end_date:
        log.error("--start-date must not be after --end-date")
        return 2
    if args.states_file:
        os.environ["STATES_FILE"] = args.states_file
    settings = Settings.from_env(input_dir=args.input_dir, internal_dir=args.internal_dir, raw_dir=args.raw_dir,
                                 database_url=args.database_url, refresh_cache=args.refresh_cache or None)
    run_id = str(uuid.uuid4())
    dump_dir = Path(args.dump_dir) if args.dump_dir else None
    engine = None
    try:
        if not args.dry_run:
            from sqlalchemy import create_engine
            from .load.loader import ensure_schema, start_run
            engine = create_engine(settings.database_url, pool_pre_ping=True)
            ensure_schema(engine)
            start_run(engine, run_id, args.start_date, args.end_date,
                      {"start_date": args.start_date, "end_date": args.end_date, "input_dir": settings.input_dir,
                       "cascade": not args.no_cascade, "refresh_cache": settings.refresh_cache})
        log.info("run %s: %s .. %s%s", run_id, args.start_date, args.end_date, " (dry run)" if args.dry_run else "")

        check_internal(settings.internal_dir)
        kaggle = read_kaggle(settings.input_dir)
        snapshot_raw(settings.input_dir, settings.internal_dir, settings.raw_dir, kaggle)   # raw layer, as received
        result, counts = process_range(settings, kaggle, args.start_date, args.end_date, engine, run_id, True, dump_dir)

        if engine is not None and not args.no_cascade:
            for k, v in cascade_next_months(settings, kaggle, engine, args.end_date, run_id, dump_dir).items():
                counts[k] = counts.get(k, 0) + v
        if engine is not None:
            from .load.loader import finish_run
            finish_run(engine, run_id, "success", counts)
        log.info("DONE run %s | %s", run_id, result.stats)
        return 0
    except ExtractionError as exc:
        log.error("EXTRACTION FAILED: %s", exc)
        _mark_failed(engine, run_id, f"ExtractionError: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - top-level guard: record and re-raise visibly
        log.exception("PIPELINE FAILED")
        _mark_failed(engine, run_id, f"{type(exc).__name__}: {exc}")
        return 1


def _mark_failed(engine, run_id: str, message: str) -> None:
    if engine is None:
        return
    try:
        from .load.loader import finish_run
        finish_run(engine, run_id, "failed", None, message)
    except Exception:  # noqa: BLE001
        log.exception("could not record failure in pipeline_run")


if __name__ == "__main__":
    sys.exit(main())
