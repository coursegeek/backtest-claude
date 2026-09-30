"""Command line interface (META-001, META-004, CLI-001..011, Q-026/Q-027 units and scope).

Unit conventions (Q-026): --threshold*, --threshold-grid, --btc/gold/stocks/rf-weight and
--max-drawdown-limit are percent; --weights, --sell-fraction and --sortino-mar are decimal
fractions; --rebalance-band-pp is in percentage points. Everything enters the configuration
as decimal fractions.
"""
from __future__ import annotations

import argparse
import sys

from .config import (RISKY_ASSETS, ResolvedConfig, int_grid, load_config_file, parse_grid,
                     percent_grid, percent_value, set_path)
from .errors import BacktestError, ConfigError

COMMANDS = ("run", "signals", "delay-scan", "threshold-scan", "optimize", "tax-compare",
            "rebalance-scan")
SCAN_COMMANDS = ("delay-scan", "threshold-scan")


def _pct(text, name):
    """Q-026: percent CLI value -> decimal fraction (exact decimal conversion)."""
    return percent_value(text, name)


def _int(text, name):
    vals = int_grid(text, name)
    if len(vals) != 1:
        raise ConfigError(f"{name}: a single positive integer is expected")
    return vals[0]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest.py", description="Backtest V2 (spec 3.1)")
    sub = p.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        s = sub.add_parser(name)
        add = s.add_argument
        add("--config", help="YAML/JSON configuration file (META-005)")
        add("--print-config", action="store_true", help="print the resolved config and exit")
        add("--start"); add("--end"); add("--as-of-date"); add("--run-name"); add("--output-dir")
        add("--frequency"); add("--currency"); add("--initial-capital", type=float)
        add("--data-file", action="append", default=[], metavar="KEY=PATH")
        for opt in ("stocks-price-file", "stocks-return-file", "gold-file", "cpi-file",
                    "btc-file", "dividend-file", "distribution-file"):
            add(f"--{opt}")
        add("--cpi-missing-policy")
        add("--weights"); add("--single-asset"); add("--asset")
        add("--ma"); add("--threshold"); add("--threshold-off"); add("--threshold-on")
        add("--confirm-weeks", type=int); add("--confirm-off-weeks", type=int)
        add("--confirm-on-weeks", type=int)
        add("--delay"); add("--sell-fraction"); add("--risk-off-action")
        add("--rebalance"); add("--rebalance-band-pp", type=float)
        add("--transaction-cost-bps", type=float); add("--slippage-bps", type=float)
        add("--tax-profile"); add("--dividend-tax-mode"); add("--dividend-estimate-policy")
        add("--cost-basis"); add("--external-solidarity-base", type=float)
        add("--foundation-tax-event"); add("--foundation-annual-cost", type=float)
        add("--foundation-cost-proration"); add("--foundation-setup-cost", type=float)
        add("--sortino-mar", type=float); add("--jobs", type=int)
        add("--btc-weight"); add("--gold-weight"); add("--stocks-weight"); add("--rf-weight")
        add("--objective"); add("--max-drawdown-limit", type=float)
        add("--optimization-mode"); add("--optimize-params"); add("--walk-forward-window")
        add("--train-years", type=int); add("--test-years", type=int); add("--step-years", type=float)
        add("--ma-grid"); add("--threshold-grid"); add("--delay-grid"); add("--confirmation-grid")
        add("--band-pp")
    return p


