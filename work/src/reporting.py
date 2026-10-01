"""Deterministic output writers (REP-001, REP-006, REP-007, REP-009, REP-014, NORM-007).
Floats use the shortest round-trip repr, so identical inputs give identical bytes."""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
from enum import Enum
from pathlib import Path

SIGNAL_FIELDS = ["asset", "week_key", "available_at", "price", "sma", "lower_band", "upper_band",
                 "raw_condition", "exit_counter", "entry_counter", "confirmed_state", "state_basis",
                 "confirmed_signal", "scheduled_execution_week", "effective_state",
                 "executed_target", "flags"]
VALIDATION_FIELDS = ["severity", "code", "role", "week_key", "requirement_ids", "message"]
NORMALIZED_FIELDS = ["role", "week_key", "value", "value_kind", "available_at", "source_date",
                     "source", "flags"]


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (tuple, list)):
        return "|".join(fmt(x) for x in v)
    return str(v)


def write_csv(path: Path, fields, rows) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(fields)
        for r in rows:
            w.writerow([fmt(r.get(k)) for k in fields])


def write_json(path: Path, obj) -> None:
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def run_directory(output_dir, run_name: str, timestamp: dt.datetime) -> Path:
    """REP-001: results/<timestamp>_<run_name>/ (never reused)."""
    base = Path(output_dir) / f"{timestamp.strftime('%Y%m%dT%H%M%S')}_{run_name}"
    path, n = base, 1
    while path.exists():
        n += 1
        path = base.with_name(f"{base.name}_{n}")
    path.mkdir(parents=True)
    return path


def signal_rows(records) -> list:
    return [{"asset": r.asset, "week_key": r.week_key, "available_at": r.available_at,
             "price": r.price, "sma": r.sma, "lower_band": r.lower_band, "upper_band": r.upper_band,
             "raw_condition": r.condition, "exit_counter": r.exit_counter,
             "entry_counter": r.entry_counter, "confirmed_state": r.confirmed_state,
             "state_basis": r.state_basis, "confirmed_signal": r.confirmation,
             "scheduled_execution_week": r.scheduled_execution_week,
             "effective_state": r.effective_state, "executed_target": r.executed_target,
             "flags": r.flags} for r in records]


def normalized_price_rows(series) -> list:
    return [{"role": series.role, "week_key": p.week_key, "value": p.price, "value_kind": "price",
             "available_at": p.available_at, "source_date": p.source_date, "source": p.source,
             "flags": p.flags} for p in series.points]


TRADE_FIELDS = ["week_key", "asset", "side", "reason", "gross_traded_value", "transaction_cost",
                "slippage", "net_cash_flow", "asset_value_before", "asset_value_after",
                "reserve_before", "reserve_after", "cash_component", "units", "cost_basis",
                "realized_gain", "confirm_week", "nominal_execution_week", "pipeline_step",
                "phase"]
# phase: weekly = PORT-011 pipeline (pipeline_step 1..6), terminal = terminal settlement
# after the last week (pipeline_step empty; never part of the weekly NAV path).
PAYMENT_FIELDS = ["week_key", "event_type", "amount", "pipeline_step", "funding_source", "context",
                  "phase"]
TRANSFER_FIELDS = ["week_key", "source", "destination", "amount", "reason", "pipeline_step", "phase"]
REBALANCE_FIELDS = ["week_key", "mode", "reason", "trigger_source_week", "nominal_execution_week",
                    "nav_after_signal", "amounts_due", "nav_net_for_rebalance",
                    "planned_final_nav", "realized_final_nav", "transaction_costs", "slippage",
                    "max_deviation"] + [f"weight_before_{s}" for s in ("stocks", "gold", "btc", "rf")] \
                   + [f"weight_after_{s}" for s in ("stocks", "gold", "btc", "rf")]
LEDGER_COMPONENTS = ("stocks", "gold", "btc", "rf_base", "rf_reserve_stocks", "rf_reserve_gold",
                     "rf_reserve_btc")
SLEEVES = ("stocks", "gold", "btc", "rf")


def trade_rows(trades) -> list:
    return [{k: getattr(t, k) for k in TRADE_FIELDS} for t in trades]


