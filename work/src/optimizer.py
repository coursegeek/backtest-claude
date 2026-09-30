"""In-sample optimize (OPT-001..010, TEST-009, ERR-005/006, REPRO-006, Q-041): the full
Cartesian weight grid, every weight-valid candidate as a complete production run on one shared
prepared input, and one deterministically selected candidate.

Pipeline (the optimizer is an orchestrator of ``app.run_prepared``, not a second engine):
  1. the weight grid is built and validated before any data is loaded (Q-041): btc x gold
     (x stocks when stocks is an explicit grid), rf fixed (OPT-005), values in decimal
     arithmetic; remainder stocks = 1 - btc - gold - rf (rejected when < -1e-12, a value in
     [-1e-12, 0) becomes exactly 0); an explicit stocks grid qualifies only when
     |stocks + gold + btc + rf - 1| <= 1e-12 (ALLOC-001; > 1 and < 1 are both rejected, nothing
     is moved to RF). Every combination keeps a stable 1-based grid_index;
  2. the union of the risky assets with a positive weight in any weight-valid candidate is
     prepared ONCE (``app.prepare_run(assets=union)``): one calendar, market and set of signal
     series bounded by every asset of the union, so no candidate gets a longer range;
  3. every weight-valid candidate runs ``app.run_prepared`` (fresh portfolio, cost basis,
     trackers, tax/foundation and rebalancing state; terminal settlement, pre-tax shadow,
     metrics, summary row) - in-process for jobs=1, in a process pool for jobs>1 (the workers
     receive the prepared input once, never read files, write nothing and return rows);
  4. rows are assembled in grid_index order, classified (objective availability, drawdown
     limit) and ONE candidate is selected by the Q-041 total order: better objective, lower
     relevant max drawdown, lower turnover, lexicographically smaller (btc, gold, rf, stocks);
  5. outputs are written only after the selection: grid_results.csv (every combination),
     summary.csv (the selected SUMMARY_FIELDS row), selected/ (the selected run's standard
     artifacts from its already computed result), manifests, shared data outputs.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import multiprocessing
import os
import shutil
import sys
import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .app import (PortfolioRunResult, PreparedRun, check_supported_run, prepare_run,
                  prepared_input_sha256, run_prepared, write_portfolio_outputs)
from .config import ResolvedConfig, flatten, parse_decimal_grid
from .errors import BacktestError, ConfigError, NotImplementedCommand
from .manifest import build_manifest, code_version, run_timestamp
from .models import RISKY_ASSETS, ValidationIssue, canonical_assets
from .reporting import (NORMALIZED_FIELDS, SUMMARY_FIELDS, VALIDATION_FIELDS, run_directory,
                        summary_row, write_csv, write_json)
from .validation import ValidationReport

# OPT-007 (Q-041): user objective -> summary field and direction. terminal_wealth is the
# pre-tax terminal wealth of the optimizer's nomenclature = summary.final_wealth_pre_tax (the
# pre-tax shadow including non-tax foundation costs), never pre_terminal_nav.
OBJECTIVES = {
    "cagr": ("cagr", "maximize"),
    "after_tax_cagr": ("after_tax_cagr", "maximize"),
    "terminal_wealth": ("final_wealth_pre_tax", "maximize"),
    "after_tax_terminal_wealth": ("after_tax_terminal_wealth", "maximize"),
    "sharpe": ("sharpe", "maximize"),
    "sortino": ("sortino", "maximize"),
    "calmar": ("calmar", "maximize"),
    "min_drawdown": ("max_drawdown", "minimize"),
}
AFTER_TAX_OBJECTIVES = ("after_tax_cagr", "after_tax_terminal_wealth")
TIE_BREAK = ("objective", "lower_maxDD", "lower_turnover", "lexicographic_weights")
LEXICOGRAPHIC_ORDER = ("btc", "gold", "rf", "stocks")
SLEEVES = ("stocks", "gold", "btc", "rf")
WEIGHT_TOLERANCE = Decimal("1e-12")

OK = "ok"
REJECTED_GT_1 = "rejected_weight_sum_gt_1"
REJECTED_LT_1 = "rejected_weight_sum_lt_1"
REJECTED_DD = "rejected_drawdown_limit"
UNAVAILABLE = "objective_unavailable"
STATUSES = (OK, REJECTED_GT_1, REJECTED_LT_1, REJECTED_DD, UNAVAILABLE)

OPT_FIELDS = ["grid_index", "status", "rejection_reason", "eligible", "selected",
              "weight_stocks", "weight_gold", "weight_btc", "weight_rf", "weight_sum",
              "objective", "objective_direction", "objective_metric", "objective_value",
              "relevant_drawdown_metric", "relevant_max_drawdown", "max_drawdown_limit"]
GRID_FIELDS = OPT_FIELDS + list(SUMMARY_FIELDS)
MANIFEST = "optimizer_manifest.json"
SHARED_STATEMENT = ("all evaluated candidates used one shared prepared data/calendar input "
                    "(the union of the risky assets of every weight-valid candidate, loaded, "
                    "validated and aligned once); only the strategic targets differ")


class OptimizerError(BacktestError):
    """No eligible candidate, or a candidate run failed; nothing was written."""
    exit_code = 4


def relevant_drawdown_metric(objective: str) -> str:
    """OPT-008 (Q-041): after-tax objectives are constrained and tie-broken on the after-tax
    drawdown, every other objective on the pre-tax max_drawdown."""
    return "after_tax_max_drawdown" if objective in AFTER_TAX_OBJECTIVES else "max_drawdown"


# ============================================================================ weight grid
@dataclass(frozen=True)
class Candidate:
    grid_index: int                 # 1-based position in the Cartesian product (btc, gold, stocks)
    weights: tuple                  # ((sleeve, float), ...) in SLEEVES order
    weight_sum: float
    status: str                     # "pending" (weight-valid, to evaluate) or a rejection status
    reason: str = ""
    overrides: tuple = ()           # ((config key, value), ...) of non-weight dimensions (WF)
    dims: tuple = ()                # ((dimension, value), ...) reported as candidate_<dimension>

    @property
    def targets(self) -> dict:
        return dict(self.weights)

    @property
    def weight_valid(self) -> bool:
        return self.status == "pending"

    def lexicographic_key(self) -> tuple:
        t = self.targets
        return tuple(t[k] for k in LEXICOGRAPHIC_ORDER)


def _weights_grid(value, name) -> list:
    vals = parse_decimal_grid(value, name)
    for v in vals:
        if not Decimal(0) <= v <= Decimal(1):
            raise ConfigError(f"{name}: weights are decimal fractions 0..1 (CLI percent 0..100, "
                              f"Q-026), got {v}")
    dup = sorted({str(v) for v in vals if vals.count(v) > 1})
    if dup:
        raise ConfigError(f"{name}: duplicate grid value(s) {dup}")
    return vals


def build_weight_grid(cfg: ResolvedConfig) -> tuple:
    """(candidates, grid spec). Nesting btc (outer), gold, stocks (inner, explicit grids only);
    rf fixed. Rejected combinations stay in the grid (OPT-010)."""
    btc = _weights_grid(cfg.get("optimizer.btc_weight"), "optimizer.btc_weight")
    gold = _weights_grid(cfg.get("optimizer.gold_weight"), "optimizer.gold_weight")
    rf_raw = cfg.get("optimizer.rf_weight")
    rf_vals = _weights_grid(0 if rf_raw is None else rf_raw, "optimizer.rf_weight")
    if len(rf_vals) != 1:
        raise ConfigError("optimizer.rf_weight: one fixed RF weight (OPT-005), not a grid")
    rf = rf_vals[0]
    stocks_raw = cfg.get("optimizer.stocks_weight")
    remainder = stocks_raw in (None, "remainder")
    stocks = None if remainder else _weights_grid(stocks_raw, "optimizer.stocks_weight")
    out, idx = [], 0
    for b in btc:
        for g in gold:
            for s in ([None] if remainder else stocks):
                idx += 1
                if remainder:
                    s = Decimal(1) - b - g - rf
                    if s < -WEIGHT_TOLERANCE:
                        status = REJECTED_GT_1
                        reason = (f"stocks = 1 - btc - gold - rf = {s} < 0: btc + gold + rf = "
                                  f"{b + g + rf} > 1 (OPT-006, Q-041)")
                    else:
                        s = max(s, Decimal(0))
                        status, reason = "pending", ""
                    total = s + g + b + rf
                else:
                    total = s + g + b + rf
                    if abs(total - 1) <= WEIGHT_TOLERANCE:
                        status, reason = "pending", ""
                    elif total > 1:
                        status = REJECTED_GT_1
                        reason = f"stocks + gold + btc + rf = {total} > 1 (OPT-006, ALLOC-001)"
                    else:
                        status = REJECTED_LT_1
                        reason = (f"stocks + gold + btc + rf = {total} < 1 (ALLOC-001: the "
                                  "remainder is never moved to RF)")
                weights = (("stocks", float(s)), ("gold", float(g)), ("btc", float(b)),
                           ("rf", float(rf)))
                out.append(Candidate(idx, weights, float(total), status, reason))
    spec = {"btc_weight": [float(v) for v in btc], "gold_weight": [float(v) for v in gold],
            "stocks_weight": "remainder" if remainder else [float(v) for v in stocks],
            "rf_weight": float(rf), "nesting": ["btc", "gold"] + ([] if remainder else ["stocks"])}
    return tuple(out), spec


def required_assets(candidates) -> tuple:
    """Union of the risky assets with a positive weight in any weight-valid candidate."""
    return canonical_assets({a for c in candidates if c.weight_valid
                             for a, w in c.weights if a in RISKY_ASSETS and w > 0})


# ============================================================================ configuration
@dataclass(frozen=True)
class OptimizerSpec:
    base: ResolvedConfig
    candidates: tuple
    grid: dict
    objective: str
    objective_metric: str
    direction: str
    drawdown_metric: str
    max_drawdown_limit: Optional[float]
    union: tuple
    jobs_requested: object
    jobs_effective: int

    @property
    def to_evaluate(self) -> tuple:
        return tuple(c for c in self.candidates if c.weight_valid)


def resolve_jobs(value, n_tasks: int) -> int:
    """ERR-006: 1 sequential, N > 1 a pool of N workers, -1 / auto = available CPUs; never more
    workers than candidates to evaluate. 0 and < -1 are configuration errors."""
    if value in ("auto", -1):
        try:
            n = len(os.sched_getaffinity(0))
        except AttributeError:                                  # pragma: no cover
            n = os.cpu_count() or 1
    elif isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        n = value
    else:
        raise ConfigError(f"performance.jobs / --jobs: 1..N, -1 (all CPUs) or auto, got {value!r}")
    return max(1, min(n, n_tasks))


def resolve_optimizer(cfg: ResolvedConfig) -> OptimizerSpec:
    """Validate the optimize configuration and the whole weight grid before any data."""
    if cfg.get("optimizer.mode") != "in-sample":
        raise ConfigError("resolve_optimizer handles --optimization-mode in-sample; walk-forward "
                          "is resolved by walk_forward.resolve_walk_forward")
    params = cfg.get("optimizer.parameters") or ["weights"]
    if list(params) != ["weights"]:
        raise NotImplementedCommand(f"in-sample optimize optimizes the strategic weights only; "
                                    f"optimizer.parameters={list(params)} (signal/band grids) "
                                    "are optimized by --optimization-mode walk-forward (WF-006)")
    if cfg.get("allocation.targets") is not None or cfg.get("allocation.single_asset") not in (None, False):
        raise ConfigError("optimize builds the strategic targets from the weight grids "
                          "(--btc-weight/--gold-weight/--stocks-weight/--rf-weight); remove "
                          "allocation.targets / --weights / --single-asset")
    if cfg.get("run.asset"):
        raise ConfigError("optimize is a multi-asset weight grid; --asset is not used")
    tie = list(cfg.get("optimizer.tie_break") or TIE_BREAK)
    if tie != list(TIE_BREAK):
        raise ConfigError(f"optimizer.tie_break: only {list(TIE_BREAK)} is supported (Q-041)")
    objective = cfg.get("optimizer.objective")
    if objective not in OBJECTIVES:
        raise ConfigError(f"optimizer.objective: {objective!r} not in {sorted(OBJECTIVES)}")
    limit = cfg.get("optimizer.max_drawdown_limit")
    if limit is not None:
        limit = float(limit)
        if math.isnan(limit) or not 0.0 <= limit <= 1.0:
            raise ConfigError("optimizer.max_drawdown_limit: decimal 0..1 (CLI percent 0..100, "
                              f"Q-026), got {limit!r}")
    check_supported_run(cfg)                             # Q-037 / Q-047 before any data
    candidates, grid = build_weight_grid(cfg)
    valid = [c for c in candidates if c.weight_valid]
    if not valid:
        raise ConfigError(f"optimize: none of the {len(candidates)} weight combinations is valid "
                          "(each must sum to 1: remainder stocks >= 0 or an explicit grid with "
                          "stocks + gold + btc + rf = 1, Q-041); no data was loaded")
    union = required_assets(candidates)
    if not union:
        raise ConfigError("optimize: no weight-valid candidate holds a risky asset")
    metric, direction = OBJECTIVES[objective]
    return OptimizerSpec(cfg, candidates, grid, objective, metric, direction,
                         relevant_drawdown_metric(objective), limit, union,
                         cfg.get("performance.jobs"), resolve_jobs(cfg.get("performance.jobs"),
                                                                   len(valid)))


def candidate_config(base: ResolvedConfig, cand: Candidate) -> ResolvedConfig:
    """A new configuration equal to the optimize configuration except allocation.targets (and,
    for walk-forward candidates, the listed signal / band dimensions)."""
    return base.with_overrides({"allocation.targets": cand.targets, **dict(cand.overrides)})


# ============================================================================ classification
def _number(v):
    if v is None or isinstance(v, bool):
        return None
    v = float(v)
    return None if math.isnan(v) else v


def classify(summary: dict, objective: str, limit: Optional[float]) -> tuple:
    """(status, reason, objective_value, relevant drawdown) of one evaluated candidate.
    An unavailable objective (None/NaN, e.g. Sharpe with zero variance) is never replaced by 0;
    the drawdown limit is inclusive (relevant drawdown == limit qualifies)."""
    metric, _ = OBJECTIVES[objective]
    dd_metric = relevant_drawdown_metric(objective)
    value, dd = _number(summary.get(metric)), _number(summary.get(dd_metric))
    turnover = _number(summary.get("turnover"))
    if value is None:
        return UNAVAILABLE, f"{metric} is not available for this candidate", None, dd
    if dd is None or turnover is None:
        return UNAVAILABLE, f"{dd_metric} or turnover (tie-break) is not available", value, dd
    if limit is not None and dd > limit:
        return REJECTED_DD, f"{dd_metric} {dd!r} > max_drawdown_limit {limit!r} (OPT-008)", value, dd
    return OK, "", value, dd


def selection_key(row: dict, objective: str) -> tuple:
    """Q-041 total order (smaller is better): objective (negated exactly for maximized
    objectives), relevant max drawdown, turnover, (btc, gold, rf, stocks). No rounding, no
    tolerance: a later criterion decides only on exact equality of the earlier ones. The final
    grid_index only separates candidates with identical weights (walk-forward signal
    dimensions, Q-022); in-sample weight grids never reach it."""
    metric, direction = OBJECTIVES[objective]
    v = float(row[metric])
    score = -v if direction == "maximize" else v
    return (score, float(row[relevant_drawdown_metric(objective)]), float(row["turnover"]),
            tuple(float(row[f"weight_{k}"]) for k in LEXICOGRAPHIC_ORDER), row["grid_index"])


def select(rows, objective: str) -> Optional[dict]:
    """The selected eligible row (independent of the order of ``rows``); None if none."""
    eligible = [r for r in rows if r["eligible"]]
    return min(eligible, key=lambda r: selection_key(r, objective)) if eligible else None


def base_row(spec: OptimizerSpec, cand: Candidate) -> dict:
    t = cand.targets
    return {"grid_index": cand.grid_index, "status": cand.status, "rejection_reason": cand.reason,
            "eligible": False, "selected": False,
            "weight_stocks": t["stocks"], "weight_gold": t["gold"], "weight_btc": t["btc"],
            "weight_rf": t["rf"], "weight_sum": cand.weight_sum,
            "objective": spec.objective, "objective_direction": spec.direction,
            "objective_metric": spec.objective_metric, "objective_value": None,
            "relevant_drawdown_metric": spec.drawdown_metric, "relevant_max_drawdown": None,
            "max_drawdown_limit": spec.max_drawdown_limit,
            **{f"candidate_{k}": v for k, v in cand.dims}}


def evaluated_row(spec: OptimizerSpec, cand: Candidate, summary: dict) -> dict:
    row = base_row(spec, cand)
    status, reason, value, dd = classify(summary, spec.objective, spec.max_drawdown_limit)
    row.update({"status": status, "rejection_reason": reason, "eligible": status == OK,
                "objective_value": value, "relevant_max_drawdown": dd})
    clash = set(row) & set(summary)
    if clash:
        raise KeyError(f"grid_results columns defined twice: {sorted(clash)}")
    row.update(summary)
    return row


# ============================================================================ evaluation
@dataclass(frozen=True)
class _EvalContext:
    spec: OptimizerSpec
    prepared: PreparedRun


_WORKER: Optional[_EvalContext] = None          # set once per worker process


def _detach(res: PortfolioRunResult) -> PortfolioRunResult:
    """Drop the shared prepared data from a result before it crosses a process boundary."""
    return dataclasses.replace(res, prepared=None, inputs=None, normalized=(), cpi_series=None,
                               provenances=(), pre_tax=dataclasses.replace(res.pre_tax, inputs=None))


def _attach(res: PortfolioRunResult, prepared: PreparedRun, cfg: ResolvedConfig) -> PortfolioRunResult:
    inputs = prepared.inputs_for(cfg)
    return dataclasses.replace(res, prepared=prepared, inputs=inputs, normalized=prepared.normalized,
                               cpi_series=prepared.cpi_series, provenances=prepared.provenances,
                               pre_tax=dataclasses.replace(res.pre_tax, inputs=inputs))


def evaluate_chunk(ctx: _EvalContext, indices: tuple) -> dict:
    """Run the candidates ``indices`` (grid_index) and return their rows, their run issues and
    the full result of the chunk's best eligible row (the only full result kept)."""
    spec = ctx.spec
    by_index = {c.grid_index: c for c in spec.candidates}
    rows, issues, best, best_key = [], [], None, None
    for gi in indices:
        cand = by_index[gi]
        cfg = candidate_config(spec.base, cand)
        try:
            res = run_prepared(cfg, ctx.prepared, write=False)
        except BacktestError as e:
            err = OptimizerError(f"optimize: candidate grid_index={gi} ({dict(cand.weights)}) "
                                 f"failed: {e}; nothing was written")
            err.exit_code = e.exit_code
            raise err from e
        row = evaluated_row(spec, cand, summary_row(cfg, res))
        rows.append(row)
        issues.append((gi, tuple(res.engine.issues)))
        if row["eligible"]:
            key = selection_key(row, spec.objective)
            if best_key is None or key < best_key:
                best, best_key = (gi, res), key
    return {"rows": rows, "issues": issues, "best": best,
            "prepared_input_sha256": prepared_input_sha256(ctx.prepared)}


