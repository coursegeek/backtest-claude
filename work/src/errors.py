"""Exception hierarchy. Every error carries a stable CLI exit code and names the config key
or requirement that produced it (ERR-001..ERR-004)."""
from __future__ import annotations


class BacktestError(Exception):
    exit_code = 1


class ConfigError(BacktestError):
    exit_code = 2


class DataFileNotFound(BacktestError):
    """ERR-001: message contains the path and the config key."""

    def __init__(self, path, config_key: str):
        self.path, self.config_key = str(path), config_key
        super().__init__(f"data file not found: {self.path} (config key: {config_key})")


class MissingColumns(BacktestError):
    """ERR-002."""

    def __init__(self, path, missing, expected, config_key: str = "", hint: str = ""):
        self.path, self.missing, self.expected = str(path), list(missing), list(expected)
        msg = (f"missing columns {self.missing} in {self.path}"
               + (f" (config key: {config_key})" if config_key else "")
               + f"; expected {self.expected}")
        super().__init__(msg + (f"; {hint}" if hint else ""))


class DataValidationError(BacktestError):
    pass


class WarmupError(BacktestError):
    """ERR-003."""

    def __init__(self, asset: str, available: int, required: int, first_week, note: str = ""):
        self.asset, self.available, self.required = asset, available, required
        self.first_week, self.note = first_week, note
        super().__init__(
            f"ERR-003 insufficient warm-up for {asset}: available {available} < required "
            f"{required} weekly observations before the first run week {first_week} (ma + "
            f"max(confirm_off, confirm_on) + delay); choose a later --start or set "
            f"signal.initial_state=RISK_ON explicitly" + (f"; {note}" if note else ""))


class DividendModeError(BacktestError):
    """ERR-004."""


class LookAheadError(BacktestError):
    """Raised when code asks for information not yet available (META-003)."""


class InsufficientHistoryError(BacktestError):
    pass


class NotImplementedCommand(BacktestError):
    exit_code = 3


class InsolvencyError(BacktestError):
    """TAX-006 (D): the net liquidation value cannot cover the amounts due."""

    def __init__(self, week, due: float, available: float):
        self.week, self.due, self.available = week, due, available
        super().__init__(f"insolvency in week {week}: amounts due {due!r} exceed the net "
                         f"liquidation value {available!r} of the portfolio (TAX-006)")
