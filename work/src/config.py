"""Configuration: defaults (DEF-*), YAML/JSON files, CLI precedence (META-004), units
(Q-026), per-asset signal parameter resolution (SIG-008, Q-027) and the resolved config
written to config_resolved.yaml (REP-007, REPRO-007).

Units: every value stored in the configuration is a decimal fraction (0.03 = 3%). The CLI
layer converts percent-style options before they enter the CLI layer.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any, Optional

import yaml

from . import SPEC_VERSION, APP_NAME
from .errors import ConfigError
from .models import RISKY_ASSETS, SignalParams

WORK_DIR = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = WORK_DIR.parent

DEFAULTS: dict = {
    "app": {"name": APP_NAME, "spec_version": SPEC_VERSION},
    "engine": {"frequency": "weekly", "no_lookahead": True, "deterministic": True},
    "data": {
        "dir": str(PACKAGE_ROOT / "input" / "data"),
        "profile": str(WORK_DIR / "config" / "cleanroom_data.yaml"),
        "stocks_price_file": "US_STOCK_PRICE_WEEKLY_1885_2026.csv",
        "stocks_return_file": "F-F_Research_Data_Factors_weekly.csv",
        "gold_file": "GOLD_LBMA_PM_weekly_backtest_ready.csv",
        "cpi_file": "CPIAUCNS.csv",
        "btc_file": "BTC_weekly_date_price_2011_2026.csv",
        "dividend_file": "SPX_dividend_return_weekly_1970_2026.csv",
        "dividend_cash_file": None,
        "overrides": {},
        "adapters": {},
        "btc_range": None,
        "stocks_signal_segments": None,
        "stocks_signal_components": None,
        "ignore_fx": True,
        "ff_zip_supported": True,
    },
    "schema": {"fx": "none"},
    "validation": {"sort": True, "duplicates": "error", "missing_weeks": True,
                   "numeric": "error", "ff_alignment_regression": True},
    "missing": {"return_policy": "error", "price_policy": "error"},
    "calendar": {"week_key": "Friday", "daily_to_weekly": "last",
                 "ff_date_mapping": "same_calendar_week_friday", "btc_availability": True,
                 "signal_only": True, "signal_only_completed_weeks_only": True},
    "cpi": {"missing_policy": "previous_available", "mapping": "previous_available",
            "optional_for_nominal": True, "label": "US CPI-U / CPIAUCNS"},
    "run": {"start": None, "end": None, "as_of_date": None, "asset": None},
    "portfolio": {"initial_capital_pln": 1000000.0, "currency": "PLN",
                  "rebalance": "signal-only", "rebalance_band_pp": None,
                  "transaction_cost_bps": 0.0, "slippage_bps": 0.0},
    "allocation": {"targets": None, "single_asset": False},
    "signal": {"ma_length": 50, "delay_weeks": 1, "initial_state": "reconstruct_history",
               "hysteresis": True, "confirmation_reset": True},
    "signals": {
        "default": {"threshold": 0.03, "sell_fraction": 0.50, "confirm_weeks": 1,
                    "risk_off_target": "rf_reserve_by_asset",
                    "risk_off_action": "sell_fraction_current"},
        "stocks": {"threshold": 0.00, "confirm_weeks": 2},
        "gold": {"threshold": 0.00, "confirm_weeks": 4},
        "btc": {"threshold": 0.03, "confirm_weeks": 1},
    },
    "tax": {
        "profile": "none", "assumption_mode": True, "dividend_tax_mode": "smoothed_weekly",
        "dividend_estimate_policy": "allow_with_warning", "dividend_reinvest": "reinvest",
        "dividend_approx_method": "use_supplied_dividend_return",
        "none": {"dividend_rate": 0.0},
        "individual": {"dividend_rate": 0.19, "capital_gains_rate": 0.19,
                       "solidarity_rate": 0.04, "solidarity_threshold_pln": 1000000.0,
                       "external_solidarity_base_pln": 0.0, "cost_basis": "FIFO",
                       "loss_carryforward_years": 5, "loss_offset_fraction": 1.0,
                       "rf_interest_rate": 0.19},
        "foundation": {"dividend_rate": 0.15, "internal_trading_tax_rate": 0.0,
                       "tax_event": "terminal", "distribution_tax_base": "distributed_amount",
                       "annual_admin_cost_pln": 40000.0, "admin_cost_proration": "prorated",
                       "rf_interest_rate": 0.0, "setup_cost_pln": 40000.0,
                       "distribution_file": None},
        "foundation_15": {"distribution_rate": 0.15},
        "foundation_19": {"distribution_rate": 0.19},
    },
    "metrics": {"sortino_mar_annual": 0.0},
    "optimizer": {"btc_weight": [i / 100 for i in range(26)],
                  "gold_weight": [i / 100 for i in range(26)], "stocks_weight": "remainder",
                  "rf_weight": 0.0, "objective": "after_tax_cagr", "max_drawdown_limit": None,
                  "mode": "in-sample", "train_years": 15, "test_years": 5, "step_years": None,
                  "window_type": "rolling", "parameters": ["weights"],
                  "tie_break": ["objective", "lower_maxDD", "lower_turnover",
                                "lexicographic_weights"]},
    "performance": {"jobs": 1, "progress": True, "cache": False},
    "report": {"output_dir": "results", "run_name": "run"},
}

# Q-027: command specific defaults layered between DEFAULTS and the config file.
COMMAND_DEFAULTS: dict = {
    "delay-scan": {"optimizer": {"delay_grid": [1, 2, 3, 4]}},                       # DELAY-002
    "threshold-scan": {"optimizer": {"threshold_grid": [0.01, 0.02, 0.03, 0.04, 0.05],  # THR-002
                                     "delay_grid": [1]}},                              # THR-003
}

ENUMS = {
    "engine.frequency": {"weekly"},
    "portfolio.currency": {"PLN"},
    "portfolio.rebalance": {"weekly", "monthly", "quarterly", "annually", "band", "signal-only"},
    "validation.duplicates": {"error", "last", "first"},
    "validation.numeric": {"error", "source_policy"},
    "missing.return_policy": {"error", "drop"},
    "missing.price_policy": {"error", "carry"},
    "cpi.missing_policy": {"previous_available", "error"},
    "cpi.mapping": {"previous_available", "month_value"},
    "signal.initial_state": {"reconstruct_history", "RISK_ON"},
    "tax.profile": {"none", "individual_pl", "family_foundation_15", "family_foundation_19"},
    "tax.dividend_tax_mode": {"smoothed_weekly", "exact", "off"},
    "tax.dividend_estimate_policy": {"allow_with_warning", "actual_only", "error_on_estimate"},
    "tax.dividend_reinvest": {"reinvest"},
    "tax.individual.cost_basis": {"FIFO", "average_cost"},
    "tax.foundation.tax_event": {"terminal", "distribution_schedule"},
    "tax.foundation.distribution_tax_base": {"distributed_amount", "gain_only"},
    "tax.foundation.admin_cost_proration": {"prorated", "full"},
    "optimizer.objective": {"cagr", "after_tax_cagr", "terminal_wealth",
                            "after_tax_terminal_wealth", "sharpe", "sortino", "calmar",
                            "min_drawdown"},
    "optimizer.mode": {"in-sample", "walk-forward"},
    "optimizer.window_type": {"rolling", "anchored"},
}
SIGNAL_KEYS = ("ma_length", "threshold", "threshold_off", "threshold_on", "confirm_weeks",
               "confirm_off_weeks", "confirm_on_weeks", "delay_weeks", "sell_fraction",
               "risk_off_action")
RISK_OFF_ACTIONS = {"sell_fraction_current", "target_fraction_of_sleeve"}


# ----------------------------------------------------------------------------- helpers
def get_path(d: dict, dotted: str, default: Any = None) -> Any:
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def has_path(d: dict, dotted: str) -> bool:
    sentinel = object()
    return get_path(d, dotted, sentinel) is not sentinel


def set_path(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and v:
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def parse_date(value, name: str) -> Optional[dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{name}: expected YYYY-MM-DD, got {value!r}") from exc


def parse_grid(text, name: str = "grid") -> list:
    """Range/list syntax used by the spec: 'a:b[:step]' (inclusive end) or 'x,y,z'
    (DELAY-002/003, THR-002/005, OPT-002/003). Returns floats in the order given."""
    if isinstance(text, (int, float)):
        return [float(text)]
    if isinstance(text, (list, tuple)):
        return [float(x) for x in text]
    out = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            raise ConfigError(f"{name}: empty element in {text!r}")
        try:
            if ":" in part:
                bits = [float(x) for x in part.split(":")]
                if len(bits) not in (2, 3):
                    raise ValueError
                start, stop = bits[0], bits[1]
                step = bits[2] if len(bits) == 3 else 1.0
                if step <= 0 or stop < start:
                    raise ValueError
                n = int(math.floor((stop - start) / step + 1e-9)) + 1
                out.extend(round(start + i * step, 12) for i in range(n))
            else:
                out.append(float(part))
        except ValueError as exc:
            raise ConfigError(f"{name}: invalid range/list {text!r}") from exc
    return out


def parse_weights(text) -> dict:
    """'stocks=0.4,gold=0.4,rf=0.2' -> {'stocks':0.4,'gold':0.4,'btc':0.0,'rf':0.2} (ALLOC-001).
    Weights are decimal fractions; each 0..1; sum must be 1."""
    if isinstance(text, dict):
        items = list(text.items())
    else:
        items = []
        for kv in str(text).split(","):
            if "=" not in kv:
                raise ConfigError(f"--weights: expected name=value, got {kv!r}")
            k, v = kv.split("=", 1)
            items.append((k.strip(), v.strip()))
    out = {a: 0.0 for a in RISKY_ASSETS + ("rf",)}
    seen = set()
    for k, v in items:
        if k not in out:
            raise ConfigError(f"allocation.targets: unknown sleeve {k!r} (allowed stocks,gold,btc,rf)")
        if k in seen:
            raise ConfigError(f"allocation.targets: duplicate sleeve {k!r}")
        seen.add(k)
        try:
            w = float(v)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"allocation.targets: weight for {k} is not numeric: {v!r}") from exc
        if not 0.0 <= w <= 1.0 or math.isnan(w):
            raise ConfigError(f"allocation.targets: weight for {k} must be in 0..1, got {w}")
        out[k] = w
    total = math.fsum(out.values())
    if abs(total - 1.0) > 1e-9:
        raise ConfigError(f"allocation.targets: weights must sum to 1, got {total!r}")
    return out


def load_config_file(path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p} (config key: config.file)")
    text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot parse config file {p}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file {p} must contain a mapping at top level")
    return _normalise_dates(data)


def _normalise_dates(obj):
    if isinstance(obj, dict):
        return {k: _normalise_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalise_dates(v) for v in obj]
    if isinstance(obj, dt.date):
        return obj.isoformat()
    return obj


# ----------------------------------------------------------------------------- resolved config
class ResolvedConfig:
    """Layered configuration. Precedence: CLI > config file > command defaults > DEFAULTS."""

    LAYERS = ("cli", "file", "command", "defaults")

    def __init__(self, command: str = "run", file_layer: Optional[dict] = None,
                 cli_layer: Optional[dict] = None, config_path: Optional[str] = None):
        self.command = command
        self.config_path = config_path
        self.layers = {
            "defaults": copy.deepcopy(DEFAULTS),
            "command": copy.deepcopy(COMMAND_DEFAULTS.get(command, {})),
            "file": copy.deepcopy(file_layer or {}),
            "cli": copy.deepcopy(cli_layer or {}),
        }
        merged = {}
        for name in reversed(self.LAYERS):
            merged = deep_merge(merged, self.layers[name])
        self.data = merged
        validate(self)

    # -- access
    def get(self, dotted: str, default: Any = None) -> Any:
        return get_path(self.data, dotted, default)

    def source_of(self, dotted: str) -> str:
        for name in self.LAYERS:
            if has_path(self.layers[name], dotted):
                return name
        return "unset"

    def provenance(self) -> dict:
        return {k: self.source_of(k) for k in sorted(flatten(self.data))}

    # -- signals
    def _signal_lookup(self, asset: str, key: str, fallbacks=()) -> tuple:
        """Q-027: layer precedence first, then specificity within the layer
        (signals.<asset>.* > signal.* > signals.default.*)."""
        for layer in self.LAYERS:
            d = self.layers[layer]
            for k in (key,) + tuple(fallbacks):
                for path in (f"signals.{asset}.{k}", f"signal.{k}", f"signals.default.{k}"):
                    if has_path(d, path) and get_path(d, path) is not None:
                        return get_path(d, path), f"{layer}:{path}"
        return None, "unset"

    def signal_params(self, asset: str) -> SignalParams:
        ma, _ = self._signal_lookup(asset, "ma_length")
        th_off, _ = self._signal_lookup(asset, "threshold_off", ("threshold",))
        th_on, _ = self._signal_lookup(asset, "threshold_on", ("threshold",))
        c_off, _ = self._signal_lookup(asset, "confirm_off_weeks", ("confirm_weeks",))
        c_on, _ = self._signal_lookup(asset, "confirm_on_weeks", ("confirm_weeks",))
        delay, _ = self._signal_lookup(asset, "delay_weeks")
        sf, _ = self._signal_lookup(asset, "sell_fraction")
        action, _ = self._signal_lookup(asset, "risk_off_action")
        return SignalParams(asset=asset, ma=_int(ma, f"signals.{asset}.ma_length", 2),
                            threshold_off=_nonneg(th_off, f"signals.{asset}.threshold_off"),
                            threshold_on=_nonneg(th_on, f"signals.{asset}.threshold_on"),
                            confirm_off=_int(c_off, f"signals.{asset}.confirm_off_weeks", 1),
                            confirm_on=_int(c_on, f"signals.{asset}.confirm_on_weeks", 1),
                            delay=_int(delay, f"signals.{asset}.delay_weeks", 1),
                            sell_fraction=_fraction(sf, f"signals.{asset}.sell_fraction"),
                            risk_off_action=_enum(action, f"signals.{asset}.risk_off_action",
                                                  RISK_OFF_ACTIONS))

    # -- run window
    @property
    def start(self) -> Optional[dt.date]:
        return parse_date(self.get("run.start"), "run.start")

    @property
    def end(self) -> Optional[dt.date]:
        return parse_date(self.get("run.end"), "run.end")

    def as_of(self) -> dt.date:
        from .calendar import default_as_of
        return parse_date(self.get("run.as_of_date"), "run.as_of_date") or default_as_of()

    # -- output
    def resolved_dict(self, as_of: Optional[dt.date] = None) -> dict:
        d = copy.deepcopy(self.data)
        if as_of is not None:
            set_path(d, "run.as_of_date", as_of.isoformat())
        d["config"] = {"precedence": "CLI > config > defaults", "file": self.config_path,
                       "command": self.command}
        return d

    def to_yaml(self, as_of: Optional[dt.date] = None) -> str:
        return yaml.safe_dump(self.resolved_dict(as_of), sort_keys=True, allow_unicode=True,
                              default_flow_style=False)


def _int(v, name, minimum):
    if isinstance(v, bool) or v is None:
        raise ConfigError(f"{name}: integer >= {minimum} required, got {v!r}")
    if isinstance(v, float):
        if not v.is_integer():
            raise ConfigError(f"{name}: integer required, got {v!r}")
        v = int(v)
    if not isinstance(v, int) or v < minimum:
        raise ConfigError(f"{name}: integer >= {minimum} required, got {v!r}")
    return v


def _nonneg(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: number required, got {v!r}") from exc
    if math.isnan(f) or f < 0:
        raise ConfigError(f"{name}: must be >= 0, got {v!r}")
    return f


def _fraction(v, name):
    f = _nonneg(v, name)
    if f > 1:
        raise ConfigError(f"{name}: must be in 0..1, got {v!r}")
    return f


def _enum(v, name, allowed):
    if v not in allowed:
        raise ConfigError(f"{name}: {v!r} not in {sorted(allowed)}")
    return v


def validate(cfg: ResolvedConfig) -> None:
    for key, allowed in ENUMS.items():
        v = cfg.get(key)
        if v is not None and v not in allowed:
            hint = ""
            if key == "tax.dividend_reinvest":
                hint = " (DIV-006: net dividends are always reinvested; no cash mode)"
            if key == "portfolio.currency":
                hint = " (RUN-004: only PLN is supported)"
            raise ConfigError(f"{key}: {v!r} not in {sorted(allowed)}{hint}")
    cap = cfg.get("portfolio.initial_capital_pln")
    try:
        if not float(cap) > 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"portfolio.initial_capital_pln must be > 0, got {cap!r}") from exc
    if cfg.get("data.ignore_fx") is not True or cfg.get("schema.fx") != "none":
        raise ConfigError("FX inputs are not supported: data.ignore_fx must be true and "
                          "schema.fx none (DATA-006, SCHEMA-006)")
    for k in ("portfolio.transaction_cost_bps", "portfolio.slippage_bps"):
        _nonneg(cfg.get(k), k)
    band = cfg.get("portfolio.rebalance_band_pp")
    if band is not None and not float(band) > 0:
        raise ConfigError("portfolio.rebalance_band_pp must be > 0 percentage points")
    if cfg.get("portfolio.rebalance") == "band" and band is None:
        raise ConfigError("portfolio.rebalance=band requires portfolio.rebalance_band_pp")
    targets = cfg.get("allocation.targets")
    if targets is not None:
        parse_weights(targets)
    single = cfg.get("allocation.single_asset")
    if single not in (False, None) and single not in RISKY_ASSETS:
        raise ConfigError(f"allocation.single_asset: {single!r} not in stocks,gold,btc,false")
    asset = cfg.get("run.asset")
    if asset is not None and asset not in RISKY_ASSETS:
        raise ConfigError(f"run.asset: {asset!r} not in stocks,gold,btc")
    for name in ("run.start", "run.end", "run.as_of_date"):
        parse_date(cfg.get(name), name)
    if cfg.start and cfg.end and cfg.start > cfg.end:
        raise ConfigError("run.start must not be after run.end")
    for asset in RISKY_ASSETS:
        cfg.signal_params(asset)