def normalized_return_rows(role, points) -> list:
    return [{"role": role, "week_key": p.week_key, "value": p.value, "value_kind": "return",
             "available_at": p.available_at, "source_date": p.source_date, "source": "",
             "flags": p.flags} for p in points]


# TAX-004 names (date, tax_base, tax_due) are kept next to the Q-035 names; date == week_key,
# tax_base == taxable_base and tax_due == amount.
TAX_EVENT_FIELDS = ["week_key", "date", "event_type", "category", "settlement", "tax_year", "asset",
                    "component", "gross_base", "taxable_base", "tax_base", "rate", "amount",
                    "tax_due", "pipeline_step", "phase", "source_status", "notes"]
REALIZATION_FIELDS = ["week_key", "tax_year", "asset", "units_sold", "proceeds_net", "cost_basis",
                      "realized_gain", "lots_consumed", "phase"]
TERMINAL_TRADE_REASON = "terminal_liquidation"
DIVIDEND_FIELDS = ["week_key", "asset", "value_before_returns", "dividend_return", "gross_dividend",
                   "dividend_tax", "net_reinvested", "units", "unit_price", "lot_id", "pipeline_step"]
# Q-037: one row per scheduled foundation distribution actually paid (gross = tax + net)
DISTRIBUTION_FIELDS = ["row_index", "scheduled_date", "nominal_week", "actual_week", "paid_week",
                       "kind", "value", "nav_base", "gross", "tax_base_mode", "tax_base", "rate",
                       "tax", "net", "basis_before", "basis_after"]


def tax_event_rows(events) -> list:
    rows = []
    for e in events:
        r = {k: getattr(e, k) for k in TAX_EVENT_FIELDS if hasattr(e, k)}
        r.update(date=e.week_key, tax_base=e.taxable_base, tax_due=e.amount)
        rows.append(r)
    return rows


def realization_rows(realizations, phase: str = "weekly") -> list:
    return [{"week_key": r.week_key, "tax_year": r.week_key.year, "asset": r.asset,
             "units_sold": r.units_sold, "proceeds_net": r.proceeds_net, "cost_basis": r.cost_basis,
             "realized_gain": r.realized_gain,
             "lots_consumed": "|".join(f"{i}:{u!r}:{c!r}" for i, u, c in r.consumed),
             "phase": phase}
            for r in realizations]


def terminal_settlement_doc(t) -> dict:
    """terminal_settlement.json: REP-017/PORT-014 breakout plus the full audit of the terminal
    liquidation (trades, realizations), the final-year liability and the payments."""
    doc = t.breakout()
    doc["liquidation"] = [{k: fmt(getattr(x, k)) for k in TRADE_FIELDS} for x in t.liquidation_trades]
    plain = lambda v: fmt(v) if not isinstance(v, (int, float)) or isinstance(v, bool) else v  # noqa: E731
    if hasattr(t, "final_liability"):                              # individual_pl
        doc["final_year_liability"] = {k: plain(v) for k, v in vars(t.final_liability).items()
                                       if k != "buckets_used"}
        doc["final_year_liability"]["buckets_used"] = [list(b) for b in t.final_liability.buckets_used]
    else:                                                          # foundation
        doc["final_year_admin_cost"] = {k: plain(v) for k, v in vars(t.final_admin_cost).items()}
    doc["terminal_payments"] = [{k: fmt(getattr(p, k)) for k in PAYMENT_FIELDS} for p in t.terminal_payments]
    doc["reserve_consolidation"] = [{k: fmt(getattr(x, k)) for k in TRANSFER_FIELDS}
                                    for x in t.terminal_transfers]
    doc["note"] = ("terminal settlement is not a backtest week: weekly_portfolio.csv ends with "
                   "pre_terminal_nav; terminal costs and taxes are not in the weekly NAV path")
    return doc


def distribution_rows(events) -> list:
    return [{k: getattr(e, k) for k in DISTRIBUTION_FIELDS} for e in events]


def dividend_rows(records) -> list:
    return [{k: getattr(r, k) for k in DIVIDEND_FIELDS} for r in records]


