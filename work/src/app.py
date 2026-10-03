"""Command implementations behind the CLI and the import API.

Implemented in this build:
  * ``signals``     - signal-only analysis (NORM-012/NORM-020);
  * ``run``         - portfolio backtest for every tax.profile (none, individual_pl,
                      family_foundation_15/19 with tax_event=terminal) and every
                      portfolio.rebalance mode, with terminal settlement, pre-tax shadow run,
                      metrics and summary.csv;
  * ``tax-compare`` - orchestration of the run pipeline for several tax profiles on one shared
                      prepared input (``tax_compare.py``, TAX-003, FND-008, Q-023);
  * ``delay-scan``, ``threshold-scan``, ``rebalance-scan`` - the full grid of runs of one
                      scanned strategy parameter on one shared prepared input (``scans.py``).
A run is split into data preparation (``prepare_run``: load, validate and align every source,
CPI window included) and execution (``run_prepared``: engine, terminal settlement, pre-tax
shadow, metrics, outputs); execution never reloads or realigns data.
  * ``optimize``    - in-sample weight-grid optimizer (``optimizer.py``, OPT-001..010, Q-041)
                      and walk-forward (``walk_forward.py``, WF-001..016, Q-022).
Foundations support tax_event terminal and distribution_schedule (Q-037) and a non-zero
internal trading tax in terminal mode (Q-047); their undefined combination is a ConfigError.
Unsupported features stop with a clear error before any output; no partial results are produced.
"""
from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .allocation import active_risky_assets, strategic_targets
from .calendar import (common_range, elapsed_days, first_key_on_or_after, first_return_week,
                       inception_date, last_key_on_or_before, last_return_week)
from .config import ResolvedConfig
from .data_loader import load_distribution_schedule, load_dividend_cash, load_role
from .errors import (BacktestError, ConfigError, DataFileNotFound, DataValidationError,
                     DividendModeError, InsufficientHistoryError, NotImplementedCommand,
                     WarmupError)
from .manifest import build_manifest, run_timestamp
from .costs import CostModel
from .engine import ComposedHooks, EngineInputs, WeekMarket, run_engine
from .rebalancing import hooks_from_config
from .metrics import (ROLLING_WINDOW_START_RULE, PathSeries, RollingMetrics, RunMetrics,
                      compute_run_metrics, cpi_window, rolling_metrics)
from .foundation import (FOUNDATION_PROFILES, SCHEDULE, FoundationHooks, FoundationParams,
                         FoundationState, map_distribution_schedule)
from .settlement import (ShadowCostSettlement, TerminalSettlementResult, settle_foundation,
                         settle_foundation_shadow_costs, settle_terminal)
from .tax import IndividualTaxHooks, TaxState, tax_hooks_from_config
from .models import RISKY_ASSETS, Severity, ValidationIssue, canonical_assets
from .reporting import (SUMMARY_FIELDS, summary_row,
                        DISTRIBUTION_FIELDS, DIVIDEND_FIELDS, NORMALIZED_FIELDS, PAYMENT_FIELDS, REALIZATION_FIELDS,
                        REBALANCE_FIELDS, ROLLING_FIELDS, SIGNAL_FIELDS, TAX_EVENT_FIELDS,
                        TRADE_FIELDS, TRANSFER_FIELDS, VALIDATION_FIELDS, dividend_rows,
                        rebalance_rows, realization_rows, record_rows, rolling_rows,
                        tax_event_rows, terminal_settlement_doc, distribution_rows,
                        normalized_price_rows, normalized_return_rows, run_directory,
                        signal_rows, trade_rows, weekly_portfolio_fields, weekly_portfolio_rows,
                        write_csv, write_json)
from .signal_analysis import evaluate, reconstruct
from .validation import (ValidationReport, apply_completion, apply_price_carry, build_run_calendar,
                         check_dividend_mode, check_warmup, gap_issues, validate_dividend_series)

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


def initial_state_manifest(cfg: ResolvedConfig, report) -> dict:
    """Q-013: configured initial-state policy and every asset that started from the explicit
    RISK_ON opt-in because its warm-up was short (also a validation_report.csv warning)."""
    return {"signal_initial_state": cfg.get("signal.initial_state"),
            "initial_state_fallback": [{"asset": i.role, "first_week": str(i.week_key),
                                        "message": i.message}
                                       for i in report.issues if i.code == "warmup_short"]}


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
    manifest.update(initial_state_manifest(cfg, result.report))
    write_json(out / "data_manifest.json", manifest)
    return out


# ============================================================================ run
@dataclass
class PortfolioRunResult:
    engine: object
    report: ValidationReport
    provenances: tuple
    first_week: dt.date
    last_week: dt.date
    as_of: dt.date
    targets: dict
    dropped_incomplete_weeks: int
    calendar: object
    output_dir: Optional[Path] = None
    normalized: tuple = ()
    dividend_mode: str = "none"
    common_range: tuple = ()                        # NORM-011 (effective_start, effective_end)
    truncations: tuple = ()                         # (role, side, own bound, effective bound)
    tax_state: Optional[TaxState] = None            # weekly (pre-terminal) tax state
    tax_params: Optional[object] = None
    terminal: Optional[TerminalSettlementResult] = None   # separate from the weekly path
    pre_tax: Optional["PreTaxRun"] = None           # Q-015 pre-tax path (shadow or actual)
    metrics: Optional[RunMetrics] = None
    inputs: Optional[EngineInputs] = None
    cpi_series: Optional[object] = None
    prepared: Optional["PreparedRun"] = None        # the shared prepared input of this run
    walk_forward: Optional[dict] = None             # summary fields of a stitched OOS path
    targets_by_week: Optional[dict] = None          # walk-forward: targets in force per week
    assets: Optional[tuple] = None                  # walk-forward: union of the OOS assets
    rolling: Optional[RollingMetrics] = None        # MET-022/023, computed when outputs are written


