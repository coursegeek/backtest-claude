"""TEST-022 (range function), TEST-023, TEST-025, TEST-026."""
import datetime as dt

from fixtures.builders import STAGED, ff_text
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


def test_signal_only_1920_full_backtest_needs_returns():
    """TEST-023 / NORM-012: signal-only analysis of US stocks works from 1920 history even
    though Fama-French returns start in July 1926."""
    cfg = ResolvedConfig("signals", cli_layer={"run": {"asset": "stocks", "as_of_date": "2026-09-29",
                                                       "end": "1926-06-30"}})
    res = run_signals(cfg, write=False)
    assert res.records[0].week_key.year == 1921          # 1920 history + warm-up
    assert res.records[-1].week_key < D("1926-07-02")
    ff = load_ff(STAGED / "F-F_Research_Data_Factors_weekly.csv")
    assert ff.stock_total.points[0].week_key == D("1926-07-02")
