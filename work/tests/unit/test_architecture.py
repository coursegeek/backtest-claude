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
