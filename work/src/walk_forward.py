"""Walk-forward optimize (WF-001..016, REP-016, TEST-021/037/049, CLI-011, Q-020, Q-022).

TRAIN is an independent, hypothetical backtest used only to choose parameters; OOS is ONE
continuous portfolio path carried across windows. A training run never hands its portfolio to
the OOS path; it provides only the selected parameters, the selected strategic weights and the
selected parameters' signal state (with its pending executions) at the end of the training
window - all computed from information before the OOS start.

Pipeline:
  1. the candidate grid (weights x the listed signal/band dimensions) is built and validated
     before any data (Q-041 weight rules, WF-006..010 grids); the union of the candidates'
     assets and the largest warm-up of the grid (max MA, confirmation, delay) are fixed;
  2. the data is prepared ONCE (``app.prepare_run(assets=union)``); run.start, if given, is the
     earliest training history, otherwise the first week with the grid's full warm-up;
  3. windows: first test anchor = train start + train_years calendar years (full training
     history is mandatory, Q-020), later anchors every step_years (default test_years; integer
     steps are calendar years, fractional steps round_half_up(step_years * 365.2425) days);
     test_start = first retained Friday >= anchor, OOS segment = [test_start, retained week
     before the next test_start] (last one to the global end, always kept - WF-015);
  4. per window: a training view that physically holds only information before test_start
     (``PreparedRun.training_view``), the in-sample optimizer's evaluation, classification and
     Q-041 selection (``optimizer.evaluate_candidates`` / ``assemble``) on it;
  5. the OOS path runs segment by segment through the central engine: segment 1 is a fresh
     initial allocation (no trade, no cost; foundation setup cost once) with the selected
     training signal state; each later segment continues from the carried state (portfolio
     snapshot with lots and unit prices, tax / foundation state, live signal trackers, pending
     band trigger) - trackers of unchanged parameters are carried, changed ones are replaced by
     the selected training state (old pending executions cancelled, new ones adopted); a
     ``walk_forward_rebalance`` in step 3 of the first week aligns the portfolio when targets,
     assets, rebalancing or the signal split change; a continuous zero-tax shadow runs alongside;
  6. after the last OOS week only: terminal settlement (profile rules) and the shadow's final
     cost; metrics and summary on the stitched OOS path; outputs.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import math
import shutil
import sys
from bisect import bisect_left
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .allocation import strategic_targets
from .app import (PortfolioRunResult, PreTaxRun, PreparedRun, build_hooks, check_supported_run,
                  distribution_ignored_issues, find_tax_hooks, needs_distribution_schedule,
                  prepare_run, prepared_input_sha256, write_portfolio_outputs)
from .calendar import elapsed_days, inception_date, last_key_on_or_before
from .config import ResolvedConfig, int_grid, parse_decimal_grid
from .engine import EngineResult, EngineStart, run_engine
from .errors import BacktestError, ConfigError, InsufficientHistoryError
from .foundation import FOUNDATION_PROFILES, FoundationHooks, map_distribution_schedule
from .manifest import code_version, run_timestamp
from .metrics import PathSeries, compute_run_metrics, cpi_window, turnover
from .models import RISKY_ASSETS, State, TradeReason, canonical_assets
from .optimizer import (OPT_FIELDS, OptimizerError, OptimizerSpec, Candidate, OBJECTIVES,
                        assemble, build_weight_grid, evaluate_candidates, relevant_drawdown_metric,
                        required_assets, resolve_jobs, selection_key, TIE_BREAK)
from .rebalancing import BoundaryRebalanceHooks, hooks_from_config
from .reporting import SUMMARY_FIELDS, run_directory, write_csv, write_json
from .settlement import settle_foundation, settle_foundation_shadow_costs, settle_terminal
from .signal_analysis import install_tracker
from .tax import IndividualTaxHooks

ALLOWED_PARAMETERS = ("weights", "ma", "threshold", "delay", "confirmation", "sell_fraction",
                      "rebalance_band")
DIMENSIONS = ("ma", "threshold", "delay", "confirmation", "sell_fraction", "rebalance_band")
DAYS_PER_YEAR = Decimal("365.2425")
MANIFEST = "walk_forward_manifest.json"
DUST = 1e-9
SLEEVES = ("stocks", "gold", "btc", "rf")

RESULT_FIELDS = [
    "window_id", "window_type", "train_start", "train_end", "train_weeks", "test_start",
    "nominal_test_end", "actual_oos_end", "actual_test_days", "actual_test_years", "oos_weeks",
    "selected_grid_index", "training_objective", "training_objective_value",
    "training_relevant_max_drawdown", "training_candidates", "training_eligible",
    "selected_weight_stocks", "selected_weight_gold", "selected_weight_btc", "selected_weight_rf",
    "selected_ma", "selected_threshold", "selected_delay", "selected_confirmation",
    "selected_sell_fraction", "selected_rebalance_mode", "selected_rebalance_band_pp",
    "oos_nav_start", "oos_nav_end", "oos_return", "oos_trade_count", "oos_turnover",
    "oos_tax_paid", "oos_costs_paid", "boundary_rebalance", "boundary_trade_count",
    "training_input_sha256", "max_training_week"]
BOUNDARY_FIELDS = [
    "window_id", "week_key", "event", "old_targets", "new_targets", "old_signal_params",
    "new_signal_params", "carried_signal_state", "reconstructed_signal_state",
    "carried_state_matches_training", "cancelled_pending_executions",
    "adopted_pending_executions", "pending_rebalance_action", "boundary_rebalance",
    "boundary_reasons", "boundary_trade_count", "shadow_boundary_rebalance"]
DIM_FIELDS = [f"candidate_{d}" for d in DIMENSIONS]
TRAINING_FIELDS = (["window_id"] + OPT_FIELDS[:5] + ["eligible_rank", "objective_gap_to_selected",
                                                      "selected_neighbour"]
                   + OPT_FIELDS[5:] + DIM_FIELDS + list(SUMMARY_FIELDS))


class WalkForwardError(BacktestError):
    exit_code = 4


# ============================================================================ dates
def add_years(day: dt.date, years: int) -> dt.date:
    """Calendar-year shift; 29 February maps to 28 February in a common year."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def step_rule(step_years: float) -> tuple:
    """WF-012 (Q-022): an integer step is calendar years; any other step is
    round_half_up(step_years * 365.2425) calendar days. Returns (kind, amount)."""
    if float(step_years).is_integer():
        return "calendar_years", int(step_years)
    days = (Decimal(repr(float(step_years))) * DAYS_PER_YEAR).quantize(Decimal(1), ROUND_HALF_UP)
    return "days", int(days)


