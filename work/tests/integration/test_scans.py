"""delay-scan, threshold-scan and rebalance-scan end to end on the staged data (mechanics
validation - stocks, gold and dividends are staged proxies, Q-002/Q-004/Q-008): one shared
prepared input per scan, one full production run per grid point, grid_results.csv with one
row per resolved grid point (DELAY-001..005, THR-001..005, REB-010, REP-010, CLI-002/003/010,
NORM-019/020, ERR-003, ERR-005)."""
import csv
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src import app, scans
from src.app import prepared_input_sha256
from src.cli import resolve
from src.config import ResolvedConfig
from src.errors import ConfigError, InsolvencyError, WarmupError
from src.models import Condition, State, TradeReason
from src.reporting import SUMMARY_FIELDS, fmt, summary_row

WORK = Path(__file__).resolve().parents[2]
ROOT = WORK.parent
BT = str(WORK / "backtest.py")
STAGED = ROOT / "input" / "data"

S02 = ["delay-scan", "--asset", "stocks", "--start", "1971-01-01", "--end", "2026-07-31",
       "--ma", "50", "--threshold", "3", "--confirm-weeks", "2", "--delay", "1:4",
       "--sell-fraction", "0.5"]
S03 = ["threshold-scan", "--asset", "stocks", "--start", "1971-01-01", "--end", "2026-07-31",
       "--ma", "50", "--threshold", "1:5:1", "--confirm-weeks", "2", "--delay", "1"]
S08 = ["rebalance-scan", "--weights", "stocks=0.6,gold=0.2,btc=0.2", "--band-pp", "1,5",
       "--start", "2018-01-01", "--end", "2026-07-31"]
AS_OF = ["--as-of-date", "2026-09-29"]


def read_csv(path):
    with Path(path).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def scan(argv, out, write=True):
    return scans.run_scan(resolve(list(argv) + AS_OF + ["--output-dir", str(out)]), write=write)


@pytest.fixture(scope="module")
def s02(tmp_path_factory):
    res = scan(S02, tmp_path_factory.mktemp("s02"))
    return res, read_csv(res.output_dir / "grid_results.csv")


@pytest.fixture(scope="module")
def s03(tmp_path_factory):
    res = scan(S03, tmp_path_factory.mktemp("s03"))
    return res, read_csv(res.output_dir / "grid_results.csv")


@pytest.fixture(scope="module")
def s08(tmp_path_factory):
    res = scan(S08, tmp_path_factory.mktemp("s08"))
    return res, read_csv(res.output_dir / "grid_results.csv")


# ------------------------------------------------------------------ REP-010 / output schema
@pytest.mark.parametrize("name,grid,column", [
    ("s02", ["1", "2", "3", "4"], "delay_weeks"),
    ("s03", ["1.0", "2.0", "3.0", "4.0", "5.0"], "threshold_pct"),
    ("s08", ["1.0", "5.0"], "band_pp")])
