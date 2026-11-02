# Marketplace Seller Performance Pipeline

Extracts Olist order data, internal reference files and three public APIs, applies business rules R1–R18,
loads a dimensional model into MySQL 8 and serves it through a read-only FastAPI service.

```
 Kaggle CSVs ─┐                                   ┌─> MySQL 8 (facts + dims + ref + DQ + run log) ─> FastAPI ─> dashboards
 internal files ─┼─> extract ─> raw layer (data/raw) ─> transform (R1–R18, pure pandas) ─> load (1 transaction / range)
 Frankfurter / Nager.Date / BrasilAPI ─┘   (retries, backoff, cache)
```

Documentation map: **ASSUMPTIONS.md** (every decision by rule ID) · **RESULTS.md** (numbers from the
2017-01-01 → 2018-08-31 run) · `sql/schema.sql` (DDL) · `tests/` (pytest).

## 1. Setup

Requirements: Python 3.10+, MySQL 8.x (Docker Compose file included), internet access for the three APIs.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

docker compose up -d                                    # MySQL 8 on 127.0.0.1:3306, db "marketplace", root/root
# (or point DATABASE_URL at your own MySQL 8 and create an empty database "marketplace")

# Olist data -> data/kaggle/  (9 CSV files; needs a free Kaggle account)
pip install kaggle
kaggle datasets download -d olistbr/brazilian-ecommerce -p data/kaggle --unzip
# or download the zip from https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce and unzip it there
```

The four internal files already live in `data/internal/` exactly as provided. Configuration (all optional) is via
environment variables or CLI flags — see `.env.example`:

| Setting | Env var | CLI flag | Default |
|---|---|---|---|
| Olist CSV folder | `PIPELINE_INPUT_DIR` | `--input-dir` | `data/kaggle` |
| Internal files folder | `PIPELINE_INTERNAL_DIR` | `--internal-dir` | `data/internal` |
| Raw layer folder | `PIPELINE_RAW_DIR` | `--raw-dir` | `data/raw` |
| Database | `DATABASE_URL` | `--database-url` | `mysql+pymysql://root:root@127.0.0.1:3306/marketplace` |
| API URLs | `FX_BASE_URL`, `HOLIDAYS_BASE_URL`, `STATES_URL` | – | the three public endpoints |
| API retries / backoff | `API_ATTEMPTS`, `API_BACKOFF_BASE` | `--refresh-cache` ignores cached responses | 4 attempts, 1 s·2ⁿ |

## 2. Run the pipeline (one command)

```bash
python -m pipeline.run --start-date 2017-01-01 --end-date 2018-08-31      # --enddate is accepted as an alias
```

Useful variants:

```bash
python -m pipeline.run --start-date 2018-03-01 --end-date 2018-03-31      # re-run one month (replaces only that month)
python -m pipeline.run --start-date 2017-01-01 --end-date 2018-08-31 --dry-run --dump-dir out/   # no MySQL, writes CSVs
python -m pipeline.results                                                 # (re)generate RESULTS.md from the database
```

* **Idempotent:** running the same range twice replaces the same rows (no duplicates, same totals).
* **Sub-range safe:** each range is replaced atomically by purchase timestamp; tiers (R12) are derived from the previous
  month read from the source files, and if the *following* month is already loaded and its tiers would now differ, it is
  re-processed automatically (see ASSUMPTIONS.md R12).
* **Fails loudly:** a missing input file or an API that is still failing after the retries aborts the run with exit code 2
  and a clear message; nothing is loaded from partial data and `pipeline_run.status = 'failed'` records why.
* Each run writes one `pipeline_run` row (parameters, start/end, row counts per table, status) and its
  `data_quality_issue` rows.

## 3. Start the API

```bash
uvicorn api.main:app --reload            # DATABASE_URL is read from the environment
# interactive docs: http://127.0.0.1:8000/docs
```

