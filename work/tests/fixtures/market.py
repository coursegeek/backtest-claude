"""Synthetic long weekly market data written in the raw source formats (walk-forward tests).

Every series is explicit and deterministic; nothing is taken from V1 or from the staged data.
Weekly returns are percent values with two decimals (the precision of the Fama/French file), so
the stock return source and the stock price index describe exactly the same path.
"""
from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

from fixtures.builders import ff_text, write_csv

WEEK = dt.timedelta(days=7)
ASSETS = ("stocks", "gold", "btc")


def fridays(first: str, n: int) -> list:
    k0 = dt.date.fromisoformat(first)
    assert k0.weekday() == 4, "first key must be a Friday"
    return [k0 + i * WEEK for i in range(n)]


def random_pct(n: int, seed: int, drift: float = 0.15, vol: float = 2.0) -> list:
    """Weekly returns in percent with two decimals."""
    rng = random.Random(seed)
    return [round(rng.gauss(drift, vol), 2) for _ in range(n)]


def prices(pct, start: float = 100.0) -> list:
    out, p = [], start
    for r in pct:
        p *= 1.0 + r / 100.0
        out.append(p)
    return out


def market_returns(first: str, n: int, seed: int = 7, assets=ASSETS, overrides=None) -> dict:
    """{asset: [weekly percent returns]} for ``n`` Fridays from ``first``; ``overrides`` maps
    asset -> function(keys, pct) -> pct to engineer scenarios."""
    out = {}
    params = {"stocks": (0.15, 2.0), "gold": (0.08, 1.8), "btc": (0.6, 8.0)}
    keys = fridays(first, n)
    for i, a in enumerate(assets):
        pct = random_pct(n, seed * 31 + i, *params[a])
        if overrides and a in overrides:
            pct = list(overrides[a](keys, pct))
        out[a] = [max(-60.0, x) for x in pct]
    return out


def write_market(directory, first: str, returns: dict, rf_pct: float = 0.05) -> list:
    """Write the raw files for ``returns`` ({asset: weekly percent returns}); returns the
    ``--data-file`` arguments. The first price of every series is the base (return week 0 of
    the price-based sources is the second key)."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    n = len(next(iter(returns.values())))
    keys = fridays(first, n)
    args = []
    if "stocks" in returns:
        pct = returns["stocks"]
        rows = [(k.strftime("%Y%m%d"), round(r - rf_pct, 2), 0.0, 0.0, rf_pct)
                for k, r in zip(keys, pct)]
        ff = d / "ff_weekly.csv"
        ff.write_text(ff_text(rows), encoding="utf-8")
        px = prices(pct)
        write_csv(d / "stocks_price.csv", ["week_start", "price_index_continuous"],
                  [[(k - dt.timedelta(days=4)).isoformat(), repr(p)] for k, p in zip(keys, px)])
        args += ["--data-file", f"stocks_return={ff}", "--data-file",
                 f"stocks_price={d / 'stocks_price.csv'}"]
    if "gold" in returns:
        px = prices(returns["gold"], 400.0)
        write_csv(d / "gold.csv", ["week_end", "gold_pm_usd"],
                  [[k.isoformat(), repr(p)] for k, p in zip(keys, px)])
        args += ["--data-file", f"gold={d / 'gold.csv'}"]
    if "btc" in returns:
        px = prices(returns["btc"], 50.0)
        write_csv(d / "btc.csv", ["date", "price", "weekly_return", "close_date", "source_week_start"],
                  [[k.isoformat(), repr(p), "", (k + dt.timedelta(days=2)).isoformat(),
                    (k - dt.timedelta(days=4)).isoformat()] for k, p in zip(keys, px)])
        args += ["--data-file", f"btc={d / 'btc.csv'}"]
    return args


# ============================================================================ walk-forward helpers
def engineered(first: str, n: int, default: float, segments=()) -> list:
    """Weekly percent returns: ``default`` everywhere except [(from, to, pct), ...] (inclusive
    Friday keys)."""
    keys = fridays(first, n)
    out = []
    for k in keys:
        v = default
        for a, b, pct in segments:
            if dt.date.fromisoformat(a) <= k <= dt.date.fromisoformat(b):
                v = pct
        out.append(v)
    return out


def wf_config(files, *argv, overrides=None):
    """A walk-forward optimize configuration on synthetic files (as_of fixed)."""
    from src.cli import resolve
    cfg = resolve(["optimize", "--optimization-mode", "walk-forward", "--as-of-date",
                   "2026-09-29", *files, *argv])
    return cfg.with_overrides(overrides) if overrides else cfg


def signal_overrides(assets=ASSETS, **kw) -> dict:
    """{'signals.<asset>.<key>': value} for every asset (ma_length, confirm_off_weeks, ...)."""
    return {f"signals.{a}.{k}": v for a in assets for k, v in kw.items()}


def oos_path_for(cfg, cfgs, assets=None):
    """The walk-forward windows of ``cfg`` and the continuous OOS path in which window i uses
    the configuration ``cfgs[i]`` (its training run on the window's training view provides the
    signal state). Returns (spec, prepared, windows, selections, path)."""
    from src import walk_forward as wf
    from src.app import prepare_run
    spec = wf.resolve_walk_forward(cfg)
    assets = assets or spec.opt.union
    warmup = {a: max((c.signal_params(a) for c in cfgs), key=lambda p: p.minimum_warmup_weeks)
              for a in assets}                      # the largest warm-up of all configurations
    prepared = prepare_run(spec.base, assets=assets, warmup_params=warmup, auto_start=True)
    windows = wf.plan_windows(prepared.inputs.weeks, spec.window_type, spec.train_years,
                              spec.test_years, spec.step)
    sels = [wf.selection_for_config(w.window_id, prepared.training_view(
        w.train_start, w.train_end, w.test_start), c) for w, c in zip(windows, cfgs)]
    path = wf.run_oos_path(prepared, windows, sels, spec.base.get("tax.profile"))
    return spec, prepared, windows, sels, path
