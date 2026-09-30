"""Optimizer configuration, weight grid and selection without data (OPT-002..009, TEST-009,
ERR-006, Q-026, Q-041)."""
import math
import os
import random

import pytest

from src import app
from src import optimizer as opt
from src.cli import resolve
from src.errors import ConfigError, NotImplementedCommand

S04 = ["optimize", "--start", "2018-01-01", "--end", "2026-07-31", "--btc-weight", "0:25:1",
       "--gold-weight", "0:25:1", "--stocks-weight", "remainder", "--objective", "cagr"]


def spec_of(*argv):
    return opt.resolve_optimizer(resolve(list(argv)))


# ------------------------------------------------------------------ weight grid (Q-041)
def test_s04_grid_is_676_valid_remainder_combinations():
    spec = spec_of(*S04)
    assert len(spec.candidates) == 676 and all(c.weight_valid for c in spec.candidates)
    assert [c.grid_index for c in spec.candidates] == list(range(1, 677))
    c = spec.candidates
    assert c[0].targets == {"stocks": 1.0, "gold": 0.0, "btc": 0.0, "rf": 0.0}
    assert c[1].targets["gold"] == 0.01 and c[1].targets["btc"] == 0.0     # gold inner
    assert c[26].targets["btc"] == 0.01 and c[26].targets["gold"] == 0.0   # btc outer
    t = next(x for x in c if x.targets["btc"] == 0.07 and x.targets["gold"] == 0.13).targets
    assert t["stocks"] == 0.8                                  # exact decimal remainder
    assert all(abs(math.fsum(x.targets.values()) - 1) <= 1e-12 for x in c)
    assert spec.union == ("stocks", "gold", "btc") and spec.grid["rf_weight"] == 0.0
    assert spec.objective == "cagr" and spec.direction == "maximize"


def test_test_009_sum_over_100_rejected():
    """TEST-009: btc 60% + gold 50% with stocks = remainder is rejected (> 100%)."""
    spec = spec_of("optimize", "--btc-weight", "0,60", "--gold-weight", "0,50",
                   "--stocks-weight", "remainder")
    st = {(c.targets["btc"], c.targets["gold"]): c for c in spec.candidates}
    bad = st[(0.6, 0.5)]
    assert bad.status == opt.REJECTED_GT_1 and not bad.weight_valid
    assert bad.targets["stocks"] == pytest.approx(-0.1) and "> 1" in bad.reason
    assert [st[k].status for k in ((0.0, 0.0), (0.0, 0.5), (0.6, 0.0))] == ["pending"] * 3
    assert st[(0.6, 0.0)].targets["stocks"] == 0.4


def test_explicit_stocks_grid_must_sum_to_one():
    """ALLOC-001 / Q-041: explicit stocks - sums < 1 and > 1 are both rejected, nothing is
    moved to RF."""
    cands, _ = opt.build_weight_grid(resolve(["optimize", "--btc-weight", "20", "--gold-weight",
                                              "20", "--stocks-weight", "20,60,70"]))
    assert [c.status for c in cands] == [opt.REJECTED_LT_1, "pending", opt.REJECTED_GT_1]
    assert cands[0].targets == {"stocks": 0.2, "gold": 0.2, "btc": 0.2, "rf": 0.0}
    assert cands[0].weight_sum == 0.6 and "< 1" in cands[0].reason
    assert cands[1].targets == {"stocks": 0.6, "gold": 0.2, "btc": 0.2, "rf": 0.0}
    with pytest.raises(ConfigError, match="none of the 1 weight combinations"):
        spec_of("optimize", "--btc-weight", "20", "--gold-weight", "20", "--stocks-weight", "20",
                "--rf-weight", "0")


def test_nesting_with_explicit_stocks_grid():
    cands, spec = opt.build_weight_grid(resolve(["optimize", "--btc-weight", "0,10",
                                                 "--gold-weight", "0,10", "--stocks-weight",
                                                 "80,90,100"]))
    assert spec["nesting"] == ["btc", "gold", "stocks"] and len(cands) == 12
    order = [(c.targets["btc"], c.targets["gold"], c.targets["stocks"]) for c in cands]
    assert order[:4] == [(0.0, 0.0, 0.8), (0.0, 0.0, 0.9), (0.0, 0.0, 1.0), (0.0, 0.1, 0.8)]
    valid = [(c.targets["btc"], c.targets["gold"], c.targets["stocks"]) for c in cands if c.weight_valid]
    assert valid == [(0.0, 0.0, 1.0), (0.0, 0.1, 0.9), (0.1, 0.0, 0.9), (0.1, 0.1, 0.8)]


def test_fixed_rf_weight_and_remainder():
    """OPT-005: --rf-weight is one fixed percent value; remainder subtracts it."""
    spec = spec_of("optimize", "--btc-weight", "0,10", "--gold-weight", "0", "--rf-weight", "10")
    assert [c.targets for c in spec.candidates] == [
        {"stocks": 0.9, "gold": 0.0, "btc": 0.0, "rf": 0.1},
        {"stocks": 0.8, "gold": 0.0, "btc": 0.1, "rf": 0.1}]
    with pytest.raises(ConfigError):
        resolve(["optimize", "--rf-weight", "0,10"])