def _init_worker(ctx: _EvalContext) -> None:
    global _WORKER
    _WORKER = ctx


def _worker_chunk(indices: tuple) -> dict:
    out = evaluate_chunk(_WORKER, indices)
    if out["best"] is not None:
        gi, res = out["best"]
        out["best"] = (gi, _detach(res))
    return out


def _chunks(indices: tuple, jobs: int) -> list:
    """Deterministic consecutive chunks of grid indices (their size never affects results)."""
    size = max(1, min(25, math.ceil(len(indices) / (jobs * 4))))
    return [indices[i:i + size] for i in range(0, len(indices), size)]


def _mp_context():
    methods = multiprocessing.get_all_start_methods()
    return multiprocessing.get_context("fork" if "fork" in methods else "spawn")


@dataclass
class OptimizerResult:
    spec: OptimizerSpec
    prepared: PreparedRun
    rows: tuple                     # every combination, grid_index order
    selected: dict                  # the selected row
    selected_config: ResolvedConfig
    selected_result: PortfolioRunResult
    issues: tuple                   # ((grid_index, engine issues), ...) grid order
    fingerprint: str
    worker_fingerprints: tuple
    output_dir: Optional[Path] = None

    def counts(self) -> dict:
        st = {s: sum(1 for r in self.rows if r["status"] == s) for s in STATUSES}
        evaluated = sum(1 for r in self.rows if r["status"] not in (REJECTED_GT_1, REJECTED_LT_1))
        return {"raw_grid_points": len(self.rows),
                "weight_valid_points": evaluated,
                "weight_rejected_points": st[REJECTED_GT_1] + st[REJECTED_LT_1],
                "evaluated_points": evaluated,
                "eligible_points": st[OK],
                "rejected_points": len(self.rows) - st[OK],
                "selected_points": sum(1 for r in self.rows if r["selected"]),
                "status_counts": st}