def shift(anchor: dt.date, rule: tuple, n: int) -> dt.date:
    kind, amount = rule
    return add_years(anchor, amount * n) if kind == "calendar_years" else anchor + dt.timedelta(days=amount * n)


# ============================================================================ specification
@dataclass(frozen=True)
class WalkForwardSpec:
    base: ResolvedConfig
    opt: OptimizerSpec              # candidates (weights x dimensions), objective, limit, jobs
    window_type: str
    train_years: int
    test_years: int
    step_years: float
    step: tuple                     # step_rule()
    parameters: tuple
    dimensions: dict                # optimized dimension -> resolved grid (tuple)
    warmup_params: dict             # asset -> SignalParams with the grid's largest warm-up
    coordinates: dict = field(default_factory=dict)   # grid_index -> grid position (WF-005)


def _dimension_grids(cfg: ResolvedConfig, params: tuple) -> dict:
    """WF-006..010, Q-022: one grid per listed signal/band parameter, shared by every active
    risky asset (a single grid value sets the parameter of all of them)."""
    dims = {}
    if "ma" in params:                                                       # WF-007
        dims["ma"] = int_grid(cfg.get("optimizer.ma_grid") or [50], "optimizer.ma_grid", minimum=2)
    if "threshold" in params:                                                # WF-008
        vals = [float(d) for d in parse_decimal_grid(cfg.get("optimizer.threshold_grid") or [0.03],
                                                     "optimizer.threshold_grid")]
        if any(not 0.0 <= v < 1.0 for v in vals):
            raise ConfigError("optimizer.threshold_grid: decimal fractions in [0, 1) (CLI "
                              "percent, Q-026)")
        dims["threshold"] = vals
    if "delay" in params:                                                    # WF-009
        dims["delay"] = int_grid(cfg.get("optimizer.delay_grid") or [1], "optimizer.delay_grid")
    if "confirmation" in params:                                             # WF-010
        grid = cfg.get("optimizer.confirmation_grid")
        if not grid:
            raise ConfigError("optimize-params confirmation requires an explicit "
                              "--confirmation-grid (WF-010); no common default is derived from "
                              "the per-asset confirmations")
        dims["confirmation"] = int_grid(grid, "optimizer.confirmation_grid")
    if "sell_fraction" in params and cfg.get("optimizer.sell_fraction_grid"):
        vals = [float(d) for d in parse_decimal_grid(cfg.get("optimizer.sell_fraction_grid"),
                                                     "optimizer.sell_fraction_grid")]
        if any(not 0.0 <= v <= 1.0 for v in vals):
            raise ConfigError("optimizer.sell_fraction_grid: decimal fractions 0..1 (Q-026)")
        dims["sell_fraction"] = vals
    if "rebalance_band" in params:
        grid = cfg.get("optimizer.rebalance_band_grid")
        if not grid:
            raise ConfigError("optimize-params rebalance_band requires --band-pp "
                              "(optimizer.rebalance_band_grid, percentage points)")
        vals = [float(d) for d in parse_decimal_grid(grid, "optimizer.rebalance_band_grid")]
        if any(not 0.0 < v <= 100.0 for v in vals):
            raise ConfigError("optimizer.rebalance_band_grid: percentage points in (0, 100]")
        dims["rebalance_band"] = vals
    for k, v in dims.items():
        dup = sorted({str(x) for x in v if v.count(x) > 1})
        if dup:
            raise ConfigError(f"walk-forward {k} grid: duplicate value(s) {dup}")
    return {k: tuple(v) for k, v in dims.items()}


def _overrides(name: str, value) -> tuple:
    out = []
    for a in RISKY_ASSETS:
        p = f"signals.{a}."
        if name == "ma":
            out.append((p + "ma_length", value))
        elif name == "threshold":
            out += [(p + "threshold_off", value), (p + "threshold_on", value)]
        elif name == "delay":
            out.append((p + "delay_weeks", value))
        elif name == "confirmation":
            out += [(p + "confirm_off_weeks", value), (p + "confirm_on_weeks", value)]
        elif name == "sell_fraction":
            out.append((p + "sell_fraction", value))
    if name == "rebalance_band":
        out = [("portfolio.rebalance", "band"), ("portfolio.rebalance_band_pp", value)]
    return tuple(out)


