# ASSUMPTIONS

Every decision on an ambiguous or under-specified rule, referenced by rule ID (the same IDs appear in code
comments and in `data_quality_issue.rule_id`). "Why" is given where an alternative was plausible.

## General (G)

| ID | Decision |
|---|---|
| G1 | **Time zones.** All Olist timestamps are `America/Sao_Paulo` local time and are stored as naive local `DATETIME`. FX dates, holiday dates and campaign windows are all matched on the *local* calendar date. The only conversion is campaign ISO timestamps ending in `Z` (UTC) -> São Paulo local (R18). |
| G2 | **Date range.** `--start-date`/`--end-date` filter on `order_purchase_timestamp` in `[start 00:00:00, end + 1 day)`, i.e. the end date is inclusive for the whole day. |
| G3 | **Raw layer** = `data/raw/`: byte-identical copies of every input file (`kaggle/`, `internal/`), every API response body exactly as received (`api/`, also used as the cache) and `manifest.json` (sha256, bytes, row counts). Raw data is never modified. CSVs are read with `dtype=str` so nothing is coerced before the business rules. |
| G4 | **Status strings** are trimmed and lower-cased before comparison. |
| G5 | **Nothing is silently dropped or defaulted.** Every rejected row, NULL-ed value, fallback and excluded order is a row in `data_quality_issue` (`rule_id`, `entity_type`, `entity_id`, `issue_type`, `details`, `run_id`). DQ rows are kept per run (history); the API summarises one run at a time. A sub-range run's DQ rows describe that run's scope only. |
| G6 | **Idempotency.** One transaction per range: delete facts whose `purchase_ts` is in the window (children first) then insert; dimensions/reference tables are upserted (never deleted, other months' facts reference them). Same range twice -> identical rows. A partial month can be re-run (delete is by timestamp, not by month). |
| G7 | **Customer-level vs order-level state.** `dim_customer.home_state` is the R1 home state. Anything about *where an order was delivered* (R9 regional holidays, R17 region, R18 campaign targeting, `/kpis/monthly?region=`) uses the state on **that order's** customer record, because holidays/campaigns/regional KPIs are about the delivery location, not where the person first bought. |
| G8 | **Revenue basis for the API.** Scorecards, rankings, KPIs and campaign performance only count `is_revenue_eligible = 1` (delivered) orders. Loaded-but-not-delivered orders are in the facts (flagged) so Ops can see them. |

## Customers & orders

**R1 – Customer identity.** `customer_unique_id` is the customer key everywhere (`dim_customer`, "unique customers" KPI = `COUNT(DISTINCT customer_unique_id)`). Home state = state on the customer's **earliest** order, computed over the **full source** (not only the processed range) so a sub-range re-run gives the same answer as a full run. Earliest = lowest `order_purchase_timestamp`, ties by `order_id`. Canceled/unavailable orders are ignored for this (R2 says they are excluded from everything except the DQ report).

**R2 – Revenue-eligible orders.** `delivered` -> `is_revenue_eligible = 1`. `canceled` and `unavailable` orders (and their items/payments/reviews) are **not loaded**; each is one `R2 / excluded_status` DQ row (`details = status=<status>`), which is where the "excluded per status" counts come from. All other statuses are loaded with `is_revenue_eligible = 0`; for them commission, tier-based rate, SLA result and delivery metrics are NULL (commission only exists on delivered revenue).

**R3 – Delivered without delivery date.** Stays revenue eligible (counts in gross, commission, KPIs "orders"), but `is_on_time`, `delivery_business_days` and `sla_result` are NULL, so it is out of every SLA metric. Logged as `R3 / delivered_without_delivery_date`. A delivery date *before* the approval date (R9) also leaves business days NULL and is logged.

## Money

**R4 – Item amounts.** One order-item row = one unit. `gross_brl = price + freight_value`; commission is on `price` only. An order with no item rows is loaded with zero amounts and logged (`R4 / order_without_items`).

**R5 – Payment reconciliation.** Per order: `payments_total = Σ payment_value`, `voucher_total = Σ(voucher)`, `net_cash_paid_brl = payments_total − voucher_total`. Flag `payment_mismatch` when `|payments_total − Σ item gross| > 1.00` (strictly greater: a difference of exactly 1.00 is tolerated). An order with no payment rows has payments 0 and is therefore flagged when it has items. Applied to every loaded order (not just delivered).

**R6 – FX.** Basis date = *local date* of `order_approved_at`; if NULL, the purchase date (logged `R6 / approved_at_null_used_purchase_date`). The rate used is the latest rate dated **on or before** the basis date (`searchsorted`, never a later one) and both `fx_rate` and `fx_rate_date` are stored on `fact_order` and `fact_order_item`. The FX request starts 14 days before the earliest basis date so the first days of a range can fall back across weekends/holidays. The extractor requests ≤ 80-day windows (so the API never switches to coarser aggregation) and **aborts** if the returned series has a gap > 6 days. If no earlier rate exists the USD value is NULL and logged — never defaulted. `gross_usd = round(gross_brl × rate)` per item; order-level `gross_usd` is the sum of its (already rounded) item values so totals tie out across tables.

**R7 – Money storage.** Python `Decimal` parsed from the CSV text (never via float), API numbers parsed with `parse_float=Decimal`. MySQL `DECIMAL(x,2)` for money and `DECIMAL(x,6)` for rates; no FLOAT/DOUBLE anywhere. Rounding (half-up) happens only when producing the stored value: e.g. `commission_usd = round(price × rate × fx)` from the *unrounded* product, not from the rounded `commission_brl`. SQL aggregates therefore sum cent-rounded rows (differences vs an all-unrounded total are at most a few cents per thousand rows).

## Categories

**R8 – Category resolution.** Lookup keys are trimmed and lower-cased. Order: (1) `category_translation_patch.csv` (wins — e.g. `casa_conforto_2` -> `home_comfort` rather than Kaggle's `home_comfort_2`), (2) Kaggle translation, (3) Portuguese name with no translation -> `unknown` (+ one DQ row per such category), (4) no category -> `uncategorized`. `dim_product.category_source` records which step decided. An item whose `product_id` is not in the products file is `uncategorized` and logged.

## Delivery performance

**R9 – Business days.** *Counting convention:* the window is **(approval date, delivered date]** — the approval day is **not** counted, the delivery day **is** (same-day delivery = 0; approved Tuesday and delivered Friday = 3 business days: Wed, Thu, Fri; approved Monday and delivered Friday = 4). A business day is Mon–Fri that is not a national holiday and not a regional holiday of the order's delivery state (G7). Approval date falls back to the purchase date when `order_approved_at` is NULL. *Holiday API:* Nager.Date `PublicHolidays/{year}/BR`. A row with a non-empty `counties` list (e.g. `["BR-SP"]`) is **regional** and applies only to those states; a row without counties is **national**. Only rows whose `types` contain `Public` are counted (so `Optional`/`Bank`-only days are not business-day holidays) — this is a constant (`HOLIDAY_TYPES`) in `rules_delivery.py`. Holiday years are derived from the data (first purchase year .. last delivered/estimated year). What the API actually returned (rows per year and scope, list of regional rows) is written to `RESULTS.md` section 8 and to a DQ row `R9 / info_holidays_found`.

**R10 – On time.** `date(delivered_customer) <= date(estimated_delivery)`; NULL when either is missing or the order is not SLA-eligible (R3).

**R11 – Lateness responsibility.** Per (order, seller): earliest `shipping_limit_date` across that seller's items; `seller_late` if `order_delivered_carrier_date` is strictly after it, else `carrier_late` if R10 says late, else `on_time`. Evaluated only for delivered orders with a delivery date (R3). A missing carrier date skips the seller test (logged `R11 / missing_carrier_date`). Note the two on-time notions: *order-level* R10 (`is_on_time`, used in KPIs) vs *seller-level* `sla_result = 'on_time'` (shipped within the limit **and** delivered by the estimate, used for scorecard `on_time_pct`). A seller who shipped late but whose parcel still arrived by the estimate is `seller_late` (R11 as written) although the order is R10-on-time. `on_time_pct = on_time seller-orders / seller-orders that have an SLA result`.

## Sellers & commission

**R12 – Seller tier.** Tier of month M = f(Σ `price` of the seller's **delivered** orders purchased in month M−1), BRL, freight excluded: `< 5,000` Bronze; `5,000 … 20,000` (inclusive) Silver; `> 20,000` Gold; no sales -> Bronze. Thresholds are compared on **integer cents** so 5,000.00 / 20,000.00 are exact. Revenue is read from the **full source files**, not from what happens to be loaded, so (a) the first month of a range sees the month before the range (Jan-2017 uses Dec-2016) and (b) a March-only re-run needs no February in the database. `seller_month_tier` stores tier + basis revenue for every seller × processed month.
*Re-run cascade:* because April's tier depends on March, after loading a range the pipeline checks the following month: if it is already in the database and its stored tiers differ from what the source now implies, it is re-processed too, and so on until a month is consistent (`--no-cascade` disables this). With unchanged source data nothing cascades; the check exists for corrected source files or rule changes.

**R13 – Commission rate.** Per delivered item: (1) category override active on the **purchase date** (inclusive both ends, open end = still active) for the item's English category, else (2) the tier rate active on the purchase date for the seller's tier of that month. `commission_brl = round(price × rate)`; `commission_usd` with the R6 rate. No rate found -> commission NULL + DQ row (`R13 / no_commission_rate_found`).
*Internal-file cleaning* (all logged as `entity_type = internal_file`, listed in `RESULTS.md` §7):

| File / row | Problem | Decision |
|---|---|---|
| `commission_rates.csv` row 8 `gold` | lower-case tier | normalised to `Gold` (title-cased, trimmed) |
| `commission_rates.csv` rows 5, 7, 8 | blank `effective_to` | treated as open ended (no end date) |
| `category_commission_overrides.csv` row 4 `Telephony ` | trailing space + capital | normalised to `telephony` |
| `category_commission_overrides.csv` rows 2, 4, 5 | blank `effective_to` | open ended |
| `category_commission_overrides.csv` row 6 `watches_gifts` 2018-05-01 → **2018-04-30** | impossible range (ends before it starts) | **rejected, not applied.** I did not guess whether the end date was a typo (open ended? 2018-05-31?) or the start was wrong, because it would silently apply a 20 % commission (the highest rate in the file) to a category; flagged for the sales team. Effect: `watches_gifts` items use their tier rate. |
| overlapping windows (none in the shipped files) | two rows of one tier/category overlap | the later `effective_from` wins from its own start; the earlier row is cut off the day before; logged `corrected_overlap` |
| invalid date / rate / unknown tier | — | row rejected + logged |

## Reviews

**R14 – Reviews.** Per order keep the review with the latest `review_answer_timestamp` (ties: later `review_creation_date`, then `review_id`); each dropped review is a DQ row. A score outside 1–5 / non-numeric is dropped and logged. The kept score is attributed to every seller of the order (`fact_order_seller.review_score`). Seller `avg_review` averages over the seller's delivered orders that have a review (one value per order, so a multi-seller order counts once for each seller).

## Geography

**R15 – Geolocation.** Zip prefixes are normalised to 5 digits (Olist drops leading zeros inconsistently). Points outside lat −33.75…5.27 / lng −73.99…−34.79 (inclusive bounds) are removed first, then the **median** lat/lng per zip prefix is used. The number of removed rows is logged (`R15 / points_outside_brazil_removed`).

**R16 – Distance.** Haversine, Earth radius 6371.0088 km, per item between the seller and customer zip-prefix centroids, stored `DECIMAL(10,3)` km. If either side has no centroid the value is NULL and logged per item (`R16 / distance_not_computed`).

**R17 – Region.** State code -> name and region from BrasilAPI IBGE (`Norte, Nordeste, Centro-Oeste, Sudeste, Sul`). A state not in that list is logged and its region is NULL. The region filter on `/kpis/monthly` is case-insensitive and rejected with 422 if unknown.

## Campaigns

**R18 – Campaign attribution.** Windows are in São Paulo local time: date-only `start` = 00:00:00, date-only `end` = 23:59:59.999999 (whole last day included); `date_format: DD/MM/YYYY` is honoured (C002 = 1–28 **February**); ISO timestamps ending in `Z` are converted from UTC (C003 therefore runs **2018-03-05 00:00:00 → 2018-03-11 23:59:59 local**; an order at 2018-03-12 00:00:10 is outside). Geography: state in `target_states` **or** region in `target_regions` (case-insensitive), `ALL` = any. Category: item English category in `categories` (`ALL` = any). Several matches -> highest `priority` number; tie -> later `start`; still tied -> highest `campaign_id` (deterministic). C004 (May 1–13, all states) and C005 (May 7–13, Sul) have equal priority, so C005 wins where both match (Sul customers, health_beauty/perfumery, May 7–13) and C004 elsewhere. A campaign with neither `target_states` nor `target_regions` is rejected + logged. Attribution is applied to **all loaded items** (R18 does not mention order status); `RESULTS.md` reports the count both for all loaded items and for revenue-eligible ones. `/campaigns/{id}/performance`: *campaign* = revenue-eligible items attributed to it; *baseline* = revenue-eligible items **in the campaign's own scope** (its states/regions and categories, regardless of which campaign they were attributed to) during the same number of days immediately before the start.

## API

* `/sellers/top`: `month=YYYYMM` (as specified; the other endpoints use `YYYY-MM`). For `on_time_rate` and `avg_review` only sellers with ≥ 10 delivered orders (revenue-eligible seller-orders) in the month are ranked. Ties are broken by `seller_id` for stable pagination.
* Pagination: `limit` (default 10/24/50 depending on endpoint, **max 100**) and `offset` on every list endpoint (`/sellers/top`, `/kpis/monthly`, `/data-quality/summary`).
* `/orders/{id}` returns 404 for unknown orders and a 404 with an explanatory message for orders excluded by R2.
* `/data-quality/summary` defaults to the latest **successful** run.

## Observed API behaviour (from the real run)

* **Nager.Date Brazil 2017 and 2018** each returned 11 national holidays plus one regional row (SP, 9 July, "Revolução Constitucionalista de 1932"), all with type `Public`. The regional row is applied by R9 only to orders delivered to SP. Carnival is not listed as a public holiday by the API, so it is **not** treated as a non-business day; no holidays are added by hand.
* **BrasilAPI (IBGE states)** returned HTTP 504 and the official IBGE host timed out from the author's network. The default is unchanged (call the API, retry, fail loudly). An explicit, opt-in `--states-file data/fallback/ibge_states.json` (27 official UFs and their regions) was used for the run, and the pipeline logs a WARNING whenever it is active.