@dataclass(frozen=True)
class PreparedRun:
    """Everything a portfolio run derives from data, prepared once (NORM-011, NORM-019, Q-015,
    Q-023): aligned EngineInputs (immutable), calendar, dropped weeks, common range, dividend
    observations/status, CPI window, provenances and the shared validation issues. The run
    pipeline (``run_prepared``) never reloads or realigns data; ``tax-compare`` passes the same
    object to every profile."""
    inputs: EngineInputs
    issues: tuple                   # shared validation issues (data, calendar, dividends, CPI)
    provenances: tuple              # every loaded source (CPI included when available)
    first_week: dt.date
    last_week: dt.date
    as_of: dt.date
    targets: dict
    dropped_incomplete_weeks: int
    calendar: object
    normalized: tuple
    dividend_mode: str              # dividend data carried by ``inputs``: none|smoothed_weekly|exact
    common_range: tuple
    truncations: tuple
    cpi_series: Optional[object] = None
    cpi_window: Optional[object] = None
    warmup_weeks: dict = field(default_factory=dict)   # asset -> verified warm-up requirement
    first_week_rule: str = "run.start"
    distribution_schedule: Optional[object] = None     # Q-037 foundation schedule (exogenous)

    @property
    def inception(self) -> dt.date:
        return inception_date(self.first_week)

    def inputs_for(self, cfg: ResolvedConfig) -> EngineInputs:
        """EngineInputs of one configuration on this prepared input: the data objects (weeks,
        market, signal series) are shared, only the strategy fields (signal parameters of the
        configuration's active assets, targets, capital, costs, cost basis) come from ``cfg``;
        the prepared object itself is returned when they are identical. The active assets
        must be prepared (a subset of an optimizer's asset union is fine) and must not need
        more warm-up than was verified."""
        targets = strategic_targets(cfg)
        assets = active_risky_assets(targets)
        prepared_assets = canonical_assets(self.inputs.params)
        if not set(assets) <= set(prepared_assets):
            raise ConfigError(f"configuration uses assets {assets}, the prepared input holds "
                              f"only {prepared_assets}")
        params = {a: cfg.signal_params(a) for a in assets}
        if cfg.get("signal.initial_state") != "RISK_ON":
            for a in assets:
                if params[a].minimum_warmup_weeks > self.warmup_weeks.get(a, 0):
                    raise ConfigError(f"{a}: the configuration needs {params[a].minimum_warmup_weeks}"
                                      f" warm-up weeks but the prepared input verified "
                                      f"{self.warmup_weeks.get(a, 0)} (prepare with the largest "
                                      "grid requirement, NORM-010)")
        strategy = dict(params=params, targets=targets,
                        initial_capital=float(cfg.get("portfolio.initial_capital_pln")),
                        costs=CostModel.from_config(cfg),
                        cost_basis_method=cfg.get("tax.individual.cost_basis"))
        if all(getattr(self.inputs, k) == v for k, v in strategy.items()):
            return self.inputs
        return dataclasses.replace(self.inputs, **strategy)

    def training_view(self, first_week: dt.date, last_week: dt.date,
                      cutoff: dt.date) -> "PreparedRun":
        """Walk-forward training input (Q-022, WF-004, WF-014, META-003): a PreparedRun that
        physically holds only information known before ``cutoff`` (the OOS test start):
        retained weeks in [first_week, last_week] (all < cutoff), their WeekMarket records
        (returns, RF, dividends), signal observations with week_key and available_at before
        ``cutoff`` (the whole earlier history stays for warm-up/reconstruction), normalized rows
        and validation issues before ``cutoff``, and no CPI (real metrics are no objective and
        a later CPI month would be future information)."""
        if not last_week < cutoff:
            raise ValueError(f"training window end {last_week} must be before {cutoff}")
        i = self.inputs
        weeks = tuple(w for w in i.weeks if first_week <= w <= last_week)
        if not weeks:
            raise ValueError(f"empty training window {first_week}..{last_week}")
        before = lambda k: k is None or k < cutoff                          # noqa: E731
        series = {a: s.replace_points(tuple(p for p in s.points
                                            if p.week_key < cutoff and p.available_at < cutoff))
                  for a, s in i.signal_series.items()}
        inputs = dataclasses.replace(i, weeks=weeks, market={w: i.market[w] for w in weeks},
                                     signal_series=series, run_start=weeks[0])
        cal = self.calendar
        inside = lambda w: weeks[0] <= w <= weeks[-1]                        # noqa: E731
        calendar = dataclasses.replace(
            cal, weeks=weeks, common_gaps=tuple(w for w in cal.common_gaps if inside(w)),
            dropped=tuple(w for w in cal.dropped if inside(w)),
            carried=tuple((r, w) for r, w in cal.carried if inside(w)),
            issues=tuple(x for x in cal.issues if before(x.week_key)))
        return dataclasses.replace(
            self, inputs=inputs, issues=tuple(x for x in self.issues if before(x.week_key)),
            first_week=weeks[0], last_week=weeks[-1], calendar=calendar,
            normalized=tuple(r for r in self.normalized if r["week_key"] < cutoff
                             and (r.get("available_at") is None or r["available_at"] < cutoff)),
            common_range=(self.common_range[0], weeks[-1]), truncations=(),
            cpi_series=None, cpi_window=None,
            first_week_rule=f"walk-forward training window {weeks[0]}..{weeks[-1]}")

    def max_information_week(self) -> dt.date:
        """Latest week of any information held (retained weeks, market records, signal
        observations and their availability, normalized rows) - the no-lookahead audit."""
        i = self.inputs
        keys = list(i.weeks) + list(i.market)
        for s in i.signal_series.values():
            keys += [p.week_key for p in s.points] + [p.available_at for p in s.points]
        keys += [r["week_key"] for r in self.normalized]
        return max(keys)

    def context(self) -> dict:
        """PortfolioRunResult fields shared by every run of this prepared input; the
        validation report is a fresh object (shared issues + the run's own engine issues)."""
        report = ValidationReport()
        report.extend(self.issues)
        return dict(report=report, provenances=self.provenances, first_week=self.first_week,
                    last_week=self.last_week, as_of=self.as_of, targets=self.targets,
                    dropped_incomplete_weeks=self.dropped_incomplete_weeks,
                    calendar=self.calendar, normalized=self.normalized,
                    dividend_mode=self.dividend_mode, common_range=self.common_range,
                    truncations=self.truncations)


@dataclass(frozen=True)
class PreTaxRun:
    """Q-015: pre-tax path of a run. individual_pl: a second central-engine run on the
    identical EngineInputs object (same weeks, WeekMarket objects, signals, targets, capital,
    costs, rebalancing configuration, dividend data) with TaxParams.zero_rates(); only the
    taxation policy differs. tax.profile=none: the actual run itself (no taxes exist)."""
    method: str                 # shadow_zero_tax | actual_run_no_taxes
    engine: object
    inputs: EngineInputs
    note: str
    terminal_cost: Optional[ShadowCostSettlement] = None   # foundation final-year admin cost

    @property
    def final_wealth(self) -> float:
        """MET-001: final NAV of the weekly pre-tax path; for foundations after the final-year
        admin cost (a cost, not a tax) paid outside the weekly path."""
        return self.terminal_cost.final_wealth if self.terminal_cost else self.engine.final_ledger.nav


