"""Synthetic fixture builders (all values explicit; no V1 data)."""
from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path

from src.models import PricePoint, PriceSeries, Provenance

REPO = Path(__file__).resolve().parents[3]
STAGED = REPO / "input" / "data"
WEEK = dt.timedelta(days=7)


def d(s):
    return dt.date.fromisoformat(s)


def provenance(role="test"):
    return Provenance(role=role, path="<memory>", sha256="0" * 64, config_key="test",
                      adapter="test", canonical=True, raw_rows=0, used_rows=0,
                      raw_first_date=None, raw_last_date=None, first_key=None, last_key=None,
                      date_convention="friday_key")


def price_series(prices, first_key="2000-01-07", role="stocks_price", skip=(), avail_days=0):
    """Weekly Friday-keyed series; indexes in ``skip`` are missing calendar weeks."""
    k0 = d(first_key) if isinstance(first_key, str) else first_key
    pts = []
    for i, p in enumerate(prices):
        if i in skip:
            continue
        k = k0 + i * WEEK
        pts.append(PricePoint(k, float(p), k + dt.timedelta(days=avail_days), k))
    return PriceSeries(role, tuple(pts), provenance(role))


def random_walk(n, seed, start=100.0, vol=0.03):
    rng = random.Random(seed)
    out, p = [], start
    for _ in range(n):
        p *= 1.0 + rng.gauss(0.0005, vol)
        out.append(round(p, 6))
    return out


def write_csv(path, header, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    return Path(path)


def ff_text(rows, preamble=True, footer=True, crlf=True):
    lines = []
    if preamble:
        lines += ["This file was created using the 202608 CRSP database.",
                  "The Tbill return is the weekly rate that, over four weeks, ",
                  "compounds to 1-month TBill rate.", ""]
    lines.append(",Mkt-RF,SMB,HML,RF")
    for r in rows:
        lines.append(",".join([r[0]] + [f"{x:8.2f}" for x in r[1:]]))
    if footer:
        lines += ["", "Copyright 2026 Eugene F. Fama and Kenneth R. French"]
    return ("\r\n" if crlf else "\n").join(lines) + ("\r\n" if crlf else "\n")


def params_for(asset, **kw):
    from src.models import SignalParams
    base = dict(asset=asset, ma=3, threshold_off=0.0, threshold_on=0.0, confirm_off=1,
                confirm_on=1, delay=1, sell_fraction=0.5, risk_off_action="sell_fraction_current")
    base.update(kw)
    return SignalParams(**base)


def engine_inputs(histories, first, targets, returns=None, rf=0.0, params=None, costs=None,
                  first_key="2000-01-07", trace=True, capital=1_000_000.0):
    """Synthetic EngineInputs.

    histories: asset -> full weekly signal price list starting at ``first_key``;
    first: index of the first run week; returns: asset -> list of run-week returns (default:
    price ratios of the history); rf: scalar or run-week list."""
    from src.costs import CostModel
    from src.engine import EngineInputs, WeekMarket
    series = {a: price_series(p, first_key=first_key, role=a) for a, p in histories.items()}
    keys = next(iter(series.values())).keys()
    weeks = keys[first:]
    market = {}
    for i, w in enumerate(weeks):
        rets = {}
        for a, p in histories.items():
            if returns and a in returns:
                rets[a] = returns[a][i]
            else:
                rets[a] = p[first + i] / p[first + i - 1] - 1.0
        market[w] = WeekMarket(w, rets, rf[i] if isinstance(rf, (list, tuple)) else rf)
    full_targets = {"stocks": 0.0, "gold": 0.0, "btc": 0.0, "rf": 0.0}
    full_targets.update(targets)
    prm = {a: (params or {}).get(a, params_for(a)) for a in histories}
    return EngineInputs(weeks=tuple(weeks), market=market, signal_series=series, params=prm,
                        targets=full_targets, initial_capital=capital,
                        costs=costs or CostModel(), trace=trace)
