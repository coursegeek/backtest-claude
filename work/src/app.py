"""Command implementations behind the CLI and the import API.

Implemented in this build:
  * ``signals`` - signal-only analysis (NORM-012/NORM-020);
  * ``run``     - portfolio backtest with tax.profile none or individual_pl and every
                  portfolio.rebalance mode (signal-only, weekly, monthly, quarterly,
                  annually/yearly, band); annual taxes of closed years, dividend and RF taxes.
Everything else (foundation profiles, terminal settlement, metrics/summary, scans, optimize,
tax-compare, walk-forward) resolves and validates its configuration and then stops with a
clear NotImplementedCommand; no partial results are produced.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .allocation import active_risky_assets, strategic_targets
from .calendar import (common_range, elapsed_days, first_key_on_or_after, first_return_week,
                       inception_date, last_key_on_or_before, last_return_week)
from .config import ResolvedConfig
from .data_loader import load_dividend_cash, load_role
from .errors import (ConfigError, DataFileNotFound, DataValidationError, DividendModeError,
                     NotImplementedCommand)
from .manifest import build_manifest, run_timestamp
from .costs import CostModel
from .engine import ComposedHooks, EngineInputs, WeekMarket, run_engine
from .rebalancing import hooks_from_config
from .settlement import TerminalSettlementResult, settle_terminal
from .tax import IndividualTaxHooks, TaxState, tax_hooks_from_config
from .models import RISKY_ASSETS, Severity, ValidationIssue, canonical_assets
from .reporting import (DIVIDEND_FIELDS, NORMALIZED_FIELDS, PAYMENT_FIELDS, REALIZATION_FIELDS,
                        REBALANCE_FIELDS, SIGNAL_FIELDS, TAX_EVENT_FIELDS, TRADE_FIELDS,
                        TRANSFER_FIELDS, VALIDATION_FIELDS, dividend_rows, rebalance_rows,
                        realization_rows, record_rows, tax_event_rows, terminal_settlement_doc,
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
    tax_state: Optional[TaxState] = None            # weekly (pre-terminal) tax state
    tax_params: Optional[object] = None
    terminal: Optional[TerminalSettlementResult] = None   # separate from the weekly path


def check_supported_run(cfg: ResolvedConfig) -> None:
    """Fail clearly instead of producing partial results for unimplemented features."""
    if cfg.get("tax.profile") not in ("none", "individual_pl"):
        raise NotImplementedCommand(
            f"tax.profile={cfg.get('tax.profile')}: foundation profiles are not implemented in "
            "this build; tax.profile none and individual_pl are supported")


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


def build_run(cfg: ResolvedConfig):
    """Load, validate and align every source of a portfolio run; returns EngineInputs and
    the run context (NORM-011, NORM-019, Q-012, Q-014)."""
    check_supported_run(cfg)
    targets = strategic_targets(cfg)
    assets = active_risky_assets(targets)
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
    div_mode = dividend_mode_of(cfg, assets)
    div_series, div_points = (None, [])
    if div_mode != "none":
        div_series, div_points = load_dividend_input(cfg, div_mode, report)
        if div_mode == "smoothed_weekly":                              # a required source
            ranges["dividend"] = (div_points[0].week_key, div_points[-1].week_key)
    start, end, truncations = common_range(ranges)
    for role, side, own, eff in truncations:
        report.add(ValidationIssue(Severity.WARNING, "range_truncated", role,
                                   f"{side} truncated from {own} to common {eff} (NORM-011)", eff,
                                   "NORM-011"))
    first = first_return_week(cfg.start, start)
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
    params = {a: cfg.signal_params(a) for a in assets}
    for a in assets:
        warn = check_warmup(a, series[a].keys(), first, params[a], cfg.get("signal.initial_state"))
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
               calendar=cal, normalized=normalized, dividend_mode=div_mode)
    return inputs, ctx


def build_hooks(cfg: ResolvedConfig):
    """Strategic funding policy (step 3/6) composed with the tax module (steps 2/5/6)."""
    strategic = hooks_from_config(cfg)
    tax = tax_hooks_from_config(cfg)
    return ComposedHooks(strategic, tax) if tax is not None else strategic


def find_tax_hooks(hooks) -> Optional[IndividualTaxHooks]:
    if isinstance(hooks, IndividualTaxHooks):
        return hooks
    for e in getattr(hooks, "extensions", ()):
        if isinstance(e, IndividualTaxHooks):
            return e
    return None


def run_portfolio(cfg: ResolvedConfig, write: bool = True, hooks=None) -> PortfolioRunResult:
    inputs, ctx = build_run(cfg)
    hooks = hooks if hooks is not None else build_hooks(cfg)
    result = run_engine(inputs, hooks)
    ctx["report"].extend(result.issues)
    tax = find_tax_hooks(hooks)
    # IND-016/IND-020: terminal settlement on copies of the final state (never a weekly record);
    # for tax.profile=none it is not applied (Q-032 open for the other profiles)
    terminal = settle_terminal(result.final_snapshot, tax.params, tax.state) if tax else None
    out = PortfolioRunResult(engine=result, **ctx, tax_state=tax.state if tax else None,
                             tax_params=tax.params if tax else None, terminal=terminal)
    if write:
        out.output_dir = write_portfolio_outputs(cfg, out)
    return out


def write_portfolio_outputs(cfg: ResolvedConfig, res: PortfolioRunResult) -> Path:
    ts = run_timestamp()
    out = run_directory(cfg.get("report.output_dir"), cfg.get("report.run_name"), ts)
    (out / "config_resolved.yaml").write_text(cfg.to_yaml(res.as_of), encoding="utf-8")
    assets = canonical_assets(res.engine.pre_start)
    t = res.terminal
    weekly_events = tuple(res.tax_state.tax_events) if res.tax_state else ()
    events = weekly_events + (t.terminal_tax_events if t else ())
    write_csv(out / "weekly_portfolio.csv", weekly_portfolio_fields(assets),
              weekly_portfolio_rows(res.engine, res.targets, assets, weekly_events))
    write_csv(out / "tax_events.csv", TAX_EVENT_FIELDS, tax_event_rows(events))
    write_csv(out / "realizations.csv", REALIZATION_FIELDS,
              realization_rows(res.engine.realizations)
              + (realization_rows(t.terminal_realizations, "terminal") if t else []))
    write_csv(out / "dividend_reinvestments.csv", DIVIDEND_FIELDS,
              dividend_rows(res.engine.dividend_reinvestments))
    write_csv(out / "trades.csv", TRADE_FIELDS,
              trade_rows(res.engine.trades + (t.liquidation_trades if t else ())))
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
    write_csv(out / "weekly_normalized.csv", NORMALIZED_FIELDS, res.normalized)
    tax_doc = {"tax_profile": cfg.get("tax.profile"), "dividend_mode": res.dividend_mode}
    if res.tax_state is not None:
        tax_doc.update({"parameters": dataclasses.asdict(res.tax_params),
                        "before_terminal": res.tax_state.to_dict(),
                        "after_terminal": t.final_tax_state.to_dict(),
                        "terminal": t.breakout(),
                        "note": "before_terminal = state at the end of the weekly path (final "
                                "year still open); after_terminal = after terminal liquidation "
                                "and final-year settlement"})
    write_json(out / "tax_state.json", tax_doc)
    manifest = build_manifest(res.provenances, res.as_of, ts, cfg.command)
    manifest.update({
        "dropped_incomplete_weeks": res.dropped_incomplete_weeks,
        "first_return_week": res.first_week.isoformat(),
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
                          "tax_state.json"] + (["terminal_settlement.json"] if t else []),
        "terminal_settlement": ("individual_pl: separate from the weekly path" if t else
                                "not applied (tax.profile=none; Q-032 open for other profiles)"),
        "not_implemented_outputs": ["summary.csv (metrics)"],
    })
    write_json(out / "data_manifest.json", manifest)
    return out


def dispatch(cfg: ResolvedConfig):
    cmd = cfg.command
    if cmd == "signals":
        return run_signals(cfg)
    if cmd == "run":
        return run_portfolio(cfg)
    if cmd in ("delay-scan", "threshold-scan"):
        if not cfg.get("run.asset"):
            raise ConfigError(f"{cmd} requires --asset stocks|gold|btc (ALLOC-002)")
    elif cmd in ("tax-compare", "rebalance-scan"):
        strategic_targets(cfg)          # ALLOC-001: fail early without explicit targets
    raise NotImplementedCommand(
        f"command '{cmd}': configuration resolved and validated, but the portfolio engine is "
        "not implemented in this build yet; use 'run' (tax.profile none|individual_pl), "
        "'signals' or --print-config")