def weekly_tax_amounts(week_record, tax_events) -> dict:
    """Taxes that actually left the ledger in the week: annual taxes as step-3 Payments,
    immediate (weekly) taxes as withholdings described by their TaxEvents - each outflow once."""
    from .tax import event_category
    annual = math.fsum(p.amount for p in week_record.payments
                       if event_category(p.event_type) == "tax")
    div = math.fsum(e.amount for e in tax_events
                    if e.week_key == week_record.week_key and e.event_type == "dividend_tax")
    rf = math.fsum(e.amount for e in tax_events
                   if e.week_key == week_record.week_key and e.event_type == "rf_interest_tax")
    costs = math.fsum(p.amount for p in week_record.payments if event_category(p.event_type) == "cost")
    # Q-037: a scheduled gross distribution leaves the NAV once, as tax + net payout
    dist_tax = math.fsum(p.amount for p in week_record.payments
                         if p.event_type == "foundation_distribution_tax")
    dist_net = math.fsum(p.amount for p in week_record.payments
                         if p.event_type == "foundation_distribution_net")
    return {"annual_tax_paid": annual, "dividend_tax": div, "rf_interest_tax": rf,
            "taxes_paid": math.fsum([annual, div, rf]), "costs_paid": costs,
            "gross_distributions_paid": math.fsum([dist_tax, dist_net]),
            "net_distributions_paid": dist_net, "distribution_tax_paid": dist_tax}


def record_rows(records, fields) -> list:
    return [{k: getattr(r, k) for k in fields} for r in records]


def rebalance_rows(events) -> list:
    rows = []
    for e in events:
        r = {k: getattr(e, k) for k in REBALANCE_FIELDS if hasattr(e, k)}
        for s in SLEEVES:
            r[f"weight_before_{s}"] = e.weights_before[s]
            r[f"weight_after_{s}"] = e.weights_after[s]
        rows.append(r)
    return rows


def weekly_portfolio_fields(assets) -> list:
    """REP-003/REP-013: NAV at each pipeline boundary, component values, the weights used for
    the week's return (after ALL start-of-week transactions and payments = ledger_before_returns,
    PORT-012), end-of-week actual weights, strategic targets, returns, trades, costs, payments
    and the effective signal state of every active asset."""
    return (["week_key", "nav_start", "nav_after_signal", "nav_before_returns",
             "nav_after_returns", "nav_end", "portfolio_return"]
            + [f"value_{c}" for c in LEDGER_COMPONENTS]
            + [f"weight_start_{s}" for s in SLEEVES] + [f"weight_end_{s}" for s in SLEEVES]
            + [f"target_{s}" for s in SLEEVES]
            + [f"return_{a}" for a in ("stocks", "gold", "btc")] + ["return_rf"]
            + ["dividend_return", "gross_dividend", "dividend_reinvested"]
            + ["trades", "traded_value", "transaction_costs", "slippage", "amounts_due",
               "payments", "rebalance", "annual_tax_paid", "dividend_tax", "rf_interest_tax",
               "taxes_paid", "costs_paid", "gross_distributions_paid", "net_distributions_paid",
               "distribution_tax_paid"] + [f"state_{a}" for a in assets])