def _progress(spec: OptimizerSpec, text: str) -> None:
    """ERR-005: progress on stderr only (completion order), never in the result files."""
    if spec.base.get("performance.progress"):
        print(text, file=sys.stderr, flush=True)


def run_optimize(cfg: ResolvedConfig, write: bool = True) -> OptimizerResult:
    spec = resolve_optimizer(cfg)
    prepared = prepare_run(spec.base, assets=spec.union, auto_start=True)      # the only load
    return assemble(spec, prepared, evaluate_candidates(spec, prepared), write)


def evaluate_candidates(spec: OptimizerSpec, prepared: PreparedRun,
                        label: str = "optimize") -> list:
    """Run every weight-valid candidate of ``spec`` on ``prepared`` (in-process for one job, a
    process pool otherwise). Returns the chunk results in completion order; ``assemble``
    orders them by grid_index."""
    todo = tuple(c.grid_index for c in spec.to_evaluate)
    ctx = _EvalContext(spec, prepared)
    chunks = _chunks(todo, spec.jobs_effective)
    _progress(spec, f"{label}: {len(spec.candidates)} grid points, {len(todo)} weight-valid "
                    f"to evaluate, jobs={spec.jobs_effective}")
    results, done = [], 0
    if spec.jobs_effective == 1:
        for ch in chunks:
            out = evaluate_chunk(ctx, ch)
            results.append(out)
            done += len(ch)
            _progress(spec, f"{label}: completed {done}/{len(todo)} evaluated candidates")
    else:
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=spec.jobs_effective, mp_context=_mp_context(),
                initializer=_init_worker, initargs=(ctx,)) as pool:
            futures = {pool.submit(_worker_chunk, ch): ch for ch in chunks}
            for fut in concurrent.futures.as_completed(futures):
                out = fut.result()                      # re-raises a candidate failure
                results.append(out)
                done += len(futures[fut])
                _progress(spec, f"{label}: completed {done}/{len(todo)} evaluated candidates")
    return results


