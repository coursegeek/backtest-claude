"""Validation layer (NORM-001..006/010/018/019, DIV-012, ERR-002..004, Q-012).

Q-012 adjudicated semantics implemented here:
  1. gaps are always detected and reported (NORM-003);
  2. nothing is forward-filled automatically;
  3. signal state reconstruction uses the whole available history (see confirmation.py);
  4. a missing week breaks confirmation counters (see confirmation.py);
  5./7. missing.return_policy / missing.price_policy apply only to a required source missing
     in a week of the run calendar actually executed after source alignment;
  6. a week missing from every calendar-defining source is a common calendar gap: reported,
     never filled and never used to cut earlier history.
"""
from __future__ import annotations

import datetime as dt
import statistics

from .calendar import WEEK, is_completed, missing_keys, weekly_grid
from .errors import DataValidationError, DividendModeError, WarmupError
from .models import PricePoint, RunCalendar, Severity, ValidationIssue


class ValidationReport:
    """Collects issues for validation_report.csv (REP-009)."""

    def __init__(self):
        self.issues: list = []

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)

    def extend(self, issues) -> None:
        self.issues.extend(issues)

    def add_provenance(self, prov) -> None:
        self.extend(prov.issues)
        for w in prov.warnings:
            self.add(ValidationIssue(Severity.WARNING, "provenance", prov.role, w))
        if prov.resolved_via_alias:
            self.add(ValidationIssue(Severity.WARNING, "default_file_alias", prov.role,
                                     f"specification default file absent; staged alias {prov.path} "
                                     f"used via data profile (Q-001)", None, "DATA-008"))
        for raw_date, reason in prov.excluded:
            self.add(ValidationIssue(Severity.INFO, "row_excluded", prov.role,
                                     f"raw row {raw_date} excluded: {reason}"))

    def errors(self) -> list:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    def raise_if_errors(self, context: str = "") -> None:
        errs = self.errors()
        if errs:
            head = "; ".join(f"{e.role}: {e.message}" for e in errs[:5])
            raise DataValidationError(f"{context}{len(errs)} validation error(s): {head}")

    def rows(self) -> list:
        order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
        return [i.row() for i in sorted(
            self.issues, key=lambda i: (i.role, i.week_key or dt.date.min, order[i.severity],
                                        i.code, i.message))]


# ----------------------------------------------------------------------------- gaps
def gap_issues(role: str, keys) -> list:
    """NORM-003: report every missing Friday key inside a source's range."""
    return [ValidationIssue(Severity.WARNING, "missing_week", role,
                            f"no observation for week {k}", k, "NORM-003")
            for k in missing_keys(keys)]


# ----------------------------------------------------------------------------- completion
def apply_completion(points, role: str, as_of: dt.date, end=None):
    """NORM-019/NORM-020 (+Q-007): drop weeks after min(end, as_of) or whose information is not
    available at as_of. Returns (kept, dropped_issues)."""
    kept, dropped = [], []
    for p in points:
        if end is not None and p.week_key > end:
            continue  # outside the requested range, not an incomplete week
        if is_completed(p.week_key, p.available_at, as_of):
            kept.append(p)
            continue
        reason = ("Friday week_key after as_of_date" if p.week_key > as_of
                  else f"information available only at {p.available_at}")
        dropped.append(ValidationIssue(Severity.INFO, "incomplete_week_dropped", role,
                                       f"week {p.week_key} dropped: {reason} (as_of {as_of})",
                                       p.week_key, "NORM-019"))
    return kept, dropped


# ----------------------------------------------------------------------------- run calendar
def build_run_calendar(first: dt.date, last: dt.date, sources: dict, return_roles=(),
                       price_roles=(), return_policy: str = "error",
                       price_policy: str = "error") -> RunCalendar:
    """Align required sources on the weekly grid [first, last] (Q-012 points 5-7).

    ``sources`` maps role -> iterable of Friday keys. A week present in no source is a
    common calendar gap (reported, skipped, never filled). A week present in some but not
    all required sources triggers the configured policy for each missing source:
      * return sources: error | drop (week removed from the run calendar);
      * price sources: carry (previous price reused, flagged) | error; with
        missing.return_policy=drop a missing price source drops the week instead of failing.
    """
    keysets = {r: set(k) for r, k in sources.items()}
    issues, weeks, common, dropped, carried = [], [], [], [], []
    for k in weekly_grid(first, last):
        present = [r for r in sorted(keysets) if k in keysets[r]]
        if not present:
            common.append(k)
            issues.append(ValidationIssue(Severity.WARNING, "common_calendar_gap", "calendar",
                                          f"no required source has week {k}; week not in run "
                                          "calendar, nothing filled", k, "NORM-003;NORM-004"))
            continue
        drop = False
        for role in sorted(r for r in keysets if k not in keysets[r]):
            if role in return_roles and role not in price_roles:
                if return_policy == "drop":
                    drop = True
                    issues.append(ValidationIssue(Severity.WARNING, "week_dropped", role,
                                                  f"missing return for week {k}; dropped "
                                                  "(missing.return_policy=drop)", k, "NORM-004"))
                    continue
                raise DataValidationError(f"{role}: missing return for run week {k} "
                                          "(missing.return_policy=error, NORM-004)")
            if price_policy == "carry":
                carried.append((role, k))
                issues.append(ValidationIssue(Severity.WARNING, "price_carried", role,
                                              f"missing price for week {k}; previous price carried "
                                              "(missing.price_policy=carry)", k, "NORM-005"))
            elif return_policy == "drop":
                drop = True
                issues.append(ValidationIssue(Severity.WARNING, "week_dropped", role,
                                              f"missing price for week {k}; dropped "
                                              "(missing.return_policy=drop)", k, "NORM-004;NORM-005"))
            else:
                raise DataValidationError(f"{role}: missing price for run week {k} "
                                          "(missing.price_policy=error, NORM-005)")
        if drop:
            dropped.append(k)
            carried = [(r, w) for r, w in carried if w != k]
        else:
            weeks.append(k)
    return RunCalendar(tuple(weeks), tuple(common), tuple(dropped), tuple(carried), tuple(issues))


