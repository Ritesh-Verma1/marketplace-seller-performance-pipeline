"""RESULTS.md generator: runs every report query against the SQLite copy of the synthetic model."""
from datetime import date

import pytest

from pipeline import results
from tests.api_fixture import RUN_ID, sqlite_conn


@pytest.fixture(scope="module")
def md():
    con = sqlite_conn()

    def fetch(sql, **params):
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    return results.generate(fetch, RUN_ID, date(2018, 2, 1), date(2018, 3, 31), gross_month="2018-02")


def test_orders_and_exclusions(md):
    assert "**Total orders loaded:** 11" in md and "**Orders excluded by R2:** 2" in md
    assert "| canceled | 1 |" in md and "| unavailable | 1 |" in md


def test_dq_gross_top_region_campaign_sections(md):
    assert "| R2 | 2 |" in md and "| R5 | 1 |" in md
    assert "USD 192.55" in md                                            # Feb-2018 gross USD of delivered orders
    assert "| 1 | S3 | 21.52 |" in md and "| 2 | S1 | 11.63 |" in md   # March commission ranking
    assert "| Sul | 1 | 0 | 0.00 |" in md                              # O2 was late
    assert "| C003 | 2 | 2 |" in md and "| C001 | 0 | 0 |" in md


def test_internal_file_table_lists_every_fix(md):
    assert "category_commission_overrides.csv:row6" in md and "rejected_impossible_range" in md
    assert "commission_rates.csv:row8" in md and "'gold' -> 'Gold'" in md


def test_internal_only_mode_needs_no_database(tmp_path):
    out = tmp_path / "R.md"
    assert results.main(["--internal-only", "--output", str(out)]) == 0
    text = out.read_text()
    assert "Rows rejected or corrected" in text and "watches_gifts" in text
