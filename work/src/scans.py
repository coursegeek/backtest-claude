"""delay-scan, threshold-scan and rebalance-scan (DELAY-001..005, THR-001..005, REB-010,
REP-010, ALLOC-001/002, Q-026, Q-027): the full resolved grid of complete portfolio runs.

A scan orchestrates the production run pipeline; it is neither a second backtester nor an
optimizer (no objective, ranking, filter or tie-break):
  1. the scan configuration and the whole grid are resolved and validated before any data is
     loaded (grid values, one variant configuration per point, profile constraints Q-037 /
     Q-047, ALLOC-001/002);
  2. the input is prepared once (``app.prepare_run``) with the largest warm-up requirement of
     the grid (NORM-010/ERR-003): one calendar, one market and one set of signal series for
     every grid point; without run.start the first week is the first common week with that
     warm-up;
  3. every grid point is a new ResolvedConfig that differs from the scan configuration only in
     the scanned parameter and runs through ``app.run_prepared`` (fresh portfolio, cost basis,
     signal trackers, tax/foundation state; terminal settlement, pre-tax shadow, metrics);
  4. grid_results.csv holds one row per resolved grid point in grid order (scan columns +
     the SUMMARY_FIELDS row of that run); outputs are written only after every point succeeded.

Scanned parameters:
  delay-scan      SignalParams.delay of --asset (signals.<asset>.delay_weeks), grid
                  optimizer.delay_grid (default 1:4, DELAY-002/003);
  threshold-scan  symmetric threshold p of --asset: threshold_off = threshold_on = p
                  (THR-002), grid optimizer.threshold_grid (decimal; CLI in percent, default
                  1:5:1); delay = the scalar delay of the asset (default 1, THR-003);
  rebalance-scan  portfolio.rebalance = band with portfolio.rebalance_band_pp = grid value in
                  percentage points (REB-010), grid optimizer.rebalance_band_grid (--band-pp).
delay-scan and threshold-scan are single-asset strategies of --asset (ALLOC-002: the asset's
strategic sleeve is 100%, RF only its risk-off reserve); rebalance-scan is a full multi-asset
portfolio run with explicit allocation.targets (ALLOC-001).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .allocation import active_risky_assets, strategic_targets
from .app import (PortfolioRunResult, PreparedRun, check_supported_run, prepare_run,
                  prepared_input_sha256, run_prepared)
from .config import ResolvedConfig, flatten, int_grid, parse_decimal_grid
from .errors import BacktestError, ConfigError
from .manifest import build_manifest, code_version, run_timestamp
from .models import State, ValidationIssue
from .reporting import (NORMALIZED_FIELDS, SUMMARY_FIELDS, VALIDATION_FIELDS, run_directory,
                        summary_row, write_csv, write_json)
from .validation import ValidationReport

SCAN_TYPES = ("delay-scan", "threshold-scan", "rebalance-scan")
SCAN_MANIFEST = "scan_manifest.json"
SHARED_STATEMENT = ("all grid points used one shared prepared data/calendar input: one "
                    "PreparedRun (weeks, market data, signal series, dividends, CPI) loaded, "
                    "validated and aligned once; only the scanned strategy parameter differs")
# grid_results.csv: scan columns, then the complete summary.csv row of the grid point's run.
# delay_weeks / threshold_pct / threshold_decimal / band_pp hold the scanned value (empty when
# the scan does not scan it); the effective parameters of every run stay in the summary part
# (signal_<asset>_*, rebalance_mode, rebalance_band_pp).
SCAN_FIELDS = ["scan_type", "grid_index", "asset", "scanned_parameter", "scanned_value",
               "delay_weeks", "threshold_pct", "threshold_decimal", "band_pp", "status",
               "rebalance_count", "signal_exit_count", "signal_reentry_count"]
GRID_FIELDS = SCAN_FIELDS + list(SUMMARY_FIELDS)
# resolved keys that never influence results (excluded from the strategy hash)
NON_STRATEGY_PREFIXES = ("report.", "config.", "performance.")


class ScanError(BacktestError):
    """A grid point could not be computed; nothing was written (atomic scan)."""

    def __init__(self, scan_type: str, point: "GridPoint", cause: BacktestError):
        self.point, self.cause = point, cause
        self.exit_code = cause.exit_code
        super().__init__(f"{scan_type}: grid point {point.grid_index} ({point.label}) failed: "
                         f"{cause}; no grid_results.csv was written")


# ============================================================================ grid
@dataclass(frozen=True)
class GridPoint:
    grid_index: int                 # 1-based position in the resolved grid (grid order)
    parameter: str                  # the configuration key(s) the point sets
    value: object                   # effective scanned value (weeks | decimal | pp)
    updates: tuple                  # ((dotted key, value), ...) applied to the scan config

    @property
    def label(self) -> str:
        return f"{self.parameter}={self.value}"


@dataclass(frozen=True)
class ScanSpec:
    scan_type: str
    asset: Optional[str]
    parameter: str
    points: tuple                   # GridPoint, grid order
    base: ResolvedConfig            # the scan configuration every point derives from
    configs: tuple                  # one ResolvedConfig per point
    warmup_params: dict             # asset -> SignalParams with the largest warm-up of the grid

    @property
    def resolved_grid(self) -> list:
        return [p.value for p in self.points]


def _unique(values, name) -> list:
    dup = sorted({str(v) for v in values if values.count(v) > 1})
    if dup:
        raise ConfigError(f"{name}: duplicate grid value(s) {dup}")
    if not values:
        raise ConfigError(f"{name}: empty grid")
    return values


def _pct_of(decimal_value: float) -> float:
    """Percent of a decimal threshold computed in decimal arithmetic (0.075 -> 7.5)."""
    return float(Decimal(repr(decimal_value)) * 100)


def scan_base(cfg: ResolvedConfig) -> tuple:
    """(scan configuration, asset). delay-/threshold-scan: single-asset strategy of --asset
    (ALLOC-002, never hidden weights); rebalance-scan: explicit targets (ALLOC-001)."""
    cmd = cfg.command
    if cmd not in SCAN_TYPES:
        raise ConfigError(f"not a scan command: {cmd}")
    asset = cfg.get("run.asset")
    if cmd == "rebalance-scan":
        if asset:
            raise ConfigError("rebalance-scan scans the band of a multi-asset portfolio "
                              "(allocation.targets / --weights, ALLOC-001); --asset is not used")
        strategic_targets(cfg)                          # ALLOC-001
        return cfg, None
    if not asset:
        raise ConfigError(f"{cmd} requires --asset stocks|gold|btc (ALLOC-002)")
    if cfg.get("allocation.targets") is not None:
        raise ConfigError(f"{cmd} --asset {asset} is a single-asset strategy (ALLOC-002: the "
                          "asset's sleeve is 100%, RF only its risk-off reserve); remove "
                          "allocation.targets / --weights")
    single = cfg.get("allocation.single_asset")
    if single not in (None, False) and single != asset:
        raise ConfigError(f"{cmd}: allocation.single_asset={single} conflicts with --asset {asset}")
    return cfg.with_overrides({"allocation.single_asset": asset}), asset


def resolve_scan(cfg: ResolvedConfig) -> ScanSpec:
    """Resolve and validate the whole grid and every variant configuration (no data loaded)."""
    base, asset = scan_base(cfg)
    cmd = base.command
    if cmd == "delay-scan":
        values = _unique(int_grid(base.get("optimizer.delay_grid"), "optimizer.delay_grid"),
                         "optimizer.delay_grid")
        parameter = f"signals.{asset}.delay_weeks"
        updates = [((parameter, v),) for v in values]
    elif cmd == "threshold-scan":
        raw = base.get("optimizer.threshold_grid")
        if raw is None:
            raise ConfigError("threshold-scan: optimizer.threshold_grid is empty")
        values = [float(d) for d in parse_decimal_grid(raw, "optimizer.threshold_grid")]
        for v in values:
            if not 0.0 <= v < 1.0:
                raise ConfigError(f"optimizer.threshold_grid: thresholds are decimal fractions "
                                  f"in [0, 1) (config) / percent on the CLI, got {v!r} (Q-026)")
        values = _unique(values, "optimizer.threshold_grid")
        parameter = f"signals.{asset}.threshold_off=threshold_on"
        updates = [((f"signals.{asset}.threshold_off", v), (f"signals.{asset}.threshold_on", v))
                   for v in values]
    else:
        raw = base.get("optimizer.rebalance_band_grid")
        if raw is None:
            raise ConfigError("rebalance-scan requires --band-pp (optimizer.rebalance_band_grid, "
                              "percentage points, REB-010)")
        values = [float(d) for d in parse_decimal_grid(raw, "optimizer.rebalance_band_grid")]
        for v in values:
            if not 0.0 < v <= 100.0:
                raise ConfigError(f"optimizer.rebalance_band_grid: band in percentage points "
                                  f"must be in (0, 100], got {v!r}")
        values = _unique(values, "optimizer.rebalance_band_grid")
        parameter = "portfolio.rebalance_band_pp"
        updates = [(("portfolio.rebalance", "band"), ("portfolio.rebalance_band_pp", v))
                   for v in values]
    points = tuple(GridPoint(i + 1, parameter, v, tuple(u))
                   for i, (v, u) in enumerate(zip(values, updates)))
    configs = tuple(base.with_overrides(dict(p.updates)) for p in points)
    assets = active_risky_assets(strategic_targets(base))
    base_view = _strategy_view(base)
    for p, c in zip(points, configs):
        try:
            check_supported_run(c)                      # Q-037 / Q-047 before any data
            if active_risky_assets(strategic_targets(c)) != assets:
                raise ConfigError(f"grid point changes the active assets of {cmd}")
            for a in assets:
                c.signal_params(a)
        except BacktestError as e:
            raise ScanError(cmd, p, e) from e
        changed = {k for k in set(base_view) | set(_strategy_view(c))
                   if base_view.get(k) != _strategy_view(c).get(k)}
        if not changed <= {k for k, _ in p.updates}:
            raise ConfigError(f"{cmd}: grid point {p.grid_index} changes {sorted(changed)}")
    warmup = {a: max((c.signal_params(a) for c in configs),
                     key=lambda sp: sp.minimum_warmup_weeks) for a in assets}
    return ScanSpec(cmd, asset, parameter, points, base, configs, warmup)


def _strategy_view(cfg: ResolvedConfig) -> dict:
    return {k: v for k, v in flatten(cfg.resolved_dict()).items()
            if not k.startswith(NON_STRATEGY_PREFIXES)}


# ============================================================================ run
@dataclass
class ScanResult:
    spec: ScanSpec
    prepared: PreparedRun           # the one shared prepared input
    results: tuple                  # PortfolioRunResult per grid point (grid order)
    rows: tuple                     # grid_results.csv rows (grid order)
    fingerprint: str                # prepared_input_sha256 shared by every point
    output_dir: Optional[Path] = None

    @property
    def scan_type(self) -> str:
        return self.spec.scan_type


def signal_transitions(res: PortfolioRunResult) -> tuple:
    """(exits, re-entries): executed changes of the effective state (RISK_ON -> RISK_OFF,
    RISK_OFF -> RISK_ON) of every active asset during the weekly path."""
    e = res.engine
    prev = {a: s.effective_state for a, s in e.pre_start.items()}
    exits = entries = 0
    for w in e.weeks:
        for a, st in w.effective_states.items():
            if st != prev[a]:
                exits += st == State.RISK_OFF
                entries += st == State.RISK_ON
                prev[a] = st
    return exits, entries


def scan_row(spec: ScanSpec, point: GridPoint, cfg: ResolvedConfig,
             res: PortfolioRunResult) -> dict:
    exits, entries = signal_transitions(res)
    row = {"scan_type": spec.scan_type, "grid_index": point.grid_index, "asset": spec.asset,
           "scanned_parameter": point.parameter, "scanned_value": point.value, "status": "ok",
           "rebalance_count": len(res.engine.rebalance_events),
           "signal_exit_count": exits, "signal_reentry_count": entries}
    if spec.scan_type == "delay-scan":
        row["delay_weeks"] = point.value
    elif spec.scan_type == "threshold-scan":
        row["threshold_decimal"] = point.value
        row["threshold_pct"] = _pct_of(point.value)
    else:
        row["band_pp"] = point.value
    summary = summary_row(cfg, res)
    clash = set(row) & set(summary)
    if clash:
        raise KeyError(f"grid_results columns defined twice: {sorted(clash)}")
    row.update(summary)
    return row


def _progress(cfg: ResolvedConfig, text: str) -> None:
    """ERR-005: progress on stderr only (never in the result files)."""
    if cfg.get("performance.progress"):
        print(text, file=sys.stderr, flush=True)


def run_scan(cfg: ResolvedConfig, write: bool = True) -> ScanResult:
    spec = resolve_scan(cfg)
    prepared = prepare_run(spec.base, warmup_params=spec.warmup_params, auto_start=True)
    results, rows = [], []
    n = len(spec.points)
    for point, vcfg in zip(spec.points, spec.configs):
        try:
            res = run_prepared(vcfg, prepared, write=False)
        except BacktestError as e:
            raise ScanError(spec.scan_type, point, e) from e
        results.append(res)
        rows.append(scan_row(spec, point, vcfg, res))
        _progress(cfg, f"{spec.scan_type}: grid point {point.grid_index}/{n} ({point.label}) done")
    out = ScanResult(spec, prepared, tuple(results), tuple(rows), prepared_input_sha256(prepared))
    if write:
        out.output_dir = write_scan_outputs(out)
    return out


# ============================================================================ outputs
def scan_checks(res: ScanResult) -> dict:
    """Audit facts of the shared grid (never a ranking)."""
    p = res.prepared
    rs = res.results
    first = rs[0]
    return {
        "same_prepared_input_object": all(r.prepared is p for r in rs),
        "same_market_data_object": all(r.inputs.market is p.inputs.market for r in rs),
        "same_signal_series_object": all(r.inputs.signal_series is p.inputs.signal_series
                                         for r in rs),
        "same_weeks": all(tuple(w.week_key for w in r.engine.weeks) == p.inputs.weeks for r in rs),
        "same_effective_first_week": all(r.engine.weeks[0].week_key == first.engine.weeks[0].week_key
                                         for r in rs),
        "same_effective_last_week": all(r.engine.weeks[-1].week_key == first.engine.weeks[-1].week_key
                                        for r in rs),
        "same_dropped_weeks": all(r.calendar.dropped == p.calendar.dropped for r in rs),
        "one_row_per_grid_point": len(res.rows) == len(res.spec.points),
    }


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def write_scan_outputs(res: ScanResult) -> Path:
    """<output_dir>/<timestamp>_<run_name>/: grid_results.csv, scan_manifest.json,
    config_resolved.yaml (the scan configuration with the resolved grid), data_manifest.json,
    validation_report.csv and weekly_normalized.csv of the shared input. No per-point
    weekly/trade/tax files. Written after every point succeeded; a failure while writing
    removes the directory."""
    ts = run_timestamp()
    base = res.spec.base
    out = run_directory(base.get("report.output_dir"), base.get("report.run_name"), ts)
    try:
        _write(res, out, ts)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return out


def _write(res: ScanResult, out: Path, ts) -> None:
    spec, prep = res.spec, res.prepared
    base = spec.base
    weeks = prep.inputs.weeks
    write_csv(out / "grid_results.csv", GRID_FIELDS, res.rows)
    (out / "config_resolved.yaml").write_text(base.to_yaml(prep.as_of), encoding="utf-8")
    report = ValidationReport()
    report.extend(prep.issues)
    for point, r in zip(spec.points, res.results):      # issues of the individual runs
        report.extend(ValidationIssue(i.severity, i.code, i.role,
                                      f"[grid point {point.grid_index}: {point.label}] {i.message}",
                                      i.week_key, i.requirement_ids) for i in r.engine.issues)
    write_csv(out / "validation_report.csv", VALIDATION_FIELDS, report.rows())
    write_csv(out / "weekly_normalized.csv", NORMALIZED_FIELDS, prep.normalized)
    data_manifest = build_manifest(prep.provenances, prep.as_of, ts, base.command)
    data_manifest.update({"shared_input": True, "shared_input_statement": SHARED_STATEMENT,
                          "prepared_input_sha256": res.fingerprint,
                          "dividend_mode": prep.dividend_mode,
                          "dropped_incomplete_weeks": prep.dropped_incomplete_weeks})
    write_json(out / "data_manifest.json", data_manifest)
    manifest = {
        "command": base.command, "scan_type": spec.scan_type,
        "spec_version": base.get("app.spec_version"), "code_version": code_version(),
        "run_timestamp": ts.isoformat(), "timezone": "Europe/Warsaw",
        "asset": spec.asset,
        "allocation": ("single-asset strategy of --asset: its sleeve 100%, RF only its risk-off "
                       "reserve (ALLOC-002)" if spec.asset else
                       "explicit allocation.targets (ALLOC-001)"),
        "targets": dict(prep.targets),
        "scanned_parameter": spec.parameter,
        "resolved_grid": spec.resolved_grid,
        "grid_points": [{"grid_index": p.grid_index, "value": p.value,
                         "updates": {k: v for k, v in p.updates}} for p in spec.points],
        "grid_order": "resolved grid order as given (never sorted by a result)",
        "grid_units": {"delay-scan": "weeks (integers)",
                       "threshold-scan": "decimal fractions (CLI percent, Q-026); symmetric "
                                         "threshold_off = threshold_on",
                       "rebalance-scan": "percentage points; portfolio.rebalance = band"}[spec.scan_type],
        "prepared_input_sha256": res.fingerprint,
        "shared_input": True, "shared_input_statement": SHARED_STATEMENT,
        "effective_first_week": weeks[0].isoformat(),
        "effective_last_week": weeks[-1].isoformat(),
        "weeks": len(weeks),
        "first_week_rule": prep.first_week_rule,
        "warmup_weeks": dict(prep.warmup_weeks),
        "requested_start": str(base.start) if base.start else None,
        "requested_end": str(base.end) if base.end else None,
        "common_data_range": [str(x) for x in prep.common_range],
        "dropped_weeks": [w.isoformat() for w in prep.calendar.dropped],
        "dropped_incomplete_weeks": prep.dropped_incomplete_weeks,
        "as_of_date": prep.as_of.isoformat(),
        "tax_profile": base.get("tax.profile"),
        "sources": [{"role": p.role, "path": p.path, "sha256": p.sha256}
                    for p in sorted(prep.provenances, key=lambda p: p.role)],
        "base_strategy_sha256": _sha(_strategy_view(base)),
        "checks": scan_checks(res),
        "outputs": {"grid_results": "grid_results.csv (one row per resolved grid point: scan "
                                    "columns + the SUMMARY_FIELDS row of the point's run)",
                    "shared": ["config_resolved.yaml", "data_manifest.json",
                               "validation_report.csv", "weekly_normalized.csv"],
                    "per_point_files": "none (weekly/trade/tax files of grid points are not "
                                       "written; use 'run' for a single point)"},
        "status_semantics": "every row has status=ok; a failing grid point fails the whole scan "
                            "without output (optimizer rejection statuses are separate)",
        "no_ranking": "a scan reports the full grid; no objective, ranking or winner",
    }
    write_json(out / SCAN_MANIFEST, manifest)