def assemble(spec: OptimizerSpec, prepared: PreparedRun, results: list, write: bool) -> OptimizerResult:
    """Order everything by grid_index (never by completion), select, then write."""
    evaluated = {r["grid_index"]: r for out in results for r in out["rows"]}
    issues = dict(i for out in results for i in out["issues"])
    rows = [evaluated.get(c.grid_index) or base_row(spec, c) for c in spec.candidates]
    chosen = select(rows, spec.objective)
    if chosen is None:
        st = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
        raise OptimizerError(f"optimize: no eligible optimization candidate ({len(rows)} grid "
                             f"points, statuses {st}); nothing was written")
    gi = chosen["grid_index"]
    rows = [dict(r, selected=r["grid_index"] == gi) for r in rows]
    selected = rows[gi - 1]
    kept = [out["best"] for out in results if out["best"] is not None and out["best"][0] == gi]
    if not kept:
        raise OptimizerError(f"internal: the result of the selected candidate {gi} was not kept")
    cand = spec.candidates[gi - 1]
    cfg = candidate_config(spec.base, cand)
    res = kept[0][1]
    if res.prepared is None:                             # came from a worker process
        res = _attach(res, prepared, cfg)
    out = OptimizerResult(spec, prepared, tuple(rows), selected, cfg, res,
                          tuple(sorted(issues.items())), prepared_input_sha256(prepared),
                          tuple(sorted({o["prepared_input_sha256"] for o in results})))
    if write:
        out.output_dir = write_optimizer_outputs(out)
    return out


