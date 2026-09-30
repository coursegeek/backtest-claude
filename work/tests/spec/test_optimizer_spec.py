"""TEST-009 (OPT-006, Q-041) on the real optimizer: the grid search rejects weight sums above
100% (and, for explicit stocks grids, below 100% - ALLOC-001); rejected combinations stay in
grid_results.csv with their weights and status and never reach the engine."""
import csv
from pathlib import Path

from src import optimizer as opt
from src.cli import resolve
from src.reporting import SUMMARY_FIELDS

SHORT = ["--start", "2018-01-01", "--end", "2019-12-31", "--as-of-date", "2026-09-29",
         "--jobs", "1"]


def test_grid_rejects_sum_over_100(tmp_path, monkeypatch):
    """btc 0:60:30 x gold 0,30,50,60, stocks = remainder, rf 0: btc 60 + gold 50 and every
    other combination above 100% are rejected with a status; valid remainders are >= 0."""
    calls = []
    real = opt.run_prepared

    def run(cfg, prepared, **kw):
        calls.append(dict(cfg.get("allocation.targets")))
        return real(cfg, prepared, **kw)

    monkeypatch.setattr(opt, "run_prepared", run)
    res = opt.run_optimize(resolve(["optimize", "--btc-weight", "0:60:30", "--gold-weight",
                                    "0,30,50,60", "--stocks-weight", "remainder",
                                    "--output-dir", str(tmp_path)] + SHORT))
    with (res.output_dir / "grid_results.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 12                                   # full Cartesian grid
    by = {(r["weight_btc"], r["weight_gold"]): r for r in rows}
    over = {("0.6", "0.5"), ("0.6", "0.6")}
    for key, r in by.items():
        total = float(key[0]) + float(key[1])
        if key in over:
            assert total > 1 and r["status"] == "rejected_weight_sum_gt_1"
            assert r["eligible"] == "false" and all(r[k] == "" for k in SUMMARY_FIELDS)
        else:
            assert r["status"] == "ok" and float(r["weight_stocks"]) >= 0
    assert len(calls) == 10 and all(t["btc"] + t["gold"] <= 1 for t in calls)


def test_grid_rejects_explicit_sum_below_100(tmp_path):
    """Explicit stocks grid: btc 20 + gold 20 + stocks 20 + rf 0 = 60% is rejected (ALLOC-001),
    never topped up with RF."""
    res = opt.run_optimize(resolve(["optimize", "--btc-weight", "20", "--gold-weight", "20",
                                    "--stocks-weight", "20,60", "--rf-weight", "0",
                                    "--output-dir", str(tmp_path)] + SHORT))
    low, ok = res.rows
    assert (low["status"], low["weight_sum"], low["weight_rf"]) == ("rejected_weight_sum_lt_1", 0.6, 0.0)
    assert ok["status"] == "ok" and ok["selected"] is True
