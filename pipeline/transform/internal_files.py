"""Clean the human-maintained internal files (R8 patch, R13 rates/overrides, R18 campaigns).

Every row that is rejected, corrected or normalised is written to the DQ log with the reason
(entity_type = 'internal_file').  Nothing is silently dropped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .common import DQLog, is_missing

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
VALID_TIERS = {"Bronze", "Silver", "Gold"}
ENTITY = "internal_file"


# --------------------------------------------------------------------------- helpers
def _read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    df.columns = [c.strip() for c in df.columns]
    return df


def _clean_str(value, fname: str, row: int, col: str, dq: DQLog, rule: str, lower=False, title=False) -> str | None:
    """strip (+ lower / capitalise) and log if the value actually changed."""
    if is_missing(value):
        return None
    new = str(value).strip()
    if lower:
        new = new.lower()
    if title:
        new = new.capitalize()
    if new != str(value):
        dq.add(rule, ENTITY, f"{fname}:row{row}", "normalized_value", f"{col}: {value!r} -> {new!r}")
    return new


def _parse_date(value, fname: str, row: int, col: str, dq: DQLog, rule: str) -> date | None | str:
    """Returns a date, None (blank = open ended) or the string 'INVALID'."""
    if is_missing(value) or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        dq.add(rule, ENTITY, f"{fname}:row{row}", "rejected_invalid_date", f"{col}={value!r} is not YYYY-MM-DD")
        return "INVALID"


def _resolve_overlaps(rows: list[dict], key: str, fname: str, dq: DQLog, rule: str) -> list[dict]:
    """Same key + overlapping validity windows: the row with the later effective_from wins from its
    own start; the earlier row is cut off the day before. Logged as a correction."""
    out: list[dict] = []
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(r[key], []).append(r)
    for k, group in by_key.items():
        group.sort(key=lambda r: r["effective_from"])
        for prev, cur in zip(group, group[1:]):
            prev_end = prev["effective_to"] or date.max
            if cur["effective_from"] <= prev_end:
                new_end = cur["effective_from"] - timedelta(days=1)
                dq.add(rule, ENTITY, f"{fname}:row{prev['source_row']}", "corrected_overlap",
                       f"{key}={k}: window {prev['effective_from']}..{prev['effective_to'] or 'open'} overlaps "
                       f"row{cur['source_row']} (from {cur['effective_from']}); end set to {new_end}")
                prev["effective_to"] = new_end
        out.extend(group)
    return out


def _cut(df: pd.DataFrame) -> pd.DataFrame:
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- commission_rates.csv
def load_commission_rates(path: Path, dq: DQLog) -> pd.DataFrame:
    """Columns: tier, rate (Decimal), effective_from, effective_to (None = open), source_row."""
    fname = Path(path).name
    raw = _read(path)
    rows = []
    for i, r in raw.iterrows():
        n = i + 2  # spreadsheet-style row number (header = 1)
        tier = _clean_str(r.get("tier"), fname, n, "tier", dq, "R13", title=True)
        if tier not in VALID_TIERS:
            dq.add("R13", ENTITY, f"{fname}:row{n}", "rejected_unknown_tier", f"tier={r.get('tier')!r}")
            continue
        try:
            rate = Decimal(str(r["rate"]).strip())
            if not (Decimal("0") <= rate <= Decimal("1")):
                raise InvalidOperation
        except (InvalidOperation, KeyError, TypeError):
            dq.add("R13", ENTITY, f"{fname}:row{n}", "rejected_invalid_rate", f"rate={r.get('rate')!r}")
            continue
        start = _parse_date(r.get("effective_from"), fname, n, "effective_from", dq, "R13")
        end = _parse_date(r.get("effective_to"), fname, n, "effective_to", dq, "R13")
        if start in (None, "INVALID") or end == "INVALID":
            if start is None:
                dq.add("R13", ENTITY, f"{fname}:row{n}", "rejected_missing_start", "effective_from is blank")
            continue
        if end is None:
            dq.add("R13", ENTITY, f"{fname}:row{n}", "open_ended_range",
                   f"tier={tier}: blank effective_to treated as open ended (no end date)")
        elif end < start:
            dq.add("R13", ENTITY, f"{fname}:row{n}", "rejected_impossible_range",
                   f"tier={tier}: effective_to {end} is before effective_from {start}")
            continue
        rows.append({"tier": tier, "rate": rate, "effective_from": start, "effective_to": end, "source_row": n})
    rows = _resolve_overlaps(rows, "tier", fname, dq, "R13")
    cols = ["tier", "rate", "effective_from", "effective_to", "source_row"]
    return _cut(pd.DataFrame(rows, columns=cols))


# --------------------------------------------------------------------------- category_commission_overrides.csv
def load_category_overrides(path: Path, dq: DQLog) -> pd.DataFrame:
    """Columns: category_en, fixed_rate (Decimal), effective_from, effective_to, notes, source_row."""
    fname = Path(path).name
    raw = _read(path)
    rows = []
    for i, r in raw.iterrows():
        n = i + 2
        cat = _clean_str(r.get("category_name_en"), fname, n, "category_name_en", dq, "R13", lower=True)
        if not cat:
            dq.add("R13", ENTITY, f"{fname}:row{n}", "rejected_missing_category", "")
            continue
        try:
            rate = Decimal(str(r["fixed_rate"]).strip())
            if not (Decimal("0") <= rate <= Decimal("1")):
                raise InvalidOperation
        except (InvalidOperation, KeyError, TypeError):
            dq.add("R13", ENTITY, f"{fname}:row{n}", "rejected_invalid_rate", f"fixed_rate={r.get('fixed_rate')!r}")
            continue
        start = _parse_date(r.get("effective_from"), fname, n, "effective_from", dq, "R13")
        end = _parse_date(r.get("effective_to"), fname, n, "effective_to", dq, "R13")
        if start in (None, "INVALID") or end == "INVALID":
            if start is None:
                dq.add("R13", ENTITY, f"{fname}:row{n}", "rejected_missing_start", "effective_from is blank")
            continue
        if end is None:
            dq.add("R13", ENTITY, f"{fname}:row{n}", "open_ended_range",
                   f"category={cat}: blank effective_to treated as open ended (no end date)")
        elif end < start:
            # Decision (ASSUMPTIONS.md): do NOT guess a 20% commission - reject and flag for Finance.
            dq.add("R13", ENTITY, f"{fname}:row{n}", "rejected_impossible_range",
                   f"category={cat}: effective_to {end} is before effective_from {start}; "
                   f"override of {rate} NOT applied (needs confirmation from sales team)")
            continue
        notes = "" if is_missing(r.get("notes")) else str(r["notes"]).strip()
        rows.append({"category_en": cat, "fixed_rate": rate, "effective_from": start,
                     "effective_to": end, "notes": notes, "source_row": n})
    rows = _resolve_overlaps(rows, "category_en", fname, dq, "R13")
    cols = ["category_en", "fixed_rate", "effective_from", "effective_to", "notes", "source_row"]
    return _cut(pd.DataFrame(rows, columns=cols))


# --------------------------------------------------------------------------- category_translation_patch.csv
def load_translation_patch(path: Path, dq: DQLog) -> dict[str, str]:
    fname = Path(path).name
    raw = _read(path)
    patch: dict[str, str] = {}
    for i, r in raw.iterrows():
        n = i + 2
        pt = _clean_str(r.get("product_category_name"), fname, n, "product_category_name", dq, "R8", lower=True)
        en = _clean_str(r.get("product_category_name_english"), fname, n, "product_category_name_english", dq,
                        "R8", lower=True)
        if not pt or not en:
            dq.add("R8", ENTITY, f"{fname}:row{n}", "rejected_blank_value", f"{r.to_dict()}")
            continue
        if pt in patch and patch[pt] != en:
            dq.add("R8", ENTITY, f"{fname}:row{n}", "corrected_duplicate_key",
                   f"{pt}: {patch[pt]!r} replaced by later row {en!r}")
        patch[pt] = en
    return patch


# --------------------------------------------------------------------------- campaigns.json
@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    name: str
    start_ts: datetime            # naive, America/Sao_Paulo local time, inclusive
    end_ts: datetime              # naive, America/Sao_Paulo local time, inclusive
    priority: int
    all_geo: bool                 # target_states or target_regions contains ALL
    states: frozenset = field(default_factory=frozenset)
    regions: frozenset = field(default_factory=frozenset)      # lower-cased
    all_categories: bool = False
    categories: frozenset = field(default_factory=frozenset)

    @property
    def window_days(self) -> int:
        return round(((self.end_ts + timedelta(seconds=1)) - self.start_ts).total_seconds() / 86400)


_PY_FORMATS = {"DD/MM/YYYY": "%d/%m/%Y", "MM/DD/YYYY": "%m/%d/%Y", "YYYY-MM-DD": "%Y-%m-%d"}


def _parse_campaign_moment(value: str, date_format: str | None, is_end: bool, cid: str, dq: DQLog) -> datetime:
    """Return naive Sao Paulo local time.
    - date-only start -> 00:00:00 ; date-only end -> 23:59:59.999999 (the whole last day counts)
    - ISO timestamp with 'Z' (UTC) -> converted to America/Sao_Paulo"""
    s = str(value).strip()
    if date_format:
        fmt = _PY_FORMATS.get(date_format.upper())
        if not fmt:
            raise ValueError(f"{cid}: unsupported date_format {date_format!r}")
        d = datetime.strptime(s, fmt).date()
        dq.add("R18", ENTITY, f"campaigns.json:{cid}", "normalized_value",
               f"{'end' if is_end else 'start'}: {s!r} parsed as {date_format} -> {d}")
        return datetime.combine(d, datetime.max.time() if is_end else datetime.min.time())
    if "T" in s:
        aware = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if aware.tzinfo is None:
            local = aware                       # no zone given: assume already local
        else:
            local = aware.astimezone(SAO_PAULO).replace(tzinfo=None)
            dq.add("R18", ENTITY, f"campaigns.json:{cid}", "normalized_value",
                   f"{'end' if is_end else 'start'}: {s} (UTC) converted to {local} America/Sao_Paulo")
        return local
    d = date.fromisoformat(s)
    return datetime.combine(d, datetime.max.time() if is_end else datetime.min.time())


def _target_list(value, lower: bool) -> tuple[bool, frozenset]:
    items = [str(v).strip() for v in (value or []) if str(v).strip()]
    all_flag = any(v.upper() == "ALL" for v in items)
    rest = [v.lower() if lower else v.upper() for v in items if v.upper() != "ALL"]
    return all_flag, frozenset(rest)


def load_campaigns(path: Path, dq: DQLog) -> list[Campaign]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Campaign] = []
    for c in raw:
        cid = str(c.get("campaign_id", "")).strip()
        try:
            start = _parse_campaign_moment(c["start"], c.get("date_format"), False, cid, dq)
            end = _parse_campaign_moment(c["end"], c.get("date_format"), True, cid, dq)
            if end < start:
                raise ValueError("end before start")
            states_all, states = _target_list(c.get("target_states"), lower=False)
            regions_all, regions = _target_list(c.get("target_regions"), lower=True)
            cats_all, cats = _target_list(c.get("categories"), lower=True)
            if "target_states" not in c and "target_regions" not in c:
                dq.add("R18", ENTITY, f"campaigns.json:{cid}", "rejected_no_geo_target",
                       "neither target_states nor target_regions present")
                continue
            out.append(Campaign(cid, str(c.get("name", cid)).strip(), start, end, int(c["priority"]),
                                states_all or regions_all, states, regions, cats_all, cats))
        except (KeyError, ValueError) as exc:
            dq.add("R18", ENTITY, f"campaigns.json:{cid or '?'}", "rejected_invalid_campaign", str(exc))
    return out