# ============================================================================ outputs
def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def optimizer_checks(res: OptimizerResult) -> dict:
    ev = [r for r in res.rows if r["status"] not in (REJECTED_GT_1, REJECTED_LT_1)]
    weeks = res.prepared.inputs.weeks
    return {
        "one_row_per_combination": len(res.rows) == len(res.spec.candidates),
        "rows_in_grid_index_order": [r["grid_index"] for r in res.rows]
        == list(range(1, len(res.rows) + 1)),
        "exactly_one_selected": sum(1 for r in res.rows if r["selected"]) == 1,
        "rejected_by_weight_not_evaluated": all(r.get("tax_profile") is None for r in res.rows
                                                if r["status"] in (REJECTED_GT_1, REJECTED_LT_1)),
        "same_effective_first_week": {r["effective_first_week"] for r in ev} == {weeks[0]},
        "same_effective_last_week": {r["effective_last_week"] for r in ev} == {weeks[-1]},
        "same_weeks": {r["weeks"] for r in ev} == {len(weeks)},
        "workers_used_the_prepared_input": res.worker_fingerprints == (res.fingerprint,),
    }


def write_optimizer_outputs(res: OptimizerResult) -> Path:
    """<output_dir>/<timestamp>_<run_name>/: grid_results.csv, summary.csv, selected/,
    optimizer_manifest.json, config_resolved.yaml, selected_config_resolved.yaml,
    data_manifest.json, validation_report.csv, weekly_normalized.csv. No per-candidate weekly
    files. A failure while writing removes the directory."""
    ts = run_timestamp()
    base = res.spec.base
    out = run_directory(base.get("report.output_dir"), base.get("report.run_name"), ts)
    try:
        _write(res, out, ts)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return out


