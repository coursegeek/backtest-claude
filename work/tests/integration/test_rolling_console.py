"""rolling_metrics.csv (MET-022/MET-023) in the command outputs and the console tables
(REP-011) on synthetic markets: which commands write the file, the pre-tax / after-tax paths
it uses (never terminal settlement), the stitched walk-forward OOS path, determinism, the
manifest, the CLI presentation and silent library calls."""
import csv
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src import app, console
from src import optimizer as opt
from src import scans
from src import tax_compare as tc
from src import walk_forward as wf
from src.cli import resolve
from src.errors import ConfigError
from src.reporting import ROLLING_FIELDS

from fixtures.market import market_returns, signal_overrides, wf_config, write_market

WORK = Path(__file__).resolve().parents[2]
BT = str(WORK / "backtest.py")
FIRST = "1999-01-01"
FAST = dict(ma_length=3, confirm_off_weeks=1, confirm_on_weeks=1, sell_fraction=0.5,
            threshold_off=0.0, threshold_on=0.0)
BASE = ["--weights", "stocks=0.5,gold=0.3,btc=0.2", "--as-of-date", "2026-09-29",
        "--dividend-tax-mode", "off", "--transaction-cost-bps", "10"]


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    """11.5 years of synthetic weekly data (1999-01-01 .. 2010-06-25)."""
    return write_market(tmp_path_factory.mktemp("m"), FIRST, market_returns(FIRST, 600, seed=11))


def run_cfg(files, out, *argv, cmd="run"):
    return resolve([cmd, *BASE, *files, "--output-dir", str(out), *argv]).with_overrides(
        signal_overrides(**FAST))


def by_end(rows, h):
    return {r["window_end"]: r for r in rows if r["horizon_years"] == str(h)}


# ============================================================================ run outputs
def test_run_writes_rolling_metrics_none_profile(files, tmp_path):
    """MET-022/023 on a normal run: rolling_metrics.csv with the stable schema, horizon then
    window_end order, full windows only (11.5 years -> 1/3/5/10Y), listed in the manifest;
    tax.profile=none has identical pre-tax and after-tax rolling values."""
    res = app.run_portfolio(run_cfg(files, tmp_path, "--tax-profile", "none"))
    out = res.output_dir
    rows = read_csv(out / "rolling_metrics.csv")
    assert list(rows[0]) == ROLLING_FIELDS
    assert [(int(r["horizon_years"]), r["window_end"]) for r in rows] == sorted(
        (int(r["horizon_years"]), r["window_end"]) for r in rows)
    m = json.loads((out / "data_manifest.json").read_text(encoding="utf-8"))
    assert "rolling_metrics.csv" in m["audit_outputs"] and "not_implemented_outputs" not in m
    rm = m["rolling_metrics"]
    assert rm["horizons_years"] == [1, 3, 5, 10] and rm["terminal_settlement_included"] is False
    assert rm["window_start_rule"] == "last_path_point_on_or_before_calendar_target"
    counts = {h: sum(r["horizon_years"] == str(h) for r in rows) for h in (1, 3, 5, 10)}
    assert counts == {int(k): v for k, v in rm["rows_per_horizon"].items()} and all(counts.values())
    for r in rows:
        for f in ("start_nav", "end_nav", "total_return", "cagr", "max_drawdown"):
            assert r[f"pre_tax_{f}"] == r[f"after_tax_{f}"]
    weekly = {r["week_key"]: float(r["nav_end"]) for r in read_csv(out / "weekly_portfolio.csv")}
    for r in rows[::37]:                                   # the actual weekly NAV path
        assert float(r["after_tax_end_nav"]) == weekly[r["window_end"]]
        assert float(r["after_tax_total_return"]) == float(r["after_tax_end_nav"]) / float(
            r["after_tax_start_nav"]) - 1.0


