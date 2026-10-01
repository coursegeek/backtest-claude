"""FND-005 / FND-009 / Q-037: tax.foundation.tax_event=distribution_schedule on the central
engine with exact accounting: gross distributions D leave the NAV once (tax T withheld from D,
net D - T to the beneficiary), percent_nav on NAV_after_signal, the cumulative gain_only
capital basis, several rows in one week, admin cost and strategic rebalance in the same week,
the TAX-006 waterfall with foundation_distribution_liquidation sales, removed nominal weeks,
rows outside the run, the schedule-mode end settlement and the pre-tax shadow."""
import dataclasses
import datetime as dt
import itertools
import math
import random

import pytest

from fixtures.builders import engine_inputs, params_for
from src.costs import CostModel
from src.engine import ComposedHooks, run_engine
from src.errors import ConfigError
from src.foundation import (ADMIN_COST, DISTRIBUTION_GROSS, DISTRIBUTION_NET, DISTRIBUTION_TAX, SCHEDULE,
                            FoundationHooks, FoundationParams, distribution_tax_base,
                            map_distribution_schedule)
from src.models import ScheduledDistribution, TradeReason
from src.rebalancing import StrategicHooks
from src.settlement import settle_foundation, settle_foundation_shadow_costs

D = dt.date.fromisoformat
WEEK = dt.timedelta(days=7)
QUIET = {a: params_for(a, threshold_off=0.9, threshold_on=0.9) for a in ("stocks", "gold")}


def flat_inputs(n=70, targets=None, skip=(), costs=(10.0, 5.0), capital=1_000_000.0):
    """Constant prices and zero returns from 2000-01-07 (run from 2000-02-04, index 4): every
    NAV change is a cost, tax or distribution outflow."""
    return engine_inputs({"stocks": [100.0] * n, "gold": [100.0] * n}, first=4,
                         targets=targets or {"stocks": 0.5, "gold": 0.3, "rf": 0.2},
                         params=QUIET, costs=CostModel(*costs), capital=capital) if not skip else \
        _skipped(n, targets, skip, costs)


def _skipped(n, targets, skip, costs):
    from fixtures.builders import price_series
    from src.engine import EngineInputs, WeekMarket
    series = {a: price_series([100.0] * n, role=a, skip=skip) for a in ("stocks", "gold")}
    weeks = series["stocks"].keys()[4:]
    market = {w: WeekMarket(w, {"stocks": 0.0, "gold": 0.0}, 0.0) for w in weeks}
    full = {"stocks": 0.0, "gold": 0.0, "btc": 0.0, "rf": 0.0}
    full.update(targets or {"stocks": 0.5, "gold": 0.3, "rf": 0.2})
    return EngineInputs(weeks=tuple(weeks), market=market, signal_series=series, params=QUIET,
                        targets=full, initial_capital=1_000_000.0, costs=CostModel(*costs))


def rows(*specs):
    """specs: (date, amount) or (date, None, percent)."""
    out = []
    for i, s in enumerate(specs, start=1):
        out.append(ScheduledDistribution(i, D(s[0]), s[1], s[2] if len(s) > 2 else None))
    return tuple(out)


def run_schedule(inp, sched, profile="family_foundation_15", mode="signal-only", band=None,
                 base="distributed_amount", zero=False, **kw):
    rate = 0.15 if profile.endswith("15") else 0.19
    params = FoundationParams(profile, distribution_rate=rate, tax_event=SCHEDULE,
                              distribution_file="schedule.csv", distribution_tax_base=base, **kw)
    if zero:
        params = params.zero_rates()
    by_week, ignored = map_distribution_schedule(sched, inp.weeks)
    fh = FoundationHooks(params, (inp.run_start or inp.weeks[0]) - WEEK, distributions=by_week)
    res = run_engine(inp, ComposedHooks(StrategicHooks(mode, band), fh))
    return res, fh, ignored


def week(res, day):
    return next(w for w in res.weeks if w.week_key == D(day))


def paid(w, event_type):
    return math.fsum(p.amount for p in w.payments if p.event_type == event_type)


