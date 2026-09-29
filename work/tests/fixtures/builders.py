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
