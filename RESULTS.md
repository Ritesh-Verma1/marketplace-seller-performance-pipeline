# RESULTS

Run `a8cb0fd9-d1c8-47ff-8571-f7177599b57c` over purchase dates **2017-01-01 .. 2018-08-31**.

## 1. Orders loaded and orders excluded per R2

* **Total orders loaded:** 97,910
* **Orders excluded by R2:** 1,182
* Orders found in the date range: 99,092

| Excluded status (R2) | Orders |
|---|---|
| canceled | 580 |
| unavailable | 602 |


Loaded orders by status (`is_revenue_eligible = 1` only for `delivered`):

| order_status | orders loaded | revenue eligible |
|---|---|---|
| delivered | 96,211 | 96,211 |
| shipped | 1,097 | 0 |
| processing | 299 | 0 |
| invoiced | 296 | 0 |
| created | 5 | 0 |
| approved | 2 | 0 |


## 2. Data-quality issue counts per rule ID

| Rule | Total issues |
|---|---|
| R2 | 1,182 |
| R3 | 8 |
| R4 | 5 |
| R5 | 247 |
| R6 | 19 |
| R8 | 1 |
| R9 | 39 |
| R11 | 1 |
| R13 | 9 |
| R14 | 543 |
| R15 | 1 |
| R16 | 549 |
| R18 | 4 |


Breakdown by issue type:

| Rule | Issue type | Count |
|---|---|---|
| R2 | excluded_status | 1,182 |
| R3 | delivered_without_delivery_date | 8 |
| R4 | order_without_items | 5 |
| R5 | payment_mismatch | 247 |
| R6 | approved_at_null_used_purchase_date | 19 |
| R8 | products_without_category | 1 |
| R9 | delivered_before_approval | 38 |
| R9 | info_holidays_found | 1 |
| R11 | missing_carrier_date | 1 |
| R13 | normalized_value | 2 |
| R13 | open_ended_range | 6 |
| R13 | rejected_impossible_range | 1 |
| R14 | duplicate_review_dropped | 543 |
| R15 | points_outside_brazil_removed | 1 |
| R16 | distance_not_computed | 549 |
| R18 | normalized_value | 4 |


## 3. Total gross USD for 2018-01

**USD 335,916.68** (revenue-eligible = delivered orders, 8,037 items; gross = price + freight, R4/R6).
For transparency, including loaded-but-not-delivered orders: USD 343,395.26.


## 4. Top 5 sellers by commission_usd - 2018-03

| Rank | seller_id | commission_usd | items |
|---|---|---|---|   
| 1 | 7d13fca15225358621be4086e1eb0964 | 1,003.40 | 100 |
| 2 | 7c67e1448b00f6e969d365cea6b010ab | 928.26 | 192 |
| 3 | 4869f7a5dfa277a7dca6462dcf3b52b2 | 906.96 | 115 |
| 4 | 5dceca129747e92ff8ef7a997dc4f8ca | 732.04 | 27 |
| 5 | ba90964cff9b9e0e6f32b23b82465f7b | 613.49 | 10 |


## 5. On-time % per region - 2018

On time = delivered date <= estimated date (R10); delivered orders with a delivery date only (R3); region = region of the order's delivery state (R17).


| Region | Orders with SLA | On time | On-time % |
|---|---|---|---|
| Centro-Oeste | 3,113 | 2,866 | 92.07 |
| Nordeste | 4,669 | 3,995 | 85.56 |
| Norte | 866 | 759 | 87.64 |
| Sudeste | 36,797 | 34,239 | 93.05 |
| Sul | 7,332 | 6,840 | 93.29 |
| **All regions** | 52,777 |  | 92.27 |


## 6. Items attributed to each campaign (R18)

| Campaign | Items attributed (all loaded statuses) | of which revenue eligible |
|---|---|---|
| C001 | 2,875 | 2,833 |
| C002 | 70 | 66 |
| C003 | 196 | 196 |
| C004 | 926 | 908 |
| C005 | 35 | 35 |


## 7. Rows rejected or corrected in the internal files

`rejected_*` = the row is NOT used; `corrected_*` / `normalized_value` = used after the fix shown; `open_ended_range` = blank `effective_to` interpreted as 'no end date'.

| File : row | Action | Reason / detail |
|---|---|---|
| category_commission_overrides.csv:row6 | rejected_impossible_range | category=watches_gifts: effective_to 2018-04-30 is before effective_from 2018-05-01; override of 0.20 NOT applied (needs confirmation from sales team) |
| campaigns.json:C002 | normalized_value | start: '01/02/2018' parsed as DD/MM/YYYY -> 2018-02-01 |
| campaigns.json:C002 | normalized_value | end: '28/02/2018' parsed as DD/MM/YYYY -> 2018-02-28 |
| campaigns.json:C003 | normalized_value | start: 2018-03-05T03:00:00Z (UTC) converted to 2018-03-05 00:00:00 America/Sao_Paulo |
| campaigns.json:C003 | normalized_value | end: 2018-03-12T02:59:59Z (UTC) converted to 2018-03-11 23:59:59 America/Sao_Paulo |
| category_commission_overrides.csv:row4 | normalized_value | category_name_en: 'Telephony ' -> 'telephony' |
| commission_rates.csv:row8 | normalized_value | tier: 'gold' -> 'Gold' |
| category_commission_overrides.csv:row2 | open_ended_range | category=computers: blank effective_to treated as open ended (no end date) |
| category_commission_overrides.csv:row4 | open_ended_range | category=telephony: blank effective_to treated as open ended (no end date) |
| category_commission_overrides.csv:row5 | open_ended_range | category=pc_gamer: blank effective_to treated as open ended (no end date) |
| commission_rates.csv:row5 | open_ended_range | tier=Bronze: blank effective_to treated as open ended (no end date) |
| commission_rates.csv:row7 | open_ended_range | tier=Silver: blank effective_to treated as open ended (no end date) |
| commission_rates.csv:row8 | open_ended_range | tier=Gold: blank effective_to treated as open ended (no end date) |


## 8. What the holiday API returned (R9)

| Year | Scope | Rows |
|---|---|---|
| 2017 | national | 11 |
| 2017 | regional | 1 |
| 2018 | national | 11 |
| 2018 | regional | 1 |


Regional (counties) rows found: 2.

| Date | State | Name |
|---|---|---|
| 2017-07-09 | SP | Revolução Constitucionalista de 1932 |
| 2018-07-09 | SP | Revolução Constitucionalista de 1932 |


Counting convention: business days in the window (approval date, delivered date] - the approval day is NOT counted, the delivery day IS counted (see ASSUMPTIONS.md R9).
