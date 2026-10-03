"""In-sample optimize end to end on the clean-room data (canonical stock signal and dividend
files since Q-004/Q-008; gold is the staged Q-002 proxy): OPT-001..010, TEST-009, CLI-004/005,
ERR-005/006, REPRO-006, Q-041."""
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src import app
from src import optimizer as opt
from src.app import prepared_input_sha256
from src.cli import resolve
from src.errors import InsolvencyError
from src.reporting import SUMMARY_FIELDS, fmt, summary_row

WORK = Path(__file__).resolve().parents[2]
ROOT = WORK.parent
BT = str(WORK / "backtest.py")
SHORT = ["--start", "2018-01-01", "--end", "2020-12-31", "--as-of-date", "2026-09-29"]
S04 = ["optimize", "--start", "2018-01-01", "--end", "2026-07-31", "--btc-weight", "0:25:1",
       "--gold-weight", "0:25:1", "--stocks-weight", "remainder", "--objective", "cagr"]
S05 = ["optimize", "--start", "2018-01-01", "--end", "2026-07-31", "--btc-weight", "0:25:1",
       "--gold-weight", "0:25:1", "--stocks-weight", "remainder", "--objective", "after_tax_cagr",
       "--tax-profile", "individual_pl", "--dividend-tax-mode", "smoothed_weekly",
       "--dividend-file", "SPX_dividend_return_weekly_1970_2026.csv"]


def read_csv(path):
    with Path(path).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def optimize(argv, out, write=True, jobs=1):
    return opt.run_optimize(resolve(["optimize"] + list(argv) + ["--output-dir", str(out),
                                                                 "--jobs", str(jobs)]), write=write)


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    """btc 0/10/20 x gold 0/20 (6 candidates), objective cagr, sequential."""
    res = optimize(SHORT + ["--btc-weight", "0,10,20", "--gold-weight", "0,20", "--objective",
                            "cagr"], tmp_path_factory.mktemp("small"))
    return res, read_csv(res.output_dir / "grid_results.csv")


