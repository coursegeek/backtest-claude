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
    assert imports("settlement") <= {"engine", "errors", "ledger", "models", "rf", "tax"}
    for mod in ("engine", "tax", "rebalancing", "sell_to_pay", "ledger"):
        assert "settlement" not in imports(mod), mod