def resolve_walk_forward(cfg: ResolvedConfig) -> WalkForwardSpec:
    """Validate the walk-forward configuration and build the whole candidate grid before any
    data is loaded."""
    if cfg.get("optimizer.mode") != "walk-forward":
        raise ConfigError("walk-forward requires optimizer.mode = walk-forward")
    params = tuple(cfg.get("optimizer.parameters") or ())
    bad = [p for p in params if p not in ALLOWED_PARAMETERS]
    if not params or bad or len(set(params)) != len(params):
        raise ConfigError(f"optimizer.parameters / --optimize-params: a non-empty list of distinct "
                          f"values from {ALLOWED_PARAMETERS}, got {list(params)} (WF-006)")
    if cfg.get("run.asset"):
        raise ConfigError("walk-forward optimizes a portfolio; --asset is not used")
    train = cfg.get("optimizer.train_years")
    test = cfg.get("optimizer.test_years")
    for name, v in (("optimizer.train_years", train), ("optimizer.test_years", test)):
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ConfigError(f"{name}: integer > 0 years required, got {v!r} (WF-002/003)")
    step = cfg.get("optimizer.step_years")
    step = float(test) if step is None else float(step)
    if math.isnan(step) or step <= 0:
        raise ConfigError(f"optimizer.step_years must be > 0, got {step!r} (WF-012)")
    window_type = cfg.get("optimizer.window_type")
    tie = list(cfg.get("optimizer.tie_break") or TIE_BREAK)
    if tie != list(TIE_BREAK):
        raise ConfigError(f"optimizer.tie_break: only {list(TIE_BREAK)} is supported (Q-041)")
    objective = cfg.get("optimizer.objective")
    limit = cfg.get("optimizer.max_drawdown_limit")
    if limit is not None:
        limit = float(limit)
        if math.isnan(limit) or not 0.0 <= limit <= 1.0:
            raise ConfigError("optimizer.max_drawdown_limit: decimal 0..1 (CLI percent, Q-026)")
    check_supported_run(cfg)                                    # Q-037 / Q-047 before data
    if cfg.source_of("report.run_name") in ("defaults", "command"):
        cfg = cfg.with_overrides({"report.run_name": "walk_forward"})
    if "weights" in params:                                     # Q-041 weight grid
        if cfg.get("allocation.targets") is not None or cfg.get("allocation.single_asset") not in (None, False):
            raise ConfigError("walk-forward with optimize-params weights builds the targets from "
                              "the weight grids; remove allocation.targets / --weights")
        base = cfg
        weights, grid = build_weight_grid(cfg)
    else:                                                       # ALLOC-001: explicit targets
        targets = strategic_targets(cfg)
        base = cfg.with_overrides({"allocation.single_asset": False, "allocation.targets": targets})
        weights = (Candidate(1, tuple((s, float(targets[s])) for s in SLEEVES),
                             math.fsum(targets.values()), "pending"),)
        grid = {"weights": "fixed (allocation.targets)", "targets": dict(targets)}
    if not any(c.weight_valid for c in weights):
        raise ConfigError(f"walk-forward: none of the {len(weights)} weight combinations is valid "
                          "(Q-041); no data was loaded")
    grids = {"ma": "ma_grid", "threshold": "threshold_grid", "delay": "delay_grid",
             "confirmation": "confirmation_grid", "sell_fraction": "sell_fraction_grid",
             "rebalance_band": "rebalance_band_grid"}
    stray = [k for d, k in grids.items() if d not in params and cfg.get(f"optimizer.{k}")
             is not None and cfg.source_of(f"optimizer.{k}") in ("file", "cli")]
    if "weights" not in params:
        stray += [k for k in ("btc_weight", "gold_weight", "stocks_weight", "rf_weight")
                  if cfg.source_of(f"optimizer.{k}") in ("file", "cli")]
    if stray:
        raise ConfigError(f"walk-forward: grid(s) {stray} given for parameters not listed in "
                          f"--optimize-params {list(params)}; only the listed parameters vary "
                          "(WF-006), the others keep their configured values")
    dims = _dimension_grids(cfg, params)
    names = [d for d in DIMENSIONS if d in dims]
    combos = [()]
    for name in names:                                          # nesting: dimension order
        combos = [c + ((name, v),) for c in combos for v in dims[name]]
    candidates, idx = [], 0
    for w in weights:                                           # weights outer
        for combo in combos:
            idx += 1
            over = tuple(o for name, v in combo for o in _overrides(name, v))
            candidates.append(dataclasses.replace(w, grid_index=idx, overrides=over, dims=combo))
    union = required_assets(weights)
    if not union:
        raise ConfigError("walk-forward: no weight-valid candidate holds a risky asset")
    warm = {}
    for a in union:
        p = base.signal_params(a)
        conf = max(dims["confirmation"]) if "confirmation" in dims else None
        warm[a] = dataclasses.replace(
            p, ma=max(dims["ma"]) if "ma" in dims else p.ma,
            confirm_off=conf or p.confirm_off, confirm_on=conf or p.confirm_on,
            delay=max(dims["delay"]) if "delay" in dims else p.delay)
    n_valid = sum(1 for c in candidates if c.weight_valid)
    metric, direction = OBJECTIVES[objective]
    grid = dict(grid, **{f"{k}_grid": list(v) for k, v in dims.items()})
    opt = OptimizerSpec(base, tuple(candidates), grid, objective, metric, direction,
                        relevant_drawdown_metric(objective), limit, union,
                        cfg.get("performance.jobs"), resolve_jobs(cfg.get("performance.jobs"), n_valid))
    return WalkForwardSpec(base, opt, window_type, train, test, step, step_rule(step), params,
                           dims, warm, grid_coordinates(opt.candidates, grid, dims))


def grid_coordinates(candidates, grid: dict, dims: dict) -> dict:
    """WF-005: the position of every candidate on the grid axes (weight grids btc, gold,
    explicit stocks, then the signal/band dimensions); two candidates are neighbours when
    they differ by one step on exactly one axis."""
    axes = [(s, list(grid[f"{s}_weight"])) for s in ("btc", "gold", "stocks")
            if isinstance(grid.get(f"{s}_weight"), list)]
    out = {}
    for c in candidates:
        t, d = c.targets, dict(c.dims)
        out[c.grid_index] = (tuple(vals.index(t[s]) for s, vals in axes)
                             + tuple(dims[k].index(d[k]) for k in DIMENSIONS if k in dims))
    return out


def neighbours(coords: dict, grid_index: int) -> tuple:
    me = coords[grid_index]
    return tuple(g for g, c in sorted(coords.items())
                 if sum(abs(x - y) for x, y in zip(c, me)) == 1)


def robustness_rows(spec: WalkForwardSpec, training, selection) -> list:
    """WF-005 per training window: every grid row with its rank among the eligible rows
    (Q-041 order), the objective gap to the selected row (>= 0 = worse by that much) and
    whether it is a grid neighbour of the selected row."""
    order = sorted((r for r in training.rows if r["eligible"]),
                   key=lambda r: selection_key(r, spec.opt.objective))
    rank = {r["grid_index"]: i + 1 for i, r in enumerate(order)}
    near = set(neighbours(spec.coordinates, selection.grid_index)) if spec.coordinates else set()
    sign = 1.0 if spec.opt.direction == "maximize" else -1.0
    out = []
    for r in training.rows:
        v = r["objective_value"]
        out.append(dict(r, window_id=selection.window_id, eligible_rank=rank.get(r["grid_index"]),
                        objective_gap_to_selected=None if v is None
                        else sign * (selection.objective_value - v),
                        selected_neighbour=r["grid_index"] in near))
    return out


# ============================================================================ windows
@dataclass(frozen=True)
class Window:
    window_id: int
    anchor: dt.date
    train_start: dt.date
    train_end: dt.date
    train_weeks: int
    test_start: dt.date
    nominal_test_end: dt.date
    actual_oos_end: dt.date
    oos_weeks: tuple

    @property
    def actual_test_days(self) -> int:
        """WF-016: actual_oos_end - test_start + 1 calendar day."""
        return (self.actual_oos_end - self.test_start).days + 1

    @property
    def actual_test_years(self) -> float:
        return self.actual_test_days / float(DAYS_PER_YEAR)