# ------------------------------------------------------------------ gross semantics
def test_amount_row_gross_semantics_and_accounting_identity():
    """Q-037 points 14/17/19/20: an amount row D = 100 000 is the gross NAV outflow; the 15 %
    distribution tax T = 15 000 is withheld from D and the beneficiary gets D - T = 85 000 -
    the NAV falls by D, never by D + T. The tax is a TaxEvent (settlement scheduled, phase
    weekly); the net payout is a Payment (no tax, no cost, no trade)."""
    inp = flat_inputs()
    res, fh, ignored = run_schedule(inp, rows(("2000-03-08", 100_000.0)))
    assert ignored == ()
    w = week(res, "2000-03-10")                       # Wednesday -> Friday of the same week
    # one gross funding item (the ledger sees D whatever the tax is); the Payment records split
    # it into the withheld tax and the net payout
    assert w.amounts_due.total == 100_000.0 and w.amounts_due.items == ((DISTRIBUTION_GROSS, 100_000.0),)
    assert not [p for p in w.payments if p.event_type == DISTRIBUTION_GROSS]
    assert w.nav_after_signal - w.nav_before_returns == pytest.approx(100_000.0, abs=1e-6)
    assert paid(w, DISTRIBUTION_TAX) == 15_000.0 and paid(w, DISTRIBUTION_NET) == 85_000.0
    assert w.trades == ()                             # rf_base (192 000) funds it (TAX-006 A)
    st = fh.state
    assert (st.gross_distributions_paid, st.net_distributions_paid, st.distribution_tax_paid) == (
        100_000.0, 85_000.0, 15_000.0)
    ev = [e for e in st.tax_events if e.event_type == DISTRIBUTION_TAX]
    assert [(e.category, e.settlement, e.phase, e.pipeline_step, e.gross_base, e.taxable_base,
             e.amount) for e in ev] == [("tax", "scheduled", "weekly", 2, 100_000.0, 100_000.0, 15_000.0)]
    d = st.distribution_events[0]
    assert (d.scheduled_date, d.nominal_week, d.actual_week, d.paid_week, d.kind, d.gross, d.tax,
            d.net) == (D("2000-03-08"), D("2000-03-10"), D("2000-03-10"), D("2000-03-10"),
                       "amount", 100_000.0, 15_000.0, 85_000.0)
    assert not [e for e in st.tax_events if e.event_type == DISTRIBUTION_NET]


def test_percent_nav_row_uses_nav_after_signal():
    """Q-037 point 15: percent_nav p -> D = p * NAV_after_signal of step 2 (after step-1 signal
    trades, before amounts due, rebalance and the week's returns)."""
    inp = flat_inputs()
    res, fh, _ = run_schedule(inp, rows(("2000-03-10", None, 0.1)))
    w = week(res, "2000-03-10")
    d = fh.state.distribution_events[0]
    assert d.kind == "percent_nav" and d.nav_base == w.nav_after_signal
    assert d.gross == pytest.approx(0.1 * w.nav_after_signal) and d.tax == pytest.approx(0.15 * d.gross)
    assert w.nav_after_signal - w.nav_before_returns == pytest.approx(d.gross)


def test_15_vs_19_same_weekly_nav_different_tax():
    """Q-037 point 34: the same gross D leaves both foundations: identical weekly NAV; only
    the distribution tax and the net payout differ."""
    inp = flat_inputs()
    sched = rows(("2000-03-10", 100_000.0), ("2000-06-09", None, 0.05))
    a, fa, _ = run_schedule(inp, sched, "family_foundation_15")
    b, fb, _ = run_schedule(inp, sched, "family_foundation_19")
    assert [w.ledger_end for w in a.weeks] == [w.ledger_end for w in b.weeks]
    assert fa.state.gross_distributions_paid == fb.state.gross_distributions_paid
    assert fa.state.distribution_tax_paid == pytest.approx(0.15 / 0.19 * fb.state.distribution_tax_paid)
    assert fa.state.net_distributions_paid > fb.state.net_distributions_paid


