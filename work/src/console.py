"""Concise console tables (REP-011, report.console).

Presentation only: every value comes from results that are already computed - the summary
rows (reporting.summary_row / the rows of tax-compare, optimize and the scans) and the rolling
windows of metrics.RollingMetrics; nothing is calculated here. Plain text from the standard
library: no colours, no dependence on the terminal width, identical when redirected to a file.
The command line interface prints the text; library calls (run_portfolio, run_prepared, ...)
never do.
"""
from __future__ import annotations

from typing import Optional

from .reporting import summary_row

LABEL = 26
VALUE = 19
RULE = "-" * (LABEL + 2 * VALUE)


# ============================================================================ formatting
def _missing(v) -> bool:
    return v is None or v == ""


def pln(v) -> str:
    return "n/a" if _missing(v) else f"{float(v):,.0f} PLN"


def pct(v) -> str:
    return "n/a" if _missing(v) else f"{float(v) * 100:.2f}%"


def ratio(v) -> str:
    return "n/a" if _missing(v) else f"{float(v):.2f}"


def times(v) -> str:
    return "n/a" if _missing(v) else f"{float(v):.2f}x"


def line(label: str, *values) -> str:
    return label.ljust(LABEL) + "".join(str(v).rjust(VALUE) for v in values)


def info(label: str, value) -> str:
    return label.ljust(LABEL) + str(value)


# ============================================================================ blocks
def metric_table(row: dict, metrics=("final_wealth", "cagr", "max_drawdown", "sharpe",
                                     "sortino", "calmar")) -> list:
    """Pre-tax vs after-tax: final_wealth_pre_tax | after_tax_terminal_wealth, the pre-tax
    metrics on the shadow path, the after-tax metrics of summary.csv (MET-025)."""
    spec = {"final_wealth": ("Final wealth", "final_wealth_pre_tax", "after_tax_terminal_wealth", pln),
            "cagr": ("CAGR", "cagr", "after_tax_cagr", pct),
            "max_drawdown": ("Max drawdown", "max_drawdown", "after_tax_max_drawdown", pct),
            "sharpe": ("Sharpe", "sharpe", "after_tax_sharpe", ratio),
            "sortino": ("Sortino", "sortino", "after_tax_sortino", ratio),
            "calmar": ("Calmar", "calmar", "after_tax_calmar", ratio)}
    out = [line("Metric", "Pre-tax", "After-tax"), RULE]
    for m in metrics:
        label, pre, after, f = spec[m]
        out.append(line(label, f(row.get(pre)), f(row.get(after))))
    return out


def costs_block(row: dict) -> list:
    out = [info("Taxes paid", pln(row.get("total_tax_paid"))),
           info("Turnover", times(row.get("turnover")))]
    if str(row.get("tax_profile", "")).startswith("family_foundation"):
        out += [info("Foundation setup cost", pln(row.get("foundation_setup_cost_paid"))),
                info("Foundation admin costs", pln(row.get("foundation_admin_cost_paid")))]
    return out


def rolling_block(rolling) -> list:
    """MET-022: the worst full window per horizon from the rolling results; n/a when the run
    has no full window of that horizon (no partial window is shown)."""
    out = ["Worst rolling total return", RULE]
    if rolling is None:
        return out + ["n/a"]
    for h in rolling.horizons_years:
        pre, after = rolling.worst(h, "pre_tax"), rolling.worst(h, "after_tax")
        out.append(line(f"{h}Y", pct(pre.pre_tax_total_return) if pre else "n/a",
                        pct(after.after_tax_total_return) if after else "n/a"))
    return out


def period(row: dict) -> str:
    return f"{row.get('effective_first_week')} .. {row.get('effective_last_week')}"


# ============================================================================ commands
def run_table(row: dict, rolling, output_dir) -> str:
    out = ["Backtest result", RULE,
           info("Period", period(row)), info("Weeks", row.get("weeks")),
           info("Tax profile", row.get("tax_profile")), ""]
    out += metric_table(row) + [""] + costs_block(row) + [""] + rolling_block(rolling)
    return "\n".join(out + ["", f"Results: {output_dir}"]) + "\n"


