"""Module boundaries (ARCH-*, SEM-004, Q-044)."""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def imports(module):
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            out.add(node.module or "")
    return out


def test_src_is_a_package_not_on_sys_path():
    assert (SRC / "__init__.py").is_file()
    assert str(SRC) not in sys.path
    import calendar
    assert hasattr(calendar, "monthrange")           # stdlib, not src/calendar.py


def test_cpi_not_used_by_signals():
    """SEM-004: signal modules never import data loading or CPI."""
    for mod in ("signals", "confirmation", "scheduling", "signal_analysis"):
        text = (SRC / f"{mod}.py").read_text(encoding="utf-8").lower()
        assert "cpi" not in text and "data_loader" not in imports(mod)


def test_layering():
    assert imports("signals") <= {"models"}
    assert imports("calendar") == set()
    assert "engine" not in imports("validation") and "cli" not in imports("data_loader")


def test_rebalancing_and_funding_are_separate_layers():
    """Rebalancing and sell_to_pay extend the central engine through its hooks and primitives:
    the engine does not know them, and neither touches loading, reporting or the CLI."""
    assert not imports("engine") & {"rebalancing", "sell_to_pay", "app", "reporting"}
    for mod in ("rebalancing", "sell_to_pay"):
        assert not imports(mod) & {"data_loader", "reporting", "app", "cli", "validation"}
    assert "rebalancing" not in imports("sell_to_pay")


def test_tax_module_boundaries():
    """TAX-001: taxes are a separate module plugged into the pipeline; the ledger, engine,
    rebalancing and sell_to_pay know nothing about tax rules."""
    assert imports("tax") <= {"engine", "errors", "models", "rf"}
    for mod in ("engine", "ledger", "rebalancing", "sell_to_pay", "cost_basis", "costs"):
        assert "tax" not in imports(mod), mod
    text = (SRC / "engine.py").read_text(encoding="utf-8")
    for word in ("0.19", "solidarity", "capital_gains", "loss_bucket"):
        assert word not in text


def test_settlement_is_a_separate_layer():
    """PORT-014, IND-017: terminal settlement is a layer after the engine; the weekly engine,
    the tax hooks and reporting of the weekly path do not depend on it."""
    assert imports("settlement") <= {"engine", "errors", "ledger", "models", "rf", "tax",
                                     "foundation", "sell_to_pay"}
    for mod in ("engine", "tax", "foundation", "rebalancing", "sell_to_pay", "ledger"):
        assert "settlement" not in imports(mod), mod


def test_foundation_module_boundaries():
    """TAX-001: foundation rules live in src/foundation.py; it shares only the step-5 primitive
    and the TaxEvent record with the individual module (no loss buckets / CG)."""
    assert imports("foundation") <= {"engine", "errors", "tax"}
    text = (SRC / "foundation.py").read_text(encoding="utf-8")
    assert "LossBucket" not in text and "close_tax_year" not in text
    for mod in ("engine", "tax", "rebalancing", "sell_to_pay", "ledger"):
        assert "foundation" not in imports(mod), mod


def test_tax_compare_is_an_orchestrator():
    """TAX-003 (Q-023): tax-compare orchestrates the production run pipeline (app.prepare_run /
    app.run_prepared); it has no engine, tax, foundation, settlement, metrics or data loading
    of its own, and nothing but the command dispatcher depends on it."""
    assert imports("tax_compare") <= {"allocation", "app", "calendar", "config", "errors",
                                      "manifest", "reporting", "validation"}
    text = (SRC / "tax_compare.py").read_text(encoding="utf-8")
    for word in ("run_engine", "load_role", "settle_terminal", "compute_run_metrics"):
        assert word not in text, word
    for mod in ("engine", "tax", "foundation", "settlement", "metrics", "reporting", "rebalancing",
                "sell_to_pay", "data_loader", "config", "cli"):
        assert "tax_compare" not in imports(mod), mod


def test_scans_are_an_orchestrator():
    """DELAY-001/THR-001/REB-010: scans orchestrate app.prepare_run / app.run_prepared; no
    engine, data loading, tax or metrics of their own and no optimizer logic."""
    assert imports("scans") <= {"allocation", "app", "config", "errors", "manifest", "models",
                                "reporting", "validation"}
    text = (SRC / "scans.py").read_text(encoding="utf-8")
    for word in ("run_engine", "load_role", "compute_run_metrics", "settle_terminal", "tie_break",
                 "sorted(res.rows", "objective ="):
        assert word not in text, word
    for mod in ("engine", "tax", "foundation", "settlement", "metrics", "reporting", "app",
                "config", "cli", "tax_compare"):
        assert "scans" not in imports(mod) or mod == "app", mod
