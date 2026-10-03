#!/usr/bin/env python3
"""Verification of the canonical stock-signal and dividend files (SEM-001, SCHEMA-005, SEM-007,
TEST-038; questions Q-004 and Q-008).

Independent from ``work/src``: every fact is recomputed from the staged CSV files and compared
with the specification and with the provenance sidecar ``<file>.provenance.json`` that
documents the construction (raw-input SHA-256 values, methods, rebase, 2026 estimate). The raw
provider files themselves (stkdatd.zip, ie_data.xls, the TradingView export) are not part of
the repository, so their hashes are declared, not re-verified; everything derivable from the
final files is re-derived here.

Output: work/audit/canonical_data_checks.csv (deterministic). Exit code 0 when every check
passes, 1 otherwise.

Usage: python work/tools/verify_canonical_data.py
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import statistics
import sys
from collections import Counter, OrderedDict
from decimal import Decimal, getcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "input" / "data"
OUT = ROOT / "work" / "audit" / "canonical_data_checks.csv"
STOCKS = DATA / "US_STOCK_PRICE_WEEKLY_1885_2026.csv"               # DATA-001
DIVIDEND = DATA / "SPX_dividend_return_weekly_1970_2026.csv"         # DATA-007
SCHWERT_SUPPLEMENTAL = DATA / "US_STOCK_PRICE_WEEKLY_schwert_1919_1962.csv"
STAGED_PROXY = DATA / "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv"

SCHEMA_005 = ["date", "dividend_return", "dividend_points", "spx_close_prev", "spx_close", "year",
              "annual_yield_pct", "trailing_dps_points", "status"]
SPEC_DIVIDEND = ("1970-01-02", "2026-09-18")                          # SEM-007
SPEC_ACTUAL_UNTIL = "2025-12-26"                                      # SEM-007
STOCK_EXPECTED = {"rows": 5569, "first": "1920-01-02", "last": "2026-09-25",
                  "Schwert": ("1920-01-02", "1927-12-30", 418), "SPX": ("1928-01-06", "2026-09-25", 5151)}
DIVIDEND_EXPECTED = {"rows": 2960, "actual": 2922, "estimate": 38}
KNOWN_GAP = ("1933-03-03", "1933-03-17")                              # Q-012
TOL_REL = 1e-9                                                        # TOL_DIVIDEND_REL of the loader
WEEK = dt.timedelta(days=7)


def day(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def table(p: Path):
    text = p.read_bytes().decode("utf-8-sig")
    lines = text.splitlines()
    return next(csv.reader(lines[:1])), list(csv.DictReader(lines))


def monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def fridays_in_year(y: int) -> int:
    d = dt.date(y, 1, 1)
    d += dt.timedelta(days=(4 - d.weekday()) % 7)
    n = 0
    while d.year == y:
        n, d = n + 1, d + WEEK
    return n


class Checks:
    def __init__(self):
        self.rows = []

    def add(self, cid, file, reqs, check, expected, observed, ok):
        self.rows.append(OrderedDict(check_id=cid, file=file, requirement_ids=reqs, check=check,
                                     expected=str(expected), observed=str(observed),
                                     result="PASS" if ok else "FAIL"))


def sidecar(p: Path) -> dict:
    s = p.with_name(p.name + ".provenance.json")
    return json.loads(s.read_text(encoding="utf-8")) if s.is_file() else {}


# ----------------------------------------------------------------------------- stocks
def verify_stocks(c: Checks) -> dict:
    name = STOCKS.name
    meta = sidecar(STOCKS)
    header, rows = table(STOCKS)
    digest = sha256(STOCKS)
    c.add("C-STK-001", name, "REPRO-001;REP-008", "sidecar provenance describes this file",
          "final.sha256 == file SHA-256", digest, meta.get("final", {}).get("sha256") == digest)
    c.add("C-STK-002", name, "SCHEMA-001", "columns",
          "week_end,price_index_continuous,source,source_date", ",".join(header),
          header == ["week_end", "price_index_continuous", "source", "source_date"])
    keys = [day(r["week_end"]) for r in rows]
    prices = [float(r["price_index_continuous"]) for r in rows]
    c.add("C-STK-003", name, "DATA-001;Q-011", "rows and range",
          f"{STOCK_EXPECTED['rows']} rows {STOCK_EXPECTED['first']}..{STOCK_EXPECTED['last']}",
          f"{len(rows)} rows {keys[0]}..{keys[-1]}",
          (len(rows), str(keys[0]), str(keys[-1])) ==
          (STOCK_EXPECTED["rows"], STOCK_EXPECTED["first"], STOCK_EXPECTED["last"]))
    c.add("C-STK-004", name, "NORM-007;NORM-002", "Friday week_end, ascending, unique",
          "all Friday, strictly ascending", dict(Counter(k.strftime("%a") for k in keys)),
          all(k.weekday() == 4 for k in keys) and all(a < b for a, b in zip(keys, keys[1:])))
    gaps = [(str(a), str(b)) for a, b in zip(keys, keys[1:]) if (b - a).days != 7]
    c.add("C-STK-005", name, "NORM-003;Q-012", "only the known common gap (not filled)",
          [KNOWN_GAP], gaps, gaps == [KNOWN_GAP])
    c.add("C-STK-006", name, "NORM-006", "prices finite and > 0", "all", sum(p > 0 for p in prices),
          all(p > 0 and p == p and p != float("inf") for p in prices))
    segs = []
    for r, k in zip(rows, keys):
        if segs and segs[-1][0] == r["source"]:
            segs[-1][2], segs[-1][3] = k, segs[-1][3] + 1
        else:
            segs.append([r["source"], k, k, 1])
    observed = [(s, str(a), str(b), n) for s, a, b, n in segs]
    expected = [("Schwert",) + STOCK_EXPECTED["Schwert"], ("SPX",) + STOCK_EXPECTED["SPX"]]
    c.add("C-STK-007", name, "SEM-001", "source segments (Schwert before 1928, SPX from 1928)",
          expected, observed, observed == expected)
    declared = [(s["source"], s["first_week_end"], s["last_week_end"], s["rows"])
                for s in meta.get("segments", [])]
    c.add("C-STK-008", name, "SEM-001;REP-008", "sidecar segments equal the file segments",
          observed, declared, declared == observed)
    sd = [day(r["source_date"]) for r in rows]
    off = [str(k) for k, s in zip(keys, sd) if monday(s) != monday(k)]
    c.add("C-STK-009", name, "NORM-013;NORM-007", "source_date in the same Monday-Sunday week",
          "0 rows outside", len(off), not off)
    by = dict(zip(keys, prices))
    anchor, first_spx = dt.date(1927, 12, 30), dt.date(1928, 1, 6)
    split = meta.get("construction", {}).get("splice", {})
    getcontext().prec = 30
    factor = Decimal(split.get("anchor_schwert_value", "0")) / Decimal(split.get("anchor_spx_raw_close", "1"))
    c.add("C-STK-010", name, "SEM-001", "rebase factor = anchor Schwert value / anchor SPX raw close",
          split.get("rebase_factor"), f"{factor} (anchor row {by[anchor]!r})",
          str(factor) == split.get("rebase_factor") and repr(by[anchor]) == split.get("anchor_schwert_value")
          and next(r["source"] for r in rows if r["week_end"] == str(anchor)) == "Schwert")
    f = float(factor)
    implied = [p / f for r, p in zip(rows, prices) if r["source"] == "SPX"]
    dev = max(abs(x - round(x, 2)) for x in implied)
    c.add("C-STK-011", name, "SEM-001", "SPX rows = raw close * factor (implied raw close on a "
          "2-decimal grid within TradingView float noise)", "<= 0.0011", f"max {dev:.6f}; first "
          f"SPX week {first_spx}: raw {by[first_spx] / f:.6f}", dev <= 0.0011)
    # Schwert segment versus the supplemental (pre-existing) Schwert derivative.
    _, srows = table(SCHWERT_SUPPLEMENTAL)
    sup = {day(r["week_start"]) + dt.timedelta(days=4): r["price_index_continuous"] for r in srows}
    diff = [r["week_end"] for r in rows if r["source"] == "Schwert"
            and sup.get(day(r["week_end"])) != r["price_index_continuous"]]
    c.add("C-STK-012", name, "SEM-001", "Schwert segment identical to the supplemental Schwert file",
          "0 differing price strings", len(diff), not diff)
    # Alignment of the SPX segment: same-week return correlation with Schwert 1928..1962.
    supf = {k: float(v) for k, v in sup.items()}
    ks = [k for k in keys if first_spx + WEEK <= k <= dt.date(1962, 6, 29)]

    def ret(d, k):
        return d[k] / d[k - WEEK] - 1 if k in d and k - WEEK in d else None

    corr = {}
    for lag in (-1, 0, 1):
        pairs = [(ret(by, k), ret(supf, k + lag * WEEK)) for k in ks]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        corr[lag] = statistics.correlation([a for a, _ in pairs], [b for _, b in pairs])
    c.add("C-STK-013", name, "SEM-001;NORM-018", "SPX weekly returns aligned with Schwert (1928-1962)",
          "same-week corr > 0.9 and > shifted", {k: round(v, 4) for k, v in corr.items()},
          corr[0] > 0.9 and corr[0] > max(corr[-1], corr[1]))
    # The superseded staged proxy carried the same SPX closes after 1962 (constant ratio).
    if STAGED_PROXY.is_file():
        _, prow = table(STAGED_PROXY)
        px = {day(r["week_start"]) + dt.timedelta(days=4): float(r["price_index_continuous"]) for r in prow}
        rat = [by[k] / px[k] for k in keys if k in px and k >= dt.date(1962, 7, 6)]
        c.add("C-STK-014", name, "SEM-001", "same SPX closes as the superseded staged proxy after 1962",
              "constant ratio", f"ratio {min(rat):.12f}..{max(rat):.12f}", max(rat) - min(rat) < 1e-12)
    last = rows[-1]
    c.add("C-STK-015", name, "SEM-011;NORM-019", "last row is the SEM-011 week",
          "week_end 2026-09-25, source_date 2026-09-21", f"{last['week_end']} {last['source_date']}",
          (last["week_end"], last["source_date"]) == ("2026-09-25", "2026-09-21"))
    return by


# ----------------------------------------------------------------------------- dividends
def verify_dividend(c: Checks, stock: dict):
    name = DIVIDEND.name
    meta = sidecar(DIVIDEND)
    header, rows = table(DIVIDEND)
    digest = sha256(DIVIDEND)
    c.add("C-DIV-001", name, "REPRO-001;REP-008", "sidecar provenance describes this file",
          "final.sha256 == file SHA-256", digest, meta.get("final", {}).get("sha256") == digest)
    c.add("C-DIV-002", name, "SCHEMA-005", "exactly the SCHEMA-005 columns", ",".join(SCHEMA_005),
          ",".join(header), header == SCHEMA_005)
    keys = [day(r["date"]) for r in rows]
    c.add("C-DIV-003", name, "SEM-007;TEST-038", "range and rows",
          f"{SPEC_DIVIDEND[0]}..{SPEC_DIVIDEND[1]}, {DIVIDEND_EXPECTED['rows']} rows",
          f"{keys[0]}..{keys[-1]}, {len(rows)} rows",
          (str(keys[0]), str(keys[-1]), len(rows)) == (*SPEC_DIVIDEND, DIVIDEND_EXPECTED["rows"]))
    steps = Counter((b - a).days for a, b in zip(keys, keys[1:]))
    c.add("C-DIV-004", name, "SEM-007;DIV-012;TEST-038", "Friday dates, exact +7 days, no gaps or "
          "duplicates", "all Friday; steps {7}", f"weekdays {dict(Counter(k.strftime('%a') for k in keys))}; "
          f"steps {dict(steps)}", all(k.weekday() == 4 for k in keys) and set(steps) == {7})
    c.add("C-DIV-005", name, "SCHEMA-005", "year == date.year", "all rows",
          sum(int(r["year"]) == k.year for r, k in zip(rows, keys)),
          all(int(r["year"]) == k.year for r, k in zip(rows, keys)))
    st = Counter(r["status"] for r in rows)
    act = [k for r, k in zip(rows, keys) if r["status"] == "actual"]
    est = [k for r, k in zip(rows, keys) if r["status"] == "estimate"]
    c.add("C-DIV-006", name, "SEM-007;DIV-011;DIV-012", "statuses and boundary",
          f"actual {SPEC_DIVIDEND[0]}..{SPEC_ACTUAL_UNTIL} ({DIVIDEND_EXPECTED['actual']}); "
          f"estimate 2026 ({DIVIDEND_EXPECTED['estimate']})",
          f"actual {act[0]}..{act[-1]} ({len(act)}); estimate {est[0]}..{est[-1]} ({len(est)}); "
          f"values {sorted(st)}",
          set(st) == {"actual", "estimate"} and str(act[-1]) == SPEC_ACTUAL_UNTIL and max(act) < min(est)
          and all(k.year == 2026 for k in est) and all(k.year < 2026 for k in act)
          and (len(act), len(est)) == (DIVIDEND_EXPECTED["actual"], DIVIDEND_EXPECTED["estimate"]))
    num = ["dividend_return", "dividend_points", "spx_close_prev", "spx_close", "annual_yield_pct",
           "trailing_dps_points"]
    c.add("C-DIV-007", name, "NORM-006;SCHEMA-005", "numeric fields finite and > 0", "all",
          sum(all(float(r[x]) > 0 for x in num) for r in rows),
          all(float(r[x]) > 0 and float(r[x]) != float("inf") for r in rows for x in num))
    err = max(abs(float(r["dividend_return"]) - float(r["dividend_points"]) / float(r["spx_close_prev"]))
              / float(r["dividend_return"]) for r in rows)
    c.add("C-DIV-008", name, "SCHEMA-005;SEM-007;DIV-012;TEST-038",
          "dividend_return == dividend_points / spx_close_prev", f"rel err <= {TOL_REL}",
          f"max rel err {err!r}", err <= TOL_REL)
    bad_c = [str(k) for r, k in zip(rows, keys) if float(r["spx_close"]) != stock.get(k)]
    bad_p = [str(k) for r, k in zip(rows, keys) if float(r["spx_close_prev"]) != stock.get(k - WEEK)]
    c.add("C-DIV-009", name, "SCHEMA-005;SEM-001", "spx_close / spx_close_prev = canonical stock "
          "signal at date / previous Friday", "exact", f"{len(bad_c)} / {len(bad_p)} mismatches",
          not bad_c and not bad_p and meta.get("stock_signal", {}).get("sha256") == sha256(STOCKS))
    smooth = max(abs(float(r["dividend_points"]) - float(r["trailing_dps_points"]) / fridays_in_year(k.year))
                 / float(r["dividend_points"]) for r, k in zip(rows, keys))
    c.add("C-DIV-010", name, "DIV-009;SEM-007", "dividend_points = trailing_dps_points / Fridays in "
          "the calendar year (spread_annual_dps semantics)", f"rel err <= {TOL_REL}", repr(smooth),
          smooth <= TOL_REL)
    split = sidecar(STOCKS).get("construction", {}).get("splice", {})
    f = float(Decimal(split.get("anchor_schwert_value", "0")) / Decimal(split.get("anchor_spx_raw_close", "1")))
    month_d, month_y = {}, {}
    for r, k in zip(rows, keys):
        month_d.setdefault((k.year, k.month), set()).add(r["trailing_dps_points"])
        month_y.setdefault((k.year, k.month), set()).add(r["annual_yield_pct"])
    const = all(len(v) == 1 for v in month_d.values()) and all(len(v) == 1 for v in month_y.values())
    c.add("C-DIV-011", name, "DIV-009", "trailing D and yield constant within each month (monthly "
          "Shiller fields)", f"{len(month_d)} months", "constant" if const else "varies", const)
    est = meta.get("estimate_2026", {}).get("monthly_ttm_D", {})
    got = {f"{y}-{m:02d}": float(next(iter(v))) / f for (y, m), v in month_d.items()
           if y == 2026 and m >= 6}
    ok = bool(est) and all(abs(got[k] - float(v)) < 1e-9 for k, v in est.items())
    c.add("C-DIV-012", name, "DIV-011;SEM-007", "2026 trailing D (in Shiller units) follows the "
          "documented estimate path", est, {k: round(v, 6) for k, v in sorted(got.items())}, ok)
    q = meta.get("estimate_2026", {})
    arith = (Decimal(q.get("ttm_2026_06", "0")) - Decimal(q.get("q3_2025_actual_dividend_points", "0"))
             + Decimal(q.get("q3_2026_estimate_dividend_points", "0")))
    c.add("C-DIV-013", name, "DIV-011", "TTM Sep 2026 = TTM Jun 2026 - Q3 2025 actual + Q3 2026 "
          "estimate", est.get("2026-09"), str(arith), est.get("2026-09") == str(arith))


def main() -> int:
    c = Checks()
    stock = verify_stocks(c)
    verify_dividend(c, stock)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(c.rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(c.rows)
    bad = [r for r in c.rows if r["result"] != "PASS"]
    for r in bad:
        print(f"FAIL {r['check_id']} {r['check']}: expected {r['expected']}; observed {r['observed']}")
    print(f"Canonical data verification {'PASS' if not bad else 'FAILED'}: {len(c.rows)} checks, "
          f"{len(bad)} failing; {OUT.relative_to(ROOT)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
