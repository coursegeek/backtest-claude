"""TEST-017, TEST-050."""
import pytest

from fixtures.builders import STAGED, write_csv
from src.cli import build_parser
from src.config import ResolvedConfig
from src.data_loader import load_dividend, load_dividend_cash
from src.errors import ConfigError, DividendModeError
from src.validation import check_dividend_mode


def test_exact_requires_cash_file_smoothed_accepts_file(tmp_path):
    """TEST-017 / DIV-010, ERR-004: exact without a cash-date file fails; the smoothed file is
    never accepted as exact; smoothed_weekly accepts a dividend_return file."""
    with pytest.raises(DividendModeError):
        check_dividend_mode("exact", "individual_pl", True, None)
    with pytest.raises(DividendModeError):
        load_dividend_cash(STAGED / "SPX_dividend_return_weekly_shiller.csv")
    with pytest.raises(DividendModeError):
        check_dividend_mode("smoothed_weekly", "individual_pl", False, None)
    check_dividend_mode("smoothed_weekly", "individual_pl", True, None)
    check_dividend_mode("smoothed_weekly", "none", False, None)
    cash = load_dividend_cash(write_csv(tmp_path / "cash.csv", ["pay_date", "dividend_return"],
                                        [["2020-03-12", "0.004"]]))
    assert cash.points[0].week_key.isoformat() == "2020-03-13"
    check_dividend_mode("exact", "individual_pl", False, str(tmp_path / "cash.csv"))
    smoothed = load_dividend(STAGED / "SPX_dividend_return_weekly_shiller.csv", adapter="shiller_proxy")
    assert len(smoothed.points) == 5218


def test_dividend_reinvest_only():
    """TEST-050 / DIV-006: reinvest is the only mode; cash is rejected and no CLI option exists."""
    assert ResolvedConfig().get("tax.dividend_reinvest") == "reinvest"
    with pytest.raises(ConfigError, match="DIV-006"):
        ResolvedConfig(file_layer={"tax": {"dividend_reinvest": "cash"}})
    opts = {o for a in build_parser()._subparsers._group_actions[0].choices["run"]._actions
            for o in a.option_strings}
    assert not any("reinvest" in o for o in opts)