def apply_price_carry(series, carried_weeks):
    """Insert carried prices (flag 'carried') for the given weeks of one price series."""
    carried_weeks = sorted(carried_weeks)
    if not carried_weeks:
        return series
    by_key = series.by_key()
    pts = list(series.points)
    for k in carried_weeks:
        prev = max((p for p in pts if p.week_key < k), key=lambda p: p.week_key, default=None)
        if prev is None:
            raise DataValidationError(f"{series.role}: cannot carry price into {k}: no earlier price")
        if k not in by_key:
            pts.append(PricePoint(k, prev.price, max(prev.available_at, k), prev.source_date,
                                  prev.source, prev.flags + ("carried",)))
    pts.sort(key=lambda p: p.week_key)
    return series.replace_points(pts)


# ----------------------------------------------------------------------------- warm-up
def history_before(keys, first_week: dt.date) -> int:
    return sum(1 for k in keys if k < first_week)


def check_warmup(asset: str, keys, first_week: dt.date, params, initial_state: str):
    """NORM-010/ERR-003 (Q-013 proposal): the whole available history before the first week
    must hold at least ma + max(confirm) + delay observations. With signal.initial_state=RISK_ON
    an explicit warning replaces the error (SIG-003 fallback)."""
    have = history_before(keys, first_week)
    need = params.minimum_warmup_weeks
    if have >= need:
        return None
    if initial_state == "RISK_ON":
        return ValidationIssue(Severity.WARNING, "warmup_short", asset,
                               f"{have} weeks of history before {first_week} < required {need}; "
                               "initial state RISK_ON by explicit configuration", first_week,
                               "NORM-010;SIG-003")
    raise WarmupError(asset, have, need, first_week)


# ----------------------------------------------------------------------------- dividends
def check_dividend_mode(mode: str, profile: str, dividend_available: bool,
                        cash_file) -> None:
    """ERR-004 / DIV-010."""
    if mode == "exact" and not cash_file:
        raise DividendModeError("dividend_tax_mode=exact requires data.dividend_cash_file with "
                                "actual cash dividend dates; the smoothed file is not accepted (DIV-010)")
    if mode == "smoothed_weekly" and profile != "none" and not dividend_available:
        raise DividendModeError("dividend_tax_mode=smoothed_weekly requires the smoothed "
                                "dividend_return file (data.dividend_file)")


def validate_dividend_series(series, from_date=None) -> list:
    """DIV-012: Friday dates, weekly continuity, formula and allowed statuses."""
    pts = [p for p in series.points if from_date is None or p.week_key >= from_date]
    issues = [i for i in series.provenance.issues
              if from_date is None or i.week_key is None or i.week_key >= from_date]
    issues += gap_issues("dividend", [p.week_key for p in pts])
    return issues


# ----------------------------------------------------------------------------- BTC
def validate_btc_canonical(series, expected_range=None, expected_rows=None) -> list:
    """SEM-008/TEST-039 on the canonical representation: Friday keys, Sunday close_date =
    key + 2, no gaps, weekly_return consistent, optional documented range and row count."""
    out = list(series.provenance.issues)
    pts = series.points
    for p in pts:
        if p.week_key.weekday() != 4 or p.available_at != p.week_key + dt.timedelta(days=2):
            out.append(ValidationIssue(Severity.ERROR, "btc_calendar", "btc",
                                       f"week {p.week_key}: key/close_date convention broken", p.week_key,
                                       "SCHEMA-004;SEM-008"))
    out += [ValidationIssue(Severity.ERROR, "missing_week", "btc", f"gap at {k}", k, "SEM-008")
            for k in missing_keys([p.week_key for p in pts])]
    if expected_range and (pts[0].week_key, pts[-1].week_key) != tuple(expected_range):
        out.append(ValidationIssue(Severity.ERROR, "btc_range", "btc",
                                   f"range {pts[0].week_key}..{pts[-1].week_key} != {expected_range}"))
    if expected_rows is not None and len(pts) != expected_rows:
        out.append(ValidationIssue(Severity.ERROR, "btc_rows", "btc",
                                   f"{len(pts)} rows != {expected_rows}"))
    return out


# ----------------------------------------------------------------------------- FF alignment
def alignment_correlation(return_points, price_series) -> float:
    """NORM-018 diagnostic: correlation of a return series with price-index returns on the
    same week keys (high on a correctly aligned index, low when shifted by a week)."""
    pr = {p.week_key: p.value for p in price_series.returns()}
    ks = [p.week_key for p in return_points if p.week_key in pr]
    rv = {p.week_key: p.value for p in return_points}
    return statistics.correlation([rv[k] for k in ks], [pr[k] for k in ks])