def check_supported_run(cfg: ResolvedConfig) -> None:
    """Fail clearly instead of producing partial results for unimplemented features: foundation
    parameters are validated before any data is loaded (distribution_schedule -> Q-037,
    internal trading tax > 0 -> Q-047)."""
    if cfg.get("tax.profile") in FOUNDATION_PROFILES:
        FoundationParams.from_config(cfg)


def dividend_mode_of(cfg: ResolvedConfig, assets) -> str:
    """Dividend taxation applies to the stock sleeve of a taxed profile (DIV-001, DIV-004)."""
    mode = cfg.get("tax.dividend_tax_mode")
    if cfg.get("tax.profile") == "none" or "stocks" not in assets or mode == "off":
        return "none"
    return mode


def load_dividend_input(cfg: ResolvedConfig, mode: str, report: ValidationReport):
    """DIV-001, DIV-010, DIV-011, DIV-012, ERR-004, Q-008, Q-036: the supplied dividend return
    series (never inferred from Fama-French minus SPX, DIV-008)."""
    cash = cfg.get("data.dividend_cash_file")
    if mode == "exact":
        check_dividend_mode(mode, cfg.get("tax.profile"), False, cash)
        path = Path(cash)
        if not path.is_absolute() and not path.is_file():
            path = Path(cfg.get("data.dir")) / path
        series = load_dividend_cash(path)
        report.add_provenance(series.provenance)
        return series, list(series.points)
    try:
        series = load_role(cfg, "dividend")
    except DataFileNotFound as e:                                        # ERR-004
        raise DividendModeError("dividend_tax_mode=smoothed_weekly requires the smoothed "
                                f"dividend_return file (data.dividend_file): {e}") from e
    report.add_provenance(series.provenance)
    pts = list(series.points)
    if cfg.get("tax.dividend_estimate_policy") == "actual_only":
        pts = [p for p in pts if p.status == "actual"]
        if not pts:
            raise DividendModeError("tax.dividend_estimate_policy=actual_only: the dividend file "
                                    "has no rows with status=actual (DIV-011)")
    return series, pts


def dividend_status_issues(cfg, weeks, div) -> list:
    """DIV-011: estimate rows used by the run are an error or a warning per contiguous block."""
    est = [w for w in weeks if div[w].status != "actual"]
    if not est:
        return []
    if cfg.get("tax.dividend_estimate_policy") == "error_on_estimate":
        raise DividendModeError(f"tax.dividend_estimate_policy=error_on_estimate: {len(est)} run "
                                f"week(s) use estimated dividends, first {est[0]} (DIV-011)")
    blocks, start, prev = [], est[0], est[0]
    for w in est[1:] + [None]:
        if w is None or (w - prev).days != 7:
            blocks.append((start, prev))
            if w is not None:
                start = w
        if w is not None:
            prev = w
    return [ValidationIssue(Severity.WARNING, "dividend_estimate", "dividend",
                            f"dividend status=estimate used for {a}..{b} "
                            "(tax.dividend_estimate_policy=allow_with_warning)", a, "DIV-011")
            for a, b in blocks]


