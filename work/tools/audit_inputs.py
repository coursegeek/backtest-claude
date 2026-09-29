#!/usr/bin/env python3
"""Clean-room input audit for Backtest V2 (diagnostic tool, not part of the engine).

Profiles every immutable raw file under ``input/data`` against the contract in
``input/python_backtest_specification_v3_1.csv`` and evaluates whether the AB
scenarios / CLI examples are feasible on the supplied data.

The parsers here are deliberately minimal and independent from the future
``work/src/data_loader.py``; they exist only to produce evidence.

Outputs (deterministic for identical inputs and ``--as-of``):
  work/audit/input_profile.json         per-source profile
  work/audit/input_checks.csv           one row per check (PASS/FAIL/WARN/INFO)
  work/audit/scenario_feasibility.csv   AB scenarios and CLI examples on supplied data

Usage:
  python work/tools/audit_inputs.py [--as-of YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import re
import shlex
import statistics
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "input"
DATA = INPUT / "data"
SPEC = INPUT / "python_backtest_specification_v3_1.csv"
MANIFEST = INPUT / "source_manifest.json"
AB_SCENARIOS = ROOT / "AB_SCENARIOS.csv"
OUT_DIR = ROOT / "work" / "audit"

# Audit date used for NORM-019 completeness. Fixed (not wall clock) so reruns are identical.
DEFAULT_AS_OF = "2026-09-29"
NORM019_EXAMPLE_AS_OF = "2026-09-22"

FILES = OrderedDict([
    ("stocks_signal", "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv"),
    ("stocks_return", "F-F_Research_Data_Factors_weekly.csv"),
    ("gold", "GOLD_REAL_weekly_1970_2026.csv"),
    ("btc", "BTC_REAL_weekly_2010_2026.csv"),
    ("dividend", "SPX_dividend_return_weekly_shiller.csv"),
    ("cpi", "CPIAUCNS.csv"),
    ("supplemental_schwert", "US_STOCK_PRICE_WEEKLY_schwert_1919_1962.csv"),
])

# Contract values quoted from the specification.
SPEC_SCHEMA = {
    "stocks_signal": ["week_start", "price_index_continuous"],            # SCHEMA-001
    "stocks_return": ["date", "Mkt-RF", "RF"],                            # SCHEMA-002
    "gold": ["week_end", "gold_pm_usd"],                                  # SCHEMA-003
    "btc": ["date", "price", "weekly_return", "close_date", "source_week_start"],  # SCHEMA-004
    "dividend": ["date", "dividend_return", "dividend_points", "spx_close_prev", "spx_close",
                 "year", "annual_yield_pct", "trailing_dps_points", "status"],     # SCHEMA-005
    "cpi": ["observation_date", "CPIAUCNS"],                              # SCHEMA-007
}
SPEC_BTC_RANGE = ("2011-07-08", "2026-09-18", 794)       # SEM-008
SPEC_DIVIDEND_RANGE = ("1970-01-02", "2026-09-18")        # SEM-007
SPEC_DIVIDEND_ACTUAL_UNTIL = "2025-12-26"                 # SEM-007
SPEC_FF_START = "1926-07-02"                              # SEM-002
SPEC_GOLD_START_YEAR = 1968                               # SEM-003
SPEC_STOCKS_SPLICE_YEAR = 1928                            # SEM-001
SPEC_PARTIAL_STOCK_WEEK = "2026-09-21"                    # SEM-011 / NORM-019
FF_HEADER_LINES_SPEC = 4                                  # SCHEMA-002 / NORM-014

# Signal defaults (SIG-001, DEF-002, DEF-004, DEF-022..024) used by the warm-up formula NORM-010.
DEFAULT_MA = 50
DEFAULT_CONFIRM = {"stocks": 2, "gold": 4, "btc": 1}
DEFAULT_DELAY = 1
TOL_RETURN = 1e-8
TOL_REL_DIVIDEND = 1e-9


# --------------------------------------------------------------------------- helpers
def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def friday_key(day: dt.date) -> dt.date:
    """Friday of the Monday-Sunday calendar week containing ``day`` (NORM-007/NORM-013)."""
    return day - dt.timedelta(days=day.weekday()) + dt.timedelta(days=4)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fnum(x: float, digits: int = 12) -> str:
    return format(x, f".{digits}g")


def weekday_counts(days) -> dict:
    c = Counter(x.strftime("%a") for x in days)
    order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {k: c[k] for k in order if c[k]}


def step_gaps(keys):
    """Return (step-size distribution, list of gaps) for an ascending list of dates."""
    steps = Counter((b - a).days for a, b in zip(keys, keys[1:]))
    gaps = [{"after": a.isoformat(), "before": b.isoformat(), "days": (b - a).days,
             "missing_week_keys": [(a + dt.timedelta(days=7 * i)).isoformat()
                                   for i in range(1, (b - a).days // 7)]}
            for a, b in zip(keys, keys[1:]) if (b - a).days != 7]
    return {str(k): v for k, v in sorted(steps.items())}, gaps


def duplicates(values):
    return sorted(k for k, v in Counter(values).items() if v > 1)


def read_csv(path: Path):
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(text.splitlines()))
    header = next(csv.reader(text.splitlines()))
    return header, rows, raw


def line_endings(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and not lf:
        return "CRLF"
    if lf and not crlf:
        return "LF"
    return f"mixed(CRLF={crlf},LF={lf})" if crlf or lf else "none"


def contiguous_tail(keys, before: dt.date):
    """Number of consecutive weekly keys strictly before ``before`` without a gap."""
    prior = [k for k in keys if k < before]
    n = 0
    for i in range(len(prior) - 1, -1, -1):
        if i < len(prior) - 1 and (prior[i + 1] - prior[i]).days != 7:
            break
        n += 1
    return n


class Checks:
    def __init__(self):
        self.rows = []

    def add(self, check_id, role, file, reqs, description, expected, observed, result, questions=""):
        assert result in {"PASS", "FAIL", "WARN", "INFO"}, result
        self.rows.append(OrderedDict([
            ("check_id", check_id), ("role", role), ("file", file),
            ("requirement_ids", reqs), ("check", description),
            ("expected", expected), ("observed", observed),
            ("result", result), ("question_ids", questions),
        ]))


# --------------------------------------------------------------------------- sources
def audit_manifest(checks: Checks, profile: dict):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = []
    for rec in manifest["files"]:
        p = ROOT / rec["staged_path"]
        if not p.is_file() or sha256(p) != rec["sha256"]:
            bad.append(rec["staged_path"])
    checks.add("A-MAN-001", "all", "input/source_manifest.json", "REPRO-001",
               "Staged files match source_manifest SHA256", "all match",
               "all match" if not bad else "mismatch: " + ";".join(bad),
               "PASS" if not bad else "FAIL")
    profile["manifest"] = {"files": len(manifest["files"]), "mismatches": bad}
    for role, name in FILES.items():
        spec_names = {
            "stocks_signal": "US_STOCK_PRICE_WEEKLY_1885_2026.csv",
            "stocks_return": "F-F_Research_Data_Factors_weekly.csv",
            "gold": "GOLD_LBMA_PM_weekly_backtest_ready.csv",
            "btc": "BTC_weekly_date_price_2011_2026.csv",
            "dividend": "SPX_dividend_return_weekly_1970_2026.csv",
            "cpi": "CPIAUCNS.csv",
        }
        if role not in spec_names:
            continue
        same = spec_names[role] == name
        checks.add(f"A-MAN-{role}", role, name, {
            "stocks_signal": "DATA-001", "stocks_return": "DATA-002", "gold": "DATA-003",
            "btc": "DATA-005", "dividend": "DATA-007", "cpi": "DATA-004"}[role],
            "Spec default filename exists in input/data",
            spec_names[role], name if same else f"absent; staged file is {name}",
            "PASS" if same else "WARN", "" if same else "Q-001")


def audit_stocks_signal(checks: Checks):
    role, name = "stocks_signal", FILES["stocks_signal"]
    header, rows, raw = read_csv(DATA / name)
    starts = [d(r["week_start"]) for r in rows]
    keys = [s + dt.timedelta(days=4) for s in starts]
    prices = [float(r["price_index_continuous"]) for r in rows]
    steps, gaps = step_gaps(keys)
    prof = OrderedDict(
        file=name, sha256=sha256(DATA / name), bytes=len(raw), line_endings=line_endings(raw),
        columns=header, rows=len(rows), date_column="week_start", date_encoding="ISO YYYY-MM-DD",
        date_weekdays=weekday_counts(starts), first_week_start=starts[0].isoformat(),
        last_week_start=starts[-1].isoformat(), first_friday_key=keys[0].isoformat(),
        last_friday_key=keys[-1].isoformat(), step_days=steps, gaps=gaps,
        duplicate_dates=[x.isoformat() for x in duplicates(starts)],
        non_positive_prices=sum(1 for p in prices if not p > 0),
        min_price=fnum(min(prices)), max_price=fnum(max(prices)),
    )
    missing = [c for c in SPEC_SCHEMA[role] if c not in header]
    checks.add("A-STK-001", role, name, "SCHEMA-001", "Minimal columns present",
               ",".join(SPEC_SCHEMA[role]), ",".join(header), "PASS" if not missing else "FAIL")
    checks.add("A-STK-002", role, name, "NORM-008;NORM-007",
               "week_start is Monday so week_end=week_start+4 is the Friday key",
               "all Monday", json.dumps(prof["date_weekdays"]),
               "PASS" if set(prof["date_weekdays"]) == {"Mon"} else "FAIL")
    checks.add("A-STK-003", role, name, "NORM-002", "No duplicate week_start", "0 duplicates",
               str(len(prof["duplicate_dates"])), "PASS" if not prof["duplicate_dates"] else "FAIL")
    checks.add("A-STK-004", role, name, "NORM-003;NORM-005;SIG-003;NORM-010",
               "Weekly continuity of signal price history", "no missing Friday keys",
               "; ".join(f"missing {','.join(g['missing_week_keys'])}" for g in gaps) or "none",
               "WARN" if gaps else "PASS", "Q-012" if gaps else "")
    checks.add("A-STK-005", role, name, "DATA-001;NORM-012;TEST-023",
               "History start vs default filename/TEST-023",
               "file name implies 1885; TEST-023 needs signal-only from 1920",
               f"first Friday key {keys[0].isoformat()}", "WARN", "Q-011")
    has_partial = any(s.isoformat() == SPEC_PARTIAL_STOCK_WEEK for s in starts)
    checks.add("A-STK-006", role, name, "SEM-011;NORM-019;TEST-051",
               "Partial last week row described by SEM-011 exists in file",
               f"week_start {SPEC_PARTIAL_STOCK_WEEK} present",
               "present" if has_partial else f"absent; last week_start {starts[-1].isoformat()}",
               "PASS" if has_partial else "WARN", "" if has_partial else "Q-010")
    checks.add("A-STK-007", role, name, "NORM-006", "All prices finite and positive",
               "0 invalid", str(prof["non_positive_prices"]),
               "PASS" if prof["non_positive_prices"] == 0 else "FAIL")

    # Segment provenance: compare with the supplemental Schwert file.
    _, srows, _ = read_csv(DATA / FILES["supplemental_schwert"])
    sch = {r["week_start"]: r["price_index_continuous"] for r in srows}
    sch_last = max(sch)
    identical_until = None
    first_diff = None
    for r in rows:
        ws = r["week_start"]
        if ws not in sch:
            if first_diff is None and identical_until is not None and ws <= sch_last:
                first_diff = ws
            continue
        if sch[ws] == r["price_index_continuous"] and first_diff is None:
            identical_until = ws
        elif first_diff is None:
            first_diff = ws
    prof["schwert_overlap"] = OrderedDict(
        schwert_file=FILES["supplemental_schwert"], identical_until_week_start=identical_until,
        first_different_week_start=first_diff,
        inferred_segments=[
            {"source": "Schwert (identical to supplemental file)",
             "week_start_from": starts[0].isoformat(), "week_start_to": identical_until},
            {"source": "non-Schwert (presumably SPX, rebased)",
             "week_start_from": first_diff, "week_start_to": starts[-1].isoformat()},
        ],
        segment_column_in_file=any(c.lower() in {"source", "segment"} for c in header),
    )
    splice_year = int(first_diff[:4]) if first_diff else None
    checks.add("A-STK-008", role, name, "SEM-001",
               "Schwert->SPX splice point", f"SPX from {SPEC_STOCKS_SPLICE_YEAR}",
               f"identical to Schwert until {identical_until}; differs from {first_diff}",
               "FAIL" if splice_year != SPEC_STOCKS_SPLICE_YEAR else "PASS", "Q-004")
    checks.add("A-STK-009", role, name, "SEM-001;REP-008",
               "File carries per-row source/segment metadata for the manifest",
               "segment source and range recoverable", "no source/segment column",
               "FAIL" if not prof["schwert_overlap"]["segment_column_in_file"] else "PASS", "Q-004")
    series = {"keys": keys, "prices": dict(zip(keys, prices))}
    return prof, series


def parse_ff(path: Path):
    raw = path.read_bytes()
    lines = raw.decode("ascii").splitlines()
    pat = re.compile(r"^\d{8}$")
    data, nondata = [], []
    for i, line in enumerate(lines, start=1):
        fields = [x.strip() for x in line.split(",")]
        if fields and pat.match(fields[0]):
            data.append((i, fields))
        else:
            nondata.append((i, line))
    return raw, lines, data, nondata


def audit_ff(checks: Checks, stock_series):
    role, name = "stocks_return", FILES["stocks_return"]
    raw, lines, data, nondata = parse_ff(DATA / name)
    first_data_line = data[0][0]
    header_line = lines[first_data_line - 2]
    header_fields = [x.strip() for x in header_line.split(",")]
    dates = [dt.datetime.strptime(f[0], "%Y%m%d").date() for _, f in data]
    keys = [friday_key(x) for x in dates]
    naive = [x + dt.timedelta(days=(4 - x.weekday()) % 7) for x in dates]
    col = {n: header_fields.index(n) for n in ("Mkt-RF", "RF")}
    mkt = [float(f[col["Mkt-RF"]]) for _, f in data]
    rf = [float(f[col["RF"]]) for _, f in data]
    steps, gaps = step_gaps(keys)
    nonnumeric = sum(1 for _, f in data for x in f[1:] if not re.match(r"^-?\d+(\.\d+)?$", x))
    prof = OrderedDict(
        file=name, sha256=sha256(DATA / name), bytes=len(raw), line_endings=line_endings(raw),
        total_lines=len(lines), preamble_lines_before_first_record=first_data_line - 1,
        header_line_number=first_data_line - 1, header_fields=header_fields,
        nondata_lines=[{"line": i, "text": t[:80]} for i, t in nondata],
        rows=len(data), date_encoding="YYYYMMDD", date_weekdays=weekday_counts(dates),
        first_date=dates[0].isoformat(), last_date=dates[-1].isoformat(),
        first_friday_key=keys[0].isoformat(), last_friday_key=keys[-1].isoformat(),
        step_days_after_mapping=steps, gaps_after_mapping=gaps,
        duplicate_friday_keys=[x.isoformat() for x in duplicates(keys)],
        naive_on_or_after_friday_mapping_differs=sum(1 for a, b in zip(naive, keys) if a != b),
        non_friday_dates=[{"date": x.isoformat(), "weekday": x.strftime("%a"),
                           "friday_key": friday_key(x).isoformat()}
                          for x in dates if x.weekday() not in (4, 5, 3)],
        nonnumeric_fields=nonnumeric,
        mkt_rf_pct_min=fnum(min(mkt)), mkt_rf_pct_max=fnum(max(mkt)),
        rf_pct_min=fnum(min(rf)), rf_pct_max=fnum(max(rf)),
        negative_rf_weeks=[k.isoformat() for k, v in zip(keys, rf) if v < 0],
        zero_rf_weeks=sum(1 for v in rf if v == 0),
    )
    checks.add("A-FF-001", role, name, "SCHEMA-002;NORM-014;TEST-027",
               "Non-data lines before first YYYYMMDD record",
               f"{FF_HEADER_LINES_SPEC} header lines (spec wording)",
               f"{prof['preamble_lines_before_first_record']} lines (4 preamble incl. blank + 1 column header); "
               f"trailer lines {[i for i, _ in nondata if i > first_data_line]}",
               "WARN", "Q-021")
    checks.add("A-FF-002", role, name, "SCHEMA-002;ERR-002",
               "Column header names", "date,Mkt-RF,RF",
               ",".join(repr(x) for x in header_fields), "WARN", "Q-021")
    checks.add("A-FF-003", role, name, "NORM-013;TEST-025;TEST-026",
               "Record weekdays (need same-calendar-week Friday mapping)",
               "Fri plus Sat/Thu/holiday dates", json.dumps(prof["date_weekdays"]), "INFO")
    checks.add("A-FF-004", role, name, "NORM-013;NORM-002",
               "Same-calendar-week Friday mapping yields unique keys", "0 duplicate keys",
               str(len(prof["duplicate_friday_keys"])),
               "PASS" if not prof["duplicate_friday_keys"] else "FAIL")
    checks.add("A-FF-005", role, name, "NORM-018",
               "Rows shifted by naive 'Friday on/after' mapping",
               "about 1158 historical weeks (NORM-018 note)",
               str(prof["naive_on_or_after_friday_mapping_differs"]),
               "PASS" if prof["naive_on_or_after_friday_mapping_differs"] == 1158 else "WARN")
    checks.add("A-FF-006", role, name, "NORM-003;NORM-004",
               "Weekly continuity of return series", "no missing Friday keys",
               "; ".join(f"missing {','.join(g['missing_week_keys'])}" for g in gaps) or "none",
               "WARN" if gaps else "PASS", "Q-012" if gaps else "")
    checks.add("A-FF-007", role, name, "SEM-002", "First record date", SPEC_FF_START,
               dates[0].isoformat(), "PASS" if dates[0].isoformat() == SPEC_FF_START else "FAIL")
    checks.add("A-FF-008", role, name, "PORT-001;PORT-002;TEST-007",
               "Values are percent (need /100)", "|Mkt-RF| well above 1 in crashes",
               f"Mkt-RF [{prof['mkt_rf_pct_min']},{prof['mkt_rf_pct_max']}] RF [{prof['rf_pct_min']},{prof['rf_pct_max']}]",
               "PASS" if max(abs(x) for x in mkt) > 1.0 else "FAIL")
    checks.add("A-FF-009", role, name, "NORM-006", "All numeric fields parse", "0 non-numeric",
               str(nonnumeric), "PASS" if nonnumeric == 0 else "FAIL")
    checks.add("A-FF-010", role, name, "IND-014;TEST-032;PORT-013",
               "Negative RF weeks exist (no tax credit rule is exercised by real data)", "info",
               f"{len(prof['negative_rf_weeks'])} weeks: {','.join(prof['negative_rf_weeks'])}", "INFO")

    # NORM-018: correlation with the stock price index on correct vs naive index.
    ff_ret = {k: (m + r) / 100.0 for k, m, r in zip(keys, mkt, rf)}
    ff_naive = {k: (m + r) / 100.0 for k, m, r in zip(naive, mkt, rf)}
    sk, sp = stock_series["keys"], stock_series["prices"]
    pret = {b: sp[b] / sp[a] - 1 for a, b in zip(sk, sk[1:]) if (b - a).days == 7}

    def corr(a, b, hi=None):
        ks = sorted(k for k in a if k in b and (hi is None or k <= hi))
        return len(ks), statistics.correlation([a[k] for k in ks], [b[k] for k in ks])

    n1, c1 = corr(ff_ret, pret)
    n2, c2 = corr(ff_naive, pret)
    n3, c3 = corr(ff_ret, pret, dt.date(1952, 12, 31))
    n4, c4 = corr(ff_naive, pret, dt.date(1952, 12, 31))
    prof["alignment_vs_stock_index"] = OrderedDict(
        same_week_corr=fnum(c1, 6), same_week_n=n1, naive_corr=fnum(c2, 6), naive_n=n2,
        same_week_corr_to_1952=fnum(c3, 6), naive_corr_to_1952=fnum(c4, 6))
    checks.add("A-FF-011", role, name, "NORM-018;NORM-013;NORM-008",
               "Correlation FF (Mkt-RF+RF) vs stock price index returns",
               "high on same-calendar-week index, low on shifted index",
               f"same-week {fnum(c1, 4)} (n={n1}); naive {fnum(c2, 4)}; to 1952: {fnum(c3, 4)} vs {fnum(c4, 4)}",
               "PASS" if c1 > 0.95 and c1 > c2 else "FAIL")
    missing_stock = sorted(k for k in keys if k not in sp)
    stock_extra = sorted(k for k in sk if keys[0] <= k <= keys[-1] and k not in ff_ret)
    checks.add("A-X-001", "cross", f"{name}|{FILES['stocks_signal']}", "NORM-007;NORM-011",
               "Friday keys of FF and stock signal agree over the FF range", "identical key sets",
               f"FF-only {len(missing_stock)}; stocks-only {len(stock_extra)}",
               "PASS" if not missing_stock and not stock_extra else "WARN")
    series = {"keys": keys, "ret": ff_ret, "rf": {k: r / 100.0 for k, r in zip(keys, rf)}}
    return prof, series


def audit_gold(checks: Checks, ff_series):
    role, name = "gold", FILES["gold"]
    header, rows, raw = read_csv(DATA / name)
    starts = [d(r["week_start"]) for r in rows]
    keys = [s + dt.timedelta(days=4) for s in starts]
    prices = [float(r["gold_pm_usd"]) for r in rows]
    steps, gaps = step_gaps(keys)
    segs = []
    for r, k in zip(rows, keys):
        if not segs or segs[-1]["source"] != r["source"]:
            segs.append({"source": r["source"], "from_key": k.isoformat(), "to_key": k.isoformat(), "rows": 0})
        segs[-1]["to_key"] = k.isoformat()
        segs[-1]["rows"] += 1
    mism, empty = [], []
    for i, r in enumerate(rows):
        if r["weekly_return"] == "":
            empty.append(r["week_start"])
            continue
        e = abs(float(r["weekly_return"]) - (prices[i] / prices[i - 1] - 1))
        if e > TOL_RETURN:
            mism.append(r["week_start"])
    prof = OrderedDict(
        file=name, sha256=sha256(DATA / name), bytes=len(raw), line_endings=line_endings(raw),
        columns=header, rows=len(rows), date_column="week_start", date_encoding="ISO YYYY-MM-DD",
        date_weekdays=weekday_counts(starts), first_friday_key=keys[0].isoformat(),
        last_friday_key=keys[-1].isoformat(), step_days=steps, gaps=gaps,
        duplicate_dates=[x.isoformat() for x in duplicates(starts)],
        source_counts=dict(sorted(Counter(r["source"] for r in rows).items())),
        source_segments_count=len(segs),
        source_segments_major=[s for s in segs if s["rows"] >= 10],
        fill_rows=[r["week_start"] for r in rows if "FILL" in r["source"]],
        empty_weekly_return=empty, weekly_return_mismatch=mism,
        zero_return_weeks=sum(1 for r in rows if r["weekly_return"] and float(r["weekly_return"]) == 0.0),
        non_positive_prices=sum(1 for p in prices if not p > 0),
    )
    checks.add("A-GLD-001", role, name, "SCHEMA-003;NORM-007",
               "Date column", "week_end (Friday)", f"week_start ({json.dumps(prof['date_weekdays'])})",
               "FAIL", "Q-003")
    checks.add("A-GLD-002", role, name, "SEM-003;DATA-003;SIG-009",
               "Price source is LBMA Gold Price PM", "LBMA PM",
               "sources " + json.dumps(prof["source_counts"]), "FAIL", "Q-002")
    checks.add("A-GLD-003", role, name, "SEM-003", "History start", f"LBMA PM from {SPEC_GOLD_START_YEAR}",
               f"first Friday key {keys[0].isoformat()}", "FAIL", "Q-002;Q-013")
    checks.add("A-GLD-004", role, name, "NORM-005;NORM-006",
               "Imputed/fill rows from a secondary source", "none or explicitly flagged",
               f"{len(prof['fill_rows'])} FOREXCOM_FILL rows {prof['fill_rows'][0]}..{prof['fill_rows'][-1]}",
               "WARN", "Q-002")
    checks.add("A-GLD-005", role, name, "NORM-003", "Weekly continuity", "no gaps",
               str(len(gaps)), "PASS" if not gaps else "WARN")
    checks.add("A-GLD-006", role, name, "PORT-003;TEST-008;SCHEMA-003",
               "weekly_return equals price ratio", f"|diff|<={TOL_RETURN}",
               f"mismatches {len(mism)}; empty {empty}", "PASS" if not mism else "FAIL")
    checks.add("A-GLD-007", role, name, "NORM-002", "No duplicate dates", "0",
               str(len(prof["duplicate_dates"])), "PASS" if not prof["duplicate_dates"] else "FAIL")
    ffk = set(ff_series["keys"])
    extra = sorted(k for k in keys if k <= ff_series["keys"][-1] and k not in ffk)
    miss = sorted(k for k in ff_series["keys"] if k >= keys[0] and k not in set(keys))
    checks.add("A-X-002", "cross", f"{name}|{FILES['stocks_return']}", "NORM-007;NORM-011",
               "Gold Friday keys (week_start+4) agree with FF keys in overlap", "identical",
               f"gold-only {len(extra)}; FF-only {len(miss)}",
               "PASS" if not extra and not miss else "WARN")
    return prof, {"keys": keys, "prices": dict(zip(keys, prices))}


def audit_btc(checks: Checks, ff_series, as_of: dt.date):
    role, name = "btc", FILES["btc"]
    header, rows, raw = read_csv(DATA / name)
    dates = [d(r["date"]) for r in rows]
    close = [d(r["close_date"]) for r in rows]
    prices = [float(r["price"]) for r in rows]
    keys = [friday_key(x) for x in dates]
    steps, gaps = step_gaps(dates)
    mism, empty = [], []
    for i, r in enumerate(rows):
        if r["weekly_return"] == "":
            empty.append(r["date"])
            continue
        if abs(float(r["weekly_return"]) - (prices[i] / prices[i - 1] - 1)) > TOL_RETURN:
            mism.append(r["date"])
    gap_spanning = [rows[i]["date"] for i in range(1, len(rows)) if (dates[i] - dates[i - 1]).days != 7]
    lo, hi, n_spec = SPEC_BTC_RANGE
    in_spec = [k for k in keys if d(lo) <= k <= d(hi)]
    prof = OrderedDict(
        file=name, sha256=sha256(DATA / name), bytes=len(raw), line_endings=line_endings(raw),
        columns=header, rows=len(rows), date_encoding="ISO YYYY-MM-DD",
        date_weekdays=weekday_counts(dates), close_date_weekdays=weekday_counts(close),
        close_minus_date_days=dict(Counter((c - x).days for c, x in zip(close, dates))),
        close_minus_friday_key_days=dict(Counter((c - k).days for c, k in zip(close, keys))),
        date_equals_source_week_start=sum(1 for r in rows if r["date"] == r["source_week_start"]),
        sources=dict(Counter(r["source"] for r in rows)),
        first_date=dates[0].isoformat(), last_date=dates[-1].isoformat(),
        first_friday_key=keys[0].isoformat(), last_friday_key=keys[-1].isoformat(),
        last_close_date=close[-1].isoformat(), step_days=steps, gaps=gaps,
        returns_spanning_gap=gap_spanning, empty_weekly_return=empty, weekly_return_mismatch=mism,
        duplicate_dates=[x.isoformat() for x in duplicates(dates)],
        rows_in_spec_range_after_monday_to_friday=len(in_spec),
        spec_range=f"{lo}..{hi} ({n_spec} rows)",
    )
    checks.add("A-BTC-001", role, name, "SCHEMA-004", "Minimal columns present",
               ",".join(SPEC_SCHEMA[role]), ",".join(header),
               "PASS" if all(c in header for c in SPEC_SCHEMA[role]) else "FAIL")
    checks.add("A-BTC-002", role, name, "SCHEMA-004;SEM-008;TEST-039;NORM-016",
               "date column is the Friday join key", "Friday",
               json.dumps(prof["date_weekdays"]) + " (date == source_week_start in "
               f"{prof['date_equals_source_week_start']}/{len(rows)} rows)", "FAIL", "Q-005")
    checks.add("A-BTC-003", role, name, "SCHEMA-004;SEM-008;TEST-039",
               "close_date is Sunday and close_date-date=2", "Sunday; +2 days",
               f"{json.dumps(prof['close_date_weekdays'])}; close-date {json.dumps(prof['close_minus_date_days'])}; "
               f"close-(date+4) {json.dumps(prof['close_minus_friday_key_days'])}", "FAIL", "Q-005")
    checks.add("A-BTC-004", role, name, "SEM-008;TEST-039",
               "Range and row count", f"{lo}..{hi}, {n_spec} rows, no gaps",
               f"{keys[0].isoformat()}..{keys[-1].isoformat()} (Friday keys), {len(rows)} rows; "
               f"{len(in_spec)} rows inside documented range",
               "FAIL", "Q-005;Q-006")
    checks.add("A-BTC-005", role, name, "NORM-003;NORM-004;NORM-005;TEST-039",
               "Weekly continuity",
               "no missing weeks",
               "; ".join(f"missing {','.join(g['missing_week_keys'])} (Monday dates)" for g in gaps)
               + f"; weekly_return of {gap_spanning} spans the gap",
               "FAIL" if gaps else "PASS", "Q-006;Q-012")
    checks.add("A-BTC-006", role, name, "SCHEMA-004;PORT-004",
               "weekly_return equals price_t/price_t-1-1", f"|diff|<={TOL_RETURN}",
               f"mismatches {len(mism)}; empty {empty}", "PASS" if not mism else "FAIL")
    ffk = set(ff_series["keys"])
    miss = sorted(k for k in ff_series["keys"] if k >= keys[0] and k not in set(keys))
    extra = sorted(k for k in keys if k <= ff_series["keys"][-1] and k not in ffk)
    checks.add("A-X-003", "cross", f"{name}|{FILES['stocks_return']}", "NORM-007;NORM-017",
               "BTC Friday keys (date+4) agree with FF keys in overlap", "identical",
               f"FF-only {[x.isoformat() for x in miss]}; BTC-only {len(extra)}",
               "PASS" if not miss and not extra else "WARN", "Q-006" if miss else "")
    # NORM-019 vs NORM-016: completeness by Friday key vs availability by close_date.
    ex = d(NORM019_EXAMPLE_AS_OF)
    problem = [(k, c) for k, c in zip(keys, close) if k <= ex < c]
    sat_probe = keys[-1] + dt.timedelta(days=1)
    checks.add("A-BTC-007", role, name, "NORM-019;NORM-016;SEM-009;META-003",
               "Friday-key completeness rule vs Sunday availability",
               "a week counted as complete only when its close is known",
               f"with as_of=Saturday {sat_probe.isoformat()} the week {keys[-1].isoformat()} passes NORM-019 "
               f"but its close_date {close[-1].isoformat()} is after as_of",
               "WARN", "Q-007")
    prof["completeness"] = OrderedDict(
        as_of=as_of.isoformat(),
        rows_with_friday_key_after_as_of=sum(1 for k in keys if k > as_of),
        rows_with_close_after_as_of=sum(1 for c in close if c > as_of),
        example_as_of=NORM019_EXAMPLE_AS_OF,
        example_rows_dropped_by_friday_rule=sum(1 for k in keys if k > ex),
        example_rows_with_close_after=sum(1 for c in close if c > ex),
        example_ambiguous_rows=len(problem),
    )
    return prof, {"keys": keys, "prices": dict(zip(keys, prices)), "close": dict(zip(keys, close))}


def audit_dividend(checks: Checks, stock_series, ff_series):
    role, name = "dividend", FILES["dividend"]
    header, rows, raw = read_csv(DATA / name)
    dates = [d(r["date"]) for r in rows]
    steps, gaps = step_gaps(dates)
    rel_err = []
    for r in rows:
        dr = float(r["dividend_return"])
        calc = float(r["dividend_points"]) / float(r["spx_close_prev"])
        rel_err.append(abs(dr - calc) / max(abs(dr), 1e-300))
    status = Counter(r["status"] for r in rows)
    by_month = {}
    for r in rows:
        by_month.setdefault(r["date"][:7], set()).add(r["dividend_return"])
    const_months = sum(1 for v in by_month.values() if len(v) == 1)
    sp = stock_series["prices"]
    link_n, link_exact, link_missing = 0, 0, []
    for x, r in zip(dates, rows):
        prev = x - dt.timedelta(days=7)
        if prev in sp:
            link_n += 1
            if abs(float(r["spx_close_prev"]) / sp[prev] - 1) < 1e-12:
                link_exact += 1
        else:
            link_missing.append(x.isoformat())
    ff_keys = set(ff_series["keys"])
    missing_cols = [c for c in SPEC_SCHEMA[role] if c not in header]
    lo, hi = SPEC_DIVIDEND_RANGE
    prof = OrderedDict(
        file=name, sha256=sha256(DATA / name), bytes=len(raw), line_endings=line_endings(raw),
        columns=header, missing_spec_columns=missing_cols, rows=len(rows),
        date_encoding="ISO YYYY-MM-DD", date_weekdays=weekday_counts(dates),
        first_date=dates[0].isoformat(), last_date=dates[-1].isoformat(),
        step_days=steps, gaps=gaps, duplicate_dates=[x.isoformat() for x in duplicates(dates)],
        status_counts=dict(status), formula_max_rel_error=fnum(max(rel_err), 3),
        months=len(by_month), months_with_constant_dividend_return=const_months,
        spx_close_prev_linked_to_stock_index=f"{link_exact}/{link_n}",
        rows_without_stock_index_prev_week=link_missing,
        rows_not_in_ff_calendar=[x.isoformat() for x in dates
                                 if ff_series["keys"][0] <= x <= ff_series["keys"][-1] and x not in ff_keys],
        rows_from_1970_01_02=sum(1 for x in dates if x >= d(lo)),
        ff_weeks_after_last_dividend=sum(1 for k in ff_series["keys"] if k > dates[-1]),
    )
    checks.add("A-DIV-001", role, name, "SCHEMA-005;ERR-002", "Columns listed in SCHEMA-005",
               ",".join(SPEC_SCHEMA[role]), "missing " + ",".join(missing_cols),
               "FAIL" if missing_cols else "PASS", "Q-008")
    checks.add("A-DIV-002", role, name, "SEM-007;DIV-012;TEST-038", "Dates are Friday",
               "all Friday", json.dumps(prof["date_weekdays"]),
               "PASS" if set(prof["date_weekdays"]) == {"Fri"} else "FAIL")
    checks.add("A-DIV-003", role, name, "SEM-007;DIV-012;TEST-038;NORM-003", "Weekly continuity",
               "no gaps", str(len(gaps)), "PASS" if not gaps else "FAIL")
    checks.add("A-DIV-004", role, name, "SEM-007;TEST-038", "Range", f"{lo}..{hi}",
               f"{dates[0].isoformat()}..{dates[-1].isoformat()} "
               f"({prof['ff_weeks_after_last_dividend']} FF weeks after last dividend row)",
               "FAIL", "Q-009")
    checks.add("A-DIV-005", role, name, "SEM-007;DIV-011", "status values",
               f"actual until {SPEC_DIVIDEND_ACTUAL_UNTIL}, estimate in 2026",
               json.dumps(prof["status_counts"]), "FAIL", "Q-008")
    checks.add("A-DIV-006", role, name, "SCHEMA-005;DIV-012;SEM-007;TEST-038",
               "dividend_return == dividend_points/spx_close_prev", f"rel err <= {TOL_REL_DIVIDEND}",
               f"max rel err {prof['formula_max_rel_error']}",
               "PASS" if max(rel_err) <= TOL_REL_DIVIDEND else "FAIL")
    checks.add("A-DIV-007", role, name, "DIV-009;SEM-007",
               "Smoothing granularity", "smoothed weekly share of annual/trailing DPS",
               f"dividend_return constant within month for {const_months}/{len(by_month)} months "
               "(monthly yield, Shiller-style)", "WARN", "Q-008")
    checks.add("A-DIV-008", role, name, "NORM-007;DIV-005",
               "spx_close_prev equals stock signal index at previous Friday key",
               "alignment evidence", f"exact {link_exact}/{link_n}; no prior index week {link_missing}",
               "PASS" if link_exact == link_n else "WARN")
    checks.add("A-DIV-009", role, name, "NORM-007;NORM-011",
               "Dividend weeks outside FF calendar", "none",
               ",".join(prof["rows_not_in_ff_calendar"]) or "none",
               "INFO")
    series = {"keys": dates, "div": {x: float(r["dividend_return"]) for x, r in zip(dates, rows)}}
    return prof, series


def audit_cpi(checks: Checks):
    role, name = "cpi", FILES["cpi"]
    header, rows, raw = read_csv(DATA / name)
    days = [d(r["observation_date"]) for r in rows]
    empty = [r["observation_date"] for r in rows if r["CPIAUCNS"].strip() == ""]
    month_gaps = [(a.isoformat(), b.isoformat()) for a, b in zip(days, days[1:])
                  if (b.year - a.year) * 12 + b.month - a.month != 1]
    prof = OrderedDict(
        file=name, sha256=sha256(DATA / name), bytes=len(raw), line_endings=line_endings(raw),
        columns=header, rows=len(rows), date_encoding="ISO YYYY-MM-DD (first of month)",
        day_of_month=dict(Counter(x.day for x in days)),
        first_month=days[0].isoformat()[:7], last_month=days[-1].isoformat()[:7],
        empty_values=empty, month_gaps=month_gaps,
    )
    checks.add("A-CPI-001", role, name, "SCHEMA-007", "Minimal columns",
               ",".join(SPEC_SCHEMA[role]), ",".join(header),
               "PASS" if header == SPEC_SCHEMA[role] else "FAIL")
    checks.add("A-CPI-002", role, name, "NORM-015;SEM-010;REAL-002;TEST-028",
               "Known empty month", "2025-10 empty, previous_available", ",".join(empty) or "none",
               "PASS" if empty == ["2025-10-01"] else "WARN")
    checks.add("A-CPI-003", role, name, "NORM-003", "Monthly continuity", "no missing months",
               str(len(month_gaps)), "PASS" if not month_gaps else "FAIL")
    series = {}
    for x, r in zip(days, rows):
        if r["CPIAUCNS"].strip():
            series[(x.year, x.month)] = float(r["CPIAUCNS"])
    return prof, series


def audit_nominal_vs_real(checks: Checks, stock_series, ff_series, div_series, cpi_series):
    """The word REAL in raw file names: are the series inflation-adjusted? Compare with FF."""
    sp = stock_series["prices"]
    keys = [k for k in ff_series["keys"] if k in sp and k in div_series["div"]]
    a, b = keys[0], keys[-1]
    growth_ff_price = 1.0
    prev = a
    for k in keys[1:]:
        if (k - prev).days == 7:
            growth_ff_price *= 1 + ff_series["ret"][k] - div_series["div"][k]
        prev = k
    growth_index = sp[b] / sp[a]
    cpi_ratio = cpi_series[(b.year, b.month)] / cpi_series[(a.year, a.month)]
    checks.add("A-X-004", "cross", FILES["stocks_signal"], "SIG-002;DATA-001",
               "Stock index is nominal (the word REAL in the file name is not deflation)",
               "index growth close to FF implied price growth, not divided by CPI",
               f"{a}..{b}: index x{fnum(growth_index, 5)}; FF total-minus-dividend x{fnum(growth_ff_price, 5)}; "
               f"CPI x{fnum(cpi_ratio, 4)}", "INFO")
    return {"from": a.isoformat(), "to": b.isoformat(), "index_growth": fnum(growth_index, 8),
            "ff_minus_dividend_growth": fnum(growth_ff_price, 8), "cpi_growth": fnum(cpi_ratio, 8)}


# --------------------------------------------------------------------------- scenarios
def parse_range(text):
    """Spec range/list syntax: a:b[:step] inclusive or comma list. Returns list of floats."""
    out = []
    for part in text.split(","):
        if ":" in part:
            bits = [float(x) for x in part.split(":")]
            start, stop = bits[0], bits[1]
            step = bits[2] if len(bits) > 2 else 1.0
            n = int(math.floor((stop - start) / step + 1e-9)) + 1
            out.extend(start + i * step for i in range(n))
        else:
            out.append(float(part))
    return out


def _missing_keys(keys, lo, hi):
    present = set(keys)
    k = lo
    out = []
    while k <= hi:
        if k not in present and keys[0] <= k <= keys[-1]:
            out.append(k)
        k += dt.timedelta(days=7)
    return out


def scenario_rows(sources, as_of):
    ff_keys = sources["ff"]["keys"]
    sig_keys = {"stocks": sources["stocks"]["keys"], "gold": sources["gold"]["keys"],
                "btc": sources["btc"]["keys"]}
    ret_first = {"ff": ff_keys[0], "gold": sources["gold"]["keys"][1], "btc": sources["btc"]["keys"][1]}
    ret_last = {"ff": ff_keys[-1], "gold": sources["gold"]["keys"][-1], "btc": sources["btc"]["keys"][-1]}
    div_keys = sources["div"]["keys"]

    items = []
    with AB_SCENARIOS.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            items.append((r["scenario_id"], r["v2_command_template"]))
    with SPEC.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["requirement_id"].startswith("CLI-"):
                items.append((r["requirement_id"], r["cli_option"]))

    out = []
    for sid, cmd in items:
        tok = shlex.split(cmd)
        prog = next(i for i, t in enumerate(tok) if t.endswith("backtest.py"))
        tok = tok[prog + 1:]
        command = tok[0] if tok else ""
        opts = {}
        i = 1
        while i < len(tok):
            if tok[i].startswith("--"):
                val = tok[i + 1] if i + 1 < len(tok) and not tok[i + 1].startswith("--") else "true"
                opts[tok[i][2:]] = val
                i += 2 if val != "true" else 1
            else:
                i += 1
        notes, issues = [], []
        weights = {}
        if "weights" in opts:
            for kv in opts["weights"].split(","):
                k, v = kv.split("=")
                weights[k] = float(v)
        risky = set()
        if "asset" in opts:
            risky.add(opts["asset"])
        risky |= {k for k, v in weights.items() if k in {"stocks", "gold", "btc"} and v > 0}
        if command == "optimize" or "optimization-mode" in opts:
            bw = parse_range(opts.get("btc-weight", "0:25:1"))
            gw = parse_range(opts.get("gold-weight", "0:25:1"))
            if max(bw) > 0:
                risky.add("btc")
            if max(gw) > 0:
                risky.add("gold")
            risky.add("stocks")
            notes.append(f"weight grid {len(bw)}x{len(gw)} (stocks=remainder)")
        if command in {"run", "tax-compare"} and not weights and "single-asset" not in opts:
            if "config" in opts:
                issues.append(f"weights must come from --config {opts['config']} (not in clean-room)")
            else:
                issues.append("no --weights/--single-asset: ALLOC-001 requires an error")
        tax = opts.get("tax-profile", "none")
        needs_div = ("stocks" in risky and any(p != "none" for p in tax.split(","))
                     and opts.get("dividend-tax-mode", "smoothed_weekly") == "smoothed_weekly")
        first_candidates = [ret_first["ff"]] + [ret_first[a] for a in ("gold", "btc") if a in risky]
        last_candidates = [ret_last["ff"]] + [ret_last[a] for a in ("gold", "btc") if a in risky]
        if needs_div:
            first_candidates.append(div_keys[0])
            last_candidates.append(div_keys[-1])
        common_first, common_last = max(first_candidates), min(min(last_candidates), as_of)
        start = d(opts["start"]) if "start" in opts else None
        end = d(opts["end"]) if "end" in opts else None
        first_week = common_first
        if start:
            fk = friday_key(start)
            if fk < start:
                fk += dt.timedelta(days=7)
            first_week = max(fk, common_first)
        requested_last = ff_keys[-1]
        last_week = common_last
        if end:
            lk = friday_key(end)
            if lk > end:
                lk -= dt.timedelta(days=7)
            requested_last = min(lk, ff_keys[-1])
            if lk > common_last:
                issues.append(f"--end {end} beyond common return data ({common_last}); truncation must be reported")
            last_week = min(lk, common_last)
        if "dividend-file" in opts and not (DATA / opts["dividend-file"]).is_file():
            issues.append(f"--dividend-file {opts['dividend-file']} not present in input/data")
        max_delay = max(parse_range(opts.get("delay", opts.get("delay-grid", str(DEFAULT_DELAY)))))
        need_by_asset = {}
        for a in sorted(risky):
            conf = int(opts.get("confirm-weeks", DEFAULT_CONFIRM[a]))
            ma = int(max(parse_range(opts.get("ma", opts.get("ma-grid", str(DEFAULT_MA))))))
            need_by_asset[a] = ma + conf + int(max_delay)
        if not start:
            # No --start: earliest week at which every active signal has its contiguous warm-up.
            candidates = [k for k in ff_keys if common_first <= k <= common_last]
            for k in candidates:
                if all(contiguous_tail(sig_keys[a], k) >= n for a, n in need_by_asset.items()):
                    if k != first_week:
                        notes.append(f"no --start: earliest warm-up-complete week {k}")
                    first_week = k
                    break
        in_range_gaps = sorted({g for g in _missing_keys(ff_keys, first_week, last_week)}
                               | {g for a in risky for g in _missing_keys(sig_keys[a], first_week, last_week)})
        if in_range_gaps:
            issues.append("missing week(s) inside backtest range " + ",".join(x.isoformat() for x in in_range_gaps)
                          + " (NORM-004/NORM-005 default=error)")
        warm = []
        for a in sorted(risky):
            need = need_by_asset[a]
            avail_all = sum(1 for k in sig_keys[a] if k < first_week)
            avail_contig = contiguous_tail(sig_keys[a], first_week)
            ok = avail_contig >= need
            warm.append(f"{a}: need {need}, contiguous {avail_contig}, all {avail_all}"
                        + ("" if ok else " -> INSUFFICIENT"))
            if not ok:
                issues.append(f"{a} warm-up {avail_contig}<{need} before {first_week}")
            elif avail_all != avail_contig:
                notes.append(f"{a} history before a gap excluded from contiguous count")
        div_missing = 0
        if needs_div:
            div_set = set(div_keys)
            div_missing = sum(1 for k in ff_keys if first_week <= k <= requested_last and k not in div_set)
            if div_missing:
                issues.append(f"dividend file missing {div_missing} weeks in requested range")
        wf = ""
        if opts.get("optimization-mode") == "walk-forward":
            train = float(opts.get("train-years", 15))
            test = float(opts.get("test-years", 5))
            span_years = ((last_week - first_week).days + 7) / 365.2425
            n_oos_years = span_years - train
            wf = (f"global {first_week}..{last_week} = {span_years:.2f}y; train {train:g}y; "
                  f"OOS available {max(n_oos_years, 0):.2f}y")
            if n_oos_years <= 0:
                issues.append("walk-forward: no OOS window after first train window")
            elif n_oos_years < test:
                notes.append("only a partial OOS window (WF-015)")
            if "start" not in opts and "btc" in risky:
                notes.append("BTC in weight grid limits the global range to BTC history")
        status = "FEASIBLE" if not issues else "NEEDS_DECISION"
        out.append(OrderedDict([
            ("scenario_id", sid), ("command", command), ("risky_assets", ",".join(sorted(risky))),
            ("tax_profiles", tax), ("needs_dividend_file", str(needs_div)),
            ("requested_start", opts.get("start", "")), ("requested_end", opts.get("end", "")),
            ("common_return_first_week", common_first.isoformat()),
            ("common_return_last_week", common_last.isoformat()),
            ("first_return_week", first_week.isoformat()), ("last_return_week", last_week.isoformat()),
            ("warmup", " | ".join(warm)), ("walk_forward", wf),
            ("dividend_weeks_missing", str(div_missing)), ("status", status),
            ("issues", " | ".join(issues)), ("notes", " | ".join(notes)),
        ]))
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--as-of", default=DEFAULT_AS_OF, help="as_of_date for completeness checks")
    args = ap.parse_args()
    as_of = d(args.as_of)

    checks = Checks()
    profile = OrderedDict(audit_tool="work/tools/audit_inputs.py", as_of_date=as_of.isoformat(),
                          audit_assumptions=[
                              "Friday key = Monday-Sunday calendar week Friday (NORM-007).",
                              "week_start files (stocks, gold, BTC date) mapped with +4 days (NORM-008 analogue).",
                              "First return week = first Friday key >= --start (see Q-014).",
                              "Warm-up = ma + confirm + max(delay) contiguous weeks before first return week (NORM-010).",
                              "Dividend file needed only if stocks active, tax profile != none, smoothed_weekly (Q-009).",
                          ])
    audit_manifest(checks, profile)
    cpi_prof, cpi_series = audit_cpi(checks)
    stk_prof, stk = audit_stocks_signal(checks)
    ff_prof, ff = audit_ff(checks, stk)
    gld_prof, gld = audit_gold(checks, ff)
    btc_prof, btc = audit_btc(checks, ff, as_of)
    div_prof, div = audit_dividend(checks, stk, ff)
    nominal = audit_nominal_vs_real(checks, stk, ff, div, cpi_series)
    _, sch_rows, sch_raw = read_csv(DATA / FILES["supplemental_schwert"])
    profile["sources"] = OrderedDict([
        ("stocks_signal", stk_prof), ("stocks_return", ff_prof), ("gold", gld_prof),
        ("btc", btc_prof), ("dividend", div_prof), ("cpi", cpi_prof),
        ("supplemental_schwert", OrderedDict(
            file=FILES["supplemental_schwert"], sha256=sha256(DATA / FILES["supplemental_schwert"]),
            rows=len(sch_rows), first_week_start=sch_rows[0]["week_start"],
            last_week_start=sch_rows[-1]["week_start"], default_config_key="none (DATA_MAP)")),
    ])
    profile["nominal_vs_real"] = nominal

    completeness = OrderedDict()
    for role, keys in (("stocks_signal", stk["keys"]), ("stocks_return", ff["keys"]),
                       ("gold", gld["keys"]), ("btc", btc["keys"]), ("dividend", div["keys"])):
        completeness[role] = OrderedDict(
            last_friday_key=keys[-1].isoformat(),
            rows_after_as_of=sum(1 for k in keys if k > as_of),
            rows_after_norm019_example=sum(1 for k in keys if k > d(NORM019_EXAMPLE_AS_OF)))
    profile["completeness_norm019"] = completeness
    ends = {r: v["last_friday_key"] for r, v in completeness.items()}
    checks.add("A-X-005", "cross", "all", "NORM-011;RUN-002;TEST-022",
               "Last complete Friday key per source (common end is the minimum of used sources)",
               "explicit truncation report", json.dumps(ends), "INFO", "Q-009")

    scen = scenario_rows({"ff": ff, "stocks": stk, "gold": gld, "btc": btc, "div": div}, as_of)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "input_profile.json").write_text(
        json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (OUT_DIR / "input_checks.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(checks.rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(checks.rows)
    with (OUT_DIR / "scenario_feasibility.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(scen[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(scen)

    res = Counter(r["result"] for r in checks.rows)
    feas = Counter(r["status"] for r in scen)
    print(f"Input audit: {len(checks.rows)} checks {dict(sorted(res.items()))}; "
          f"scenarios {dict(sorted(feas.items()))}; outputs in {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
