"""tax-compare (TAX-003, FND-008, CLI-006, Q-023): one experiment, several tax profiles.

The command orchestrates the production run pipeline; it is not a second backtester:
  1. the profile list is validated (allowed values, no duplicates, at least one profile) and
     put in the canonical order none, individual_pl, family_foundation_15,
     family_foundation_19;
  2. every profile configuration is a new ResolvedConfig derived from the base configuration
     by changing only ``tax.profile`` (the base and the other profiles are never mutated);
     all of them are validated before any data is loaded (ALLOC-001, Q-037, Q-047);
  3. the data requirements of all profiles are merged: as soon as one profile needs dividend
     data the dividend source is part of the common calendar of every profile, so all rows
     share effective_first_week, effective_last_week, weeks and dropped weeks;
  4. the input is prepared once (``app.prepare_run``) and the same PreparedRun / EngineInputs
     object is passed to ``app.run_prepared`` of every profile (central engine + funding /
     rebalancing hooks + profile hooks + terminal settlement + pre-tax shadow + metrics);
  5. outputs are written only after every profile succeeded (a failing profile fails the whole
     command and names the profile; no summary that looks like a complete comparison).
The command reports numbers side by side; it never ranks profiles or names a winner.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .allocation import active_risky_assets, strategic_targets
from .app import (PortfolioRunResult, PreparedRun, check_supported_run, dividend_mode_of,
                  prepare_run, run_prepared, write_portfolio_outputs)
from .calendar import elapsed_days, inception_date
from .config import ResolvedConfig, flatten, set_path
from .errors import BacktestError, ConfigError
from .manifest import build_manifest, code_version, run_timestamp
from .reporting import (NORMALIZED_FIELDS, SUMMARY_FIELDS, VALIDATION_FIELDS, run_directory,
                        summary_row, write_csv, write_json)
from .validation import ValidationReport

CANONICAL_PROFILES = ("none", "individual_pl", "family_foundation_15", "family_foundation_19")
PROFILE_DIR = "profiles"
SHARED_MANIFEST = "tax_compare_manifest.json"
SHARED_STATEMENT = ("all profiles used the same prepared market/calendar inputs: one PreparedRun "
                    "(one EngineInputs object) loaded, validated and aligned once; no profile "
                    "reloaded or realigned any source")
# keys that may differ between the configurations of the compared profiles
PROFILE_KEYS = ("tax.profile",)
# resolved keys that never influence results (output location, run label, config metadata,
# execution settings, the profile axis): excluded from resolved_strategy_sha256
NON_STRATEGY_PREFIXES = ("report.", "config.", "performance.", "tax.compare_profiles",
                         "tax.profile")


class TaxCompareError(BacktestError):
    """A profile of a tax-compare could not be computed; nothing was written."""

    def __init__(self, profile: str, cause: BacktestError):
        self.profile, self.cause = profile, cause
        self.exit_code = cause.exit_code
        super().__init__(f"tax-compare: profile {profile} failed: {cause}; no tax-compare "
                         "outputs were written (no partial comparison)")


# ============================================================================ profiles
def parse_profiles(value) -> tuple:
    """The requested profiles in input order (a comma separated string or a list)."""
    if value is None:
        raise ConfigError("tax-compare: no tax profiles given (--tax-profile p1,p2,...)")
    items = value.split(",") if isinstance(value, str) else list(value)
    names = [str(x).strip() for x in items]
    if not names or any(not n for n in names):
        raise ConfigError(f"tax-compare: empty tax profile in {value!r} (at least one profile; "
                          f"allowed {','.join(CANONICAL_PROFILES)})")
    unknown = [n for n in names if n not in CANONICAL_PROFILES]
    if unknown:
        raise ConfigError(f"tax-compare: unknown tax profile(s) {unknown}; allowed "
                          f"{','.join(CANONICAL_PROFILES)}")
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise ConfigError(f"tax-compare: duplicate tax profile(s) {dup}")
    return tuple(names)


def canonical_order(profiles) -> tuple:
    """Deterministic output order, independent of the order given by the user."""
    return tuple(p for p in CANONICAL_PROFILES if p in profiles)


def profile_config(base: ResolvedConfig, profile: str, profiles: Optional[tuple] = None
                   ) -> ResolvedConfig:
    """A new configuration equal to ``base`` except tax.profile (base is not mutated); the
    recorded compare_profiles list is the canonical one (independent of the requested order)."""
    cli = copy.deepcopy(base.layers["cli"])
    set_path(cli, "tax.profile", profile)
    if profiles is not None:
        set_path(cli, "tax.compare_profiles", list(profiles))
    cfg = ResolvedConfig(base.command, base.layers["file"], cli, base.config_path)
    cfg.print_only = False
    return cfg


def strategy_view(cfg: ResolvedConfig) -> dict:
    """The resolved configuration without the profile axis (identical for every profile). The
    as_of date is not expanded here: every profile uses the as_of of the shared PreparedRun."""
    flat = flatten(cfg.resolved_dict())
    for k in PROFILE_KEYS:
        flat.pop(k, None)
    return flat


def strategy_sha256(cfg: ResolvedConfig) -> str:
    """Hash of the resolved parameters that determine the results of every profile."""
    view = {k: v for k, v in strategy_view(cfg).items()
            if not any(k == p or k.startswith(p) for p in NON_STRATEGY_PREFIXES)}
    return _sha(view)


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def prepared_fingerprint(prepared: PreparedRun) -> str:
    """SHA-256 of the prepared input as the engine sees it: calendar, weekly returns, RF,
    dividend yields and status, signal price series, signal parameters, targets, capital,
    costs, cost basis, as_of, common range, dropped weeks and the source hashes."""
    i = prepared.inputs
    return _sha({
        "weeks": [w.isoformat() for w in i.weeks],
        "market": [[w.isoformat(), sorted((a, repr(r)) for a, r in m.asset_returns.items()),
                    repr(m.rf_return), sorted((a, repr(d)) for a, d in m.dividend_yield.items()),
                    sorted(m.dividend_status.items())] for w, m in sorted(i.market.items())],
        "signal_series": {a: [[p.week_key.isoformat(), repr(p.price), p.available_at.isoformat()]
                              for p in s.points] for a, s in sorted(i.signal_series.items())},
        "params": {a: dataclasses.asdict(p) for a, p in sorted(i.params.items())},
        "targets": {k: repr(v) for k, v in sorted(i.targets.items())},
        "initial_capital": repr(i.initial_capital), "costs": dataclasses.asdict(i.costs),
        "cost_basis_method": i.cost_basis_method, "run_start": str(i.run_start),
        "as_of": prepared.as_of.isoformat(), "dividend_mode": prepared.dividend_mode,
        "common_range": [str(x) for x in prepared.common_range],
        "dropped": [w.isoformat() for w in prepared.calendar.dropped],
        "sources": [[p.role, p.sha256] for p in sorted(prepared.provenances, key=lambda p: p.role)],
    })


# ============================================================================ result
@dataclass
class TaxCompareResult:
    profiles: tuple                 # canonical order
    requested_profiles: tuple       # input order
    profiles_source: str
    base: ResolvedConfig
    configs: dict                   # profile -> ResolvedConfig
    prepared: PreparedRun           # the one shared prepared input
    data_requirements: dict         # profile -> dividend data needed (none|smoothed_weekly|exact)
    results: dict                   # profile -> PortfolioRunResult
    rows: tuple                     # summary rows, canonical order
    fingerprint: str
    output_dir: Optional[Path] = None

    def row(self, profile: str) -> dict:
        return self.rows[self.profiles.index(profile)]


def requested_profiles(cfg: ResolvedConfig) -> tuple:
    value = cfg.get("tax.compare_profiles")
    return parse_profiles(value), cfg.source_of("tax.compare_profiles")


def data_requirements(configs: dict) -> tuple:
    """Per-profile dividend data requirement and their superset for the shared input."""
    need = {}
    for p, cfg in configs.items():
        need[p] = dividend_mode_of(cfg, active_risky_assets(strategic_targets(cfg)))
    modes = sorted({m for m in need.values() if m != "none"})
    if len(modes) > 1:                  # impossible with one tax.dividend_tax_mode; defensive
        raise ConfigError(f"tax-compare: incompatible dividend data requirements {need}")
    return need, (modes[0] if modes else "none")


def run_tax_compare(base: ResolvedConfig, write: bool = True) -> TaxCompareResult:
    if base.source_of("tax.profile") != "defaults":
        raise ConfigError(f"tax-compare: tax.profile={base.get('tax.profile')!r} is set in the "
                          f"{base.source_of('tax.profile')} layer; the profile axis of tax-compare "
                          "is --tax-profile / tax.compare_profiles, remove the single tax.profile "
                          "(Q-023)")
    requested, source = requested_profiles(base)
    profiles = canonical_order(requested)
    # ALLOC-001 (Q-023): a full portfolio run needs explicit allocation.targets
    strategic_targets(base)
    configs = {p: profile_config(base, p, profiles) for p in profiles}
    for p, cfg in configs.items():                      # fail before any data is loaded
        try:
            check_supported_run(cfg)
            strategic_targets(cfg)
        except BacktestError as e:
            raise TaxCompareError(p, e) from e
    views = {p: strategy_view(cfg) for p, cfg in configs.items()}
    first = views[profiles[0]]
    for p, v in views.items():
        if v != first:
            diff = sorted(k for k in set(v) | set(first) if v.get(k) != first.get(k))
            raise ConfigError(f"tax-compare: profile {p} differs in strategy keys {diff}")
    need, superset = data_requirements(configs)
    prepared = prepare_run(base, dividend_mode=superset)            # the only data load
    results = {}
    for p in profiles:
        try:
            results[p] = run_prepared(configs[p], prepared, write=False)
        except BacktestError as e:
            raise TaxCompareError(p, e) from e
    rows = tuple(summary_row(configs[p], results[p]) for p in profiles)
    out = TaxCompareResult(profiles=profiles, requested_profiles=requested,
                           profiles_source=source, base=base, configs=configs,
                           prepared=prepared, data_requirements=need, results=results, rows=rows,
                           fingerprint=prepared_fingerprint(prepared))
    if write:
        out.output_dir = write_tax_compare_outputs(out)
    return out


# ============================================================================ outputs
def base_config_doc(res: TaxCompareResult) -> dict:
    """config_resolved.yaml of the compare: the shared configuration; the profile axis is
    tax.compare_profiles (no single tax.profile)."""
    d = res.base.resolved_dict(res.prepared.as_of)
    d["tax"]["compare_profiles"] = list(res.profiles)
    return d


def _strategy(res: TaxCompareResult) -> dict:
    cfg = res.base
    return {
        "targets": dict(res.prepared.targets),
        "initial_capital_pln": float(cfg.get("portfolio.initial_capital_pln")),
        "rebalance": cfg.get("portfolio.rebalance"),
        "rebalance_band_pp": cfg.get("portfolio.rebalance_band_pp"),
        "transaction_cost_bps": float(cfg.get("portfolio.transaction_cost_bps")),
        "slippage_bps": float(cfg.get("portfolio.slippage_bps")),
        "signal_params": {a: dataclasses.asdict(p)
                          for a, p in sorted(res.prepared.inputs.params.items())},
        "dividend_tax_mode": cfg.get("tax.dividend_tax_mode"),
        "foundation_tax_event": cfg.get("tax.foundation.tax_event"),
        "foundation_distribution_tax_base": cfg.get("tax.foundation.distribution_tax_base"),
    }


def compare_checks(res: TaxCompareResult) -> dict:
    """Audit facts of the shared experiment (never a ranking)."""
    rs = res.results
    first = res.profiles[0]
    same = lambda f: all(f(rs[p]) == f(rs[first]) for p in res.profiles)   # noqa: E731
    checks = {
        "same_prepared_input_object": all(r.prepared is res.prepared for r in rs.values()),
        "same_engine_inputs_object": all(r.inputs is res.prepared.inputs for r in rs.values()),
        "same_effective_first_week": same(lambda r: r.engine.weeks[0].week_key),
        "same_effective_last_week": same(lambda r: r.engine.weeks[-1].week_key),
        "same_weeks": same(lambda r: tuple(w.week_key for w in r.engine.weeks)),
        "same_dropped_weeks": same(lambda r: r.calendar.dropped),
        "same_source_hashes": same(lambda r: tuple(sorted((p.role, p.sha256)
                                                          for p in r.provenances))),
    }
    if "none" in rs and "individual_pl" in rs:
        checks["final_wealth_pre_tax_none_equals_individual_pl"] = (
            rs["none"].pre_tax.final_wealth == rs["individual_pl"].pre_tax.final_wealth)
    if "family_foundation_15" in rs and "family_foundation_19" in rs:
        a, b = rs["family_foundation_15"], rs["family_foundation_19"]
        checks["final_wealth_pre_tax_foundation_15_equals_19"] = (
            a.pre_tax.final_wealth == b.pre_tax.final_wealth)
        checks["weekly_path_foundation_15_equals_19"] = weekly_path(a) == weekly_path(b)
    return checks


def weekly_path(res: PortfolioRunResult) -> tuple:
    """Everything of the weekly (pre-terminal) path of a run that must not depend on the
    terminal distribution rate: the week records (ledgers after every pipeline step, returns,
    trades, payments, RF transfers, rebalancing, amounts due, dividend settlements, states),
    the journals, the weekly tax/cost events (setup and admin costs, dividend and RF taxes)
    and pre_terminal_nav."""
    e = res.engine
    return (e.initial_ledger, e.weeks, e.trades, e.payments, e.transfers, e.rebalance_events,
            e.dividend_reinvestments, e.realizations,
            tuple(res.tax_state.tax_events) if res.tax_state else (), e.final_ledger.nav)


def write_tax_compare_outputs(res: TaxCompareResult) -> Path:
    """<output_dir>/<timestamp>_<run_name>/: summary.csv (one row per profile, SUMMARY_FIELDS),
    tax_compare_manifest.json, data_manifest.json, config_resolved.yaml, validation_report.csv
    and weekly_normalized.csv of the shared input, and profiles/<profile>/ with the standard
    run artifacts of each profile. Written only after every profile succeeded; a failure
    while writing removes the directory."""
    ts = run_timestamp()
    base = res.base
    out = run_directory(base.get("report.output_dir"), base.get("report.run_name"), ts)
    try:
        _write(res, out, ts)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return out


def _write(res: TaxCompareResult, out: Path, ts) -> None:
    base, prep = res.base, res.prepared
    weeks = prep.inputs.weeks
    write_csv(out / "summary.csv", SUMMARY_FIELDS, res.rows)
    (out / "config_resolved.yaml").write_text(
        yaml.safe_dump(base_config_doc(res), sort_keys=True, allow_unicode=True,
                       default_flow_style=False), encoding="utf-8")
    shared_report = ValidationReport()
    shared_report.extend(prep.issues)
    write_csv(out / "validation_report.csv", VALIDATION_FIELDS, shared_report.rows())
    write_csv(out / "weekly_normalized.csv", NORMALIZED_FIELDS, prep.normalized)
    data_manifest = build_manifest(prep.provenances, prep.as_of, ts, base.command)
    data_manifest.update({"shared_input": True, "shared_input_statement": SHARED_STATEMENT,
                          "prepared_input_sha256": res.fingerprint,
                          "dividend_mode": prep.dividend_mode})
    write_json(out / "data_manifest.json", data_manifest)
    config_bytes = Path(base.config_path).read_bytes() if base.config_path else None
    manifest = {
        "command": base.command, "spec_version": base.get("app.spec_version"),
        "code_version": code_version(), "run_timestamp": ts.isoformat(),
        "timezone": "Europe/Warsaw",
        "profiles": list(res.profiles), "profiles_requested": list(res.requested_profiles),
        "profiles_source": res.profiles_source,
        "profile_order": "canonical (none, individual_pl, family_foundation_15, "
                         "family_foundation_19), independent of the requested order",
        "shared_input": True, "shared_input_statement": SHARED_STATEMENT,
        "prepared_input_sha256": res.fingerprint,
        "data_requirements": dict(res.data_requirements),
        "shared_dividend_mode": prep.dividend_mode,
        "calendar_rule": "superset of the data requirements of the compared profiles: the "
                         "dividend source is part of the common calendar of every profile as "
                         "soon as one profile needs it (Q-023)",
        "requested_start": str(base.start) if base.start else None,
        "requested_end": str(base.end) if base.end else None,
        "effective_first_week": weeks[0].isoformat(),
        "effective_last_week": weeks[-1].isoformat(),
        "weeks": len(weeks),
        "inception_date": inception_date(prep.first_week).isoformat(),
        "elapsed_days": elapsed_days(prep.first_week, weeks[-1]),
        "common_data_range": [str(x) for x in prep.common_range],
        "range_truncations": [f"{r}:{side}:{own}->{eff}" for r, side, own, eff in prep.truncations],
        "dropped_weeks": [w.isoformat() for w in prep.calendar.dropped],
        "dropped_incomplete_weeks": prep.dropped_incomplete_weeks,
        "as_of_date": prep.as_of.isoformat(),
        "sources": [{"role": p.role, "path": p.path, "sha256": p.sha256}
                    for p in sorted(prep.provenances, key=lambda p: p.role)],
        "config_file": base.config_path,
        "config_file_sha256": hashlib.sha256(config_bytes).hexdigest() if config_bytes else None,
        "resolved_strategy_sha256": strategy_sha256(res.configs[res.profiles[0]]),
        "strategy": _strategy(res),
        "profile_axis": "only tax.profile differs between the profile configurations",
        "checks": compare_checks(res),
        "outputs": {"summary": "summary.csv (one row per profile, the SUMMARY_FIELDS schema of "
                               "'run')",
                    "profiles": {p: f"{PROFILE_DIR}/{p}" for p in res.profiles},
                    "shared": ["data_manifest.json", "config_resolved.yaml",
                               "validation_report.csv", "weekly_normalized.csv"]},
        "summary_column_semantics": {
            "applied_*": "parameters the row's profile actually applies (0.0 or "
                         "not_applicable when the profile does not apply them)",
            "tax_*": "configured scenario assumptions shared by every row (REP-012), not the "
                     "active taxes of the row"},
        "data_note": "staged proxy data (Q-002, Q-004, Q-008 remain DATA_BLOCKER): mechanics "
                     "validation, not proof of canonical-data compliance; see "
                     "validation_report.csv",
        "no_ranking": "profiles are reported side by side; no ranking, winner or recommendation",
    }
    write_json(out / SHARED_MANIFEST, manifest)
    for p in res.profiles:
        shared = {"timestamp": ts, "profile_manifest": {
            "shared_input": True,
            "shared_manifest": f"../../{SHARED_MANIFEST}",
            "shared_input_statement": SHARED_STATEMENT,
            "prepared_input_sha256": res.fingerprint,
            "data_preparation": "prepared once by tax-compare; this profile did not reload or "
                                "realign any source (the sources below repeat the shared hashes)",
            "shared_outputs": {"weekly_normalized.csv": "../../weekly_normalized.csv"},
            "compare_profiles": list(res.profiles),
        }}
        write_portfolio_outputs(res.configs[p], res.results[p], out_dir=out / PROFILE_DIR / p,
                                shared=shared)
