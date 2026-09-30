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
    doc["final_year_liability"] = {k: fmt(v) if not isinstance(v, (int, float)) or isinstance(v, bool)
                                   else v for k, v in vars(t.final_liability).items()
                                   if k != "buckets_used"}
    doc["final_year_liability"]["buckets_used"] = [list(b) for b in t.final_liability.buckets_used]
    doc["terminal_payments"] = [{k: fmt(getattr(p, k)) for k in PAYMENT_FIELDS} for p in t.terminal_payments]
    doc["reserve_consolidation"] = [{k: fmt(getattr(x, k)) for k in TRANSFER_FIELDS}
                                    for x in t.terminal_transfers]
    doc["note"] = ("terminal settlement is not a backtest week: weekly_portfolio.csv ends with "
                   "pre_terminal_nav; terminal costs and taxes are not in the weekly NAV path")
    return doc


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
    return {"annual_tax_paid": annual, "dividend_tax": div, "rf_interest_tax": rf,
            "taxes_paid": math.fsum([annual, div, rf])}


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
               "taxes_paid"] + [f"state_{a}" for a in assets])


def weekly_portfolio_rows(result, targets, assets, tax_events=()) -> list:
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
        for s in SLEEVES:
            r[f"target_{s}"] = targets[s]
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
            r[f"state_{a}"] = w.effective_states[a]
        rows.append(r)
    return rows
