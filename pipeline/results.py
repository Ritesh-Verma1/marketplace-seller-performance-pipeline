"""Generate RESULTS.md from the database after a run over 2017-01-01 .. 2018-08-31.

    python -m pipeline.results                       # uses DATABASE_URL, latest matching successful run
    python -m pipeline.results --run-id <uuid> --output RESULTS.md
    python -m pipeline.results --internal-only       # no database: only the internal-file cleaning table

Every number in the file is produced by a SQL query over the loaded model - nothing is typed by hand.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from api import queries as API_Q

log = logging.getLogger("pipeline.results")
CAMPAIGNS = ["C001", "C002", "C003", "C004", "C005"]

Fetch = Callable[..., list[dict]]


def _md_table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_no rows_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _num(v, nd=2):
    return None if v is None else f"{float(v):,.{nd}f}"


# ---------------------------------------------------------------------------- internal files (no DB needed)
def internal_files_section(dq_rows: list[tuple]) -> str:
    rows = [(r[2], r[3], r[4]) for r in dq_rows if r[1] == "internal_file"]
    order = {"rejected": 0, "corrected": 1, "normalized": 2, "open_ended": 3}
    rows.sort(key=lambda r: (order.get(r[1].split("_")[0], 9), r[0]))
    body = _md_table(["File : row", "Action", "Reason / detail"], [list(r) for r in rows])
    return ("## 7. Rows rejected or corrected in the internal files\n\n"
            "`rejected_*` = the row is NOT used; `corrected_*` / `normalized_value` = used after the fix shown; "
            "`open_ended_range` = blank `effective_to` interpreted as 'no end date'.\n\n" + body)


def internal_only_markdown(internal_dir: Path) -> str:
    from .run import clean_internal
    from .transform.common import DQLog
    dq = DQLog()
    clean_internal(internal_dir, dq)
    return ("# RESULTS\n\n> The database-dependent sections (1-6, 8) are produced by "
            "`python -m pipeline.results` after running\n> `python -m pipeline.run --start-date 2017-01-01 "
            "--end-date 2018-08-31` on the real Olist data. They are intentionally not hand-written.\n\n"
            + internal_files_section(dq.rows))


# ---------------------------------------------------------------------------- database sections
def generate(fetch: Fetch, run_id: str, start: date, end: date, gross_month: str = "2018-01",
             top_month: str = "2018-03", region_year: str = "2018") -> str:
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time())
    win = {"lo": lo.strftime("%Y-%m-%d %H:%M:%S"), "hi": hi.strftime("%Y-%m-%d %H:%M:%S")}
    out = [f"# RESULTS\n\nRun `{run_id}` over purchase dates **{start} .. {end}**.\n"]

    # 1 ---------------------------------------------------------------- orders loaded / excluded
    loaded = fetch("SELECT COUNT(*) AS n FROM fact_order WHERE purchase_ts >= :lo AND purchase_ts < :hi", **win)[0]["n"]
    by_status = fetch("SELECT order_status, COUNT(*) AS n, SUM(is_revenue_eligible) AS eligible FROM fact_order "
                      "WHERE purchase_ts >= :lo AND purchase_ts < :hi GROUP BY order_status ORDER BY n DESC", **win)
    excluded = fetch("SELECT details, COUNT(*) AS n FROM data_quality_issue WHERE run_id = :r AND rule_id = 'R2' "
                     "AND issue_type = 'excluded_status' GROUP BY details ORDER BY details", r=run_id)
    total_excl = sum(e["n"] for e in excluded)
    out.append("## 1. Orders loaded and orders excluded per R2\n")
    out.append(f"* **Total orders loaded:** {loaded:,}\n* **Orders excluded by R2:** {total_excl:,}\n"
               f"* Orders found in the date range: {loaded + total_excl:,}\n")
    out.append(_md_table(["Excluded status (R2)", "Orders"], [[e["details"].replace("status=", ""), f"{e['n']:,}"]
                                                              for e in excluded]))
    out.append("\nLoaded orders by status (`is_revenue_eligible = 1` only for `delivered`):\n")
    out.append(_md_table(["order_status", "orders loaded", "revenue eligible"],
                         [[r["order_status"], f"{r['n']:,}", f"{int(r['eligible'] or 0):,}"] for r in by_status]))

    # 2 ---------------------------------------------------------------- DQ per rule
    dq = fetch("SELECT rule_id, issue_type, COUNT(*) AS n FROM data_quality_issue WHERE run_id = :r "
               "GROUP BY rule_id, issue_type ORDER BY LENGTH(rule_id), rule_id, issue_type", r=run_id)
    per_rule: dict[str, int] = {}
    for r in dq:
        per_rule[r["rule_id"]] = per_rule.get(r["rule_id"], 0) + r["n"]
    out.append("\n## 2. Data-quality issue counts per rule ID\n")
    out.append(_md_table(["Rule", "Total issues"], [[k, f"{v:,}"] for k, v in per_rule.items()]))
    out.append("\nBreakdown by issue type:\n")
    out.append(_md_table(["Rule", "Issue type", "Count"], [[r["rule_id"], r["issue_type"], f"{r['n']:,}"] for r in dq]))

    # 3 ---------------------------------------------------------------- Jan 2018 gross USD
    g = fetch("SELECT SUM(gross_usd) AS usd, COUNT(*) AS items FROM fact_order_item "
              "WHERE `year_month` = :m AND is_revenue_eligible = 1", m=gross_month)[0]
    g_all = fetch("SELECT SUM(gross_usd) AS usd FROM fact_order_item WHERE `year_month` = :m", m=gross_month)[0]
    out.append(f"\n## 3. Total gross USD for {gross_month}\n")
    out.append(f"**USD {_num(g['usd'])}** (revenue-eligible = delivered orders, {g['items'] or 0:,} items; "
               f"gross = price + freight, R4/R6).\nFor transparency, including loaded-but-not-delivered orders: "
               f"USD {_num(g_all['usd'])}.\n")

    # 4 ---------------------------------------------------------------- top 5 sellers
    top = fetch(API_Q.TOP_SELLERS["commission_usd"], ym=top_month, limit=5, offset=0)
    out.append(f"\n## 4. Top 5 sellers by commission_usd - {top_month}\n")
    out.append(_md_table(["Rank", "seller_id", "commission_usd", "items"],
                         [[i + 1, r["seller_id"], _num(r["metric_value"]), r["basis_count"]] for i, r in enumerate(top)]))

    # 5 ---------------------------------------------------------------- on-time % per region
    reg = fetch("SELECT customer_region AS region, COUNT(*) AS orders, SUM(is_on_time) AS on_time, "
                "ROUND(100.0 * SUM(is_on_time) / COUNT(*), 2) AS pct FROM fact_order "
                "WHERE is_revenue_eligible = 1 AND is_on_time IS NOT NULL AND `year_month` >= :a AND `year_month` <= :b "
                "GROUP BY customer_region ORDER BY customer_region", a=f"{region_year}-01", b=f"{region_year}-12")
    tot = fetch("SELECT COUNT(*) AS orders, SUM(is_on_time) AS on_time, ROUND(100.0 * SUM(is_on_time) / COUNT(*), 2) AS pct FROM fact_order "
                "WHERE is_revenue_eligible = 1 AND is_on_time IS NOT NULL AND `year_month` >= :a AND `year_month` <= :b",
                a=f"{region_year}-01", b=f"{region_year}-12")[0]
    out.append(f"\n## 5. On-time % per region - {region_year}\n")
    out.append("On time = delivered date <= estimated date (R10); delivered orders with a delivery date only (R3); "
               "region = region of the order's delivery state (R17).\n\n")
    out.append(_md_table(["Region", "Orders with SLA", "On time", "On-time %"],
                         [[r["region"] or "(unknown)", f"{r['orders']:,}", f"{int(r['on_time'] or 0):,}", _num(r["pct"])]
                          for r in reg] + [["**All regions**", f"{tot['orders']:,}", f"{int(tot['on_time'] or 0):,}", _num(tot["pct"])]]))

    # 6 ---------------------------------------------------------------- campaigns
    camp = {r["campaign_id"]: r for r in fetch(
        "SELECT campaign_id, COUNT(*) AS items, SUM(is_revenue_eligible) AS eligible FROM fact_order_item "
        "WHERE campaign_id IS NOT NULL AND purchase_ts >= :lo AND purchase_ts < :hi GROUP BY campaign_id", **win)}
    out.append("\n## 6. Items attributed to each campaign (R18)\n")
    out.append(_md_table(["Campaign", "Items attributed (all loaded statuses)", "of which revenue eligible"],
                         [[c, f"{camp[c]['items']:,}" if c in camp else "0",
                           f"{int(camp[c]['eligible'] or 0):,}" if c in camp else "0"] for c in CAMPAIGNS]))

    # 7 ---------------------------------------------------------------- internal files
    rows = fetch("SELECT rule_id, entity_type, entity_id, issue_type, details FROM data_quality_issue "
                 "WHERE run_id = :r AND entity_type = 'internal_file' ORDER BY issue_id", r=run_id)
    out.append("\n" + internal_files_section([(x["rule_id"], x["entity_type"], x["entity_id"], x["issue_type"],
                                               x["details"]) for x in rows]))

    # 8 ---------------------------------------------------------------- holiday API findings (R9)
    hol = fetch("SELECT YEAR(holiday_date) AS y, scope, COUNT(*) AS n FROM ref_holiday GROUP BY YEAR(holiday_date), scope "
                "ORDER BY y, scope") if _is_mysql(fetch) else fetch(
        "SELECT SUBSTR(holiday_date, 1, 4) AS y, scope, COUNT(*) AS n FROM ref_holiday GROUP BY 1, 2 ORDER BY 1, 2")
    regional = fetch("SELECT holiday_date, state_code, local_name FROM ref_holiday WHERE scope = 'regional' "
                     "ORDER BY holiday_date, state_code")
    out.append("\n## 8. What the holiday API returned (R9)\n")
    out.append(_md_table(["Year", "Scope", "Rows"], [[h["y"], h["scope"], h["n"]] for h in hol]))
    out.append(f"\nRegional (counties) rows found: {len(regional)}.\n")
    if regional:
        out.append(_md_table(["Date", "State", "Name"], [[r["holiday_date"], r["state_code"], r["local_name"]]
                                                          for r in regional[:60]]))
    out.append("\nCounting convention: business days in the window (approval date, delivered date] - the approval day is "
               "NOT counted, the delivery day IS counted (see ASSUMPTIONS.md R9).\n")
    return "\n".join(out)


def _is_mysql(fetch) -> bool:
    return getattr(fetch, "dialect", "") == "mysql"


# ---------------------------------------------------------------------------- CLI
def _sqlalchemy_fetch(url: str) -> Fetch:
    from sqlalchemy import create_engine, text
    engine = create_engine(url, pool_pre_ping=True)

    def fetch(sql: str, **params) -> list[dict]:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]
    fetch.dialect = engine.dialect.name  # type: ignore[attr-defined]
    fetch.engine = engine                # type: ignore[attr-defined]
    return fetch


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline.results")
    p.add_argument("--run-id", help="default: latest successful run over --start/--end")
    p.add_argument("--start-date", type=date.fromisoformat, default=date(2017, 1, 1))
    p.add_argument("--end-date", type=date.fromisoformat, default=date(2018, 8, 31))
    p.add_argument("--database-url")
    p.add_argument("--internal-dir", default="data/internal")
    p.add_argument("--output", default="RESULTS.md")
    p.add_argument("--internal-only", action="store_true", help="no database; write only the internal-files table")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.internal_only:
        md = internal_only_markdown(Path(args.internal_dir))
    else:
        from .config import Settings
        fetch = _sqlalchemy_fetch(args.database_url or Settings.from_env().database_url)
        run_id = args.run_id
        if not run_id:
            rows = fetch("SELECT run_id FROM pipeline_run WHERE status = 'success' AND range_start = :s AND "
                         "range_end = :e ORDER BY started_at DESC LIMIT 1", s=args.start_date, e=args.end_date)
            if not rows:
                log.error("no successful run for %s..%s; run `python -m pipeline.run` first or pass --run-id",
                          args.start_date, args.end_date)
                return 2
            run_id = rows[0]["run_id"]
        md = generate(fetch, run_id, args.start_date, args.end_date)
    Path(args.output).write_text(md, encoding="utf-8")
    log.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