def test_individual_pre_and_after_tax_paths_differ_terminal_excluded(files, tmp_path):
    """individual_pl: the after-tax rolling path is the actual weekly path (annual taxes paid in
    the weekly path, RF tax, costs, sell_to_pay), the pre-tax path the zero-tax shadow; they
    differ, and the last after-tax window ends at pre_terminal_nav, not at the terminal wealth."""
    res = app.run_portfolio(run_cfg(files, tmp_path, "--tax-profile", "individual_pl"))
    rows = read_csv(res.output_dir / "rolling_metrics.csv")
    differ = [r for r in rows if r["pre_tax_total_return"] != r["after_tax_total_return"]]
    assert len(differ) > len(rows) // 2
    shadow = {w.week_key.isoformat(): w.nav_end for w in res.pre_tax.engine.weeks}
    for r in rows[::41]:
        assert float(r["pre_tax_end_nav"]) == shadow[r["window_end"]]
    last = rows[-1]
    s = read_csv(res.output_dir / "summary.csv")[0]
    assert float(last["after_tax_end_nav"]) == float(s["pre_terminal_nav"])
    assert float(s["after_tax_terminal_wealth"]) < float(s["pre_terminal_nav"])   # terminal CG tax
    worst = res.rolling.worst(1, "pre_tax"), res.rolling.worst(1, "after_tax")
    assert all(w is not None for w in worst)


def test_terminal_tax_does_not_change_rolling_metrics(files, tmp_path):
    """MET-025: family_foundation_15 and _19 have the identical weekly path and differ only in
    the terminal distribution tax: rolling_metrics.csv is byte-identical while
    after_tax_terminal_wealth and the full-run after_tax_cagr differ."""
    out = {}
    for p in ("family_foundation_15", "family_foundation_19"):
        res = app.run_portfolio(run_cfg(files, tmp_path / p, "--tax-profile", p))
        out[p] = (res.output_dir, read_csv(res.output_dir / "summary.csv")[0])
    (d15, s15), (d19, s19) = out.values()
    assert (d15 / "weekly_portfolio.csv").read_bytes() == (d19 / "weekly_portfolio.csv").read_bytes()
    assert (d15 / "rolling_metrics.csv").read_bytes() == (d19 / "rolling_metrics.csv").read_bytes()
    assert float(s19["terminal_foundation_tax"]) > float(s15["terminal_foundation_tax"]) > 0
    assert s15["after_tax_terminal_wealth"] != s19["after_tax_terminal_wealth"]
    assert s15["after_tax_cagr"] != s19["after_tax_cagr"]
    assert s15["after_tax_max_drawdown"] == s19["after_tax_max_drawdown"]


def test_rolling_configuration(files, tmp_path):
    """metrics.rolling_returns selects the horizons (allowed 1, 3, 5, 10), metrics.rolling_stats
    false leaves rolling CAGR / drawdown empty; summary.csv does not depend on either."""
    cfg = run_cfg(files, tmp_path / "a").with_overrides({"metrics.rolling_returns": [10, 1],
                                                          "metrics.rolling_stats": False})
    res = app.run_portfolio(cfg)
    rows = read_csv(res.output_dir / "rolling_metrics.csv")
    assert {r["horizon_years"] for r in rows} == {"1", "10"}
    assert all(r["pre_tax_cagr"] == "" and r["after_tax_max_drawdown"] == "" for r in rows)
    ref = app.run_portfolio(run_cfg(files, tmp_path / "b"))
    assert (res.output_dir / "summary.csv").read_bytes() == (ref.output_dir / "summary.csv").read_bytes()
    for bad in ([2], [], [True], "1,3"):
        with pytest.raises(ConfigError):
            run_cfg(files, tmp_path).with_overrides({"metrics.rolling_returns": bad})
    with pytest.raises(ConfigError):
        run_cfg(files, tmp_path).with_overrides({"report.console": "yes"})


# ============================================================================ other commands
def test_tax_compare_profiles_have_rolling_files(files, tmp_path):
    """Each profiles/<profile>/ directory has its own rolling_metrics.csv on the common
    calendar; no merged top-level file."""
    cfg = resolve(["tax-compare", *BASE, *files, "--tax-profile", "none,individual_pl",
                   "--output-dir", str(tmp_path)]).with_overrides(signal_overrides(**FAST))
    res = tc.run_tax_compare(cfg)
    out = res.output_dir
    assert not (out / "rolling_metrics.csv").exists()
    a = read_csv(out / "profiles" / "none" / "rolling_metrics.csv")
    b = read_csv(out / "profiles" / "individual_pl" / "rolling_metrics.csv")
    assert [(r["horizon_years"], r["window_start"], r["window_end"]) for r in a] == \
        [(r["horizon_years"], r["window_start"], r["window_end"]) for r in b]
    assert [r["pre_tax_total_return"] for r in a] == [r["pre_tax_total_return"] for r in b]
    text = console.render(cfg, res)
    assert "none" in text and "individual_pl" in text and "winner" not in text.lower()


