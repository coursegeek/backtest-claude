"""Backtest V2 clean-room reference implementation (specification 3.1).

Import modules as ``src.<module>`` with ``work/`` on ``sys.path``; ``work/src`` itself must
never be on ``sys.path`` because ``src/calendar.py`` would shadow the standard library.
"""
SPEC_VERSION = "3.1"
APP_NAME = "backtest.py"