def weekly_portfolio_rows(result, targets, assets, tax_events=(), targets_by_week=None) -> list:
    """``targets_by_week`` (walk-forward): the strategic targets in force in each week."""
    by_week = {}
    for e in tax_events:
        by_week.setdefault(e.week_key, []).append(e)
    rows = []
    for w in result.weeks:
        r = {"week_key": w.week_key, "nav_start": w.nav_start, "nav_after_signal": w.nav_after_signal,
             "nav_before_returns": w.nav_before_returns, "nav_after_returns": w.nav_after_returns,
             "nav_end": w.nav_end, "portfolio_return": w.portfolio_return}
        for c, v in w.ledger_end.components().items():
            r[f"value_{c}"] = v
        for s, v in w.ledger_before_returns.sleeve_weights().items():
            r[f"weight_start_{s}"] = v
        for s, v in w.ledger_end.sleeve_weights().items():
            r[f"weight_end_{s}"] = v
        tw = targets_by_week[w.week_key] if targets_by_week else targets
        for s in SLEEVES:
            r[f"target_{s}"] = tw[s]
        for a in ("stocks", "gold", "btc"):
            r[f"return_{a}"] = w.market.asset_returns.get(a)
        r["return_rf"] = w.market.rf_return
        r["dividend_return"] = w.market.dividend_yield.get("stocks")
        r["gross_dividend"] = math.fsum(d.gross_dividend for d in w.dividends)
        r["dividend_reinvested"] = math.fsum(d.net_reinvested for d in w.dividends)
        r["trades"] = len(w.trades)
        r["traded_value"] = math.fsum(t.gross_traded_value for t in w.trades)
        r["transaction_costs"] = math.fsum(t.transaction_cost for t in w.trades)
        r["slippage"] = math.fsum(t.slippage for t in w.trades)
        r["amounts_due"] = w.amounts_due.total
        r["payments"] = math.fsum(p.amount for p in w.payments)
        r["rebalance"] = w.rebalance.reason if w.rebalance else ""
        r.update(weekly_tax_amounts(w, by_week.get(w.week_key, ())))
        for a in assets:
            r[f"state_{a}"] = w.effective_states.get(a)       # empty when not active that week
        rows.append(r)
    return rows


# ============================================================================ summary.csv
# REP-002, REP-012, REP-017, REP-018, MET-001..021: one wide row per run, stable column order,
# no timestamp (the run timestamp stays in the run directory name and data_manifest.json).
# tax-compare writes one row per profile with the same schema; tax_profile is the first column
# (the key of the comparison, TAX-003/FND-008).
SIGNAL_PARAM_FIELDS = ("ma", "threshold_off", "threshold_on", "confirm_off", "confirm_on", "delay",
                       "sell_fraction", "risk_off_action")
TAX_ASSUMPTION_KEYS = (
    "tax.dividend_tax_mode", "tax.dividend_estimate_policy", "tax.dividend_reinvest",
    "tax.none.dividend_rate",
    "tax.individual.dividend_rate", "tax.individual.capital_gains_rate",
    "tax.individual.solidarity_rate", "tax.individual.solidarity_threshold_pln",
    "tax.individual.external_solidarity_base_pln", "tax.individual.rf_interest_rate",
    "tax.individual.loss_carryforward_years", "tax.individual.loss_offset_fraction",
    "tax.individual.cost_basis",
    "tax.foundation.dividend_rate", "tax.foundation.internal_trading_tax_rate",
    "tax.foundation_15.distribution_rate", "tax.foundation_19.distribution_rate",
    "tax.foundation.distribution_tax_base", "tax.foundation.tax_event",
    "tax.foundation.rf_interest_rate", "tax.foundation.setup_cost_pln",
    "tax.foundation.annual_admin_cost_pln", "tax.foundation.admin_cost_proration")
YEAR_FIELDS = ("year", "year_return", "year_is_partial")
# walk-forward (Q-022): the summary describes the stitched OOS path; empty for other commands
# (optimization_mode is in-sample for the in-sample optimizer's rows)
WALK_FORWARD_FIELDS = ("optimization_mode", "walk_forward_window", "train_years", "test_years",
                       "step_years", "oos_windows", "first_oos_week", "last_oos_week")
# REP-012 (Q-023): applied_* = parameters the row's profile actually applies; a profile that does
# not apply a parameter shows 0.0 (amounts) or NOT_APPLICABLE (thresholds, modes). The tax_*
# columns (TAX_ASSUMPTION_KEYS) are the configured scenario assumptions shared by every profile
# of a run or tax-compare, not the active taxes of the row.
NOT_APPLICABLE = "not_applicable"
APPLIED_PROFILE_FIELDS = (
    "applied_solidarity_threshold_pln", "applied_external_solidarity_base_pln",
    "applied_loss_carryforward_years", "applied_loss_offset_fraction",
    "applied_foundation_tax_event", "applied_distribution_tax_base",
    "applied_foundation_setup_cost_pln", "applied_foundation_annual_admin_cost_pln",
    "applied_foundation_admin_cost_proration")