def test_optimizer_writes_rolling_only_for_selected(files, tmp_path):
    """optimize in-sample: rolling_metrics.csv only in selected/ (never per candidate, never
    used for the objective or the tie-break)."""
    cfg = resolve(["optimize", "--btc-weight", "0,20", "--gold-weight", "0,30", "--objective",
                   "cagr", "--jobs", "1", "--as-of-date", "2026-09-29", "--dividend-tax-mode",
                   "off", *files, "--output-dir", str(tmp_path)]).with_overrides(
        signal_overrides(**FAST) | {"performance.progress": False})
    res = opt.run_optimize(cfg)
    found = sorted(p.relative_to(res.output_dir).as_posix() for p in res.output_dir.rglob("rolling_metrics.csv"))
    assert found == ["selected/rolling_metrics.csv"]
    assert "rolling" not in (res.output_dir / "grid_results.csv").read_text().split("\n")[0]
    text = console.render(cfg, res)
    assert "Selected weights" in text and "Worst rolling total return" in text


def test_scans_write_no_rolling_files(files, tmp_path):
    """Scans keep their outputs: no per-variant rolling_metrics.csv."""
    cfg = resolve(["delay-scan", "--asset", "stocks", "--delay", "1,2", "--as-of-date",
                   "2026-09-29", *files, "--output-dir", str(tmp_path)]).with_overrides(
        signal_overrides(**FAST) | {"performance.progress": False})
    res = scans.run_scan(cfg)
    assert not list(res.output_dir.rglob("rolling_metrics.csv"))
    assert "delay_weeks=1" in console.render(cfg, res)


def test_walk_forward_rolling_on_stitched_oos_path(tmp_path):
    """Rolling windows of a walk-forward are computed on the stitched OOS path only, as one
    continuous path: with the same selection in every window they equal the rolling windows of
    one continuous run, including 1Y windows crossing the OOS boundaries (no reset)."""
    first, n = "1999-01-01", 287
    files = write_market(tmp_path / "m", first, market_returns(first, n, seed=3))
    argv = ["--weights", "stocks=0.5,gold=0.3,btc=0.2", "--tax-profile", "individual_pl",
            "--dividend-tax-mode", "off", "--transaction-cost-bps", "10"]
    cfg = wf_config(files, "--optimize-params", "delay", "--delay-grid", "1", "--train-years", "2",
                    "--test-years", "1", "--start", "1999-12-31", "--jobs", "1", *argv,
                    "--output-dir", str(tmp_path / "wf"),
                    overrides=signal_overrides(**FAST) | {"performance.progress": False})
    res = wf.run_walk_forward(cfg)
    starts = [w.test_start for w in res.windows]
    one_cfg = resolve(["run", "--start", starts[0].isoformat(), "--as-of-date", "2026-09-29",
                       *files, *argv]).with_overrides(signal_overrides(**FAST))
    one = app.run_portfolio(one_cfg, write=False)
    stitched = res.portfolio.rolling
    assert stitched.windows == app.rolling_for(one_cfg, one).windows
    crossing = [w for w in stitched.for_horizon(1)
                if any(w.window_start < b <= w.window_end for b in starts[1:])]
    assert len(crossing) == len(stitched.for_horizon(1)) > 0          # every 1Y window crosses
    assert stitched.windows[0].window_start == starts[0] - dt.timedelta(days=7)   # OOS inception
    rows = read_csv(res.output_dir / "rolling_metrics.csv")
    assert len(rows) == len(stitched.windows) and rows[0]["window_end"] == stitched.windows[0].window_end.isoformat()
    m = json.loads((res.output_dir / "data_manifest.json").read_text(encoding="utf-8"))
    assert "rolling_metrics.csv" in m["audit_outputs"]
    text = console.render(cfg, res)
    assert "stitched out-of-sample" in text and "OOS windows" in text


# ============================================================================ console (REP-011)
def cli(*args, cwd=None):
    return subprocess.run([sys.executable, BT, *args], capture_output=True, text=True,
                          timeout=300, cwd=cwd)