def build_run(cfg: ResolvedConfig, dividend_mode: Optional[str] = None,
              warmup_params: Optional[dict] = None, auto_start: bool = False,
              assets: Optional[tuple] = None):
    """Load, validate and align every source of a portfolio run; returns EngineInputs and
    the run context (NORM-011, NORM-019, Q-012, Q-014). ``dividend_mode`` overrides the data
    requirement of the configured profile: ``tax-compare`` passes the superset requirement of
    all compared profiles so that every profile shares one calendar (Q-023).
    ``warmup_params`` (asset -> SignalParams) replaces the configured parameters in the warm-up
    requirement (NORM-010/ERR-003): a scan passes the largest requirement of its grid so that
    one calendar is valid for every grid point. Without run.start (Q-025, every command) the
    first return week is the first week of the common range at which every asset has that
    warm-up (never a later start for only some grid points); ``auto_start`` is accepted for
    the callers that always asked for it and changes nothing. An explicit run.start before the
    stocks return history of an active stocks sleeve is an error (TEST-023). ``assets`` (optimize): the
    union of the risky assets of every candidate; every source of the union bounds the one
    common calendar of all candidates and the prepared input carries no strategic targets
    (each candidate brings its own, ``PreparedRun.inputs_for``)."""
    check_supported_run(cfg)
    if assets is None:
        targets = strategic_targets(cfg)
        assets = active_risky_assets(targets)
    else:
        targets = None
        assets = canonical_assets(assets)
    as_of = cfg.as_of()
    report = ValidationReport()
    dropped = 0
    ff = load_role(cfg, "stocks_return")
    report.add_provenance(ff.provenance)
    ff_stock, d1 = apply_completion(ff.stock_total.points, "stocks_return", as_of, cfg.end)
    ff_rf, _ = apply_completion(ff.rf.points, "rf", as_of, cfg.end)
    report.extend(d1)
    dropped += len(d1)
    report.extend(gap_issues("stocks_return", [p.week_key for p in ff_stock]))
    series, ranges = {}, {"stocks_return": (ff_stock[0].week_key, ff_stock[-1].week_key)}
    for a in assets:
        s, n = load_signal_series(cfg, a, as_of, report)
        dropped += n
        series[a] = s
        keys = s.keys()
        if len(keys) < 2:
            raise ConfigError(f"{a}: not enough price history")
        # price-based return sources need a previous price for their first return week
        ranges[SIGNAL_ROLE[a]] = (keys[0] if a == "stocks" else keys[1], keys[-1])
    div_mode = dividend_mode_of(cfg, assets) if dividend_mode is None else dividend_mode
    div_series, div_points = (None, [])
    if div_mode != "none":
        div_series, div_points = load_dividend_input(cfg, div_mode, report)
        if div_mode == "smoothed_weekly":                              # a required source
            # NORM-019 like every other source: weeks after min(end, as_of) are not part of
            # its range (otherwise a file reaching past run.end reports a false truncation)
            kept, d_div = apply_completion(div_points, "dividend", as_of, cfg.end)
            if kept:
                div_points = kept
                report.extend(d_div)
                dropped += len(d_div)
            ranges["dividend"] = (div_points[0].week_key, div_points[-1].week_key)
    start, end, truncations = common_range(ranges)
    for role, side, own, eff in truncations:
        report.add(ValidationIssue(Severity.WARNING, "range_truncated", role,
                                   f"{side} truncated from {own} to common {eff} (NORM-011)", eff,
                                   "NORM-011"))
    params = {a: cfg.signal_params(a) for a in assets}
    wparams = dict(params, **(warmup_params or {}))
    initial_state = cfg.get("signal.initial_state")
    check_stock_return_start(cfg, assets, ff_stock, ff.provenance)
    first = first_return_week(cfg.start, start)
    first_rule = "run.start" if cfg.start else "common range start"
    if cfg.start is None and initial_state != "RISK_ON":               # Q-025
        first = first_warmup_week(ff_stock, start, end, {a: series[a].keys() for a in assets},
                                  {a: wparams[a].minimum_warmup_weeks for a in assets})
        first_rule = ("first common week with the complete signal warm-up of every asset"
                      + (" and grid point" if warmup_params else "") + " (Q-025)")
    last = last_return_week(cfg.end, end)
    if div_mode == "smoothed_weekly":
        for i in validate_dividend_series(div_series, first):
            if i not in div_series.provenance.issues:
                report.add(i)
        errs = [i for i in div_series.provenance.issues if i.severity == Severity.ERROR
                and i.week_key is not None and first <= i.week_key <= last]
        if errs:
            raise DataValidationError(f"dividend: {len(errs)} validation error(s) in the run range, "
                                      f"first: {errs[0].message} (DIV-012)")
    if cfg.end and last_key_on_or_before(cfg.end) > end:
        report.add(ValidationIssue(Severity.WARNING, "end_truncated", "calendar",
                                   f"requested end {cfg.end} beyond common data end {end}",
                                   end, "NORM-011;RUN-002"))
    if first > last:
        raise ConfigError(f"empty backtest range: first week {first} after last week {last}")
    for a in assets:
        try:
            warn = check_warmup(a, series[a].keys(), first, wparams[a], initial_state)
        except WarmupError as e:
            raise WarmupError(e.asset, e.available, e.required, e.first_week,
                              data_blocker_note(series[a].provenance)) from None
        if warn:
            report.add(warn)
    sources = {"stocks_return": [p.week_key for p in ff_stock]}
    sources.update({SIGNAL_ROLE[a]: series[a].keys() for a in assets})
    cal = build_run_calendar(first, last, sources, {"stocks_return"},
                             {SIGNAL_ROLE[a] for a in assets},
                             cfg.get("missing.return_policy"), cfg.get("missing.price_policy"))
    div = {p.week_key: p for p in div_points}
    if div_mode == "smoothed_weekly":
        missing = [w for w in cal.weeks if w not in div]
        if missing and cfg.get("missing.return_policy") != "drop":
            raise DataValidationError(f"dividend: missing dividend_return for run week {missing[0]} "
                                      f"({len(missing)} week(s); missing.return_policy=error, NORM-004)")
        if missing:
            gone = set(missing)
            cal = dataclasses.replace(
                cal, weeks=tuple(w for w in cal.weeks if w not in gone),
                dropped=tuple(sorted(set(cal.dropped) | gone)),
                carried=tuple((r, w) for r, w in cal.carried if w not in gone),
                issues=cal.issues + tuple(
                    ValidationIssue(Severity.WARNING, "week_dropped", "dividend",
                                    f"missing dividend_return for week {w}; dropped "
                                    "(missing.return_policy=drop)", w, "NORM-004") for w in missing))
        report.extend(dividend_status_issues(cfg, list(cal.weeks), div))
    elif div_mode == "exact":
        off = [w for w in div if first <= w <= last and w not in cal.weeks]
        report.extend(ValidationIssue(Severity.WARNING, "dividend_off_calendar", "dividend_cash",
                                      f"cash dividend in week {w} not on the run calendar; not "
                                      "applied", w, "DIV-010") for w in off)
    report.extend(cal.issues)
    for a in assets:
        series[a] = apply_price_carry(series[a], [w for r, w in cal.carried if r == SIGNAL_ROLE[a]])
    stock_r = {p.week_key: p.value for p in ff_stock}
    rf_r = {p.week_key: p.value for p in ff_rf}
    market, prev_price = {}, {}
    for a in assets:
        if a != "stocks":
            before = [p for p in series[a].points if p.week_key < first]
            prev_price[a] = before[-1].price
    prices = {a: series[a].by_key() for a in assets if a != "stocks"}
    for w in cal.weeks:
        rets = {}
        for a in assets:
            if a == "stocks":
                rets[a] = stock_r[w]                                   # PORT-001
            else:                                                      # PORT-003/004
                rets[a] = prices[a][w].price / prev_price[a] - 1.0
                prev_price[a] = prices[a][w].price
        dy, ds = {}, {}
        if w in div:                                                   # DIV-005, Q-016
            dy["stocks"], ds["stocks"] = div[w].dividend_return, div[w].status
        market[w] = WeekMarket(w, rets, rf_r[w], dy, ds)               # PORT-002
    inputs = EngineInputs(weeks=cal.weeks, market=market, signal_series=series, params=params,
                          targets=targets,
                          initial_capital=float(cfg.get("portfolio.initial_capital_pln")),
                          costs=CostModel.from_config(cfg),
                          cost_basis_method=cfg.get("tax.individual.cost_basis"),
                          run_start=first)
    provs = (ff.provenance,) + tuple(series[a].provenance for a in assets)
    if div_series is not None:
        provs += (div_series.provenance,)
    normalized = tuple(normalized_return_rows("stocks_return", ff_stock)
                       + normalized_return_rows("rf", ff_rf))
    for a in assets:
        normalized += tuple(normalized_price_rows(series[a]))
    normalized += tuple({"role": "dividend", "week_key": w, "value": div[w].dividend_return,
                         "value_kind": "dividend_return", "available_at": div[w].available_at,
                         "source_date": "", "source": div[w].status, "flags": ()}
                        for w in sorted(div) if w in market)
    ctx = dict(report=report, provenances=provs, first_week=first,
               last_week=last, as_of=as_of, targets=targets, dropped_incomplete_weeks=dropped,
               calendar=cal, normalized=normalized, dividend_mode=div_mode,
               common_range=(start, end), truncations=tuple(truncations),
               warmup_weeks={a: wparams[a].minimum_warmup_weeks for a in assets},
               first_week_rule=first_rule)
    return inputs, ctx