def cli_layer(args) -> dict:
    """Translate parsed arguments into a config layer (decimal units)."""
    cmd = args.command
    layer: dict = {}

    def put(key, value):
        if value is not None:
            set_path(layer, key, value)

    put("run.start", args.start); put("run.end", args.end)
    put("run.as_of_date", args.as_of_date); put("report.run_name", args.run_name)
    put("report.output_dir", args.output_dir); put("engine.frequency", args.frequency)
    put("portfolio.currency", args.currency)
    put("portfolio.initial_capital_pln", args.initial_capital)
    files = {"stocks_price_file": args.stocks_price_file, "stocks_return_file": args.stocks_return_file,
             "gold_file": args.gold_file, "cpi_file": args.cpi_file, "btc_file": args.btc_file,
             "dividend_file": args.dividend_file}
    for k, v in files.items():
        put(f"data.{k}", v)
    put("tax.foundation.distribution_file", args.distribution_file)
    if args.data_file:
        overrides = {}
        for item in args.data_file:
            if "=" not in item:
                raise ConfigError(f"--data-file expects KEY=PATH, got {item!r} (DATA-008)")
            k, v = item.split("=", 1)
            overrides[k.strip()] = v.strip()
        put("data.overrides", overrides)
    put("cpi.missing_policy", args.cpi_missing_policy)
    put("allocation.targets", args.weights)
    put("allocation.single_asset", args.single_asset)
    put("run.asset", args.asset)
    put("portfolio.rebalance", args.rebalance)
    put("portfolio.rebalance_band_pp", args.rebalance_band_pp)
    put("portfolio.transaction_cost_bps", args.transaction_cost_bps)
    put("portfolio.slippage_bps", args.slippage_bps)
    if cmd != "tax-compare":                    # tax-compare: a profile list (see below)
        put("tax.profile", args.tax_profile)
    put("tax.dividend_tax_mode", args.dividend_tax_mode)
    put("tax.dividend_estimate_policy", args.dividend_estimate_policy)
    put("tax.individual.cost_basis", args.cost_basis)
    put("tax.individual.external_solidarity_base_pln", args.external_solidarity_base)
    put("tax.foundation.tax_event", args.foundation_tax_event)
    put("tax.foundation.annual_admin_cost_pln", args.foundation_annual_cost)
    put("tax.foundation.admin_cost_proration", args.foundation_cost_proration)
    put("tax.foundation.setup_cost_pln", args.foundation_setup_cost)
    put("metrics.sortino_mar_annual", args.sortino_mar)
    put("performance.jobs", args.jobs)

    # signal parameters: scans/signals target --asset; single-asset mode its asset;
    # a multi-asset run applies CLI signal flags to every risky asset (Q-027).
    targets = ([args.asset] if args.asset else [args.single_asset] if args.single_asset
               else list(RISKY_ASSETS))
    sig = {}
    if args.ma is not None and cmd != "optimize":
        sig["ma_length"] = _int(args.ma, "--ma")
    if args.threshold is not None and cmd != "threshold-scan":
        sig["threshold"] = _pct(args.threshold, "--threshold")
    if cmd == "threshold-scan" and (args.threshold_off is not None or args.threshold_on is not None
                                    or args.threshold_grid):
        raise ConfigError("threshold-scan scans one symmetric threshold per grid point "
                          "(threshold_off = threshold_on = p, THR-002): give the grid with "
                          "--threshold (percent); --threshold-off/--threshold-on/--threshold-grid "
                          "are not accepted")
    if args.threshold_off is not None:
        sig["threshold_off"] = _pct(args.threshold_off, "--threshold-off")
    if args.threshold_on is not None:
        sig["threshold_on"] = _pct(args.threshold_on, "--threshold-on")
    wf_params = [x.strip() for x in (args.optimize_params or "").split(",")]
    if args.sell_fraction is not None:
        if cmd == "optimize" and "sell_fraction" in wf_params:
            # Q-022: walk-forward optimizes sell_fraction over a decimal grid ('0.25,0.5,0.75'
            # or '0.25:0.75:0.25'); decimals, never percent
            put("optimizer.sell_fraction_grid", parse_grid(args.sell_fraction, "--sell-fraction"))
        else:
            vals = parse_grid(args.sell_fraction, "--sell-fraction")
            if len(vals) != 1:
                raise ConfigError("--sell-fraction: one decimal fraction is expected (a list or "
                                  "range only with optimize --optimize-params ...,sell_fraction)")
            sig["sell_fraction"] = vals[0]
    for key, val in (("confirm_weeks", args.confirm_weeks),
                     ("confirm_off_weeks", args.confirm_off_weeks),
                     ("confirm_on_weeks", args.confirm_on_weeks),
                     ("risk_off_action", args.risk_off_action)):
        if val is not None:
            sig[key] = val
    if args.delay is not None:
        if cmd == "delay-scan":                     # DELAY-002/003: the scanned grid
            put("optimizer.delay_grid", int_grid(args.delay, "--delay"))
        else:                                       # THR-003 / run: one scalar delay
            vals = int_grid(args.delay, "--delay")
            if len(vals) != 1:
                raise ConfigError("--delay: a single positive integer is expected for this command")
            sig["delay_weeks"] = vals[0]
    if cmd == "threshold-scan" and args.threshold is not None:     # THR-002/005, percent
        put("optimizer.threshold_grid", percent_grid(args.threshold, "--threshold"))
    for asset in targets:
        for k, v in sig.items():
            put(f"signals.{asset}.{k}", v)

    # optimizer (percent grids -> decimals)
    for opt, key in (("btc_weight", "optimizer.btc_weight"), ("gold_weight", "optimizer.gold_weight")):
        v = getattr(args, opt)
        if v is not None:
            put(key, percent_grid(v, f"--{opt.replace('_', '-')}"))
    if args.stocks_weight is not None:
        put("optimizer.stocks_weight", "remainder" if args.stocks_weight == "remainder"
            else percent_grid(args.stocks_weight, "--stocks-weight"))
    if args.rf_weight is not None:
        put("optimizer.rf_weight", _pct(args.rf_weight, "--rf-weight"))
    if args.max_drawdown_limit is not None:
        put("optimizer.max_drawdown_limit", _pct(args.max_drawdown_limit, "--max-drawdown-limit"))
    put("optimizer.objective", args.objective); put("optimizer.mode", args.optimization_mode)
    if args.optimize_params:
        put("optimizer.parameters", [x.strip() for x in args.optimize_params.split(",")])
    put("optimizer.window_type", args.walk_forward_window)
    put("optimizer.train_years", args.train_years); put("optimizer.test_years", args.test_years)
    put("optimizer.step_years", args.step_years)
    if args.ma_grid:
        put("optimizer.ma_grid", int_grid(args.ma_grid, "--ma-grid", minimum=2))
    if args.threshold_grid:
        put("optimizer.threshold_grid", percent_grid(args.threshold_grid, "--threshold-grid"))
    if args.delay_grid:
        put("optimizer.delay_grid", int_grid(args.delay_grid, "--delay-grid"))
    if args.confirmation_grid:
        put("optimizer.confirmation_grid", int_grid(args.confirmation_grid, "--confirmation-grid"))
    if args.band_pp is not None:                    # REB-010: percentage points
        put("optimizer.rebalance_band_grid", parse_grid(args.band_pp, "--band-pp"))
    if cmd == "tax-compare" and args.tax_profile is not None:
        # validated (allowed values, no duplicates, >= 1) and ordered by tax_compare
        put("tax.compare_profiles", [x.strip() for x in args.tax_profile.split(",")])
    return layer


def resolve(argv) -> ResolvedConfig:
    args = build_parser().parse_args(argv)
    file_layer = load_config_file(args.config) if args.config else {}
    cfg = ResolvedConfig(args.command, file_layer, cli_layer(args), args.config)
    cfg.print_only = args.print_config
    return cfg


def main(argv=None) -> int:
    from . import app
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cfg = resolve(argv)
        if getattr(cfg, "print_only", False):
            sys.stdout.write(cfg.to_yaml(cfg.as_of()))
            return 0
        result = app.dispatch(cfg)
        if result is not None and getattr(result, "output_dir", None):
            print(f"outputs written to {result.output_dir}")
        return 0
    except BacktestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