Example calls (replace ids with real ones, e.g. take an `order_id` / `seller_id` from the database):

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/orders/<order_id>
curl -s "http://127.0.0.1:8000/sellers/<seller_id>/scorecard?month=2018-03"
curl -s "http://127.0.0.1:8000/sellers/top?month=201803&metric=commission_usd&limit=5"
curl -s "http://127.0.0.1:8000/sellers/top?month=201803&metric=on_time_rate&limit=10&offset=0"
curl -s "http://127.0.0.1:8000/kpis/monthly?start=2017-01&end=2018-08&region=Sul&limit=24"
curl -s http://127.0.0.1:8000/campaigns/C003/performance
curl -s "http://127.0.0.1:8000/data-quality/summary"
curl -s "http://127.0.0.1:8000/data-quality/summary?run_id=<run_id>&limit=20"
```

Status codes: `404` unknown order/seller/campaign/run (an order excluded by R2 returns 404 with an explanation),
`422` invalid input (bad month format, unknown metric/region, `limit` > 100, `start` after `end`), `503` from `/health`
when the database is unreachable. All SQL is parameterised, aggregations run in SQL, list endpoints take
`limit` (max 100) and `offset`.

## 4. Tests

```bash
pytest                      # ~100 tests, no network and no MySQL needed
```

* `test_rules.py` – unit tests of the tricky rules: **R6** (weekend/holiday fallback, never a later rate), **R9** (business-day
  convention, national vs regional holidays), **R12** (exact 5,000 / 20,000 boundaries, previous-month logic, sub-range),
  **R13** (override vs tier, date windows, missing rate), **R18** (UTC → São Paulo, date-only end, priority and tie-break), plus R5, R8, R10/R11, R14, R15/R16.
* `test_internal_files.py` – the cleaning of the human-maintained files.
* `test_pipeline_e2e.py` – the whole transform on a hand-checked synthetic dataset (`tests/synth.py`), including
  "a March-only run reproduces the March rows of a Feb–Mar run".
* `test_extract_cli.py` – retries/backoff/cache/failure behaviour and the CLI against a local fake of the three APIs.
* `test_api_sql.py` – every API query and the response models, run against an in-memory SQLite copy of the model
  (the SQL uses only the portable subset of MySQL 8).
* `test_api_http.py` – FastAPI status codes / validation / pagination via `TestClient` (runs when FastAPI is installed).

## 5. Layout

```
pipeline/
  config.py                     settings (env / CLI)
  extract/  api_client.py       timeout + retries + exponential backoff + raw cache
            apis.py             Frankfurter (FX), Nager.Date (holidays), BrasilAPI (states)
            files.py            CSV readers, raw-layer snapshot + manifest
  transform/ internal_files.py  cleaning of the internal files (+ DQ log)
            rules_money.py      R4 R5 R6 R8      rules_delivery.py  R9 R10 R11
            rules_commission.py R12 R13          rules_geo.py       R15 R16 R17
            rules_campaign.py   R14 R18          build.py           orchestrator -> modelled tables + DQ
  load/     loader.py           transactional replace-window load, upserts, cascade helpers
  run.py                        CLI;  results.py  generates RESULTS.md
api/        main.py models.py queries.py db.py
sql/schema.sql                  DDL (17 tables, PKs, FKs, indexes; DECIMAL money)
data/internal/                  the four internal files    data/raw/  raw layer    data/kaggle/  Olist CSVs
```

Model: `dim_customer` (R1) · `dim_seller` · `dim_product` · `dim_date` · `dim_campaign` + `dim_campaign_target` ·
`seller_month_tier` (R12) · `fact_order` · `fact_order_item` · `fact_order_seller` (per seller within an order: R11 SLA, R14 review) ·
`ref_fx_rate` · `ref_holiday` · `ref_state` · `ref_commission_rate` · `ref_category_override` · `data_quality_issue` · `pipeline_run`.

## 6. What would you change if data volume grew 100× and the pipeline had to run daily?

Today's design is a single-process pandas job that rebuilds a whole date range. At ~10 M orders / ~11 M items and a
daily schedule I would change:

1. **Incremental, partitioned processing instead of full ranges.** Daily job = new/changed orders only (watermark on a
   source `updated_at` / ingestion date) plus the current open month; everything is written per **month partition**
   (partition overwrite = the same idempotent "replace the window" idea, but with `PARTITION BY RANGE` on `purchase_ts`
   and `EXCHANGE PARTITION`/`DROP PARTITION` instead of a large `DELETE`).
2. **Move heavy transforms out of in-memory pandas.** Keep the raw layer as columnar files (Parquet on object storage),
   run the rules as set-based SQL / DuckDB / Spark (or dbt models), and only load the final marts into MySQL — or move the
   analytics store to a columnar warehouse (ClickHouse, BigQuery, Snowflake) and keep MySQL for the API's small
   serving tables. The pure-function rule modules translate directly to SQL expressions / window functions.
3. **Bulk loading.** Replace row-wise `INSERT` batches with `LOAD DATA LOCAL INFILE` (or `COPY`) into staging tables and an
   `INSERT … SELECT` swap; load dimensions with change detection rather than upserting every row each run.
4. **R12 tiers as an incremental monthly aggregate.** Maintain `seller_month_revenue` incrementally; the daily run only
   recomputes the current month and, when late-arriving data changes a closed month, cascades exactly one month forward
   (the cascade already exists). Backfills run month by month, in parallel across months that don't depend on each other.
5. **APIs.** Fetch only the missing FX dates / the current and next holiday year, cache responses in object storage,
   keep the retry/fail-loud behaviour, add alerting on failure and a freshness check (latest FX date ≥ yesterday).
6. **Data quality at scale.** Per-row DQ inserts would create tens of millions of rows. Write DQ as set-based
   `INSERT … SELECT` per rule with aggregated counts plus capped samples, enforce thresholds (fail the run if e.g.
   `payment_mismatch` jumps), and add automated checks (dbt tests / Great Expectations) on the marts.
7. **Serving layer.** Pre-aggregate monthly seller/KPI tables (materialised by the pipeline) so `/kpis` and `/sellers/top`
   read hundreds of rows instead of scanning facts; add covering indexes, a read replica, response caching with an
   invalidation key per run, and keyset pagination for deep pages.
8. **Operations.** Orchestrate with Airflow/Dagster (retries, SLAs, backfill UI), version the schema with Alembic migrations,
   containerise the pipeline/API, run in CI with the test-suite, and emit run metrics (row counts, durations, DQ counts)
   to monitoring — `pipeline_run` already records the basics.