def plan_windows(weeks: tuple, window_type: str, train_years: int, test_years: int,
                 step: tuple) -> tuple:
    """WF-002/003/011/012/015/016, Q-020, Q-022 on the retained global calendar ``weeks``
    (weeks[0] = the earliest training week)."""
    if not weeks:
        raise InsufficientHistoryError("insufficient history for requested walk-forward training "
                                       "window: empty calendar")
    start, end = weeks[0], weeks[-1]

    def first_on_or_after(day):
        i = bisect_left(weeks, day)
        return weeks[i] if i < len(weeks) else None

    def last_before(day):
        i = bisect_left(weeks, day)
        return weeks[i - 1] if i > 0 else None

    first_anchor = add_years(start, train_years)
    if first_on_or_after(first_anchor) is None:
        raise InsufficientHistoryError(
            f"insufficient history for requested walk-forward training window: train_years="
            f"{train_years} needs history from {start} to {first_anchor}, the common data ends "
            f"{end} (Q-020: the training window is never shortened)")
    anchors, starts = [], []
    n = 0
    while True:
        a = shift(first_anchor, step, n)
        ts = first_on_or_after(a)
        if ts is None:
            break
        if not starts or ts > starts[-1]:
            anchors.append(a)
            starts.append(ts)
        n += 1
    out = []
    for i, (a, ts) in enumerate(zip(anchors, starts)):
        train_start = start if window_type == "anchored" else first_on_or_after(add_years(a, -train_years))
        train_end = last_before(ts)
        oos_end = last_before(starts[i + 1]) if i + 1 < len(starts) else end
        oos = tuple(w for w in weeks if ts <= w <= oos_end)
        nominal = last_key_on_or_before(add_years(a, test_years) - dt.timedelta(days=1))
        out.append(Window(i + 1, a, train_start, train_end,
                          sum(1 for w in weeks if train_start <= w <= train_end), ts, nominal,
                          oos_end, oos))
    return tuple(out)


# ============================================================================ selection
@dataclass(frozen=True)
class Selection:
    window_id: int
    grid_index: int
    config: ResolvedConfig
    targets: dict
    params: dict                    # active asset -> SignalParams
    signal_states: dict             # active asset -> SignalTrackerSnapshot (end of training)
    rebalance: tuple                # (mode, band_pp)
    objective_value: float
    relevant_max_drawdown: float
    row: dict
    training_input_sha256: str
    max_training_week: dt.date
    top: tuple = ()                 # WF-005: (grid_index, objective_value, gap) of the top 5


def selection_from_result(window_id, spec_opt: OptimizerSpec, view: PreparedRun, res) -> Selection:
    cfg = res.selected_config
    engine = res.selected_result.engine
    sel = res.selected
    eligible = sorted((r for r in res.rows if r["eligible"]),
                      key=lambda r: selection_key(r, spec_opt.objective))[:5]
    sign = 1.0 if spec_opt.direction == "maximize" else -1.0
    top = tuple((r["grid_index"], r["objective_value"],
                 sign * (sel["objective_value"] - r["objective_value"])) for r in eligible)
    return Selection(window_id, sel["grid_index"], cfg, dict(res.selected_result.inputs.targets),
                     dict(res.selected_result.inputs.params), dict(engine.final_signal_states),
                     _rebalance_of(cfg), sel["objective_value"], sel["relevant_max_drawdown"], sel,
                     training_input_sha256(view), view.max_information_week(), top)


def selection_for_config(window_id, view: PreparedRun, cfg: ResolvedConfig) -> Selection:
    """A selection from one given configuration (tests and audits): the configuration's
    standalone training run on ``view`` provides the signal state."""
    from .app import run_prepared
    res = run_prepared(cfg, view, write=False)
    return Selection(window_id, 0, cfg, dict(res.inputs.targets), dict(res.inputs.params),
                     dict(res.engine.final_signal_states), _rebalance_of(cfg), float("nan"),
                     float("nan"), {}, training_input_sha256(view), view.max_information_week())


def training_input_sha256(view: PreparedRun) -> str:
    """Hash of the information a training window holds (weeks, market records, signal
    observations, calendar) - identical whenever the data before the test start is identical,
    whatever follows it (TEST-021)."""
    return prepared_input_sha256(view, include_sources=False)


def _rebalance_of(cfg) -> tuple:
    band = cfg.get("portfolio.rebalance_band_pp")
    return (cfg.get("portfolio.rebalance"), None if band is None else float(band))


# ============================================================================ OOS continuation
@dataclass(frozen=True)
class OOSContinuationState:
    """Immutable state of one OOS path at the end of a segment (Q-022): portfolio snapshot
    (ledger incl. rf_base and reserves, lots with ids and cost basis, unit prices, costs), the
    tax or foundation state (realizations, loss buckets, open year, annual liabilities, admin
    years, totals, events), the live signal trackers, the pending band trigger, the last retained
    week and the selection in force."""
    portfolio: object
    tax_state: object
    signal_states: dict
    pending_band: Optional[tuple]
    last_week: dt.date
    targets: dict
    params: dict
    rebalance: tuple


def _pending_text(pending) -> str:
    return "|".join(f"{e.asset}:{e.confirm_week}->{e.execution_week}:{e.target_state.value}"
                    for e in pending)


def _params_text(params: dict) -> str:
    return "|".join(f"{a}:ma={p.ma},thr={p.threshold_off}/{p.threshold_on},conf={p.confirm_off}/"
                    f"{p.confirm_on},delay={p.delay},sf={p.sell_fraction}"
                    for a, p in sorted(params.items()))


def _targets_text(t: dict) -> str:
    return "|".join(f"{s}={t[s]!r}" for s in SLEEVES) if t else ""