def test_cli_run_prints_console_table(files, tmp_path):
    """REP-011: a run prints the concise table (period, pre-tax vs after-tax metrics, taxes,
    turnover, worst rolling returns, n/a for a horizon without a full window) instead of the
    plain 'outputs written to' line; values match summary.csv / rolling_metrics.csv."""
    r = cli("run", *BASE, *files, "--tax-profile", "individual_pl", "--start", "2005-01-07",
            "--output-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = next(tmp_path.iterdir())
    s = read_csv(out / "summary.csv")[0]
    lines = r.stdout.splitlines()
    assert lines[0] == "Backtest result" and lines[-1] == f"Results: {out}"
    assert "outputs written to" not in r.stdout and "\x1b[" not in r.stdout
    assert f"Final wealth{'':14}" in r.stdout
    row = next(x for x in lines if x.startswith("CAGR"))
    assert row.split()[1:] == [f"{float(s['cagr']) * 100:.2f}%", f"{float(s['after_tax_cagr']) * 100:.2f}%"]
    assert any(x.startswith("10Y") and x.split()[1:] == ["n/a", "n/a"] for x in lines)
    worst1 = min(float(x["after_tax_total_return"]) for x in read_csv(out / "rolling_metrics.csv")
                 if x["horizon_years"] == "1")
    assert next(x for x in lines if x.startswith("1Y")).split()[2] == f"{worst1 * 100:.2f}%"
    assert next(x for x in lines if x.startswith("Taxes paid")).endswith(
        f"{float(s['total_tax_paid']):,.0f} PLN")


def test_cli_console_can_be_disabled_by_config(files, tmp_path):
    """report.console: false (config file) restores the plain output line; signals has no
    portfolio table."""
    conf = tmp_path / "c.yaml"
    conf.write_text(yaml.safe_dump({"report": {"console": False}}), encoding="utf-8")
    r = cli("run", "--config", str(conf), *BASE, *files, "--output-dir", str(tmp_path / "o"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("outputs written to") and "Backtest result" not in r.stdout
    assert (next((tmp_path / "o").iterdir()) / "rolling_metrics.csv").is_file()
    r = cli("signals", "--asset", "stocks", "--as-of-date", "2026-09-29", *files,
            "--output-dir", str(tmp_path / "s"))
    assert r.returncode == 0 and r.stdout.startswith("outputs written to")


def test_library_calls_print_nothing(files, tmp_path, capsys):
    """Library / API calls never print to stdout: the console table belongs to the CLI only."""
    cfg = run_cfg(files, tmp_path / "r", "--tax-profile", "individual_pl")
    res = app.run_portfolio(cfg)
    app.run_prepared(cfg, app.prepare_run(cfg), write=True, out_dir=tmp_path / "p")
    tc.run_tax_compare(resolve(["tax-compare", *BASE, *files, "--tax-profile", "none",
                                "--output-dir", str(tmp_path / "t")]).with_overrides(signal_overrides(**FAST)))
    assert capsys.readouterr().out == ""
    assert res.rolling is not None and (res.output_dir / "rolling_metrics.csv").is_file()


def test_console_formatting():
    """Presentation formats: PLN with thousands separators, percent with 2 decimals, ratios,
    turnover multiples, n/a for missing values."""
    assert console.pln(1234567.4) == "1,234,567 PLN" and console.pln(None) == "n/a"
    assert console.pct(0.1234) == "12.34%" and console.pct(-0.3) == "-30.00%" and console.pct("") == "n/a"
    assert console.ratio(0.8249) == "0.82" and console.times(3.956) == "3.96x"
    assert console.line("CAGR", "1.00%", "2.00%") == "CAGR" + " " * 22 + " " * 14 + "1.00%" + " " * 14 + "2.00%"


def test_rolling_metrics_deterministic(files, tmp_path):
    """Two identical runs write byte-identical rolling_metrics.csv (no timestamp, fixed order)."""
    a = app.run_portfolio(run_cfg(files, tmp_path / "a", "--tax-profile", "family_foundation_19"))
    b = app.run_portfolio(run_cfg(files, tmp_path / "b", "--tax-profile", "family_foundation_19"))
    assert (a.output_dir / "rolling_metrics.csv").read_bytes() == \
        (b.output_dir / "rolling_metrics.csv").read_bytes()