def _write(res: OptimizerResult, out: Path, ts) -> None:
    spec, prep = res.spec, res.prepared
    base = spec.base
    weeks = prep.inputs.weeks
    write_csv(out / "grid_results.csv", GRID_FIELDS, res.rows)
    write_csv(out / "summary.csv", SUMMARY_FIELDS, [{k: res.selected.get(k) for k in SUMMARY_FIELDS}])
    (out / "config_resolved.yaml").write_text(base.to_yaml(prep.as_of), encoding="utf-8")
    (out / "selected_config_resolved.yaml").write_text(res.selected_config.to_yaml(prep.as_of),
                                                       encoding="utf-8")
    report = ValidationReport()
    report.extend(prep.issues)
    seen = {}
    for gi, issues in res.issues:                    # identical run issues reported once
        for i in issues:
            key = (i.severity, i.code, i.role, i.message, i.week_key, i.requirement_ids)
            seen.setdefault(key, []).append(gi)
    n = len(res.issues)
    for (sev, code, role, msg, week, req), gis in seen.items():
        report.add(ValidationIssue(sev, code, role, f"{msg} [in {len(gis)} of {n} evaluated "
                                                    f"candidates, first grid_index {gis[0]}]",
                                   week, req))
    write_csv(out / "validation_report.csv", VALIDATION_FIELDS, report.rows())
    write_csv(out / "weekly_normalized.csv", NORMALIZED_FIELDS, prep.normalized)
    data_manifest = build_manifest(prep.provenances, prep.as_of, ts, base.command)
    data_manifest.update({"shared_input": True, "shared_input_statement": SHARED_STATEMENT,
                          "prepared_input_sha256": res.fingerprint,
                          "required_assets_union": list(spec.union),
                          "dividend_mode": prep.dividend_mode,
                          "dropped_incomplete_weeks": prep.dropped_incomplete_weeks})
    write_json(out / "data_manifest.json", data_manifest)
    sel = res.selected
    counts = res.counts()
    manifest = {
        "command": base.command, "mode": "in-sample",
        "spec_version": base.get("app.spec_version"), "code_version": code_version(),
        "run_timestamp": ts.isoformat(), "timezone": "Europe/Warsaw",
        "objective": spec.objective, "objective_metric": spec.objective_metric,
        "objective_direction": spec.direction, "objective_map": {k: {"metric": m, "direction": d}
                                                                  for k, (m, d) in OBJECTIVES.items()},
        "tie_break": list(TIE_BREAK),
        "tie_break_detail": "better objective, then lower relevant max drawdown, then lower "
                            "turnover, then lexicographically smaller (btc, gold, rf, stocks); "
                            "exact comparisons, no rounding or tolerance (Q-041)",
        "max_drawdown_limit": spec.max_drawdown_limit,
        "relevant_drawdown_metric": spec.drawdown_metric,
        "weight_grid": spec.grid,
        "weight_validation": "remainder: stocks = 1 - btc - gold - rf, rejected when < -1e-12; "
                             "explicit stocks: |stocks + gold + btc + rf - 1| <= 1e-12, > 1 and "
                             "< 1 rejected (ALLOC-001); decimal arithmetic (Q-041)",
        **{k: v for k, v in counts.items()},
        "selected_grid_index": sel["grid_index"],
        "selected_weights": {k: sel[f"weight_{k}"] for k in SLEEVES},
        "selected_objective_value": sel["objective_value"],
        "selected_relevant_max_drawdown": sel["relevant_max_drawdown"],
        "required_assets_union": list(spec.union),
        "prepared_input_sha256": res.fingerprint,
        "shared_input": True, "shared_input_statement": SHARED_STATEMENT,
        "effective_first_week": weeks[0].isoformat(),
        "effective_last_week": weeks[-1].isoformat(),
        "weeks": len(weeks),
        "first_week_rule": prep.first_week_rule,
        "requested_start": str(base.start) if base.start else None,
        "requested_end": str(base.end) if base.end else None,
        "common_data_range": [str(x) for x in prep.common_range],
        "dropped_weeks": [w.isoformat() for w in prep.calendar.dropped],
        "dropped_incomplete_weeks": prep.dropped_incomplete_weeks,
        "as_of_date": prep.as_of.isoformat(),
        "tax_profile": base.get("tax.profile"),
        "jobs_requested": spec.jobs_requested,
        "jobs_effective": spec.jobs_effective,
        "parallel_execution": spec.jobs_effective > 1,
        "sources": [{"role": p.role, "path": p.path, "sha256": p.sha256}
                    for p in sorted(prep.provenances, key=lambda p: p.role)],
        "base_strategy_sha256": _sha({k: v for k, v in flatten(base.resolved_dict()).items()
                                      if not k.startswith(("report.", "config.", "performance."))}),
        "checks": optimizer_checks(res),
        "outputs": {"grid_results": "grid_results.csv (every combination in grid_index order; "
                                    "rejected combinations with empty metrics)",
                    "summary": "summary.csv (the selected candidate's SUMMARY_FIELDS row)",
                    "selected": "selected/ (standard run artifacts of the selected candidate, "
                                "from its computed result)",
                    "shared": ["config_resolved.yaml", "selected_config_resolved.yaml",
                               "data_manifest.json", "validation_report.csv",
                               "weekly_normalized.csv"]},
    }
    write_json(out / MANIFEST, manifest)
    shared = {"timestamp": ts, "profile_manifest": {
        "shared_input": True, "shared_manifest": f"../{MANIFEST}",
        "prepared_input_sha256": res.fingerprint,
        "data_preparation": "prepared once by optimize (union of the candidates' assets); the "
                            "selected candidate was not rerun - these files come from its "
                            "computed result",
        "shared_outputs": {"weekly_normalized.csv": "../weekly_normalized.csv"},
        "selected_grid_index": sel["grid_index"]}}
    write_portfolio_outputs(res.selected_config, res.selected_result, out_dir=out / "selected",
                            shared=shared)