def boundary_plan(prev: Optional[OOSContinuationState], sel: Selection, window: Window) -> dict:
    """Signal states, pending band trigger and boundary rebalance decision for the first week
    of an OOS segment (Q-022): a tracker whose asset stays active with identical parameters
    is carried (live SMA window, counters, effective state, pending executions); a changed or
    newly active asset gets the selected training state (its old pending executions are
    cancelled, the training pending executions adopted); an asset leaving the portfolio has
    its pending executions cancelled. The boundary rebalance is forced when the targets, the
    active assets or the rebalancing change, when a replaced tracker's state (or its RISK_OFF
    split) differs from the previous one, or when a non-active asset still holds value."""
    active = canonical_assets(sel.params)
    if prev is None:                                            # first OOS segment
        return {"signal_states": {a: sel.signal_states[a] for a in active}, "carried": (),
                "replaced": active, "cancelled": (),
                "adopted": tuple(e for a in active for e in sel.signal_states[a].pending
                                 if e.execution_week >= window.test_start),
                "matches": {}, "pending_band": None, "pending_action": "none", "force": False,
                "reasons": ("initial_allocation",), "old_targets": {}, "old_params": {}}
    states, carried, replaced, cancelled, adopted, matches = {}, [], [], [], [], {}
    reasons = []
    if prev.targets != sel.targets:
        reasons.append("strategic_weights_changed")
    if set(prev.params) != set(active):
        reasons.append("active_assets_changed")
    if prev.rebalance != sel.rebalance:
        reasons.append("rebalancing_changed")
    for a in active:
        old = prev.params.get(a)
        if old is not None and old == sel.params[a]:
            states[a] = prev.signal_states[a]                   # continue the live tracker
            carried.append(a)
            matches[a] = prev.signal_states[a].same_state(sel.signal_states[a])
            continue
        states[a] = sel.signal_states[a]                        # selected training state
        replaced.append(a)
        adopted += [e for e in sel.signal_states[a].pending if e.execution_week >= window.test_start]
        if old is None:
            continue
        cancelled += list(prev.signal_states[a].pending)
        installed = install_tracker(states[a], window.test_start)[0].effective_state
        before = prev.signal_states[a].effective_state
        if installed != before:
            reasons.append(f"signal_state_changed:{a}")
        elif installed == State.RISK_OFF and (old.sell_fraction, old.risk_off_action) != (
                sel.params[a].sell_fraction, sel.params[a].risk_off_action):
            reasons.append(f"risk_off_split_changed:{a}")
    for a in prev.params:
        if a not in active:
            cancelled += list(prev.signal_states[a].pending)
    ledger = prev.portfolio.ledger
    for a in RISKY_ASSETS:
        if a not in active and (ledger.asset(a) > DUST or ledger.reserve(a) > DUST):
            reasons.append(f"orphan_holding:{a}")
    reasons = tuple(dict.fromkeys(reasons))
    force = bool(reasons)
    keep_band = not force
    pending = prev.pending_band if keep_band else None
    action = ("none" if prev.pending_band is None else
              "carried" if keep_band else "cancelled_superseded_by_walk_forward_rebalance")
    return {"signal_states": states, "carried": tuple(carried), "replaced": tuple(replaced),
            "cancelled": tuple(cancelled), "adopted": tuple(adopted), "matches": matches,
            "pending_band": pending, "pending_action": action, "force": force, "reasons": reasons,
            "old_targets": dict(prev.targets), "old_params": dict(prev.params)}


@dataclass
class Segment:
    window: Window
    selection: Selection
    engine: EngineResult
    state: OOSContinuationState
    plan: dict
    boundary_event: Optional[object]


def run_segment(prepared: PreparedRun, window: Window, sel: Selection,
                prev: Optional[OOSContinuationState], inception: dt.date,
                shadow: bool, distributions: Optional[dict] = None) -> Segment:
    """One OOS segment of one path (actual or zero-tax shadow) through the central engine.
    ``distributions``: the scheduled foundation distributions whose actual week lies in this
    segment (Q-037; mapped once on the whole OOS calendar, so every row is paid exactly once)."""
    cfg = sel.config
    inputs = dataclasses.replace(prepared.inputs_for(cfg), weeks=window.oos_weeks,
                                 run_start=window.test_start)
    plan = boundary_plan(prev, sel, window)
    funding = BoundaryRebalanceHooks(hooks_from_config(cfg, plan["pending_band"]),
                                     window.test_start, plan["force"])
    tax_state = copy.deepcopy(prev.tax_state) if prev is not None else None
    hooks = build_hooks(cfg, inception, funding=funding, tax_state=tax_state, zero_rates=shadow,
                        distributions=distributions)
    start = None if prev is None else EngineStart(prev.portfolio, prev.last_week)
    result = run_engine(inputs, hooks, start=start, signal_states=plan["signal_states"],
                        carried=plan["carried"])
    tax = find_tax_hooks(hooks)
    state = OOSContinuationState(result.final_snapshot,
                                 copy.deepcopy(tax.state) if tax is not None else None,
                                 dict(result.final_signal_states), funding.pending,
                                 window.oos_weeks[-1], dict(inputs.targets), dict(inputs.params),
                                 sel.rebalance)
    return Segment(window, sel, result, state, plan, funding.event)


def stitch(results) -> EngineResult:
    """One chronological EngineResult of consecutive segments (each record exactly once)."""
    first, last = results[0], results[-1]
    cat = lambda name: tuple(x for r in results for x in getattr(r, name))   # noqa: E731
    return EngineResult(first.initial_ledger, cat("weeks"), cat("trades"), cat("payments"),
                        cat("transfers"), cat("rebalance_events"), cat("signal_records"),
                        cat("skipped_signal_observations"), cat("realizations"),
                        cat("dividend_reinvestments"), first.pre_start, last.final_ledger,
                        last.final_lots, cat("issues"), last.final_snapshot,
                        dict(last.final_signal_states))


@dataclass
class OOSPath:
    segments: list
    shadow_segments: list           # empty for tax.profile = none (the actual path is pre-tax)
    engine: EngineResult
    shadow_engine: EngineResult


def oos_distributions(prepared: PreparedRun, windows, cfg) -> tuple:
    """Q-037: the global distribution schedule mapped once onto the stitched OOS calendar ->
    ({actual week: rows}, ignored rows). Training runs apply the rows of their own range to
    their hypothetical objective; the live OOS path pays every row exactly once."""
    if cfg.get("tax.profile") not in FOUNDATION_PROFILES or not needs_distribution_schedule(cfg):
        return None, ()
    weeks = tuple(w for win in windows for w in win.oos_weeks)
    return map_distribution_schedule(prepared.distribution_schedule.rows, weeks)