SUMMARY_FIELDS = (
    ["tax_profile", "spec_version", "run_name", "pre_tax_method"]
    + list(WALK_FORWARD_FIELDS)
    + [
     "requested_start", "requested_end", "effective_first_week", "effective_last_week",
     "inception_date", "elapsed_days", "weeks",
     "initial_capital", "nav_start",
     "investable_initial_capital", "weekly_path_start_nav", "growth_base_nav",
     "final_wealth_pre_tax", "pre_terminal_nav", "after_tax_terminal_wealth",
     "cagr", "after_tax_cagr", "real_cagr", "after_tax_real_cagr",
     "volatility", "after_tax_volatility", "sharpe", "after_tax_sharpe",
     "sortino", "after_tax_sortino", "max_drawdown", "after_tax_max_drawdown",
     "calmar", "after_tax_calmar"]
    + [f"{p}_{f}" for p in ("best", "worst") for f in YEAR_FIELDS]
    + [f"after_tax_{p}_{f}" for p in ("best", "worst") for f in YEAR_FIELDS]
    + ["trade_count", "turnover", "turnover_annualized",
       "total_tax_paid", "dividend_tax_paid", "rf_interest_tax_paid", "capital_gains_tax_paid",
       "solidarity_tax_paid", "internal_trading_tax_paid", "foundation_distribution_tax_paid",
       "foundation_setup_cost_paid", "foundation_admin_cost_paid", "foundation_admin_cost_weekly",
       "foundation_admin_cost_terminal", "foundation_gross_distributions_paid",
       "foundation_net_distributions_paid", "distribution_capital_basis_remaining",
       "distributed_amount", "distribution_tax_base_mode",
       "distribution_tax_base", "pre_tax_final_admin_cost", "pre_tax_terminal_trading_costs",
       "terminal_trade_count", "terminal_traded_value", "terminal_transaction_costs",
       "terminal_slippage", "terminal_liquidation_costs", "terminal_capital_gains_tax",
       "terminal_solidarity_tax", "terminal_internal_trading_tax", "terminal_foundation_tax",
       "terminal_tax_total",
       "transaction_cost_bps", "slippage_bps", "rebalance_mode", "rebalance_band_pp",
       "sortino_mar_annual",
       "cpi_label", "real_return_warning", "cpi_start_month", "cpi_end_month", "cpi_start",
       "cpi_end", "cpi_start_imputed", "cpi_end_imputed", "cpi_note",
       "as_of_date", "dropped_incomplete_weeks",
       "common_data_start", "common_data_end", "range_truncations", "range_truncation_detail",
       "signal_initial_state", "warmup_fallback_assets"]
    + [f"target_{s}" for s in SLEEVES]
    + [f"risk_{st}_share_{a}" for a in ("stocks", "gold", "btc") for st in ("on", "off")]
    + [f"signal_{a}_{f}" for a in ("stocks", "gold", "btc") for f in SIGNAL_PARAM_FIELDS]
    + ["applied_dividend_tax_rate", "applied_capital_gains_rate", "applied_solidarity_rate",
       "applied_rf_interest_rate", "applied_distribution_rate", "applied_internal_trading_tax_rate"]
    + list(APPLIED_PROFILE_FIELDS)
    + [k.replace(".", "_") for k in TAX_ASSUMPTION_KEYS])


def warmup_fallback_assets(report) -> list:
    """Assets whose signal warm-up was shorter than required and that started under the
    explicit signal.initial_state=RISK_ON opt-in (Q-013, SIG-003)."""
    if report is None:
        return []
    return sorted({i.role for i in report.issues if i.code == "warmup_short"})


def applied_profile_cells(params) -> dict:
    """APPLIED_PROFILE_FIELDS of one row (params: TaxParams | FoundationParams | None)."""
    individual = params is not None and hasattr(params, "solidarity_threshold_pln")
    foundation = params is not None and hasattr(params, "distribution_rate")
    na = NOT_APPLICABLE
    return {
        "applied_solidarity_threshold_pln": params.solidarity_threshold_pln if individual else na,
        "applied_external_solidarity_base_pln": (params.external_solidarity_base_pln if individual
                                                 else na),
        "applied_loss_carryforward_years": params.loss_carryforward_years if individual else na,
        "applied_loss_offset_fraction": params.loss_offset_fraction if individual else na,
        "applied_foundation_tax_event": params.tax_event if foundation else na,
        "applied_distribution_tax_base": params.distribution_tax_base if foundation else na,
        "applied_foundation_setup_cost_pln": params.setup_cost_pln if foundation else 0.0,
        "applied_foundation_annual_admin_cost_pln": (params.annual_admin_cost_pln if foundation
                                                     else 0.0),
        "applied_foundation_admin_cost_proration": params.admin_cost_proration if foundation else na,
    }


