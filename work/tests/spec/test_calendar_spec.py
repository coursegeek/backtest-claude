"""TEST-022 (range function), TEST-023, TEST-025, TEST-026."""
import csv
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from fixtures.builders import STAGED, ff_text
from fixtures.market import random_pct
from src.app import run_signals
from src.calendar import common_range, friday_key
from src.config import ResolvedConfig
from src.data_loader import load_ff

D = dt.date.fromisoformat


def test_ff_saturday_mapping(tmp_path):
    """TEST-025 / NORM-013, NORM-018: a Saturday maps to the preceding Friday of the same
    Monday-Sunday week, not to the Friday six days later."""
    assert friday_key(D("1926-07-10")) == D("1926-07-09")
    p = tmp_path / "ff.csv"
    p.write_text(ff_text([("19260702", 1.0, 0, 0, 0.06), ("19260710", 0.37, 0, 0, 0.06)]),
                 encoding="utf-8")
    assert [x.week_key for x in load_ff(p).stock_total.points] == [D("1926-07-02"), D("1926-07-09")]
    # regression on the staged file: naive 'Friday on/after' mapping would shift 1158 weeks
    staged = load_ff(STAGED / "F-F_Research_Data_Factors_weekly.csv")
    naive = sum(1 for x in staged.stock_total.points
                if x.source_date + dt.timedelta(days=(4 - x.source_date.weekday()) % 7) != x.week_key)
    assert naive == 1158


def test_ff_thursday_mapping():
    """TEST-026 / NORM-013: holiday Thursday (and other short weeks) map to the Friday of the
    same calendar week."""
    assert friday_key(D("2023-04-06")) == D("2023-04-07")       # Good Friday week
    assert friday_key(D("1929-11-27")) == D("1929-11-29")       # Wednesday
    assert friday_key(D("2001-09-10")) == D("2001-09-14")       # Monday before 9/11 closure


def test_common_range_truncation_reported():
    """TEST-022 (range part) / NORM-011: effective range is the intersection and every
    truncation is listed. Summary reporting follows with the portfolio engine."""
    start, end, trunc = common_range({"a": (D("2000-01-07"), D("2020-12-25")),
                                      "b": (D("2005-01-07"), D("2018-12-28"))})
    assert (start, end) == (D("2005-01-07"), D("2018-12-28"))
    assert trunc == [("a", "start", D("2000-01-07"), D("2005-01-07")),
                     ("a", "end", D("2020-12-25"), D("2018-12-28"))]


def test_common_range_truncation_in_summary(tmp_path):
    """TEST-022 (summary part) / NORM-011: a portfolio run reports the common data range and
    every truncation in summary.csv (and as validation warnings)."""
    import csv
    from src.app import run_portfolio
    cfg = ResolvedConfig("run", cli_layer={
        "allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"},
        "run": {"start": "2018-01-01", "end": "2018-12-31", "as_of_date": "2026-09-29"},
        "report": {"output_dir": str(tmp_path), "run_name": "trunc"}})
    res = run_portfolio(cfg)
    row = next(csv.DictReader((res.output_dir / "summary.csv").open(encoding="utf-8")))
    assert row["common_data_start"] == "2011-07-15"               # limited by BTC
    detail = row["range_truncation_detail"].split("|")
    assert int(row["range_truncations"]) == len(detail) == len(res.truncations) > 0
    assert "gold:start:1970-01-16->2011-07-15" in detail
    warned = [i for i in res.report.issues if i.code == "range_truncated"]
    assert len(warned) == len(detail)