def run_oos_path(prepared: PreparedRun, windows, selections, profile: str) -> OOSPath:
    """The continuous OOS path (and its continuous zero-tax shadow) for given selections."""
    inception = inception_date(windows[0].test_start)
    by_week, _ = oos_distributions(prepared, windows, selections[0].config)
    segs, shadow, prev, prev_s = [], [], None, None
    for w, sel in zip(windows, selections):
        dist = None if by_week is None else {k: v for k, v in by_week.items() if k in w.oos_weeks}
        seg = run_segment(prepared, w, sel, prev, inception, shadow=False, distributions=dist)
        segs.append(seg)
        prev = seg.state
        if profile != "none":
            s = run_segment(prepared, w, sel, prev_s, inception, shadow=True, distributions=dist)
            shadow.append(s)
            prev_s = s.state
    actual = stitch([s.engine for s in segs])
    return OOSPath(segs, shadow, actual, stitch([s.engine for s in shadow]) if shadow else actual)


# ============================================================================ run
@dataclass
class WalkForwardResult:
    spec: WalkForwardSpec
    prepared: PreparedRun
    windows: tuple
    selections: tuple
    training: tuple                 # per window: optimizer.OptimizerResult (rows, selection)
    path: OOSPath
    portfolio: PortfolioRunResult   # the stitched OOS path as a standard run result
    results_rows: tuple
    boundary_rows: tuple
    manifest: dict
    output_dir: Optional[object] = None


def _progress(cfg, text: str) -> None:
    if cfg.get("performance.progress"):
        print(text, file=sys.stderr, flush=True)


def run_walk_forward(cfg: ResolvedConfig, write: bool = True) -> WalkForwardResult:
    spec = resolve_walk_forward(cfg)
    prepared = prepare_run(spec.base, assets=spec.opt.union, warmup_params=spec.warmup_params,
                           auto_start=True)                                 # the only load
    windows = plan_windows(prepared.inputs.weeks, spec.window_type, spec.train_years,
                           spec.test_years, spec.step)
    return run_walk_forward_prepared(spec, prepared, windows, write)


def run_walk_forward_prepared(spec: WalkForwardSpec, prepared: PreparedRun, windows,
                              write: bool = True) -> WalkForwardResult:
    training, selections = [], []
    n = len(windows)
    for w in windows:
        view = prepared.training_view(w.train_start, w.train_end, w.test_start)
        label = f"walk-forward window {w.window_id}/{n} training"
        try:
            res = assemble(spec.opt, view, evaluate_candidates(spec.opt, view, label), False)
        except OptimizerError as e:
            raise WalkForwardError(f"walk-forward window {w.window_id} ({w.train_start}.."
                                   f"{w.train_end}): {e}") from e
        training.append(res)
        selections.append(selection_from_result(w.window_id, spec.opt, view, res))
        _progress(spec.base, f"walk-forward window {w.window_id}/{n}: selected grid_index "
                             f"{res.selected['grid_index']}")
    path = run_oos_path(prepared, windows, selections, spec.base.get("tax.profile"))
    return finish(spec, prepared, windows, tuple(selections), tuple(training), path, write)


def finish(spec, prepared, windows, selections, training, path: OOSPath, write=True):
    base = spec.base
    profile = base.get("tax.profile")
    first, last = windows[0].test_start, windows[-1].actual_oos_end
    inception = inception_date(first)
    capital = float(base.get("portfolio.initial_capital_pln"))
    actual, final_state = path.engine, path.segments[-1].state
    last_cfg = selections[-1].config
    tax_hooks = find_tax_hooks(build_hooks(last_cfg, inception))
    terminal, tax_params, shadow_cost = None, None, None
    if isinstance(tax_hooks, FoundationHooks):
        tax_params = tax_hooks.params
        terminal = settle_foundation(actual.final_snapshot, tax_params, final_state.tax_state,
                                     capital, inception, final_state.targets,
                                     actual.weeks[-1].effective_states)
        zero = tax_params.zero_rates()
        s_state = path.shadow_segments[-1].state
        shadow_cost = settle_foundation_shadow_costs(
            path.shadow_engine.final_snapshot, zero, s_state.tax_state, inception,
            s_state.targets, path.shadow_engine.weeks[-1].effective_states)
    elif isinstance(tax_hooks, IndividualTaxHooks):
        tax_params = tax_hooks.params
        terminal = settle_terminal(actual.final_snapshot, tax_params, final_state.tax_state)
    pre = PreTaxRun("shadow_zero_tax" if profile != "none" else "actual_run_no_taxes",
                    path.shadow_engine, None,
                    "walk-forward: one continuous zero-tax shadow OOS path with the same selections "
                    "(setup/admin/transaction costs kept, final-year admin cost after the path)"
                    if profile != "none" else "tax.profile=none: the OOS path has no taxes",
                    shadow_cost)
    assets = canonical_assets({a for s in path.segments for a in s.state.params})
    cpi = None
    if prepared.cpi_series is not None:
        cpi = cpi_window(prepared.cpi_series, inception, last, base.get("cpi.mapping"),
                         base.get("cpi.label"))
    metrics = compute_run_metrics(
        pre=PathSeries.from_engine(pre.engine), after=PathSeries.from_engine(actual),
        elapsed_days=elapsed_days(first, last), rf_returns=[w.market.rf_return for w in actual.weeks],
        rf_after_tax_rate=tax_params.rf_interest_rate if tax_params else 0.0,
        pre_terminal_nav=actual.final_ledger.nav,
        after_tax_terminal_wealth=terminal.after_tax_terminal_wealth if terminal else actual.final_ledger.nav,
        trades=actual.trades, terminal_trades=terminal.liquidation_trades if terminal else (),
        week_states=[w.effective_states for w in actual.weeks], assets=assets,
        mar_annual=float(base.get("metrics.sortino_mar_annual")), cpi=cpi,
        growth_base_nav=capital, final_wealth_pre_tax=pre.final_wealth)
    targets_by_week = {w: s.state.targets for s in path.segments for w in s.window.oos_weeks}
    ctx = prepared.context()
    for r in (s.engine for s in path.segments):
        ctx["report"].extend(r.issues)
    _, ignored = oos_distributions(prepared, windows, selections[0].config)
    ctx["report"].extend(distribution_ignored_issues(
        ignored, [w for win in windows for w in win.oos_weeks]))
    oos_weeks = tuple(w for win in windows for w in win.oos_weeks)
    # targets / signal parameters in summary.csv only when every OOS window used the same
    # ones; otherwise per window in walk_forward_results.csv
    same_targets = all(s.targets == selections[0].targets for s in selections)
    same_params = all(s.params == selections[0].params for s in selections)
    ctx.update(first_week=first, last_week=last,
               targets=dict(selections[0].targets) if same_targets else {s: None for s in SLEEVES},
               calendar=dataclasses.replace(prepared.calendar, weeks=oos_weeks,
                                            common_gaps=tuple(g for g in prepared.calendar.common_gaps
                                                              if first <= g <= last),
                                            dropped=tuple(g for g in prepared.calendar.dropped
                                                          if first <= g <= last)))
    wf = {"optimization_mode": "walk-forward", "walk_forward_window": spec.window_type,
          "train_years": spec.train_years, "test_years": spec.test_years,
          "step_years": spec.step_years, "oos_windows": len(windows), "first_oos_week": first,
          "last_oos_week": last}
    portfolio = PortfolioRunResult(
        engine=actual, **ctx, tax_state=final_state.tax_state, tax_params=tax_params,
        terminal=terminal, pre_tax=pre, metrics=metrics,
        inputs=dataclasses.replace(prepared.inputs, params=dict(selections[0].params) if same_params
                                   else {}, targets=None),
        cpi_series=prepared.cpi_series, prepared=prepared, walk_forward=wf,
        targets_by_week=targets_by_week, assets=assets)
    rows = tuple(result_row(spec, seg, tr) for seg, tr in zip(path.segments, training))
    bounds = tuple(boundary_row(seg, sh) for seg, sh in
                   zip(path.segments, path.shadow_segments or [None] * len(path.segments)))
    manifest = build_wf_manifest(spec, prepared, windows, selections, training, rows)
    out = WalkForwardResult(spec, prepared, windows, selections, training, path, portfolio, rows,
                            bounds, manifest)
    if write:
        out.output_dir = write_walk_forward_outputs(out)
    return out