def check_stock_return_start(cfg: ResolvedConfig, assets, ff_stock, provenance) -> None:
    """TEST-023 (NORM-012): a full portfolio backtest of an active stocks sleeve cannot start
    before the stocks return history. An explicit run.start earlier than the first available
    stocks return week is an error (never silently moved to that week); every other range
    difference keeps the NORM-011 common-range truncation."""
    if cfg.start is None or "stocks" not in assets or not ff_stock:
        return
    requested = first_key_on_or_after(cfg.start)
    available = ff_stock[0].week_key
    if requested < available:
        raise InsufficientHistoryError(
            f"requested start {cfg.start} (first return week {requested}) precedes the "
            f"available stocks return history (first available return week {available}, "
            f"{provenance.path}); provide an alternative stocks_return_file "
            f"(--stocks-return-file or --data-file stocks_return=...) to run a full portfolio "
            f"backtest; signal-only analysis of the earlier price history: the 'signals' command "
            f"(TEST-023)")


def data_blocker_note(provenance) -> str:
    """Context for ERR-003 on a non-canonical (staged proxy) source."""
    if provenance.canonical or not provenance.warnings:
        return ""
    return f"{provenance.role} source is non-canonical: {'; '.join(provenance.warnings)}"


def first_warmup_week(returns, start, end, keys: dict, need: dict) -> dt.date:
    """First return week in [start, end] before which every asset has at least ``need``
    observations of its signal series (NORM-010, Q-050: warm-up counts observations)."""
    for p in returns:
        w = p.week_key
        if w < start or w > end:
            continue
        if all(bisect.bisect_left(keys[a], w) >= need[a] for a in keys):
            return w
    worst = max(need, key=lambda a: need[a])
    raise WarmupError(worst, bisect.bisect_left(keys[worst], end), need[worst], end)


def build_hooks(cfg: ResolvedConfig, inception: Optional[dt.date] = None, *, funding=None,
                tax_state=None, zero_rates: bool = False, distributions: Optional[dict] = None):
    """Strategic funding policy (step 3/6) composed with the profile module (steps 0/2/5/6):
    individual_pl taxes or a foundation (which needs the inception date, Q-034). Walk-forward
    continuation passes its own ``funding`` policy (boundary rebalance, carried band trigger)
    and the carried ``tax_state`` (TaxState / FoundationState); ``zero_rates`` builds the
    pre-tax shadow policy of the profile (Q-015); ``distributions`` the scheduled foundation
    distributions of the run, mapped to its retained weeks (Q-037)."""
    strategic = funding if funding is not None else hooks_from_config(cfg)
    if cfg.get("tax.profile") in FOUNDATION_PROFILES:
        if inception is None:
            raise ConfigError("foundation hooks need the inception date of the run")
        params = FoundationParams.from_config(cfg)
        params = params.zero_rates() if zero_rates else params
        return ComposedHooks(strategic, FoundationHooks(params, inception, tax_state,
                                                        distributions))
    tax = tax_hooks_from_config(cfg)
    if tax is None:
        return strategic
    params = tax.params.zero_rates() if zero_rates else tax.params
    return ComposedHooks(strategic, IndividualTaxHooks(params, tax_state))


def find_tax_hooks(hooks):
    """The profile module of a run (IndividualTaxHooks | FoundationHooks | None)."""
    kinds = (IndividualTaxHooks, FoundationHooks)
    if isinstance(hooks, kinds):
        return hooks
    for e in getattr(hooks, "extensions", ()):
        if isinstance(e, kinds):
            return e
    return None


def run_pre_tax(cfg: ResolvedConfig, inputs: EngineInputs, actual, tax) -> PreTaxRun:
    """Q-015 (RESOLVED): never rebuilds data or calendars - reuses ``inputs`` as is."""
    if tax is None:
        return PreTaxRun("actual_run_no_taxes", actual, inputs,
                         "tax.profile=none: the actual weekly run has no taxes and is the "
                         "pre-tax path")
    if isinstance(tax, FoundationHooks):
        zero = tax.params.zero_rates()
        fh = FoundationHooks(zero, tax.inception, distributions=tax.distributions)
        result = run_engine(inputs, ComposedHooks(hooks_from_config(cfg), fh))
        cost = settle_foundation_shadow_costs(result.final_snapshot, zero, fh.state, tax.inception,
                                              inputs.targets, result.weeks[-1].effective_states)
        return PreTaxRun("shadow_zero_tax", result, inputs,
                         "same EngineInputs object as the actual run; every tax rate set to 0 "
                         "(dividend, RF, internal, distribution); setup and admin costs, "
                         "transaction costs and slippage kept; the final-year admin cost is paid "
                         "after the weekly path (TAX-006 waterfall, no full liquidation, no "
                         "distribution tax)"
                         + ("; the same gross distribution schedule, untaxed: final wealth = "
                            "remaining NAV + cumulative gross distributions (Q-037)"
                            if zero.schedule_mode else ""), cost)
    shadow = ComposedHooks(hooks_from_config(cfg), IndividualTaxHooks(tax.params.zero_rates()))
    return PreTaxRun("shadow_zero_tax", run_engine(inputs, shadow), inputs,
                     "same EngineInputs object as the actual run (weeks, markets, signals, "
                     "targets, capital, rebalancing, dividend data); every tax rate set to 0; "
                     "transaction costs and slippage kept; no terminal settlement")


def load_cpi_window(cfg: ResolvedConfig, inception, last_week, report):
    """REAL-001..004, Q-045: CPI of the inception month and of the last retained week's month;
    CPI problems never block the nominal run (REAL-003)."""
    try:
        cpi = load_role(cfg, "cpi")
    except BacktestError as e:
        report.add(ValidationIssue(Severity.WARNING, "cpi_unavailable", "cpi",
                                   f"real metrics not computed: {e}", None, "REAL-003"))
        return None, None
    report.add_provenance(cpi.provenance)
    window = cpi_window(cpi, inception, last_week, cfg.get("cpi.mapping"), cfg.get("cpi.label"))
    if window.cpi_start is None or window.cpi_end is None:
        report.add(ValidationIssue(Severity.WARNING, "cpi_unavailable", "cpi",
                                   f"real metrics not computed: {window.note}", None, "REAL-003"))
    elif window.start_imputed or window.end_imputed:
        report.add(ValidationIssue(Severity.WARNING, "cpi_imputed_for_metrics", "cpi",
                                   f"CPI previous_available used for real metrics: {window.note}",
                                   None, "REAL-002;NORM-015"))
    return cpi, window