def test_remainder_tolerance(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("optimizer:\n  btc_weight: [0.5]\n  gold_weight: [0.5000000000001, 0.50001]\n",
                 encoding="utf-8")
    spec_c, _ = opt.build_weight_grid(resolve(["optimize", "--config", str(f)]))
    assert spec_c[0].weight_valid and spec_c[0].targets["stocks"] == 0.0     # -1e-13 -> 0
    assert spec_c[1].status == opt.REJECTED_GT_1                              # -1e-5


def test_all_invalid_grid_fails_before_data(monkeypatch):
    def no_data(*a, **k):
        raise AssertionError("data must not be loaded")

    monkeypatch.setattr(app, "build_run", no_data)
    with pytest.raises(ConfigError, match="none of the 2 weight combinations"):
        opt.run_optimize(resolve(["optimize", "--btc-weight", "60,70", "--gold-weight", "50"]))


def test_invalid_grids_and_configuration():
    for argv, err, msg in (
            (["optimize", "--btc-weight", "0,0"], ConfigError, "duplicate"),
            (["optimize", "--btc-weight", "0:120:60"], ConfigError, "0..1"),
            (["optimize", "--weights", "stocks=1"], ConfigError, "remove allocation.targets"),
            (["optimize", "--single-asset", "btc"], ConfigError, "remove allocation.targets"),
            (["optimize", "--asset", "btc"], ConfigError, "--asset is not used"),
            (["optimize", "--optimization-mode", "walk-forward"], NotImplementedCommand,
             "walk-forward"),
            (["optimize", "--optimize-params", "weights,ma"], NotImplementedCommand, "WF-006"),
            (["optimize", "--max-drawdown-limit", "150"], ConfigError, "0..1")):
        with pytest.raises(err, match=msg):
            spec_of(*argv)
    with pytest.raises(ConfigError):
        resolve(["optimize", "--objective", "best"])


def test_grid_index_independent_of_objective():
    a = spec_of(*S04)
    b = spec_of(*(S04[:-1] + ["min_drawdown"]))
    assert a.candidates == b.candidates


# ------------------------------------------------------------------ objectives (OPT-007/008)
def test_objective_map_and_relevant_drawdown():
    assert opt.OBJECTIVES == {
        "cagr": ("cagr", "maximize"), "after_tax_cagr": ("after_tax_cagr", "maximize"),
        "terminal_wealth": ("final_wealth_pre_tax", "maximize"),
        "after_tax_terminal_wealth": ("after_tax_terminal_wealth", "maximize"),
        "sharpe": ("sharpe", "maximize"), "sortino": ("sortino", "maximize"),
        "calmar": ("calmar", "maximize"), "min_drawdown": ("max_drawdown", "minimize")}
    for o in ("after_tax_cagr", "after_tax_terminal_wealth"):
        assert opt.relevant_drawdown_metric(o) == "after_tax_max_drawdown"
    for o in ("cagr", "terminal_wealth", "sharpe", "sortino", "calmar", "min_drawdown"):
        assert opt.relevant_drawdown_metric(o) == "max_drawdown"
    assert "pre_terminal_nav" not in {m for m, _ in opt.OBJECTIVES.values()}


def summ(**kw):
    base = {"cagr": 0.1, "after_tax_cagr": 0.08, "max_drawdown": 0.2,
            "after_tax_max_drawdown": 0.3, "turnover": 1.0, "sharpe": 0.5}
    base.update(kw)
    return base


def test_classify_availability_and_limit():
    assert opt.classify(summ(sharpe=None), "sharpe", None)[0] == opt.UNAVAILABLE
    assert opt.classify(summ(sharpe=float("nan")), "sharpe", None)[0] == opt.UNAVAILABLE
    st, _, value, dd = opt.classify(summ(), "cagr", 0.2)                 # equal passes
    assert (st, value, dd) == (opt.OK, 0.1, 0.2)
    st, reason, value, dd = opt.classify(summ(), "cagr", 0.19)
    assert st == opt.REJECTED_DD and value == 0.1 and "max_drawdown 0.2 > " in reason
    # after-tax objective: the after-tax drawdown decides
    assert opt.classify(summ(), "after_tax_cagr", 0.25)[0] == opt.REJECTED_DD
    assert opt.classify(summ(), "after_tax_cagr", 0.3)[0] == opt.OK
    assert opt.classify(summ(), "cagr", 0.25)[0] == opt.OK


# ------------------------------------------------------------------ tie-break (OPT-009, Q-041)
def row(gi, obj, dd, turnover, btc, gold, rf, stocks, metric="cagr", eligible=True):
    return {"grid_index": gi, "eligible": eligible, metric: obj, "max_drawdown": dd,
            "after_tax_max_drawdown": dd, "turnover": turnover, "weight_btc": btc,
            "weight_gold": gold, "weight_rf": rf, "weight_stocks": stocks}


def test_selector_a_better_objective():
    rows = [row(1, 0.10, 0.1, 1.0, 0, 0, 0, 1), row(2, 0.11, 0.9, 9.0, 0.1, 0, 0, 0.9)]
    assert opt.select(rows, "cagr")["grid_index"] == 2


def test_selector_b_equal_objective_lower_drawdown():
    rows = [row(1, 0.10, 0.25, 1.0, 0, 0, 0, 1), row(2, 0.10, 0.2, 5.0, 0.1, 0, 0, 0.9)]
    assert opt.select(rows, "cagr")["grid_index"] == 2


def test_selector_c_equal_objective_and_drawdown_lower_turnover():
    rows = [row(1, 0.10, 0.2, 2.0, 0, 0, 0, 1), row(2, 0.10, 0.2, 1.5, 0.1, 0, 0, 0.9)]
    assert opt.select(rows, "cagr")["grid_index"] == 2


def test_selector_d_lexicographic_weights():
    """(btc, gold, rf, stocks) ascending decides only when everything else is equal."""
    rows = [row(1, 0.1, 0.2, 1.0, 0.1, 0.0, 0.0, 0.9), row(2, 0.1, 0.2, 1.0, 0.0, 0.2, 0.0, 0.8),
            row(3, 0.1, 0.2, 1.0, 0.0, 0.1, 0.1, 0.8), row(4, 0.1, 0.2, 1.0, 0.0, 0.1, 0.0, 0.9)]
    assert opt.select(rows, "cagr")["grid_index"] == 4


def test_selector_no_rounding_or_tolerance():
    rows = [row(1, 0.1, 0.2, 1.0, 0, 0, 0, 1), row(2, 0.1, 0.1, 1.0, 0.1, 0, 0, 0.9),
            row(3, math.nextafter(0.1, 1.0), 0.9, 9, 0.2, 0, 0, 0.8)]
    assert opt.select(rows, "cagr")["grid_index"] == 3          # one ulp better wins
    assert opt.select(rows[:2], "cagr")["grid_index"] == 2      # exact tie -> lower drawdown


def test_selector_e_input_order_does_not_matter():
    rows = [row(i, [0.1, 0.12, 0.12, 0.12, 0.05][i % 5], [0.2, 0.3, 0.25, 0.25, 0.1][i % 5],
                [1, 2, 3, 3, 1][i % 5], i / 100, 0.0, 0.0, 1 - i / 100) for i in range(1, 30)]
    rows.append(row(99, 0.2, 0.1, 1, 0.5, 0, 0, 0.5, eligible=False))   # not eligible
    ref = opt.select(rows, "cagr")["grid_index"]
    rnd = random.Random(7)
    for _ in range(20):
        shuffled = rows[:]
        rnd.shuffle(shuffled)
        assert opt.select(shuffled, "cagr")["grid_index"] == ref
    assert ref != 99


def test_min_drawdown_selects_smallest_positive_drawdown():
    rows = [row(1, 0.3, 0.3, 1, 0, 0, 0, 1, metric="max_drawdown"),
            row(2, 0.12, 0.12, 1, 0.1, 0, 0, 0.9, metric="max_drawdown"),
            row(3, 0.2, 0.2, 1, 0.2, 0, 0, 0.8, metric="max_drawdown")]
    assert opt.select(rows, "min_drawdown")["grid_index"] == 2
    spec = spec_of(*(S04[:-1] + ["min_drawdown"]))
    r = opt.evaluated_row(spec, spec.candidates[0], summ(max_drawdown=0.12))
    assert (r["objective_value"], r["objective_direction"], r["objective_metric"]) == (
        0.12, "minimize", "max_drawdown")                        # positive, never -maxDD


def test_selector_none_when_nothing_eligible():
    assert opt.select([row(1, 0.1, 0.2, 1, 0, 0, 0, 1, eligible=False)], "cagr") is None


# ------------------------------------------------------------------ jobs (ERR-006)
def test_jobs_semantics():
    cpus = len(os.sched_getaffinity(0))
    assert opt.resolve_jobs(1, 100) == 1
    assert opt.resolve_jobs(3, 100) == 3 and opt.resolve_jobs(8, 2) == 2
    assert opt.resolve_jobs(-1, 1000) == cpus and opt.resolve_jobs("auto", 1000) == cpus
    for bad in (0, -2, "x", True):
        with pytest.raises(ConfigError):
            opt.resolve_jobs(bad, 10)
    for bad in ("0", "-2"):
        with pytest.raises(ConfigError, match="jobs"):
            resolve(["optimize", "--jobs", bad])
    assert resolve(["optimize"]).get("performance.jobs") == "auto"     # ERR-006 default