def tax_compare_table(rows, profiles, output_dir) -> str:
    """One row per profile in the canonical profile order; no ranking, no winner."""
    first = rows[0] if rows else {}
    widths = (22, 18, 18, 9, 15, 16)
    head = ("Profile", "Pre-tax wealth", "After-tax wealth", "CAGR", "After-tax CAGR", "Tax paid")
    fmt = lambda cells: cells[0].ljust(widths[0]) + "".join(   # noqa: E731
        c.rjust(w) for c, w in zip(cells[1:], widths[1:]))
    out = ["Tax compare", "-" * sum(widths),
           info("Period", period(first)), info("Weeks", first.get("weeks")), "", fmt(head),
           "-" * sum(widths)]
    for p, r in zip(profiles, rows):
        out.append(fmt((p, pln(r.get("final_wealth_pre_tax")), pln(r.get("after_tax_terminal_wealth")),
                        pct(r.get("cagr")), pct(r.get("after_tax_cagr")), pln(r.get("total_tax_paid")))))
    out += ["", "Rolling windows per profile: profiles/<profile>/rolling_metrics.csv"]
    return "\n".join(out + ["", f"Results: {output_dir}"]) + "\n"


def optimize_table(selected: dict, rolling, output_dir, evaluated: Optional[int] = None) -> str:
    weights = ", ".join(f"{s} {pct(selected.get(f'weight_{s}'))}" for s in ("stocks", "gold", "btc", "rf"))
    out = ["Optimize result (in-sample, selected candidate)", RULE,
           info("Period", period(selected)), info("Weeks", selected.get("weeks")),
           info("Tax profile", selected.get("tax_profile")),
           info("Evaluated candidates", evaluated if evaluated is not None else "n/a"),
           info("Selected grid index", selected.get("grid_index")),
           info("Selected weights", weights),
           info("Objective", f"{selected.get('objective')} ({selected.get('objective_direction')})"),
           info("Objective value", objective_value(selected)), ""]
    out += metric_table(selected, ("final_wealth", "cagr", "max_drawdown")) + [""] + rolling_block(rolling)
    return "\n".join(out + ["", f"Results: {output_dir}"]) + "\n"


def objective_value(row: dict) -> str:
    metric = str(row.get("objective_metric") or "")
    if "wealth" in metric:
        return pln(row.get("objective_value"))
    if metric.endswith(("sharpe", "sortino", "calmar")):
        return ratio(row.get("objective_value"))
    return pct(row.get("objective_value"))


def walk_forward_table(row: dict, windows: int, rolling, output_dir) -> str:
    out = ["Walk-forward result (stitched out-of-sample path)", RULE,
           info("OOS period", period(row)), info("OOS weeks", row.get("weeks")),
           info("OOS windows", windows), info("Tax profile", row.get("tax_profile")), ""]
    out += metric_table(row, ("final_wealth", "cagr", "max_drawdown")) + [""] + rolling_block(rolling)
    return "\n".join(out + ["", f"Results: {output_dir}"]) + "\n"


def scan_table(scan_type: str, rows, output_dir) -> str:
    """The already written grid_results.csv rows, grid order (no ranking)."""
    widths = (6, 26, 18, 10, 14, 16)
    head = ("Point", "Parameter", "Final wealth", "CAGR", "Max drawdown", "After-tax CAGR")
    fmt = lambda cells: cells[0].ljust(widths[0]) + cells[1].ljust(widths[1]) + "".join(  # noqa: E731
        c.rjust(w) for c, w in zip(cells[2:], widths[2:]))
    out = [f"{scan_type} result", "-" * sum(widths), fmt(head), "-" * sum(widths)]
    for r in rows:
        name = str(r.get("scanned_parameter") or "").rsplit(".", 1)[-1]
        out.append(fmt((str(r.get("grid_index")), f"{name}={r.get('scanned_value')}",
                        pln(r.get("final_wealth_pre_tax")), pct(r.get("cagr")),
                        pct(r.get("max_drawdown")), pct(r.get("after_tax_cagr")))))
    return "\n".join(out + ["", f"Results: {output_dir}"]) + "\n"


def render(cfg, result) -> Optional[str]:
    """The console table of a finished command (None: no table, e.g. ``signals``)."""
    cmd = cfg.command
    if cmd == "run":
        return run_table(summary_row(cfg, result), result.rolling, result.output_dir)
    if cmd == "tax-compare":
        return tax_compare_table(result.rows, result.profiles, result.output_dir)
    if cmd == "optimize" and cfg.get("optimizer.mode") == "walk-forward":
        return walk_forward_table(summary_row(result.spec.base, result.portfolio), len(result.windows),
                                  result.portfolio.rolling, result.output_dir)
    if cmd == "optimize":
        return optimize_table(result.selected, result.selected_result.rolling, result.output_dir,
                              result.counts().get("evaluated_points"))
    if cmd in ("delay-scan", "threshold-scan", "rebalance-scan"):
        return scan_table(result.scan_type, result.rows, result.output_dir)
    return None