def test_distributed_amount_and_gain_only_bases():
    """FND-006 / Q-037 points 21-23: distributed_amount taxes each gross D; gain_only taxes only
    the cumulative excess over the initial capital (1 000 000 before the setup cost):
    600 000 + 600 000 -> bases 0 and 200 000 (= one distribution of 1 200 000)."""
    inp = flat_inputs(targets={"stocks": 0.4, "rf": 0.6}, costs=(0.0, 0.0))
    sched = rows(("2000-03-10", 300_000.0), ("2000-04-07", 300_000.0), ("2000-05-05", 200_000.0))
    _, fh, _ = run_schedule(inp, sched)
    assert [e.tax_base for e in fh.state.distribution_events] == [300_000.0, 300_000.0, 200_000.0]
    assert fh.state.distribution_tax_paid == pytest.approx(0.15 * 800_000.0)
    _, fh, _ = run_schedule(inp, sched, base="gain_only")
    ev = fh.state.distribution_events
    assert [(e.basis_before, e.tax_base, e.basis_after) for e in ev] == [
        (1_000_000.0, 0.0, 700_000.0), (700_000.0, 0.0, 400_000.0), (400_000.0, 0.0, 200_000.0)]
    assert fh.state.distribution_tax_paid == 0.0      # cumulative 800 000 <= initial capital
    assert fh.state.distribution_capital_basis_remaining == 200_000.0
    inp = flat_inputs(n=40, targets={"rf": 1.0}, costs=(0.0, 0.0), capital=3_000_000.0)
    # initial capital 3 000 000 (no setup cost): 1.6m + 1.4m -> bases 0 and 0; one 3.0m ->
    # base 0; 1.6m + 1.6m would exceed the NAV - the cumulative rule is the property below
    _, two, _ = run_schedule(inp, rows(("2000-03-10", 1_600_000.0), ("2000-04-07", 1_400_000.0)),
                             base="gain_only", setup_cost_pln=0.0)
    assert [e.tax_base for e in two.state.distribution_events] == [0.0, 0.0]
    assert two.state.distribution_capital_basis_remaining == 0.0
    inp = flat_inputs(n=40, targets={"rf": 1.0}, costs=(0.0, 0.0), capital=1_000_000.0)
    _, one, _ = run_schedule(inp, rows(("2000-03-10", 500_000.0), ("2000-04-07", 300_000.0)),
                             base="gain_only", setup_cost_pln=0.0, annual_admin_cost_pln=0.0)
    assert [e.tax_base for e in one.state.distribution_events] == [0.0, 0.0]


def test_gain_only_cumulative_base_is_order_independent():
    """Q-037 point 23 (property): the total gain_only base of any split of the same total and
    in any order equals max(0, total - initial capital); one 1.2m distribution or 0.6m + 0.6m
    -> 0.2m; with one rate the total tax does not depend on the order."""
    def total_base(amounts, basis=1_000_000.0):
        out = []
        for a in amounts:
            b, basis = distribution_tax_base("gain_only", a, basis)
            out.append(b)
        return math.fsum(out)
    assert total_base([1_200_000.0]) == 200_000.0 == total_base([600_000.0, 600_000.0])
    rng = random.Random(37)
    for _ in range(200):
        amounts = [round(rng.uniform(1_000, 400_000), 2) for _ in range(rng.randint(1, 7))]
        expected = max(0.0, math.fsum(amounts) - 1_000_000.0)
        for perm in itertools.islice(itertools.permutations(amounts), 6):
            assert total_base(perm) == pytest.approx(expected, abs=1e-6)
    b, after = distribution_tax_base("distributed_amount", 250_000.0, 1_000_000.0)
    assert (b, after) == (250_000.0, 750_000.0)


# ------------------------------------------------------------------ several items in one week
def test_two_rows_same_week_and_admin_cost_same_week():
    """Q-037 points 16/17: two rows in one week are processed by (scheduled_date, row index),
    percent rows on the same NAV_after_signal; together with the annual admin cost of the
    closed year (first week of 2001) the amounts due are the sum of the economic outflows
    admin + D1 + D2."""
    inp = flat_inputs(n=60)
    sched = rows(("2001-01-04", None, 0.05), ("2001-01-02", 50_000.0))    # same Mon-Sun week
    res, fh, _ = run_schedule(inp, sched)
    w = week(res, "2001-01-05")
    ev = fh.state.distribution_events
    assert [(e.row_index, e.scheduled_date) for e in ev] == [(2, D("2001-01-02")), (1, D("2001-01-04"))]
    assert ev[1].gross == pytest.approx(0.05 * w.nav_after_signal) and ev[1].nav_base == w.nav_after_signal
    admin = fh.state.admin_cost_by_year[2000].amount
    assert admin > 0
    assert w.amounts_due.total == pytest.approx(admin + 50_000.0 + ev[1].gross)
    assert [t for t, _ in w.amounts_due.items] == [ADMIN_COST, DISTRIBUTION_GROSS, DISTRIBUTION_GROSS]
    assert paid(w, DISTRIBUTION_TAX) == pytest.approx(math.fsum(e.tax for e in ev))
    assert paid(w, DISTRIBUTION_NET) == pytest.approx(math.fsum(e.net for e in ev))
    assert w.nav_after_signal - w.nav_before_returns == pytest.approx(w.amounts_due.total)
    assert paid(w, DISTRIBUTION_TAX) + paid(w, DISTRIBUTION_NET) == pytest.approx(50_000.0 + ev[1].gross)