def test_signal_only_1920_full_backtest_needs_returns(tmp_path):
    """TEST-023 / NORM-012 / Q-011 / Q-038: (A) the 'signals' command analyses US stocks on
    the stock signal history from Friday 1920-01-02 (the earliest confirmed history, Q-011),
    without any return series; (B) a full portfolio run with an explicit start before July 1926
    and the default Fama-French file is refused - never silently moved to 1926-07-02; (C) the
    same run with an alternative stocks-return file covering the earlier period runs end to
    end from the requested week."""
    bt = [sys.executable, str(Path(__file__).resolve().parents[2] / "backtest.py")]
    # (A) signal-only: whole history from 1920 (default warm-up -> first record 1921) ...
    cfg = ResolvedConfig("signals", cli_layer={"run": {"asset": "stocks", "as_of_date": "2026-09-29",
                                                       "end": "1926-06-30"}})
    res = run_signals(cfg, write=False)
    assert res.series["stocks"].points[0].week_key == D("1920-01-02")
    assert res.records[0].week_key.year == 1921          # 1920 history + warm-up
    assert res.records[-1].week_key < D("1926-07-02")
    ff = load_ff(STAGED / "F-F_Research_Data_Factors_weekly.csv")
    assert ff.stock_total.points[0].week_key == D("1926-07-02")
    # ... and through the CLI with an explicit 1920 start (MA 10: 13 weeks of 1920 warm-up)
    r = subprocess.run(bt + ["signals", "--asset", "stocks", "--start", "1920-04-02", "--end",
                             "1926-06-30", "--ma", "10", "--as-of-date", "2026-09-29",
                             "--output-dir", str(tmp_path / "a")], capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, r.stderr
    out = next((tmp_path / "a").iterdir())
    rows = list(csv.DictReader((out / "signals.csv").open(encoding="utf-8")))
    assert rows[0]["week_key"] == "1920-04-02" and rows[-1]["week_key"] < "1926-07-02"
    assert {"signals.csv", "validation_report.csv", "data_manifest.json", "config_resolved.yaml",
            "weekly_normalized.csv"} <= {f.name for f in out.iterdir()}
    assert "weekly_portfolio.csv" not in {f.name for f in out.iterdir()}       # no portfolio
    # (B) full run from 1920 with the default Fama-French returns: a clear error
    run = bt + ["run", "--weights", "stocks=1.0", "--start", "1920-04-02", "--ma", "10",
                "--as-of-date", "2026-09-29", "--output-dir", str(tmp_path / "b")]
    r = subprocess.run(run, capture_output=True, text=True, timeout=300)
    assert r.returncode == 1
    for text in ("requested start 1920-04-02", "precedes the available stocks return history",
                 "first available return week 1926-07-02", "alternative stocks_return_file",
                 "TEST-023"):
        assert text in r.stderr, text
    assert not (tmp_path / "b").exists()                               # nothing written
    r = subprocess.run(bt + ["run", "--weights", "stocks=1.0", "--start", "1926-06-01",
                             "--end", "1927-12-31", "--as-of-date", "2026-09-29"],
                       capture_output=True, text=True, timeout=300, cwd=tmp_path)
    assert r.returncode == 1 and "first available return week 1926-07-02" in r.stderr
    # (C) an alternative stocks-return file from 1920 (Fama-French layout, synthetic values)
    keys = [D("1920-01-02") + dt.timedelta(days=7 * i) for i in range(574)]   # .. 1930-12-26
    pct = random_pct(len(keys), seed=23)
    alt = tmp_path / "stocks_return_1920.csv"
    alt.write_text(ff_text([(k.strftime("%Y%m%d"), round(x - 0.05, 2), 0.0, 0.0, 0.05)
                            for k, x in zip(keys, pct)]), encoding="utf-8")
    r = subprocess.run(bt + ["run", "--weights", "stocks=1.0", "--start", "1922-01-06", "--end",
                             "1930-12-31", "--stocks-return-file", str(alt), "--as-of-date",
                             "2026-09-29", "--output-dir", str(tmp_path / "c")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = next((tmp_path / "c").iterdir())
    summary = next(csv.DictReader((out / "summary.csv").open(encoding="utf-8")))
    assert (summary["effective_first_week"], summary["effective_last_week"]) == ("1922-01-06",
                                                                                  "1930-12-26")
    weekly = list(csv.DictReader((out / "weekly_portfolio.csv").open(encoding="utf-8")))
    assert weekly[0]["week_key"] == "1922-01-06" and len(weekly) == 469         # every Friday
    m = json.loads((out / "data_manifest.json").read_text(encoding="utf-8"))
    src = next(x for x in m["sources"] if x["role"] == "stocks_return")
    assert src["path"] == str(alt) and src["config_key"] == "data.stocks_return_file"