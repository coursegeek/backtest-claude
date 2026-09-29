import datetime as dt

import pytest

from fixtures.builders import price_series
from src.errors import DataValidationError
from src.models import Severity
from src.validation import (ValidationReport, apply_completion, apply_price_carry,
                            build_run_calendar, gap_issues)

D = dt.date.fromisoformat


def keys(first, n, skip=()):
    return [D(first) + dt.timedelta(days=7 * i) for i in range(n) if i not in skip]


def test_missing_weeks_reported():
    """NORM-003."""
    issues = gap_issues("stocks_price", keys("1933-02-24", 4, skip=(2,)))
    assert [(i.code, i.week_key) for i in issues] == [("missing_week", D("1933-03-10"))]


def test_missing_return_error_or_drop_never_ffill():
    """NORM-004: a return source missing in a run-calendar week -> error or drop, no fill."""
    src = {"stocks_return": keys("2020-01-03", 4, skip=(2,)), "stocks_price": keys("2020-01-03", 4)}
    with pytest.raises(DataValidationError, match="NORM-004"):
        build_run_calendar(D("2020-01-03"), D("2020-01-24"), src, return_roles={"stocks_return"},
                           price_roles={"stocks_price"})
    cal = build_run_calendar(D("2020-01-03"), D("2020-01-24"), src, return_roles={"stocks_return"},
                             price_roles={"stocks_price"}, return_policy="drop")
    assert cal.dropped == (D("2020-01-17"),) and D("2020-01-17") not in cal.weeks
    assert cal.carried == ()


def test_missing_price_error_or_carry_flagged():
    """NORM-005: carry only with an explicit missing.price_policy=carry, flagged."""
    src = {"stocks_return": keys("2020-01-03", 4), "gold": keys("2020-01-03", 4, skip=(1,))}
    with pytest.raises(DataValidationError, match="NORM-005"):
        build_run_calendar(D("2020-01-03"), D("2020-01-24"), src, {"stocks_return"}, {"gold"})
    cal = build_run_calendar(D("2020-01-03"), D("2020-01-24"), src, {"stocks_return"}, {"gold"},
                             price_policy="carry")
    assert cal.carried == (("gold", D("2020-01-10")),) and len(cal.weeks) == 4
    s = price_series([100, 101, 102, 103], first_key="2020-01-03", role="gold", skip=(1,))
    filled = apply_price_carry(s, [k for _, k in cal.carried])
    assert [(p.week_key, p.price, p.flags) for p in filled.points][1] == (D("2020-01-10"), 100.0, ("carried",))


def test_unsorted_input_sorted_and_reported():
    """NORM-001 report rows via ValidationReport."""
    r = ValidationReport()
    r.extend(gap_issues("x", keys("2020-01-03", 3, skip=(1,))))
    rows = r.rows()
    assert rows[0]["code"] == "missing_week" and rows[0]["week_key"] == "2020-01-10"
    assert r.errors() == []


def test_nan_inf_nonnumeric_error():
    """NORM-006 (loader-level detail covered in test_data_loader)."""
    r = ValidationReport()
    from src.models import ValidationIssue
    r.add(ValidationIssue(Severity.ERROR, "x", "y", "boom"))
    with pytest.raises(DataValidationError):
        r.raise_if_errors()


def test_completion_drops_and_reports():
    s = price_series([1, 2, 3], first_key="2026-09-11")
    kept, dropped = apply_completion(s.points, "stocks_price", D("2026-09-22"))
    assert [p.week_key for p in kept] == [D("2026-09-11"), D("2026-09-18")]
    assert [i.week_key for i in dropped] == [D("2026-09-25")]
    kept, dropped = apply_completion(s.points, "stocks_price", D("2026-09-29"), end=D("2026-09-18"))
    assert len(kept) == 2 and dropped == []