# ============================================================================ reporting
def _dims_of(sel: Selection) -> tuple:
    return tuple((k[len("candidate_"):], v) for k, v in sel.row.items()
                 if k.startswith("candidate_") and v is not None)


def result_row(spec: WalkForwardSpec, seg: Segment, training) -> dict:
    w, sel, e = seg.window, seg.selection, seg.engine
    dims = dict(_dims_of(sel))

    def value(dim, attr):
        if dim in dims:
            return dims[dim]
        vals = {getattr(p, attr) for p in sel.params.values()}
        return vals.pop() if len(vals) == 1 else "|".join(
            f"{a}={getattr(p, attr)}" for a, p in sorted(sel.params.items()))

    nav0 = e.weeks[0].nav_start
    nav1 = e.weeks[-1].nav_end
    weeks = set(w.oos_weeks)
    events = [x for x in (seg.state.tax_state.tax_events if seg.state.tax_state else ())
              if x.week_key in weeks or (x.phase == "initialization" and w.window_id == 1)]
    trades = e.trades
    boundary_trades = [t for t in trades if t.reason == TradeReason.WALK_FORWARD_REBALANCE]
    return {
        "window_id": w.window_id, "window_type": spec.window_type, "train_start": w.train_start,
        "train_end": w.train_end, "train_weeks": w.train_weeks, "test_start": w.test_start,
        "nominal_test_end": w.nominal_test_end, "actual_oos_end": w.actual_oos_end,
        "actual_test_days": w.actual_test_days, "actual_test_years": w.actual_test_years,
        "oos_weeks": len(w.oos_weeks), "selected_grid_index": sel.grid_index,
        "training_objective": spec.opt.objective, "training_objective_value": sel.objective_value,
        "training_relevant_max_drawdown": sel.relevant_max_drawdown,
        "training_candidates": len(training.rows) if training else None,
        "training_eligible": sum(1 for r in training.rows if r["eligible"]) if training else None,
        **{f"selected_weight_{s}": sel.targets[s] for s in SLEEVES},
        "selected_ma": value("ma", "ma"), "selected_threshold": value("threshold", "threshold_off"),
        "selected_delay": value("delay", "delay"),
        "selected_confirmation": value("confirmation", "confirm_off"),
        "selected_sell_fraction": value("sell_fraction", "sell_fraction"),
        "selected_rebalance_mode": sel.rebalance[0], "selected_rebalance_band_pp": sel.rebalance[1],
        "oos_nav_start": nav0, "oos_nav_end": nav1, "oos_return": nav1 / nav0 - 1.0,
        "oos_trade_count": len(trades), "oos_turnover": turnover(trades, [x.nav_end for x in e.weeks]),
        "oos_tax_paid": math.fsum(x.amount for x in events if x.category == "tax"),
        "oos_costs_paid": math.fsum([t.transaction_cost + t.slippage for t in trades]
                                    + [x.amount for x in events if x.category == "cost"]),
        "boundary_rebalance": seg.boundary_event is not None,
        "boundary_trade_count": len(boundary_trades),
        "training_input_sha256": sel.training_input_sha256,
        "max_training_week": sel.max_training_week,
    }


def boundary_row(seg: Segment, shadow: Optional[Segment]) -> dict:
    p, w = seg.plan, seg.window
    return {
        "window_id": w.window_id, "week_key": w.test_start,
        "event": "initial_allocation" if w.window_id == 1 else "walk_forward_boundary",
        "old_targets": _targets_text(p["old_targets"]),
        "new_targets": _targets_text(seg.state.targets),
        "old_signal_params": _params_text(p["old_params"]),
        "new_signal_params": _params_text(seg.state.params),
        "carried_signal_state": "|".join(p["carried"]),
        "reconstructed_signal_state": "|".join(p["replaced"]),
        "carried_state_matches_training": "|".join(f"{a}={v}" for a, v in sorted(p["matches"].items())),
        "cancelled_pending_executions": _pending_text(p["cancelled"]),
        "adopted_pending_executions": _pending_text(p["adopted"]),
        "pending_rebalance_action": p["pending_action"],
        "boundary_rebalance": seg.boundary_event is not None,
        "boundary_reasons": "|".join(p["reasons"]),
        "boundary_trade_count": sum(1 for t in seg.engine.trades
                                    if t.reason == TradeReason.WALK_FORWARD_REBALANCE),
        "shadow_boundary_rebalance": (shadow.boundary_event is not None) if shadow else None,
    }