def _year_cells(prefix, y) -> dict:
    return {f"{prefix}year": y.year if y else None, f"{prefix}year_return": y.ret if y else None,
            f"{prefix}year_is_partial": y.is_partial if y else None}


def summary_row(cfg, res) -> dict:
    """Maps already computed results (metrics.RunMetrics, terminal settlement, tax state,
    configuration) to the summary columns; no metric is computed here."""
    m, t = res.metrics, res.terminal
    totals = t.final_tax_state.totals() if t else {}
    params = res.tax_params
    foundation = hasattr(params, "distribution_rate")
    shadow_cost = getattr(res.pre_tax, "terminal_cost", None)
    row = {
        "spec_version": cfg.get("app.spec_version"), "run_name": cfg.get("report.run_name"),
        "tax_profile": cfg.get("tax.profile"), "pre_tax_method": res.pre_tax.method,
        **{k: None for k in WALK_FORWARD_FIELDS},
        "optimization_mode": cfg.get("optimizer.mode") if cfg.command == "optimize" else None,
        **(getattr(res, "walk_forward", None) or {}),
        "requested_start": cfg.start, "requested_end": cfg.end,
        "effective_first_week": res.engine.weeks[0].week_key,
        "effective_last_week": res.engine.weeks[-1].week_key,
        "inception_date": res.first_week - dt.timedelta(days=7), "elapsed_days": m.elapsed_days,
        "weeks": m.weeks, "initial_capital": float(cfg.get("portfolio.initial_capital_pln")),
        # Q-033: nav_start = growth_base_nav = CAGR/real-CAGR denominator (initial capital before
        # a foundation setup cost); the weekly path, drawdown and calendar years start at
        # weekly_path_start_nav = investable_initial_capital
        "nav_start": m.nav_start, "growth_base_nav": m.growth_base_nav,
        "investable_initial_capital": res.engine.initial_ledger.nav,
        "weekly_path_start_nav": m.path_start_nav,
    }
    for k in ("final_wealth_pre_tax", "pre_terminal_nav", "after_tax_terminal_wealth", "cagr",
              "after_tax_cagr", "real_cagr", "after_tax_real_cagr", "volatility",
              "after_tax_volatility", "sharpe", "after_tax_sharpe", "sortino", "after_tax_sortino",
              "max_drawdown", "after_tax_max_drawdown", "calmar", "after_tax_calmar", "trade_count",
              "turnover", "turnover_annualized", "terminal_trade_count", "terminal_traded_value"):
        row[k] = getattr(m, k)
    row.update(_year_cells("best_", m.best_year))
    row.update(_year_cells("worst_", m.worst_year))
    row.update(_year_cells("after_tax_best_", m.after_tax_best_year))
    row.update(_year_cells("after_tax_worst_", m.after_tax_worst_year))
    for k in ("total_tax_paid", "dividend_tax_paid", "rf_interest_tax_paid", "capital_gains_tax_paid",
              "solidarity_tax_paid", "internal_trading_tax_paid", "foundation_distribution_tax_paid",
              "foundation_setup_cost_paid", "foundation_admin_cost_paid", "foundation_admin_cost_weekly",
              "foundation_admin_cost_terminal", "foundation_gross_distributions_paid",
              "foundation_net_distributions_paid"):
        row[k] = totals.get(k, 0.0)
    # Q-037: remaining capital basis of the gain_only distribution base (schedule mode only)
    row["distribution_capital_basis_remaining"] = (
        getattr(t.final_tax_state, "distribution_capital_basis_remaining", None) if t else None)
    row.update({
        "distributed_amount": getattr(t, "distributed_amount", None),
        "distribution_tax_base_mode": getattr(t, "distribution_tax_base_mode", None),
        "distribution_tax_base": getattr(t, "distribution_tax_base", None),
        "pre_tax_final_admin_cost": shadow_cost.admin_cost.amount if shadow_cost else 0.0,
        "pre_tax_terminal_trading_costs": math.fsum(x.transaction_cost + x.slippage
                                                    for x in shadow_cost.trades) if shadow_cost else 0.0,
        "terminal_transaction_costs": t.terminal_transaction_costs if t else 0.0,
        "terminal_slippage": t.terminal_slippage if t else 0.0,
        "terminal_liquidation_costs": t.terminal_trading_costs if t else 0.0,
        "terminal_capital_gains_tax": t.terminal_capital_gains_tax if t else 0.0,
        "terminal_solidarity_tax": t.terminal_solidarity_tax if t else 0.0,
        "terminal_internal_trading_tax": getattr(t, "terminal_internal_trading_tax", 0.0) if t else 0.0,
        "terminal_foundation_tax": getattr(t, "terminal_foundation_tax", 0.0) if t else 0.0,
        "terminal_tax_total": t.terminal_tax_total if t else 0.0,
        "transaction_cost_bps": float(cfg.get("portfolio.transaction_cost_bps")),
        "slippage_bps": float(cfg.get("portfolio.slippage_bps")),
        "rebalance_mode": cfg.get("portfolio.rebalance"),
        "rebalance_band_pp": cfg.get("portfolio.rebalance_band_pp"),
        "sortino_mar_annual": float(cfg.get("metrics.sortino_mar_annual")),
        "as_of_date": res.as_of, "dropped_incomplete_weeks": res.dropped_incomplete_weeks,
        "common_data_start": res.common_range[0] if res.common_range else None,
        "common_data_end": res.common_range[1] if res.common_range else None,
        "range_truncations": len(res.truncations),
        "range_truncation_detail": "|".join(f"{r}:{side}:{own}->{eff}"
                                            for r, side, own, eff in res.truncations),
        # Q-013: the explicit RISK_ON opt-in is the only fallback for a short warm-up
        "signal_initial_state": cfg.get("signal.initial_state"),
        "warmup_fallback_assets": "|".join(warmup_fallback_assets(getattr(res, "report", None))),
    })
    c = m.cpi
    row.update({"cpi_label": c.label if c else cfg.get("cpi.label"),
                "real_return_warning": c.warning if c else "real metrics not available (CPI)",
                "cpi_start_month": c.start_month if c else None,
                "cpi_end_month": c.end_month if c else None,
                "cpi_start": c.cpi_start if c else None, "cpi_end": c.cpi_end if c else None,
                "cpi_start_imputed": c.start_imputed if c else None,
                "cpi_end_imputed": c.end_imputed if c else None,
                "cpi_note": c.note if c else "CPI unavailable (REAL-003)"})
    for s in SLEEVES:
        row[f"target_{s}"] = res.targets[s]
    for a, (on, off) in m.risk_state_shares.items():
        row[f"risk_on_share_{a}"], row[f"risk_off_share_{a}"] = on, off
    for a, p in res.inputs.params.items():
        for f in SIGNAL_PARAM_FIELDS:
            row[f"signal_{a}_{f}"] = getattr(p, f)
    row.update({"applied_dividend_tax_rate": params.dividend_rate if params else 0.0,
                "applied_capital_gains_rate": getattr(params, "capital_gains_rate", 0.0),
                "applied_solidarity_rate": getattr(params, "solidarity_rate", 0.0),
                "applied_rf_interest_rate": params.rf_interest_rate if params else 0.0,
                "applied_distribution_rate": params.distribution_rate if foundation else 0.0,
                "applied_internal_trading_tax_rate": params.internal_trading_tax_rate if foundation
                else 0.0})
    row.update(applied_profile_cells(params))
    for k in TAX_ASSUMPTION_KEYS:
        row[k.replace(".", "_")] = cfg.get(k)
    unknown = set(row) - set(SUMMARY_FIELDS)
    if unknown:
        raise KeyError(f"summary fields not declared: {sorted(unknown)}")
    return row
