"""Command implementations behind the CLI and the import API.

Implemented in this build: ``signals`` (signal-only analysis, NORM-012/NORM-020). Portfolio
commands resolve and validate their configuration (ALLOC-001/002) and then stop with a
clear NotImplementedCommand until the portfolio engine exists.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .allocation import active_risky_assets, strategic_targets
from .calendar import first_key_on_or_after, last_key_on_or_before
from .config import ResolvedConfig
from .data_loader import load_role
from .errors import ConfigError, NotImplementedCommand
from .manifest import build_manifest, run_timestamp
from .models import RISKY_ASSETS, canonical_assets
from .reporting import (NORMALIZED_FIELDS, SIGNAL_FIELDS, VALIDATION_FIELDS, normalized_price_rows,
                        run_directory, signal_rows, write_csv, write_json)
from .signal_analysis import evaluate, reconstruct
from .validation import ValidationReport, apply_completion, check_warmup, gap_issues

SIGNAL_ROLE = {"stocks": "stocks_price", "gold": "gold", "btc": "btc"}


@dataclass
class SignalRunResult:
    records: tuple
    report: ValidationReport
    provenances: tuple
    first_weeks: dict
    as_of: dt.date
    dropped_incomplete_weeks: int
    output_dir: Optional[Path] = None
    series: dict = field(default_factory=dict)


def signal_assets(cfg: ResolvedConfig) -> tuple:
    if cfg.get("run.asset"):
        return (cfg.get("run.asset"),)
    if cfg.get("allocation.single_asset") not in (None, False):
        return (cfg.get("allocation.single_asset"),)
    if cfg.get("allocation.targets") is not None:
        return active_risky_assets(strategic_targets(cfg))
    return RISKY_ASSETS


def load_signal_series(cfg, asset, as_of, report):
    series = load_role(cfg, SIGNAL_ROLE[asset])
    report.add_provenance(series.provenance)
    kept, dropped = apply_completion(series.points, series.role, as_of, cfg.end)
    report.extend(dropped)
    series = series.replace_points(kept)
    report.extend(gap_issues(series.role, series.keys()))
    return series, len(dropped)


def run_signals(cfg: ResolvedConfig, write: bool = True) -> SignalRunResult:
    """Signal-only analysis: whole available history for state reconstruction, completed
    weeks only (NORM-019/020), records reported from the first analysed week."""
    as_of = cfg.as_of()
    report = ValidationReport()
    records, provs, firsts, all_series = [], [], {}, {}
    dropped_total = 0
    for asset in canonical_assets(signal_assets(cfg)):
        params = cfg.signal_params(asset)
        series, dropped = load_signal_series(cfg, asset, as_of, report)
        dropped_total += dropped
        keys = series.keys()
        if cfg.start:
            first = first_key_on_or_after(cfg.start)
        else:
            need = params.minimum_warmup_weeks
            if len(keys) <= need:
                first = keys[-1] + dt.timedelta(days=7) if keys else None
            else:
                first = keys[need]
        if first is None:
            raise ConfigError(f"{asset}: no signal data")
        warn = check_warmup(asset, keys, first, params, cfg.get("signal.initial_state"))
        if warn:
            report.add(warn)
        report.extend(reconstruct(series, params, first).issues)
        last = last_key_on_or_before(cfg.end) if cfg.end else None
        recs, _, _ = evaluate(series, params)
        records += [r for r in recs if r.week_key >= first and (last is None or r.week_key <= last)]
        provs.append(series.provenance)
        firsts[asset] = first
        all_series[asset] = series
    result = SignalRunResult(tuple(records), report, tuple(provs), firsts, as_of, dropped_total,
                             series=all_series)
    if write:
        result.output_dir = write_signal_outputs(cfg, result)
    return result


def write_signal_outputs(cfg: ResolvedConfig, result: SignalRunResult) -> Path:
    ts = run_timestamp()
    out = run_directory(cfg.get("report.output_dir"), cfg.get("report.run_name"), ts)
    (out / "config_resolved.yaml").write_text(cfg.to_yaml(result.as_of), encoding="utf-8")
    write_csv(out / "signals.csv", SIGNAL_FIELDS, signal_rows(result.records))
    write_csv(out / "validation_report.csv", VALIDATION_FIELDS, result.report.rows())
    rows = []
    for asset in canonical_assets(result.series):
        rows += normalized_price_rows(result.series[asset])
    write_csv(out / "weekly_normalized.csv", NORMALIZED_FIELDS, rows)
    manifest = build_manifest(result.provenances, result.as_of, ts, cfg.command)
    manifest["dropped_incomplete_weeks"] = result.dropped_incomplete_weeks
    manifest["first_analysed_week"] = {a: k.isoformat() for a, k in sorted(result.first_weeks.items())}
    write_json(out / "data_manifest.json", manifest)
    return out


def dispatch(cfg: ResolvedConfig):
    cmd = cfg.command
    if cmd == "signals":
        return run_signals(cfg)
    if cmd in ("delay-scan", "threshold-scan"):
        if not cfg.get("run.asset"):
            raise ConfigError(f"{cmd} requires --asset stocks|gold|btc (ALLOC-002)")
    elif cmd in ("run", "tax-compare", "rebalance-scan"):
        strategic_targets(cfg)          # ALLOC-001: fail early without explicit targets
    raise NotImplementedCommand(
        f"command '{cmd}': configuration resolved and validated, but the portfolio engine is "
        "not implemented in this build yet (foundation stage); use 'signals' or --print-config")