def test_distribution_with_strategic_rebalance_same_week():
    """Q-037 point 18 / TAX-007: a monthly rebalance in the distribution week targets
    NAV_after_signal - amounts_due; sales first, then the gross distribution, then purchases;
    weights after step 3 equal the targets."""
    inp = flat_inputs(targets={"stocks": 0.6, "gold": 0.4})
    res, fh, _ = run_schedule(inp, rows(("2000-03-03", 120_000.0)), mode="monthly")
    w = week(res, "2000-03-03")                       # first retained week of March
    assert w.rebalance is not None and w.rebalance.reason == TradeReason.CALENDAR_REBALANCE
    assert w.rebalance.amounts_due == 120_000.0
    assert w.rebalance.nav_net_for_rebalance == pytest.approx(w.nav_after_signal - 120_000.0)
    assert {p.context for p in w.payments} == {"strategic_rebalance"}
    for s, v in w.ledger_before_returns.sleeve_weights().items():
        assert v == pytest.approx({"stocks": 0.6, "gold": 0.4, "btc": 0.0, "rf": 0.0}[s], abs=1e-12)
    assert {t.reason for t in w.trades} == {TradeReason.CALENDAR_REBALANCE}


def test_distribution_without_rebalance_uses_waterfall_with_distribution_reason():
    """Q-037 point 24 / TAX-006 / REB-011: without a trigger and without RF the gross D is
    funded by step-C sales pro rata with reason foundation_distribution_liquidation, each with
    costs and slippage and a realization; other dues keep reason sell_to_pay."""
    inp = flat_inputs(targets={"stocks": 0.6, "gold": 0.4})
    res, fh, _ = run_schedule(inp, rows(("2000-03-10", 100_000.0)))
    w = week(res, "2000-03-10")
    sales = w.trades
    assert {t.reason for t in sales} == {TradeReason.FOUNDATION_DISTRIBUTION_LIQUIDATION}
    assert {t.asset for t in sales} == {"stocks", "gold"} and all(t.side == "sell" for t in sales)
    assert all(t.transaction_cost > 0 and t.slippage > 0 for t in sales)              # REB-011
    c = 0.0015
    assert math.fsum(t.gross_traded_value for t in sales) == pytest.approx(100_000.0 / (1 - c))
    assert all(t.pipeline_step == 3 for t in sales)
    assert len([r for r in res.realizations if r.week_key == D("2000-03-10")]) == 2
    res2, _, _ = run_schedule(inp, ())               # the 2001 admin cost alone: sell_to_pay
    w2 = week(res2, "2001-01-05")
    assert {t.reason for t in w2.trades} == {TradeReason.SELL_TO_PAY}


def test_removed_nominal_week_and_rows_outside_the_run():
    """Q-037 points 13/32: a row whose nominal Friday is not a retained week runs in the next
    retained week (kept with scheduled_date, nominal_week, actual_week); rows before the first
    or after the last run week are ignored with a reason and never extend the run."""
    inp = flat_inputs(skip=(10,))                     # 2000-03-17 removed from the calendar
    assert D("2000-03-17") not in inp.weeks
    sched = rows(("2000-03-15", 10_000.0), ("2000-01-12", 5_000.0), ("2002-06-07", 5_000.0))
    res, fh, ignored = run_schedule(inp, sched)
    ev = fh.state.distribution_events
    assert [(e.scheduled_date, e.nominal_week, e.actual_week) for e in ev] == [
        (D("2000-03-15"), D("2000-03-17"), D("2000-03-24"))]
    assert [(r.row_index, reason) for r, reason in ignored] == [
        (2, "before the first run week"), (3, "after the last run week")]
    assert res.weeks[-1].week_key < D("2002-06-07")


