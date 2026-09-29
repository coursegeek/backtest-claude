#!/usr/bin/env python3
"""Backtest V2 clean-room reference implementation (specification 3.1).

CLI:    python work/backtest.py <command> [options]
Import: from backtest import resolve_config, run_signals   (with work/ on sys.path)
"""
from __future__ import annotations

import sys
from pathlib import Path

_WORK = str(Path(__file__).resolve().parent)
if _WORK not in sys.path:
    sys.path.insert(0, _WORK)

from src.app import run_signals, dispatch          # noqa: E402
from src.cli import main, resolve as _resolve      # noqa: E402
from src.config import ResolvedConfig              # noqa: E402

__all__ = ["main", "resolve_config", "run_signals", "dispatch", "ResolvedConfig"]


def resolve_config(argv) -> ResolvedConfig:
    """Resolve CLI-style arguments (CLI > config file > defaults) without running."""
    return _resolve(list(argv))


if __name__ == "__main__":
    sys.exit(main())