def prepare_run(cfg: ResolvedConfig, dividend_mode: Optional[str] = None,
                warmup_params: Optional[dict] = None, auto_start: bool = False,
                assets: Optional[tuple] = None) -> PreparedRun:
    """Data preparation of a portfolio run: sources, calendar, EngineInputs and the CPI window
    of the retained range (REAL-001..004). ``dividend_mode``, ``warmup_params``,
    ``auto_start`` and ``assets`` - see ``build_run``. A foundation distribution schedule
    (Q-037) is loaded first and kept as an exogenous plan (no part of the market calendar)."""
    check_supported_run(cfg)                        # configuration errors before any file
    schedule = load_distribution_schedule(cfg) if needs_distribution_schedule(cfg) else None
    inputs, ctx = build_run(cfg, dividend_mode, warmup_params, auto_start, assets)
    report = ctx.pop("report")
    cpi, window = load_cpi_window(cfg, inception_date(ctx["first_week"]), inputs.weeks[-1], report)
    if cpi is not None:
        ctx["provenances"] += (cpi.provenance,)
    if schedule is not None:
        ctx["provenances"] += (schedule.provenance,)
    return PreparedRun(inputs=inputs, issues=tuple(report.issues), cpi_series=cpi,
                       cpi_window=window, distribution_schedule=schedule, **ctx)


def needs_distribution_schedule(cfg: ResolvedConfig) -> bool:
    """Q-037: a foundation profile (or a tax-compare with one) with
    tax.foundation.tax_event=distribution_schedule."""
    if cfg.get("tax.foundation.tax_event") != SCHEDULE:
        return False
    profiles = [cfg.get("tax.profile")]
    if cfg.command == "tax-compare":
        profiles += list(cfg.get("tax.compare_profiles") or ())
    return any(p in FOUNDATION_PROFILES for p in profiles)


def run_distributions(cfg: ResolvedConfig, prepared: PreparedRun, weeks, report) -> Optional[dict]:
    """Q-037: the schedule rows of a run mapped to its retained weeks; rows outside the run are
    ignored with a validation warning (they never extend the run range)."""
    if cfg.get("tax.profile") not in FOUNDATION_PROFILES or not needs_distribution_schedule(cfg):
        return None
    by_week, ignored = map_distribution_schedule(prepared.distribution_schedule.rows, weeks)
    report.extend(distribution_ignored_issues(ignored, weeks))
    return by_week


def distribution_ignored_issues(ignored, weeks) -> list:
    return [ValidationIssue(Severity.WARNING, "distribution_row_ignored", "distribution_schedule",
                            f"scheduled distribution row {r.row_index} ({r.scheduled_date}, nominal "
                            f"week {r.nominal_week}) ignored: {reason} {weeks[0]}..{weeks[-1]}",
                            r.nominal_week, "FND-009;Q-037") for r, reason in ignored]