# ------------------------------------------------------------------ OPT-010 full grid
def test_full_grid_counts_and_outputs(small):
    res, rows = small
    header = (res.output_dir / "grid_results.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == opt.GRID_FIELDS and opt.GRID_FIELDS[len(opt.OPT_FIELDS):] == list(SUMMARY_FIELDS)
    assert [r["grid_index"] for r in rows] == [str(i) for i in range(1, 7)]
    assert sum(r["selected"] == "true" for r in rows) == 1
    m = json.loads((res.output_dir / "optimizer_manifest.json").read_text(encoding="utf-8"))
    assert (m["raw_grid_points"], m["weight_valid_points"], m["evaluated_points"],
            m["eligible_points"], m["rejected_points"], m["selected_points"]) == (6, 6, 6, 6, 0, 1)
    assert all(m["checks"].values()) and m["mode"] == "in-sample"
    for k in ("objective", "objective_direction", "tie_break", "max_drawdown_limit",
              "selected_grid_index", "selected_weights", "selected_objective_value",
              "required_assets_union", "prepared_input_sha256", "effective_first_week",
              "effective_last_week", "weeks", "dropped_weeks", "as_of_date", "jobs_requested",
              "jobs_effective", "parallel_execution", "sources"):
        assert k in m, k
    files = {f.name for f in res.output_dir.iterdir()}
    assert files == {"grid_results.csv", "summary.csv", "optimizer_manifest.json",
                     "config_resolved.yaml", "selected_config_resolved.yaml", "data_manifest.json",
                     "validation_report.csv", "weekly_normalized.csv", "selected"}
    sel = next(r for r in rows if r["selected"] == "true")
    summary = read_csv(res.output_dir / "summary.csv")
    assert summary == [{k: sel[k] for k in SUMMARY_FIELDS}]
    assert read_csv(res.output_dir / "selected" / "summary.csv") == summary
    assert {"weekly_portfolio.csv", "trades.csv", "tax_events.csv", "payments.csv"} <= {
        f.name for f in (res.output_dir / "selected").iterdir()}
    import yaml
    sc = yaml.safe_load((res.output_dir / "selected_config_resolved.yaml").read_text())
    assert sc["allocation"]["targets"] == {k: float(sel[f"weight_{k}"])
                                           for k in ("stocks", "gold", "btc", "rf")}
    for r in rows:
        assert r["objective"] == "cagr" and r["objective_direction"] == "maximize"
        assert r["objective_value"] == r["cagr"] and r["relevant_max_drawdown"] == r["max_drawdown"]


def test_selected_follows_q041_order(small):
    res, rows = small
    eligible = [r for r in res.rows if r["eligible"]]
    best = min(eligible, key=lambda r: (-r["cagr"], r["max_drawdown"], r["turnover"],
                                        (r["weight_btc"], r["weight_gold"], r["weight_rf"],
                                         r["weight_stocks"])))
    assert res.selected["grid_index"] == best["grid_index"]


def test_same_calendar_for_every_candidate(small):
    res, rows = small
    for k in ("effective_first_week", "effective_last_week", "weeks", "inception_date",
              "common_data_start", "common_data_end", "dropped_incomplete_weeks"):
        assert len({r[k] for r in rows}) == 1, k
    assert res.worker_fingerprints == (res.fingerprint,) == (prepared_input_sha256(res.prepared),)


# ------------------------------------------------------------------ weight union (Q-041)
def test_btc_zero_and_btc_positive_share_the_btc_bounded_calendar(tmp_path, monkeypatch):
    """Candidate A (btc = 0) and B (btc > 0): BTC is loaded once, bounds the common calendar
    of both (auto start after the BTC warm-up) and both have the same range and hash."""
    loads = []
    real = app.load_role

    def load(cfg, role):
        loads.append(role)
        return real(cfg, role)

    monkeypatch.setattr(app, "load_role", load)
    res = optimize(["--end", "2013-12-31", "--as-of-date", "2026-09-29", "--btc-weight", "0,10",
                    "--gold-weight", "0"], tmp_path)
    assert loads.count("btc") == 1 and loads.count("stocks_return") == 1
    a, b = res.rows
    assert (a["weight_btc"], b["weight_btc"]) == (0.0, 0.1)
    assert res.spec.union == ("stocks", "btc")
    for k in ("effective_first_week", "effective_last_week", "weeks"):
        assert a[k] == b[k]
    first = a["effective_first_week"]
    btc_keys = app.load_role(res.spec.base, "btc").keys()
    assert sum(1 for k in btc_keys if k < first) == 52          # BTC warm-up bounds both
    alone = app.prepare_run(opt.candidate_config(res.spec.base, res.spec.candidates[0]),
                            auto_start=True)                    # A prepared on its own
    assert alone.first_week < first                              # would have started earlier
    assert a.get("signal_btc_ma") is None and b["signal_btc_ma"] == 50   # A has no BTC tracker


def test_gold_union_bounds_the_calendar(tmp_path):
    res = optimize(["--end", "1972-12-31", "--as-of-date", "2026-09-29", "--btc-weight", "0",
                    "--gold-weight", "0,10"], tmp_path, write=False)
    a, b = res.rows
    assert res.spec.union == ("stocks", "gold")
    assert a["effective_first_week"] == b["effective_first_week"] and a["weeks"] == b["weeks"]
    assert a["effective_first_week"].year >= 1970


# ------------------------------------------------------------------ candidate == run_prepared
def test_candidate_rows_equal_standalone_run_prepared(small):
    """No separate optimizer logic: two grid points recomputed with run_prepared on the same
    shared prepared input give the same summary values and the same weekly path."""
    res, rows = small
    for gi in (2, 5):
        cand = res.spec.candidates[gi - 1]
        cfg = opt.candidate_config(res.spec.base, cand)
        alone = app.run_prepared(cfg, res.prepared, write=False)
        mine = {k: fmt(v) for k, v in summary_row(cfg, alone).items()}
        assert {k for k in SUMMARY_FIELDS if mine.get(k, "") != rows[gi - 1][k]} == set()
    sel = res.selected_result
    again = app.run_prepared(res.selected_config, res.prepared, write=False)
    assert [w.nav_end for w in sel.engine.weeks] == [w.nav_end for w in again.engine.weeks]
    assert sel.engine.trades == again.engine.trades


# ------------------------------------------------------------------ TEST-009 with data
def test_test_009_rejected_rows_kept_and_not_run(tmp_path, monkeypatch):
    calls = []
    real = opt.run_prepared

    def run(cfg, prepared, **kw):
        calls.append(cfg.get("allocation.targets"))
        return real(cfg, prepared, **kw)

    monkeypatch.setattr(opt, "run_prepared", run)
    res = optimize(SHORT + ["--btc-weight", "0,60", "--gold-weight", "0,50"], tmp_path)
    rows = read_csv(res.output_dir / "grid_results.csv")
    assert len(rows) == 4 and len(calls) == 3                    # the >100% point never runs
    bad = rows[3]
    assert (bad["weight_btc"], bad["weight_gold"], bad["status"]) == ("0.6", "0.5",
                                                                      "rejected_weight_sum_gt_1")
    assert bad["eligible"] == "false" and bad["selected"] == "false"
    assert all(bad[k] == "" for k in SUMMARY_FIELDS) and bad["objective_value"] == ""
    assert bad["rejection_reason"] and bad["weight_stocks"].startswith("-0.1")
    m = json.loads((res.output_dir / "optimizer_manifest.json").read_text(encoding="utf-8"))
    assert (m["raw_grid_points"], m["weight_rejected_points"], m["evaluated_points"]) == (4, 1, 3)
    assert m["status_counts"]["rejected_weight_sum_gt_1"] == 1


def test_explicit_stocks_grid_rejects_sums_below_one(tmp_path):
    res = optimize(SHORT + ["--btc-weight", "0,20", "--gold-weight", "20", "--stocks-weight",
                            "20,60"], tmp_path, write=False)
    assert [r["status"] for r in res.rows] == ["rejected_weight_sum_lt_1", "rejected_weight_sum_lt_1",
                                               "rejected_weight_sum_lt_1", "ok"]
    assert res.selected["grid_index"] == 4
    assert (res.selected["target_stocks"], res.selected["target_rf"]) == (0.6, 0.0)


# ------------------------------------------------------------------ OPT-008 drawdown limit
def test_pre_tax_drawdown_limit_inclusive(small, tmp_path):
    res, _ = small
    dds = sorted({r["max_drawdown"] for r in res.rows})
    limit = dds[len(dds) // 2]                                  # exactly one candidate's DD
    cfg = resolve(["optimize"] + SHORT + ["--btc-weight", "0,10,20", "--gold-weight", "0,20",
                                          "--objective", "cagr", "--jobs", "1"])
    cfg = cfg.with_overrides({"optimizer.max_drawdown_limit": limit})
    lim = opt.run_optimize(cfg, write=False)
    for r in lim.rows:
        assert r["relevant_drawdown_metric"] == "max_drawdown"
        expected = "ok" if r["max_drawdown"] <= limit else "rejected_drawdown_limit"
        assert r["status"] == expected
        assert r["cagr"] is not None                             # metrics kept when rejected
    assert any(r["max_drawdown"] == limit and r["status"] == "ok" for r in lim.rows)
    assert lim.selected["max_drawdown"] <= limit
    assert any(r["status"] == "rejected_drawdown_limit" for r in lim.rows)


def test_after_tax_objective_uses_after_tax_drawdown(tmp_path):
    base = SHORT + ["--btc-weight", "0,20", "--gold-weight", "0,20", "--objective",
                    "after_tax_cagr", "--tax-profile", "individual_pl"]
    free = optimize(base, tmp_path, write=False)
    for r in free.rows:
        assert r["relevant_drawdown_metric"] == "after_tax_max_drawdown"
        assert r["relevant_max_drawdown"] == r["after_tax_max_drawdown"]
        assert r["objective_value"] == r["after_tax_cagr"]
    pick = next(r for r in free.rows if r["after_tax_max_drawdown"] > r["max_drawdown"])
    limit = (pick["max_drawdown"] + pick["after_tax_max_drawdown"]) / 2   # between the two
    cfg = resolve(["optimize"] + base + ["--jobs", "1"]).with_overrides(
        {"optimizer.max_drawdown_limit": limit})
    lim = opt.run_optimize(cfg, write=False)
    row = lim.rows[pick["grid_index"] - 1]
    assert row["status"] == "rejected_drawdown_limit"            # pre-tax DD alone would pass
    for r in lim.rows:
        assert (r["status"] == "ok") == (r["after_tax_max_drawdown"] <= limit)


def test_min_drawdown_objective(tmp_path):
    res = optimize(SHORT + ["--btc-weight", "0,10,20", "--gold-weight", "0,20", "--objective",
                            "min_drawdown"], tmp_path, write=False)
    best = min(r["max_drawdown"] for r in res.rows)
    assert res.selected["max_drawdown"] == best > 0
    assert res.selected["objective_value"] == best
    assert {r["objective_direction"] for r in res.rows} == {"minimize"}


def test_no_eligible_candidate_writes_nothing(tmp_path):
    with pytest.raises(opt.OptimizerError, match="no eligible optimization candidate") as e:
        optimize(SHORT + ["--btc-weight", "0,10", "--gold-weight", "0", "--max-drawdown-limit",
                          "0.01"], tmp_path / "o")
    assert e.value.exit_code == 4
    assert not (tmp_path / "o").exists()


def test_candidate_failure_is_atomic(tmp_path, monkeypatch):
    real = opt.run_prepared

    def run(cfg, prepared, **kw):
        if cfg.get("allocation.targets")["btc"] == 0.1:
            raise InsolvencyError(prepared.inputs.weeks[3], 1.0, 0.5)
        return real(cfg, prepared, **kw)

    monkeypatch.setattr(opt, "run_prepared", run)
    with pytest.raises(opt.OptimizerError, match="grid_index=2") as e:
        optimize(SHORT + ["--btc-weight", "0,10", "--gold-weight", "0"], tmp_path / "o")
    assert e.value.exit_code == 1
    assert not (tmp_path / "o").exists()


def test_foundation_profile_candidates(tmp_path):
    res = optimize(SHORT + ["--btc-weight", "0,10", "--gold-weight", "0", "--objective",
                            "after_tax_terminal_wealth", "--tax-profile", "family_foundation_19"],
                   tmp_path, write=False)
    for r in res.rows:
        assert r["foundation_setup_cost_paid"] == 40000.0 and r["terminal_foundation_tax"] > 0
        assert r["objective_value"] == r["after_tax_terminal_wealth"]


# ------------------------------------------------------------------ ERR-006 / REPRO-006
def test_serial_and_parallel_are_byte_identical(tmp_path):
    grid = SHORT + ["--btc-weight", "0:20:5", "--gold-weight", "0,10,20", "--objective", "sharpe"]
    runs = {j: optimize(grid, tmp_path / f"j{j}", jobs=j) for j in (1, 2, -1)}
    ref = runs[1].output_dir
    m1 = json.loads((ref / "optimizer_manifest.json").read_text(encoding="utf-8"))
    for j in (2, -1):
        d = runs[j].output_dir
        for f in ("grid_results.csv", "summary.csv", "weekly_normalized.csv",
                  "validation_report.csv"):
            assert (d / f).read_bytes() == (ref / f).read_bytes(), (j, f)
        for f in (ref / "selected").iterdir():
            if f.suffix == ".csv" or f.name.endswith("state.json") or f.name.startswith("terminal"):
                assert (d / "selected" / f.name).read_bytes() == f.read_bytes(), (j, f.name)
        mj = json.loads((d / "optimizer_manifest.json").read_text(encoding="utf-8"))
        assert {k for k in m1 if m1[k] != mj[k]} <= {"run_timestamp", "jobs_requested",
                                                     "jobs_effective", "parallel_execution",
                                                     "code_version"}
        assert mj["parallel_execution"] is True and mj["jobs_effective"] >= 2
        assert runs[j].selected["grid_index"] == runs[1].selected["grid_index"]
    assert m1["parallel_execution"] is False and m1["jobs_effective"] == 1


def test_repeated_optimize_is_byte_identical(tmp_path):
    grid = SHORT + ["--btc-weight", "0,10", "--gold-weight", "0,10"]
    a, b = optimize(grid, tmp_path / "a", jobs=2), optimize(grid, tmp_path / "b", jobs=2)
    for f in ("grid_results.csv", "summary.csv"):
        assert (a.output_dir / f).read_bytes() == (b.output_dir / f).read_bytes()


# ------------------------------------------------------------------ CLI-004 / CLI-005
def cli(*args):
    return subprocess.run([sys.executable, BT, *args], capture_output=True, text=True,
                          timeout=900, cwd=str(ROOT))


def test_cli_004_exact_command(tmp_path):
    r = cli(*S04, "--output-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = next(tmp_path.iterdir())
    rows = read_csv(out / "grid_results.csv")
    assert len(rows) == 676 and sum(x["selected"] == "true" for x in rows) == 1
    assert {x["objective"] for x in rows} == {"cagr"} and {x["status"] for x in rows} == {"ok"}
    assert {(x["effective_first_week"], x["effective_last_week"]) for x in rows} == {
        ("2018-01-05", "2026-07-31")}
    m = json.loads((out / "optimizer_manifest.json").read_text(encoding="utf-8"))
    assert m["required_assets_union"] == ["stocks", "gold", "btc"] and m["tax_profile"] == "none"
    assert m["raw_grid_points"] == m["evaluated_points"] == m["eligible_points"] == 676
    assert "optimize: completed 676/676 evaluated candidates" in r.stderr     # ERR-005
    assert "completed" not in r.stdout


def test_cli_005_exact_command(tmp_path):
    r = cli(*S05, "--output-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = next(tmp_path.iterdir())
    rows = read_csv(out / "grid_results.csv")
    assert len(rows) == 676 and sum(x["selected"] == "true" for x in rows) == 1
    assert {x["objective"] for x in rows} == {"after_tax_cagr"}
    assert {x["tax_profile"] for x in rows} == {"individual_pl"}
    assert {x["relevant_drawdown_metric"] for x in rows} == {"after_tax_max_drawdown"}
    ends = {x["effective_last_week"] for x in rows}
    assert ends == {"2026-07-31"}               # canonical dividends (to 2026-09-18) keep --end
    m = json.loads((out / "optimizer_manifest.json").read_text(encoding="utf-8"))
    assert "dividend" in {s["role"] for s in m["sources"]}
    assert m["effective_last_week"] == "2026-07-31"
    data = json.loads((out / "data_manifest.json").read_text(encoding="utf-8"))
    div = next(s for s in data["sources"] if s["role"] == "dividend")
    assert div["canonical"] and not div["resolved_via_alias"] and div["last_key"] == "2026-09-18"
    sel = next(x for x in rows if x["selected"] == "true")
    assert float(sel["after_tax_cagr"]) < float(sel["cagr"])