def test_rep_010_full_grid_one_row_per_point(request, name, grid, column):
    res, rows = request.getfixturevalue(name)
    header = (res.output_dir / "grid_results.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == scans.GRID_FIELDS
    assert scans.GRID_FIELDS[len(scans.SCAN_FIELDS):] == list(SUMMARY_FIELDS)
    assert [r[column] for r in rows] == grid                   # resolved grid order
    assert [r["grid_index"] for r in rows] == [str(i) for i in range(1, len(grid) + 1)]
    assert {r["status"] for r in rows} == {"ok"} and {r["scan_type"] for r in rows} == {res.scan_type}
    files = {f.name for f in res.output_dir.iterdir()}
    assert files == {"grid_results.csv", "scan_manifest.json", "config_resolved.yaml",
                     "data_manifest.json", "validation_report.csv", "weekly_normalized.csv"}
    manifest = json.loads((res.output_dir / "scan_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["resolved_grid"]) == len(rows) and all(manifest["checks"].values())
    assert manifest["prepared_input_sha256"] == res.fingerprint
    assert "all grid points used one shared prepared data/calendar input" in \
        manifest["shared_input_statement"]
    header_low = header.lower()
    assert "rank" not in header_low and "winner" not in header_low and "objective" not in header_low


def test_grid_order_is_never_sorted(tmp_path):
    res = scan(["delay-scan", "--asset", "stocks", "--start", "2000-01-01", "--end", "2005-12-31",
                "--delay", "4,1,2"], tmp_path, write=False)
    assert [r["delay_weeks"] for r in res.rows] == [4, 1, 2]
    assert [r["signal_stocks_delay"] for r in res.rows] == [4, 1, 2]


# ------------------------------------------------------------------ one shared input
@pytest.mark.parametrize("argv,roles", [
    (S02, ["cpi", "stocks_price", "stocks_return"]),
    (S03, ["cpi", "stocks_price", "stocks_return"]),
    (S08, ["btc", "cpi", "gold", "stocks_price", "stocks_return"])])
def test_sources_prepared_once_per_scan(tmp_path, monkeypatch, argv, roles):
    """Every source is loaded once per scan (FF, signal prices, gold/BTC only when used, CPI),
    build_run runs once, whatever the grid size."""
    calls = {"build": 0, "roles": []}
    real_build, real_load = app.build_run, app.load_role

    def build(*a, **k):
        calls["build"] += 1
        return real_build(*a, **k)

    def load(cfg, role):
        calls["roles"].append(role)
        return real_load(cfg, role)

    monkeypatch.setattr(app, "build_run", build)
    monkeypatch.setattr(app, "load_role", load)
    res = scan(argv, tmp_path, write=False)
    assert calls["build"] == 1
    assert sorted(calls["roles"]) == roles
    assert len(res.results) == len(res.spec.points) > 1


@pytest.mark.parametrize("name", ["s02", "s03", "s08"])
def test_shared_calendar_and_prepared_hash(request, name):
    """All grid points: the same PreparedRun, market data, signal series, weeks, dropped weeks
    and prepared_input_sha256; the scanned parameter is only in the row."""
    res, rows = request.getfixturevalue(name)
    p = res.prepared
    for r in res.results:
        assert r.prepared is p and r.inputs.market is p.inputs.market
        assert r.inputs.signal_series is p.inputs.signal_series and r.inputs.weeks is p.inputs.weeks
        assert tuple(w.week_key for w in r.engine.weeks) == p.inputs.weeks
        assert prepared_input_sha256(r.prepared) == res.fingerprint
    for k in ("effective_first_week", "effective_last_week", "weeks", "inception_date",
              "dropped_incomplete_weeks", "common_data_start", "common_data_end", "as_of_date"):
        assert len({r[k] for r in rows}) == 1, k


def test_prepared_hash_independent_of_scanned_parameter(s02, s03, tmp_path):
    """delay and threshold do not enter the data hash: the delay-scan and the threshold-scan of
    the same asset/range share it; a prepare with another delay/threshold gives it again."""
    assert s02[0].fingerprint == s03[0].fingerprint
    other = resolve(["delay-scan", "--asset", "stocks", "--start", "1971-01-01", "--end",
                     "2026-07-31", "--threshold", "7", "--delay", "3"] + AS_OF)
    base, _ = scans.scan_base(other)
    assert prepared_input_sha256(app.prepare_run(base)) == s02[0].fingerprint


# ------------------------------------------------------------------ DELAY
def _standalone(delay, tmp_path):
    cfg = resolve(["run", "--single-asset", "stocks", "--start", "1971-01-01", "--end",
                   "2026-07-31", "--ma", "50", "--threshold", "3", "--confirm-weeks", "2",
                   "--sell-fraction", "0.5", "--delay", str(delay)] + AS_OF
                  + ["--output-dir", str(tmp_path)])
    return cfg, app.run_portfolio(cfg, write=False)


@pytest.mark.parametrize("delay", [1, 4])
def test_delay_grid_row_equals_standalone_run(s02, tmp_path, delay):
    """Scan-level regression: the grid row with delay=N is the standalone 'run' with delay=N on
    the same inputs (same path, trades and summary; only run_name differs)."""
    res, rows = s02
    cfg, alone = _standalone(delay, tmp_path)
    point = res.results[delay - 1]
    assert [w.nav_end for w in point.engine.weeks] == [w.nav_end for w in alone.engine.weeks]
    assert point.engine.trades == alone.engine.trades
    row = {k: fmt(v) for k, v in summary_row(cfg, alone).items()}
    diff = {k for k in SUMMARY_FIELDS if row.get(k, "") != rows[delay - 1][k]}
    assert diff == {"run_name"}, diff


def test_delay_002_execution_at_confirm_plus_delay(s02):
    """DELAY-002/004 end to end: every signal execution of the grid point with delay=N is
    nominally due exactly N weeks after its confirmation week; no hidden +1."""
    res, rows = s02
    for r, point in zip(res.results, res.spec.points):
        n = point.value
        sig = [t for t in r.engine.trades
               if t.reason in (TradeReason.SIGNAL_EXIT, TradeReason.SIGNAL_REENTRY)]
        assert sig
        for t in sig:
            assert t.nominal_execution_week == t.confirm_week + dt.timedelta(weeks=n)
            assert t.week_key == t.nominal_execution_week          # no dropped weeks here
        for rec in r.engine.signal_records:
            if rec.scheduled_execution_week is not None:
                assert rec.scheduled_execution_week == rec.week_key + dt.timedelta(weeks=n)
    assert [r["signal_stocks_delay"] for r in rows] == ["1", "2", "3", "4"]
    # only the delay differs between the rows' signal parameters
    sig_cols = [c for c in SUMMARY_FIELDS if c.startswith("signal_stocks_") and c != "signal_stocks_delay"]
    for c in sig_cols:
        assert len({r[c] for r in rows}) == 1, c


def test_delay_003_irregular_list(tmp_path):
    res = scan(["delay-scan", "--asset", "stocks", "--start", "1990-01-01", "--end", "2000-12-31",
                "--delay", "1,2,4,8"], tmp_path, write=False)
    assert [r["delay_weeks"] for r in res.rows] == [1, 2, 4, 8]
    assert res.prepared.warmup_weeks["stocks"] == 50 + 2 + 8


def test_delay_005_pre_and_after_tax_metrics(tmp_path):
    """DELAY-005: with an active tax profile every row has the full pre-tax and after-tax
    metric set (same SUMMARY_FIELDS as run: shadow pre-tax, terminal settlement)."""
    res = scan(["delay-scan", "--asset", "stocks", "--start", "2010-01-01", "--end", "2020-12-31",
                "--delay", "1,3", "--tax-profile", "individual_pl"], tmp_path, write=False)
    for r in res.rows:
        assert r["pre_tax_method"] == "shadow_zero_tax" and r["tax_profile"] == "individual_pl"
        assert r["after_tax_cagr"] < r["cagr"]
        assert r["after_tax_terminal_wealth"] < r["final_wealth_pre_tax"]
        assert r["terminal_capital_gains_tax"] > 0 and r["dividend_tax_paid"] > 0
        for k in ("after_tax_volatility", "after_tax_sharpe", "after_tax_sortino",
                  "after_tax_max_drawdown", "after_tax_calmar", "after_tax_real_cagr"):
            assert r[k] is not None
    ff = scan(["delay-scan", "--asset", "stocks", "--start", "2010-01-01", "--end", "2020-12-31",
               "--delay", "1,3", "--tax-profile", "family_foundation_19"], tmp_path, write=False)
    for r in ff.rows:
        assert r["foundation_setup_cost_paid"] == 40000.0 and r["terminal_foundation_tax"] > 0
        assert r["applied_distribution_rate"] == 0.19


# ------------------------------------------------------------------ THR
def test_thr_002_symmetric_thresholds_and_thr_003_delay(s03):
    res, rows = s03
    for r, p in zip(res.results, res.spec.points):
        sp = r.inputs.params["stocks"]
        assert sp.threshold_off == sp.threshold_on == p.value
        assert sp.delay == 1                                     # THR-003
        for rec in r.engine.signal_records[:200]:
            if rec.sma is not None:
                assert rec.lower_band == pytest.approx(rec.sma * (1 - p.value), rel=1e-12)
                assert rec.upper_band == pytest.approx(rec.sma * (1 + p.value), rel=1e-12)
    assert [(r["threshold_pct"], r["threshold_decimal"]) for r in rows] == [
        ("1.0", "0.01"), ("2.0", "0.02"), ("3.0", "0.03"), ("4.0", "0.04"), ("5.0", "0.05")]
    assert [r["signal_stocks_threshold_off"] for r in rows] == [r["threshold_decimal"] for r in rows]


def test_thr_004_thr_005_zero_and_irregular_grid(tmp_path):
    """0,1,2,3,5,7.5 end to end; threshold 0 keeps the strict comparison (price < SMA below,
    price > SMA above, equality keeps the state)."""
    res = scan(["threshold-scan", "--asset", "stocks", "--start", "1990-01-01", "--end",
                "2005-12-31", "--threshold", "0,1,2,3,5,7.5"], tmp_path, write=False)
    assert [r["threshold_decimal"] for r in res.rows] == [0.0, 0.01, 0.02, 0.03, 0.05, 0.075]
    assert [r["threshold_pct"] for r in res.rows] == [0.0, 1.0, 2.0, 3.0, 5.0, 7.5]
    zero = res.results[0]
    checked = 0
    for rec in zero.engine.signal_records:
        if rec.sma is None:
            continue
        assert rec.lower_band == rec.upper_band == rec.sma
        expected = (Condition.BELOW if rec.price < rec.sma else
                    Condition.ABOVE if rec.price > rec.sma else Condition.INSIDE)
        assert rec.condition == expected
        checked += 1
    assert checked > 500


# ------------------------------------------------------------------ REB-010
def test_reb_010_band_rows(s08):
    res, rows = s08
    for r, row, p in zip(res.results, rows, res.spec.points):
        assert row["rebalance_mode"] == "band" and float(row["rebalance_band_pp"]) == p.value
        assert int(row["rebalance_count"]) == len(r.engine.rebalance_events) > 0
        assert {e.mode for e in r.engine.rebalance_events} == {"band"}
        assert {e.reason for e in r.engine.rebalance_events} == {TradeReason.BAND_REBALANCE}
        assert all(e.max_deviation >= p.value / 100 for e in r.engine.rebalance_events)
        assert r.inputs is res.prepared.inputs                  # band is not an input change
    assert rows[0]["rebalance_count"] != rows[1]["rebalance_count"]


def test_reb_010_band_row_equals_standalone_run(s08, tmp_path):
    res, rows = s08
    cfg = resolve(["run", "--weights", "stocks=0.6,gold=0.2,btc=0.2", "--rebalance", "band",
                   "--rebalance-band-pp", "5", "--start", "2018-01-01", "--end", "2026-07-31"]
                  + AS_OF)
    alone = app.run_portfolio(cfg, write=False)
    row = {k: fmt(v) for k, v in summary_row(cfg, alone).items()}
    assert {k for k in SUMMARY_FIELDS if row[k] != rows[1][k]} == {"run_name"}
    assert len(alone.engine.rebalance_events) == int(rows[1]["rebalance_count"])


# ------------------------------------------------------------------ fresh state per point
def test_no_state_carries_between_grid_points(s02, tmp_path):
    """Each point starts from a fresh portfolio, cost basis, trackers and tax state: running
    the grid in another order gives the same rows."""
    res, rows = s02
    rev = scan([a if a != "1:4" else "4,3,2,1" for a in S02], tmp_path, write=False)
    by_delay = {r["delay_weeks"]: r for r in rev.rows}
    for row in rows:
        mine = {k: fmt(v) for k, v in by_delay[int(row["delay_weeks"])].items()
                if k not in ("grid_index",)}
        assert all(mine[k] == row[k] for k in mine), row["delay_weeks"]


# ------------------------------------------------------------------ warm-up (NORM-010/ERR-003)
def _btc_keys(tmp_path):
    base, _ = scans.scan_base(resolve(["delay-scan", "--asset", "btc"] + AS_OF))
    return app.load_role(base, "btc").keys()


def test_warmup_of_the_largest_delay_with_start(tmp_path):
    """With --start, the history before it must hold ma + confirm + max(delay grid); otherwise
    the whole scan stops with ERR-003 before any grid point (no silent later start)."""
    keys = _btc_keys(tmp_path)
    start = keys[53]                                   # 53 observations before the start
    base = ["delay-scan", "--asset", "btc", "--ma", "50", "--confirm-weeks", "1",
            "--start", start.isoformat(), "--end", (start + dt.timedelta(weeks=60)).isoformat()]
    ok = scan(base + ["--delay", "1:2"], tmp_path / "ok", write=False)     # needs 53
    assert ok.prepared.first_week == start and ok.prepared.warmup_weeks == {"btc": 53}
    with pytest.raises(WarmupError) as e:
        scan(base + ["--delay", "1:4"], tmp_path / "bad")                  # needs 55
    assert e.value.required == 55 and e.value.available == 53 and e.value.asset == "btc"
    assert not (tmp_path / "bad").exists()


def test_warmup_without_start_uses_first_common_complete_week(tmp_path):
    keys = _btc_keys(tmp_path)
    small = scan(["delay-scan", "--asset", "btc", "--ma", "50", "--confirm-weeks", "1",
                  "--delay", "1:2", "--end", "2014-12-31"], tmp_path, write=False)
    large = scan(["delay-scan", "--asset", "btc", "--ma", "50", "--confirm-weeks", "1",
                  "--delay", "1:4", "--end", "2014-12-31"], tmp_path, write=False)
    for res, need in ((small, 53), (large, 55)):
        first = res.prepared.first_week
        assert sum(1 for k in keys if k < first) == need               # earliest possible week
        assert res.prepared.first_week_rule.startswith("first common week")
        assert {r["effective_first_week"] for r in res.rows} == {first}
    assert large.prepared.first_week == small.prepared.first_week + dt.timedelta(weeks=2)


# ------------------------------------------------------------------ NORM-019/020
@pytest.mark.parametrize("command", ["delay-scan", "threshold-scan", "run"])
@pytest.mark.parametrize("as_of,dropped", [("2026-09-22", 1), ("2026-09-25", 0)])
def test_norm_020_incomplete_week_dropped_in_scans(tmp_path, command, as_of, dropped):
    """NORM-019/020: the partial week (Friday 2026-09-25 with as_of 2026-09-22) is dropped
    before any SMA/confirmation in run, delay-scan and threshold-scan alike."""
    src = STAGED / "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv"
    path = tmp_path / "stocks.csv"
    shutil.copy(src, path)
    with path.open("a", encoding="utf-8") as f:
        f.write("2026-09-21,178000.0000000000\n")
    argv = [command, "--stocks-price-file", str(path), "--as-of-date", as_of,
            "--start", "2026-01-02", "--output-dir", str(tmp_path / "o")]
    argv += ["--single-asset", "stocks"] if command == "run" else ["--asset", "stocks"]
    if command == "delay-scan":
        argv += ["--delay", "1,2"]
    cfg = resolve(argv)
    prepared = app.prepare_run(scans.scan_base(cfg)[0] if command != "run" else cfg)
    assert prepared.dropped_incomplete_weeks == dropped
    last = prepared.inputs.signal_series["stocks"].points[-1].week_key
    assert (last == dt.date(2026, 9, 25)) is (dropped == 0)
    if dropped:
        assert any(i.code == "incomplete_week_dropped" and i.week_key == dt.date(2026, 9, 25)
                   for i in prepared.issues)
    if command != "run":
        res = scans.run_scan(cfg)
        m = json.loads((res.output_dir / "scan_manifest.json").read_text(encoding="utf-8"))
        assert m["dropped_incomplete_weeks"] == dropped
        assert all(r.engine.signal_records[-1].week_key <= dt.date(2026, 9, 25) for r in res.results)


# ------------------------------------------------------------------ reproducibility
@pytest.mark.parametrize("argv", [S02, S08], ids=["delay", "rebalance"])
def test_grid_results_byte_identical(tmp_path, argv):
    a = scan(argv, tmp_path / "a")
    b = scan(argv, tmp_path / "b")
    assert (a.output_dir / "grid_results.csv").read_bytes() == \
        (b.output_dir / "grid_results.csv").read_bytes()
    ma, mb = (json.loads((x.output_dir / "scan_manifest.json").read_text(encoding="utf-8"))
              for x in (a, b))
    strip = lambda m: {k: v for k, v in m.items() if k not in ("run_timestamp", "code_version")}  # noqa: E731
    assert strip(ma) == strip(mb)
    assert (a.output_dir / "weekly_normalized.csv").read_bytes() == \
        (b.output_dir / "weekly_normalized.csv").read_bytes()
    assert "timestamp" not in (a.output_dir / "grid_results.csv").read_text().splitlines()[0]


# ------------------------------------------------------------------ atomicity
def _nothing_written(out):
    assert not Path(out).exists() or not any(Path(out).iterdir())


def test_atomic_failure_of_one_grid_point(tmp_path, monkeypatch):
    real = scans.run_prepared

    def run(cfg, prepared, **kw):
        if cfg.signal_params("stocks").delay == 3:
            raise InsolvencyError(prepared.inputs.weeks[5], 1.0, 0.5)
        return real(cfg, prepared, **kw)

    monkeypatch.setattr(scans, "run_prepared", run)
    with pytest.raises(scans.ScanError) as e:
        scan(S02, tmp_path / "o")
    assert e.value.point.grid_index == 3 and "insolvency" in str(e.value)
    _nothing_written(tmp_path / "o")


def test_atomic_failure_while_writing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(scans, "write_json", boom)
    with pytest.raises(OSError):
        scan(S08, tmp_path / "o")
    _nothing_written(tmp_path / "o")


def test_invalid_grid_writes_nothing(tmp_path):
    with pytest.raises(ConfigError):
        scan(["threshold-scan", "--asset", "stocks", "--threshold", "1,1"], tmp_path / "o")
    _nothing_written(tmp_path / "o")


# ------------------------------------------------------------------ CLI-002 / CLI-003 / CLI-010
def cli(*args):
    return subprocess.run([sys.executable, BT, *args], capture_output=True, text=True,
                          timeout=300, cwd=str(ROOT))


@pytest.mark.parametrize("argv,column,grid", [
    (S02, "delay_weeks", ["1", "2", "3", "4"]),
    (S03, "threshold_decimal", ["0.01", "0.02", "0.03", "0.04", "0.05"]),
    (S08, "band_pp", ["1.0", "5.0"])], ids=["CLI-002", "CLI-003", "CLI-010"])
def test_cli_scan_commands(tmp_path, argv, column, grid):
    """The exact spec commands (only --output-dir added) exit 0 and write the full grid."""
    r = cli(*argv, "--output-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = next(tmp_path.iterdir())
    rows = read_csv(out / "grid_results.csv")
    assert [x[column] for x in rows] == grid and {x["status"] for x in rows} == {"ok"}
    assert f"grid point {len(grid)}/{len(grid)}" in r.stderr         # ERR-005 progress, stderr
    assert "grid point" not in r.stdout
    manifest = json.loads((out / "scan_manifest.json").read_text(encoding="utf-8"))
    assert [fmt(v) for v in manifest["resolved_grid"]] == grid


def test_cli_scan_errors(tmp_path):
    r = cli("delay-scan", "--delay", "1:4", "--output-dir", str(tmp_path))
    assert r.returncode == 2 and "ALLOC-002" in r.stderr
    r = cli("rebalance-scan", "--band-pp", "1,5", "--output-dir", str(tmp_path))
    assert r.returncode == 2 and "ALLOC-001" in r.stderr
    r = cli("delay-scan", "--asset", "stocks", "--tax-profile", "family_foundation_15",
            "--foundation-tax-event", "distribution_schedule", "--output-dir", str(tmp_path))
    assert r.returncode == 2 and "Q-037" in r.stderr and "distribution_file" in r.stderr
    assert not any(tmp_path.iterdir())
