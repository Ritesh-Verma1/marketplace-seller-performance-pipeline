"""Cleaning of the human-maintained internal files (R13 / R8 / R18 hygiene) using the real shipped files."""
from datetime import date, datetime
from pathlib import Path

from pipeline.transform.common import DQLog
from pipeline.transform import internal_files as inf

INTERNAL = Path(__file__).resolve().parents[1] / "data" / "internal"


def _issues(dq, issue_type):
    return [r for r in dq.rows if r[3] == issue_type]


def test_commission_rates_cleaning():
    dq = DQLog()
    df = inf.load_commission_rates(INTERNAL / "commission_rates.csv", dq)
    assert set(df["tier"]) == {"Bronze", "Silver", "Gold"}                      # 'gold' -> 'Gold'
    assert len(df) == 7                                                         # nothing rejected
    assert any("'gold' -> 'Gold'" in r[4] for r in _issues(dq, "normalized_value"))
    assert len(_issues(dq, "open_ended_range")) == 3                            # Bronze 0.16, Silver 0.11, Gold 0.09
    open_rows = df[df["effective_to"].isna()]
    assert set(open_rows["tier"]) == {"Bronze", "Silver", "Gold"}


def test_category_overrides_cleaning():
    dq = DQLog()
    df = inf.load_category_overrides(INTERNAL / "category_commission_overrides.csv", dq)
    assert sorted(df["category_en"]) == ["computers", "pc_gamer", "telephony", "telephony"]   # 'Telephony ' fixed
    rejected = _issues(dq, "rejected_impossible_range")
    assert len(rejected) == 1 and "watches_gifts" in rejected[0][4]            # 2018-05-01 .. 2018-04-30
    assert "watches_gifts" not in set(df["category_en"])                       # logged, not applied
    assert any("'Telephony ' -> 'telephony'" in r[4] for r in _issues(dq, "normalized_value"))


def test_translation_patch():
    dq = DQLog()
    patch = inf.load_translation_patch(INTERNAL / "category_translation_patch.csv", dq)
    assert patch["casa_conforto_2"] == "home_comfort" and len(patch) == 3


def test_campaign_dates_parsed_per_their_own_format():
    dq = DQLog()
    camps = {c.campaign_id: c for c in inf.load_campaigns(INTERNAL / "campaigns.json", dq)}
    assert len(camps) == 5
    assert camps["C001"].start_ts == datetime(2017, 11, 24) and camps["C001"].end_ts.date() == date(2017, 11, 27)
    assert camps["C002"].start_ts == datetime(2018, 2, 1)                       # DD/MM/YYYY, not Jan 2
    assert camps["C002"].end_ts.date() == date(2018, 2, 28)
    assert camps["C003"].start_ts == datetime(2018, 3, 5, 0, 0, 0)              # 03:00Z -> 00:00 Sao Paulo
    assert camps["C003"].end_ts == datetime(2018, 3, 11, 23, 59, 59)            # 02:59:59Z -> previous local day
    assert camps["C005"].regions == frozenset({"sul"}) and not camps["C005"].states
    assert camps["C001"].all_geo and camps["C001"].all_categories
    assert camps["C004"].all_geo and not camps["C004"].all_categories


def test_overlapping_rate_windows_are_resolved_and_logged(tmp_path):
    f = tmp_path / "rates.csv"
    f.write_text("tier,rate,effective_from,effective_to\nBronze,0.15,2016-01-01,2018-06-30\nBronze,0.16,2018-01-01,\n")
    dq = DQLog()
    df = inf.load_commission_rates(f, dq).sort_values("effective_from")
    assert df.iloc[0]["effective_to"] == date(2017, 12, 31)                     # earlier row cut off
    assert len(_issues(dq, "corrected_overlap")) == 1