def prepared_input_sha256(prepared: PreparedRun, include_sources: bool = True) -> str:
    """SHA-256 of the prepared data/calendar input as the engine sees it: retained weeks,
    weekly returns, RF, dividend yields and status, signal price series, first week, as_of,
    dividend mode, common range, dropped weeks, CPI window and the source hashes. Strategy
    parameters (signal parameters, targets, capital, costs, rebalancing band, tax profile) are
    not part of it: every variant of a scan or tax-compare shares this hash.
    ``include_sources=False`` (walk-forward training views) hashes only the content held by the
    view, not the hashes of the whole source files (which also cover later weeks)."""
    i = prepared.inputs
    cpi = prepared.cpi_window
    payload = {
        "weeks": [w.isoformat() for w in i.weeks],
        "market": [[w.isoformat(), sorted((a, repr(r)) for a, r in m.asset_returns.items()),
                    repr(m.rf_return), sorted((a, repr(d)) for a, d in m.dividend_yield.items()),
                    sorted(m.dividend_status.items())] for w, m in sorted(i.market.items())],
        "signal_series": {a: [[p.week_key.isoformat(), repr(p.price), p.available_at.isoformat()]
                              for p in s.points] for a, s in sorted(i.signal_series.items())},
        "run_start": str(i.run_start), "as_of": prepared.as_of.isoformat(),
        "dividend_mode": prepared.dividend_mode,
        "common_range": [str(x) for x in prepared.common_range],
        "dropped": [w.isoformat() for w in prepared.calendar.dropped],
        "cpi_window": dataclasses.asdict(cpi) if cpi is not None else None,
        "sources": [[p.role, p.sha256] for p in sorted(prepared.provenances, key=lambda p: p.role)]
        if include_sources else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def check_prepared_for(cfg: ResolvedConfig, prepared: PreparedRun) -> None:
    """A profile may only run on a prepared input that carries the data it needs."""
    need = dividend_mode_of(cfg, active_risky_assets(strategic_targets(cfg)))
    if need != "none" and need != prepared.dividend_mode:
        raise ConfigError(f"tax.profile={cfg.get('tax.profile')} needs dividend data "
                          f"({need}) but the prepared input carries {prepared.dividend_mode}")
    if (cfg.get("tax.profile") in FOUNDATION_PROFILES and needs_distribution_schedule(cfg)
            and prepared.distribution_schedule is None):
        raise ConfigError("tax.foundation.tax_event=distribution_schedule but the prepared input "
                          "carries no distribution schedule (Q-037)")


def run_portfolio(cfg: ResolvedConfig, write: bool = True, hooks=None) -> PortfolioRunResult:
    return run_prepared(cfg, prepare_run(cfg), write=write, hooks=hooks)


def run_prepared(cfg: ResolvedConfig, prepared: PreparedRun, write: bool = True, hooks=None,
                 out_dir: Optional[Path] = None, shared: Optional[dict] = None) -> PortfolioRunResult:
    """The production run pipeline on an already prepared input: central engine + funding /
    rebalancing hooks + profile tax/foundation hooks, terminal settlement, pre-tax shadow run,
    metrics and (optionally) the standard outputs."""
    check_supported_run(cfg)
    check_prepared_for(cfg, prepared)
    inputs = prepared.inputs_for(cfg)               # shared data, strategy of ``cfg``
    ctx = prepared.context()
    ctx["targets"] = inputs.targets
    inception = prepared.inception
    distributions = run_distributions(cfg, prepared, inputs.weeks, ctx["report"])
    hooks = hooks if hooks is not None else build_hooks(cfg, inception, distributions=distributions)
    result = run_engine(inputs, hooks)
    ctx["report"].extend(result.issues)
    tax = find_tax_hooks(hooks)
    initial_capital = float(cfg.get("portfolio.initial_capital_pln"))
    # IND-016/IND-020, Q-032: terminal settlement on copies of the final state (never a weekly
    # record); tax.profile=none has no terminal settlement
    if isinstance(tax, FoundationHooks):                   # FND-005: terminal | schedule end
        terminal = settle_foundation(result.final_snapshot, tax.params, tax.state,
                                     initial_capital, tax.inception, inputs.targets,
                                     result.weeks[-1].effective_states)
    elif tax is not None:
        terminal = settle_terminal(result.final_snapshot, tax.params, tax.state)
    else:
        terminal = None
    pre_tax = run_pre_tax(cfg, inputs, result, tax)
    first, last = prepared.first_week, result.weeks[-1].week_key
    metrics = compute_run_metrics(
        pre=PathSeries.from_engine(pre_tax.engine), after=PathSeries.from_engine(result),
        elapsed_days=elapsed_days(first, last), rf_returns=[w.market.rf_return for w in result.weeks],
        rf_after_tax_rate=tax.params.rf_interest_rate if tax else 0.0,
        pre_terminal_nav=result.final_ledger.nav,
        after_tax_terminal_wealth=(terminal.after_tax_terminal_wealth if terminal
                                   else result.final_ledger.nav),
        trades=result.trades, terminal_trades=terminal.liquidation_trades if terminal else (),
        week_states=[w.effective_states for w in result.weeks],
        assets=canonical_assets(inputs.params), mar_annual=float(cfg.get("metrics.sortino_mar_annual")),
        cpi=prepared.cpi_window, growth_base_nav=initial_capital,   # Q-033: before the setup cost
        final_wealth_pre_tax=pre_tax.final_wealth)
    out = PortfolioRunResult(engine=result, **ctx, tax_state=tax.state if tax else None,
                             tax_params=tax.params if tax else None, terminal=terminal,
                             pre_tax=pre_tax, metrics=metrics, inputs=inputs,
                             cpi_series=prepared.cpi_series, prepared=prepared)
    if write:
        out.output_dir = write_portfolio_outputs(cfg, out, out_dir=out_dir, shared=shared)
    return out


def rolling_for(cfg: ResolvedConfig, res: PortfolioRunResult) -> RollingMetrics:
    """MET-022/MET-023 on the two weekly paths of a run result (pre-tax shadow, actual
    after-tax; the walk-forward result carries the stitched OOS paths); terminal settlement is
    never part of either path (MET-025/MET-026)."""
    return rolling_metrics(PathSeries.from_engine(res.pre_tax.engine), PathSeries.from_engine(res.engine),
                           horizons=cfg.get("metrics.rolling_returns"),
                           stats=cfg.get("metrics.rolling_stats"))


def rolling_manifest(rolling: RollingMetrics) -> dict:
    return {"file": "rolling_metrics.csv", "horizons_years": list(rolling.horizons_years),
            "window_start_rule": ROLLING_WINDOW_START_RULE,
            "calendar_target": "window_end minus N calendar years (29 Feb -> 28 Feb)",
            "paths": {"pre_tax": "pre-tax shadow weekly NAV path",
                      "after_tax": "actual weekly NAV path (current taxes and costs)"},
            "terminal_settlement_included": False, "rolling_stats": rolling.stats,
            "rows_per_horizon": {str(h): len(rolling.for_horizon(h)) for h in rolling.horizons_years},
            "sort_order": "horizon_years, window_end ascending"}


FOUNDATION_LIMITATIONS = [
    "tax.foundation.internal_trading_tax_rate > 0 together with tax_event=distribution_schedule "
    "is not defined by the clean-room specification adjudication (Q-037/Q-047): ConfigError",
    "distribution_schedule: no terminal full distribution and no terminal distribution tax; "
    "after_tax_terminal_wealth = remaining NAV after the final admin cost + cumulative net "
    "scheduled distributions (Q-037)"]


def write_portfolio_outputs(cfg: ResolvedConfig, res: PortfolioRunResult,
                            out_dir: Optional[Path] = None, shared: Optional[dict] = None,
                            timestamp=None, extra_manifest: Optional[dict] = None) -> Path:
    """Standard run outputs. ``shared`` (tax-compare): the profile directory ``out_dir`` gets
    the profile's own artifacts; shared data outputs (weekly_normalized.csv) live once in the
    compare directory and the profile manifest points to the shared manifest (Q-023).
    ``timestamp`` / ``extra_manifest`` (walk-forward): the run timestamp of the enclosing
    command and additional data_manifest.json entries."""
    ts = shared["timestamp"] if shared else (timestamp or run_timestamp())
    if out_dir is None:
        out = run_directory(cfg.get("report.output_dir"), cfg.get("report.run_name"), ts)
    else:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=False)
    (out / "config_resolved.yaml").write_text(cfg.to_yaml(res.as_of), encoding="utf-8")
    assets = res.assets if res.assets is not None else canonical_assets(res.engine.pre_start)
    t = res.terminal
    weekly_events = tuple(res.tax_state.tax_events) if res.tax_state else ()
    events = weekly_events + (t.terminal_tax_events if t else ())
    write_csv(out / "weekly_portfolio.csv", weekly_portfolio_fields(assets),
              weekly_portfolio_rows(res.engine, res.targets, assets, weekly_events,
                                    res.targets_by_week))
    write_csv(out / "tax_events.csv", TAX_EVENT_FIELDS, tax_event_rows(events))
    write_csv(out / "realizations.csv", REALIZATION_FIELDS,
              realization_rows(res.engine.realizations)
              + (realization_rows(t.terminal_realizations, "terminal") if t else []))
    write_csv(out / "dividend_reinvestments.csv", DIVIDEND_FIELDS,
              dividend_rows(res.engine.dividend_reinvestments))
    write_csv(out / "trades.csv", TRADE_FIELDS,
              trade_rows(res.engine.trades + (t.liquidation_trades if t else ())))
    # (the pre-tax shadow run is not written as a second set of CSV files; summary.csv and
    #  data_manifest.json describe it, Q-015)
    write_csv(out / "payments.csv", PAYMENT_FIELDS,
              record_rows(res.engine.payments + (t.terminal_payments if t else ()), PAYMENT_FIELDS))
    write_csv(out / "rf_transfers.csv", TRANSFER_FIELDS,
              record_rows(res.engine.transfers + (t.terminal_transfers if t else ()), TRANSFER_FIELDS))
    if t is not None:
        write_json(out / "terminal_settlement.json", terminal_settlement_doc(t))
    write_csv(out / "rebalance_events.csv", REBALANCE_FIELDS,
              rebalance_rows(res.engine.rebalance_events))
    write_csv(out / "signals.csv", SIGNAL_FIELDS, signal_rows(res.engine.signal_records))
    write_csv(out / "validation_report.csv", VALIDATION_FIELDS, res.report.rows())
    if not shared:
        write_csv(out / "weekly_normalized.csv", NORMALIZED_FIELDS, res.normalized)
    tax_doc = {"tax_profile": cfg.get("tax.profile"), "dividend_mode": res.dividend_mode}
    if res.tax_state is None and res.dividend_mode != "none":
        tax_doc["dividend_note"] = (
            "dividend data carried by the shared prepared input (common tax-compare calendar, "
            "Q-023); tax.profile=none withholds no dividend tax: gross dividends are reinvested "
            "(Q-016), no tax events exist")
    if res.tax_state is not None:
        tax_doc.update({"parameters": dataclasses.asdict(res.tax_params),
                        "before_terminal": res.tax_state.to_dict(),
                        "after_terminal": t.final_tax_state.to_dict(),
                        "terminal": t.breakout(),
                        "note": "before_terminal = state at the end of the weekly path (final "
                                "year still open); after_terminal = after terminal liquidation "
                                "and final-year settlement"})
    if isinstance(res.tax_state, FoundationState):
        if res.tax_params.schedule_mode:                                # Q-037 audit
            write_csv(out / "distributions.csv", DISTRIBUTION_FIELDS,
                      distribution_rows(t.final_tax_state.distribution_events))
        tax_doc["pre_tax_shadow_final_cost"] = {
            "final_admin_cost": res.pre_tax.terminal_cost.admin_cost.amount,
            "pre_cost_nav": res.pre_tax.terminal_cost.pre_cost_nav,
            "final_wealth_pre_tax": res.pre_tax.terminal_cost.final_wealth,
            "trades": len(res.pre_tax.terminal_cost.trades)}
        write_json(out / "foundation_state.json", tax_doc)
    else:
        write_json(out / "tax_state.json", tax_doc)
    write_csv(out / "summary.csv", SUMMARY_FIELDS, [summary_row(cfg, res)])
    res.rolling = rolling_for(cfg, res)                         # MET-022/023
    write_csv(out / "rolling_metrics.csv", ROLLING_FIELDS, rolling_rows(res.rolling))
    manifest = build_manifest(res.provenances, res.as_of, ts, cfg.command)
    manifest.update({
        "dropped_incomplete_weeks": res.dropped_incomplete_weeks,
        "first_return_week": res.first_week.isoformat(),
        "first_week_rule": res.prepared.first_week_rule if res.prepared is not None else None,
        "last_return_week": res.last_week.isoformat(),
        "inception_date": inception_date(res.first_week).isoformat(),
        "elapsed_days": elapsed_days(res.first_week, res.last_week),
        "tax_profile": cfg.get("tax.profile"),
        "dividend_mode": res.dividend_mode,
        "run_calendar_weeks": len(res.calendar.weeks),
        "common_calendar_gaps": [w.isoformat() for w in res.calendar.common_gaps],
        "dropped_weeks": [w.isoformat() for w in res.calendar.dropped],
        "rebalance_mode": cfg.get("portfolio.rebalance"),
        "skipped_signal_observations": [[a, w.isoformat()] for a, w in
                                        res.engine.skipped_signal_observations],
        "audit_outputs": ["payments.csv", "rf_transfers.csv", "rebalance_events.csv",
                          "tax_events.csv", "realizations.csv", "dividend_reinvestments.csv",
                          "foundation_state.json" if isinstance(res.tax_state, FoundationState)
                          else "tax_state.json"] + (["terminal_settlement.json"] if t else [])
                         + ["rolling_metrics.csv"],
        "rolling_metrics": rolling_manifest(res.rolling),
        "terminal_settlement": (f"{cfg.get('tax.profile')}: separate from the weekly path" if t
                                else "none: no terminal settlement (after_tax_terminal_wealth = "
                                "pre_terminal_nav, Q-032)"),
        "pre_tax_method": res.pre_tax.method,
        "pre_tax_note": res.pre_tax.note,
        **initial_state_manifest(cfg, res.report),
    })
    if isinstance(res.tax_state, FoundationState):
        manifest["foundation_tax_event"] = res.tax_params.tax_event
        manifest["foundation_limitations"] = FOUNDATION_LIMITATIONS
        if res.tax_params.schedule_mode:
            manifest["audit_outputs"].append("distributions.csv")
    if shared:
        manifest.update(shared["profile_manifest"])
    manifest.update(extra_manifest or {})
    write_json(out / "data_manifest.json", manifest)
    return out


def dispatch(cfg: ResolvedConfig):
    cmd = cfg.command
    if cmd == "signals":
        return run_signals(cfg)
    if cmd == "run":
        return run_portfolio(cfg)
    if cmd == "tax-compare":
        from .tax_compare import run_tax_compare
        return run_tax_compare(cfg)
    if cmd in ("delay-scan", "threshold-scan", "rebalance-scan"):
        from .scans import run_scan
        return run_scan(cfg)
    if cmd == "optimize" and cfg.get("optimizer.mode") == "walk-forward":
        from .walk_forward import run_walk_forward
        return run_walk_forward(cfg)
    if cmd == "optimize":
        from .optimizer import run_optimize
        return run_optimize(cfg)
    raise NotImplementedCommand(
        f"command '{cmd}': configuration resolved and validated, but this command is not "
        "implemented in this build yet; use 'run', 'tax-compare', 'delay-scan', "
        "'threshold-scan', 'rebalance-scan', 'signals' or --print-config")