def build_wf_manifest(spec, prepared, windows, selections, training, rows) -> dict:
    base = spec.base
    wins = []
    for w, sel, tr in zip(windows, selections, training):
        counts = tr.counts()
        wins.append({
            "window_id": w.window_id, "anchor": w.anchor.isoformat(),
            "train_start": w.train_start.isoformat(), "train_end": w.train_end.isoformat(),
            "max_training_week": sel.max_training_week.isoformat(),
            "test_start": w.test_start.isoformat(),
            "no_future_training": sel.max_training_week < w.test_start,
            "training_input_sha256": sel.training_input_sha256,
            "selected_grid_index": sel.grid_index,
            "selected_objective_value": sel.objective_value,
            "top_eligible": [{"grid_index": g, "objective_value": v, "gap_to_selected": d}
                             for g, v, d in sel.top],
            "selected_neighbours": [
                {"grid_index": r["grid_index"], "status": r["status"],
                 "objective_value": r["objective_value"], "gap_to_selected": r["objective_gap_to_selected"],
                 **{k: r[k] for k in r if k.startswith("candidate_") or k.startswith("weight_")}}
                for r in robustness_rows(spec, tr, sel) if r["selected_neighbour"]],
            "nominal_test_end": w.nominal_test_end.isoformat(),
            "actual_oos_end": w.actual_oos_end.isoformat(),
            "actual_test_days": w.actual_test_days, "actual_test_years": w.actual_test_years,
            **{k: counts[k] for k in ("raw_grid_points", "evaluated_points", "eligible_points",
                                      "rejected_points")},
        })
    weeks = prepared.inputs.weeks
    return {
        "command": base.command, "mode": "walk-forward", "spec_version": base.get("app.spec_version"),
        "code_version": code_version(), "window_type": spec.window_type,
        "train_years": spec.train_years, "test_years": spec.test_years,
        "step_years": spec.step_years,
        "step_rule": ("calendar years" if spec.step[0] == "calendar_years" else
                      f"{spec.step[1]} calendar days = round_half_up(step_years * 365.2425)"),
        "window_rules": {
            "first_test_anchor": "earliest training week + train_years calendar years (full "
                                 "training history mandatory, Q-020)",
            "test_start": "first retained Friday >= anchor",
            "train_end": "last retained Friday < test_start",
            "rolling_train_start": "first retained Friday >= anchor - train_years",
            "anchored_train_start": "the earliest training week",
            "actual_oos_end": "retained week before the next test_start; the last one ends at the "
                              "global end (WF-015)",
            "nominal_test_end": "last Friday before anchor + test_years",
            "actual_test_days": "actual_oos_end - test_start + 1 calendar day; actual_test_years "
                                "= actual_test_days / 365.2425 (WF-016)"},
        "parameters": list(spec.parameters), "grids": spec.opt.grid,
        "robustness": "WF-005: per window the five best eligible candidates (Q-041 order) and the "
                      "grid neighbours of the selection (one step on one axis) with their "
                      "objective gap; every training row carries eligible_rank, "
                      "objective_gap_to_selected and selected_neighbour "
                      "(training_grid_results.csv)",
        "candidates_per_window": len(spec.opt.candidates),
        "objective": spec.opt.objective, "objective_direction": spec.opt.direction,
        "tie_break": list(TIE_BREAK) + ["grid_index (identical weights, signal dimensions)"],
        "max_drawdown_limit": spec.opt.max_drawdown_limit,
        "required_assets_union": list(spec.opt.union),
        "warmup_weeks": dict(prepared.warmup_weeks),
        "prepared_input_sha256": prepared_input_sha256(prepared),
        "global_first_week": weeks[0].isoformat(), "global_last_week": weeks[-1].isoformat(),
        "first_week_rule": prepared.first_week_rule, "as_of_date": prepared.as_of.isoformat(),
        "tax_profile": base.get("tax.profile"),
        "jobs_requested": spec.opt.jobs_requested, "jobs_effective": spec.opt.jobs_effective,
        "parallel_training": spec.opt.jobs_effective > 1,
        "windows": wins,
        "no_future_training_checks_passed": all(x["no_future_training"] for x in wins),
        "oos_first_week": windows[0].test_start.isoformat(),
        "oos_last_week": windows[-1].actual_oos_end.isoformat(),
        "state_carry": "NAV, holdings, rf_base and reserves, lots (ids, cost basis), unit prices, "
                       "realizations, loss buckets, open tax year and liabilities, foundation "
                       "admin state and totals carried across OOS boundaries; no terminal "
                       "settlement before the last OOS week (Q-022)",
        "sources": [{"role": p.role, "path": p.path, "sha256": p.sha256}
                    for p in sorted(prepared.provenances, key=lambda p: p.role)],
    }


def write_walk_forward_outputs(res: WalkForwardResult):
    """Standard OOS artifacts of the stitched path (summary, weekly, trades, taxes, payments,
    transfers, rebalancing, signals, terminal settlement, validation, normalized data, config,
    data manifest) plus walk_forward_results.csv, training_grid_results.csv,
    walk_forward_boundary_events.csv and walk_forward_manifest.json."""
    base = res.spec.base
    ts = run_timestamp()
    out = run_directory(base.get("report.output_dir"), base.get("report.run_name"), ts)
    try:
        out.rmdir()                                 # recreated by write_portfolio_outputs
        write_portfolio_outputs(base, res.portfolio, out_dir=out, timestamp=ts, extra_manifest={
            "optimization_mode": "walk-forward", "walk_forward_manifest": MANIFEST,
            "prepared_input_sha256": res.manifest["prepared_input_sha256"],
            "oos_path": "first_return_week..last_return_week = the stitched OOS path (training "
                        "history before it is used only to select parameters)"})
        write_csv(out / "walk_forward_results.csv", RESULT_FIELDS, res.results_rows)
        training = [r for tr, sel in zip(res.training, res.selections)
                    for r in robustness_rows(res.spec, tr, sel)]
        write_csv(out / "training_grid_results.csv", TRAINING_FIELDS, training)
        write_csv(out / "walk_forward_boundary_events.csv", BOUNDARY_FIELDS, res.boundary_rows)
        write_json(out / MANIFEST, dict(res.manifest, run_timestamp=ts.isoformat()))
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return out