# ------------------------------------------------------------------ end of the path
def test_schedule_end_settlement_and_shadow_wealth():
    """Q-037 points 26-28: no terminal liquidation and no terminal distribution tax; only the
    final-year admin cost is paid (waterfall, portfolio stays invested);
    after_tax_terminal_wealth = remaining NAV + cumulative net distributions; the pre-tax shadow
    pays the same gross schedule untaxed: final wealth = remaining shadow NAV + cumulative
    gross distributions."""
    inp = flat_inputs()
    sched = rows(("2000-03-10", 100_000.0), ("2000-09-08", None, 0.1))
    res, fh, _ = run_schedule(inp, sched)
    t = settle_foundation(res.final_snapshot, fh.params, fh.state, 1_000_000.0, fh.inception,
                          inp.targets, res.weeks[-1].effective_states)
    assert t.tax_event == SCHEDULE and t.distributed_amount is None
    assert t.terminal_tax_total == 0.0 and t.terminal_foundation_tax == 0.0
    assert not [x for x in t.liquidation_trades
                if x.reason == TradeReason.FOUNDATION_DISTRIBUTION_LIQUIDATION]
    assert [e.event_type for e in t.terminal_tax_events] == [ADMIN_COST]
    assert t.final_cash_ledger.stocks > 0 and t.final_cash_ledger.gold > 0      # still invested
    assert t.remaining_nav == pytest.approx(res.final_ledger.nav - t.final_admin_cost.amount
                                            - t.terminal_trading_costs)
    st = t.final_tax_state
    assert t.after_tax_terminal_wealth == pytest.approx(t.remaining_nav + st.net_distributions_paid)
    assert st.net_distributions_paid == pytest.approx(st.gross_distributions_paid - st.distribution_tax_paid)
    assert st.total_tax_paid() == pytest.approx(math.fsum(
        e.amount for e in st.tax_events if e.category == "tax"), rel=1e-12)       # TEST-024
    shadow, fs, _ = run_schedule(inp, sched, zero=True)
    assert fs.state.distribution_tax_paid == 0.0
    assert fs.state.gross_distributions_paid == fs.state.net_distributions_paid
    cost = settle_foundation_shadow_costs(shadow.final_snapshot, fs.params, fs.state, fs.inception,
                                          inp.targets, shadow.weeks[-1].effective_states)
    assert cost.final_wealth == pytest.approx(cost.final_ledger.nav + fs.state.gross_distributions_paid)
    assert [w.ledger_end for w in shadow.weeks] == [w.ledger_end for w in res.weeks]   # same D


def test_schedule_configuration_errors(tmp_path):
    """Q-037 points 11/12/35: no file key, a missing file, an invalid row and the schedule +
    non-zero internal trading tax combination are ConfigErrors (no fallback to terminal)."""
    from src.config import ResolvedConfig
    from src.data_loader import load_distribution_schedule
    with pytest.raises(ConfigError, match="requires tax.foundation.distribution_file"):
        FoundationParams("family_foundation_15", tax_event=SCHEDULE)
    with pytest.raises(ConfigError, match="not defined by clean-room specification adjudication"):
        FoundationParams("family_foundation_15", tax_event=SCHEDULE, distribution_file="d.csv",
                         internal_trading_tax_rate=0.1)

    def load(text):
        f = tmp_path / "d.csv"
        f.write_text(text, encoding="utf-8")
        return load_distribution_schedule(ResolvedConfig("run", cli_layer={
            "tax": {"foundation": {"distribution_file": str(f)}}}))
    with pytest.raises(ConfigError, match="not found"):
        load_distribution_schedule(ResolvedConfig("run", cli_layer={
            "tax": {"foundation": {"distribution_file": str(tmp_path / "none.csv")}}}))
    for text, msg in (("date,amount\n2020-01-03,-5\n", "amount must be > 0"),
                      ("date,percent_nav\n2020-01-03,1.5\n", "0 < p <= 1"),
                      ("date,percent_nav\n2020-01-03,0\n", "0 < p <= 1"),
                      ("date,amount,percent_nav\n2020-01-03,5,0.1\n", "exactly one"),
                      ("date,amount,percent_nav\n2020-01-03,,\n", "exactly one"),
                      ("date,amount\n03/01/2020,5\n", "invalid date"),
                      ("date,amount\n2020-01-03,abc\n", "non-numeric"),
                      ("date,value\n2020-01-03,5\n", "date and amount and/or percent_nav"),
                      ("date,amount\n", "no rows")):
        with pytest.raises(ConfigError, match=msg):
            load(text)
    s = load("date,amount,percent_nav\n2020-01-03,1000,\n2020-02-07,,0.25\n2019-12-31,500,\n")
    assert [(r.row_index, r.kind) for r in s.rows] == [(1, "amount"), (2, "percent_nav"), (3, "amount")]
    p = s.provenance
    assert (p.raw_rows, p.raw_first_date, p.raw_last_date) == (3, D("2019-12-31"), D("2020-02-07"))
    assert dict(p.extra)["amount_rows"] == 2 and dict(p.extra)["percent_nav_rows"] == 1
    assert len(p.sha256) == 64
