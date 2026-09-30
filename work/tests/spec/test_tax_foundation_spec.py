"""TEST-012, TEST-015, TEST-016, TEST-035, TEST-052: family_foundation_15/19 with
tax.foundation.tax_event=terminal (FND-001..016, Q-032, Q-033, Q-034)."""
import datetime as dt
import math

import pytest

from fixtures.builders import engine_inputs, foundation_hooks, params_for, with_dividends
from src.costs import CostModel
from src.engine import run_engine
from src.errors import ConfigError, NotImplementedCommand
from src.foundation import FoundationParams, active_days_in_year, admin_cost_for_year
from src.models import TradeReason
from src.settlement import settle_foundation_terminal

D = dt.date.fromisoformat
QUIET = {a: params_for(a, threshold_off=0.9, threshold_on=0.9) for a in ("stocks", "gold")}


def weeks_until(first_key, last):
    return (D(last) - D(first_key)).days // 7 + 1


def test_foundation_setup_cost(tmp_path):
    """TEST-052 / FND-015, FND-016, REP-019, Q-033: with the same initial capital, none and
    individual_pl invest it all, both foundations invest initial - 40 000 with an identical
    initial ledger; the setup cost is no trade, no turnover, no tax (category cost, step 0,
    phase initialization); CAGR is measured from the initial capital, the weekly path and its
    drawdown start at the investable capital."""
    import csv
    from src.app import run_portfolio
    from src.config import ResolvedConfig
    from src import metrics

    def run(profile):
        cfg = ResolvedConfig("run", cli_layer={
            "allocation": {"targets": "stocks=0.6,gold=0.4"},
            "run": {"start": "2018-01-01", "end": "2019-12-31", "as_of_date": "2026-09-29"},
            "tax": {"profile": profile}, "portfolio": {"transaction_cost_bps": 10.0},
            "report": {"output_dir": str(tmp_path / profile), "run_name": profile}})
        return run_portfolio(cfg)

    res = {p: run(p) for p in ("none", "individual_pl", "family_foundation_15", "family_foundation_19")}
    navs = {p: r.engine.initial_ledger.nav for p, r in res.items()}
    assert navs == {"none": 1_000_000.0, "individual_pl": 1_000_000.0,
                    "family_foundation_15": 960_000.0, "family_foundation_19": 960_000.0}
    assert res["family_foundation_15"].engine.initial_ledger == res["family_foundation_19"].engine.initial_ledger
    for p in ("family_foundation_15", "family_foundation_19"):
        r = res[p]
        row = next(csv.DictReader((r.output_dir / "summary.csv").open(encoding="utf-8")))
        assert (float(row["initial_capital"]), float(row["investable_initial_capital"]),
                float(row["weekly_path_start_nav"]), float(row["nav_start"])) == (
            1_000_000.0, 960_000.0, 960_000.0, 1_000_000.0)
        assert float(row["foundation_setup_cost_paid"]) == 40_000.0
        ev = list(csv.DictReader((r.output_dir / "tax_events.csv").open(encoding="utf-8")))
        setup = [e for e in ev if e["event_type"] == "foundation_setup_cost"]
        assert len(setup) == 1 and (setup[0]["category"], setup[0]["pipeline_step"], setup[0]["phase"],
                                    float(setup[0]["amount"]), setup[0]["week_key"]) == (
            "cost", "0", "initialization", 40_000.0, "2017-12-29")
        assert float(row["total_tax_paid"]) == pytest.approx(
            math.fsum(float(e["amount"]) for e in ev if e["category"] == "tax"), rel=1e-12)
        assert not any(t.week_key < r.engine.weeks[0].week_key for t in r.engine.trades)
        m = r.metrics
        assert m.growth_base_nav == 1_000_000.0 and m.path_start_nav == 960_000.0
        assert m.cagr == pytest.approx((m.final_wealth_pre_tax / 1_000_000.0) ** (365.2425 / m.elapsed_days) - 1)
        path = metrics.nav_path(960_000.0, [w.nav_end for w in r.pre_tax.engine.weeks])
        assert m.max_drawdown == metrics.max_drawdown(path)            # setup is no drawdown
        assert r.metrics.calendar_years[0].start_nav == 960_000.0
    for p in ("none", "individual_pl"):                  # FND-014/015: no foundation costs
        m = res[p].metrics
        assert m.growth_base_nav == m.path_start_nav == 1_000_000.0
        assert not any(e.event_type.startswith("foundation_") for e in
                       (res[p].tax_state.tax_events if res[p].tax_state else []))
        assert not any(x.event_type.startswith("foundation_") for x in res[p].engine.payments)
    admin = {p: res[p].terminal.final_tax_state.admin_cost_paid for p in
             ("family_foundation_15", "family_foundation_19")}
    assert admin["family_foundation_15"] == admin["family_foundation_19"] > 0     # FND-014
    with pytest.raises(ConfigError, match="FND-015"):
        from src.foundation import FoundationHooks
        FoundationHooks(FoundationParams("family_foundation_15", setup_cost_pln=1_000_000.0),
                        D("2017-12-29")).investable_capital(1_000_000.0)


def test_foundation_admin_cost():
    """TEST-035 / FND-010, FND-011, FND-012, FND-014, Q-034: annual cost 40 000 over
    (inception, last week]: inception 2019-06-28, run 2019-07-05..2021-03-05 (2020 leap).
    prorated: 2019 = 186/365 (Jun 29..Dec 31), 2020 = 366/366, 2021 = 64/365 (Jan 1..Mar 5);
    full: 40 000 per active year. 2019 and 2020 are determined in step 2 of the first retained
    week of the next year and paid in step 3; 2021 in the terminal settlement."""
    first_key = "2019-06-07"                               # run from index 4 = 2019-07-05
    n = weeks_until(first_key, "2021-03-05")
    inp = engine_inputs({"stocks": [100.0] * n}, first=4, first_key=first_key,
                        targets={"stocks": 0.5, "rf": 0.5}, returns={"stocks": [0.0] * (n - 4)},
                        params={"stocks": QUIET["stocks"]}, capital=1_040_000.0)
    assert (inp.weeks[0], inp.weeks[-1]) == (D("2019-07-05"), D("2021-03-05"))
    # hand-computed calendar days
    assert (active_days_in_year(2019, D("2019-06-28"), D("2021-03-05")),
            active_days_in_year(2020, D("2019-06-28"), D("2021-03-05")),
            active_days_in_year(2021, D("2019-06-28"), D("2021-03-05"))) == (186, 366, 64)
    expected = {"prorated": {2019: 40_000.0 * 186 / 365, 2020: 40_000.0, 2021: 40_000.0 * 64 / 365},
                "full": {2019: 40_000.0, 2020: 40_000.0, 2021: 40_000.0}}
    for mode, exp in expected.items():
        hooks, fh = foundation_hooks(inp, admin_cost_proration=mode)
        res = run_engine(inp, hooks)
        assert res.initial_ledger.nav == 1_000_000.0
        for y, week in ((2019, D("2020-01-03")), (2020, D("2021-01-01"))):
            w = next(x for x in res.weeks if x.week_key == week)
            assert w.amounts_due.items == (("foundation_annual_admin_cost", pytest.approx(exp[y])),)
            assert dict(w.step_ledgers)[2] == w.ledger_after_signal          # not paid in step 2
            assert [(p.event_type, p.pipeline_step) for p in w.payments] == [
                ("foundation_annual_admin_cost", 3)]
            assert w.payments[0].amount == pytest.approx(exp[y], rel=1e-12)
            c = fh.state.admin_cost_by_year[y]
            assert (c.settlement, c.determined_week, c.paid_week) == ("annual", week, week)
        assert sum(1 for w in res.weeks if w.payments) == 2
        t = settle_foundation_terminal(res.final_snapshot, fh.params, fh.state, 1_040_000.0,
                                       D("2019-06-28"))
        assert t.final_admin_cost.year == 2021 and t.final_admin_cost.active_days == 64
        assert t.final_admin_cost.amount == pytest.approx(exp[2021], rel=1e-12)
        ev = [e for e in t.terminal_tax_events if e.event_type == "foundation_annual_admin_cost"]
        assert [(e.settlement, e.phase, e.category, e.tax_year) for e in ev] == [
            ("terminal", "terminal", "cost", 2021)]
        st = t.final_tax_state
        assert st.admin_cost_paid == pytest.approx(math.fsum(exp.values()), rel=1e-12)
        assert st.total_tax_paid() == t.distribution_tax                     # costs are no tax
        assert res.weeks[-1].nav_end == t.pre_terminal_nav                   # not in the path
    # leap-year check of the proration itself: 2020 has 366 days, 2021 has 365
    p = FoundationParams("family_foundation_15")
    assert admin_cost_for_year(p, 2020, D("2020-06-26"), D("2021-01-01"))[1:3] == (188, 366)
    assert admin_cost_for_year(p, 2020, D("2020-06-26"), D("2021-01-01"))[0] == pytest.approx(
        40_000.0 * 188 / 366)


def test_admin_cost_year_boundary():
    """Q-034 boundary: first return week 2021-01-01, inception 2020-12-25 -> the foundation is
    active on 2020-12-26..31 (6 of 366 days); that cost is due in step 2 of the very first
    retained week (2021-01-01) and paid in its step 3."""
    first_key = "2020-12-04"                               # run from index 4 = 2021-01-01
    n = weeks_until(first_key, "2021-03-26")
    inp = engine_inputs({"stocks": [100.0] * n}, first=4, first_key=first_key,
                        targets={"stocks": 0.5, "rf": 0.5}, returns={"stocks": [0.0] * (n - 4)},
                        params={"stocks": QUIET["stocks"]})
    assert inp.weeks[0] == D("2021-01-01")
    hooks, fh = foundation_hooks(inp)
    res = run_engine(inp, hooks)
    w = res.weeks[0]
    assert w.amounts_due.items == (("foundation_annual_admin_cost", pytest.approx(40_000.0 * 6 / 366)),)
    assert w.payments[0].amount == pytest.approx(40_000.0 * 6 / 366, rel=1e-12)
    assert fh.state.admin_cost_by_year[2020].active_days == 6


def test_dividend_tax_15():
    """TEST-012 / FND-001, DIV-003: both foundation profiles withhold 15% of the gross dividend
    cash (value before returns x dividend_return) in step 5; net dividend reinvested (Q-016)."""
    inp = with_dividends(engine_inputs({"stocks": [100.0] * 8}, first=4, targets={"stocks": 1.0},
                                       returns={"stocks": [0.01] * 4}, params={"stocks": QUIET["stocks"]},
                                       capital=1_040_000.0), 0.0005)
    for profile in ("family_foundation_15", "family_foundation_19"):
        hooks, fh = foundation_hooks(inp, profile)
        res = run_engine(inp, hooks)
        divs = [e for e in fh.state.tax_events if e.event_type == "dividend_tax"]
        assert divs[0].gross_base == 500.0 and divs[0].rate == 0.15
        assert divs[0].amount == pytest.approx(75.0, abs=1e-12)
        for w, e in zip(res.weeks, divs):
            assert e.gross_base == w.ledger_before_returns.stocks * 0.0005
            assert w.ledger_end.stocks / w.ledger_before_returns.stocks - 1 == pytest.approx(
                0.01 - 0.0005 * 0.15, abs=1e-15)


def _dist_run(profile, mode="signal-only", base="distributed_amount"):
    n = 30
    rising = [100.0 * 1.02 ** i for i in range(n)]
    inp = with_dividends(engine_inputs({"stocks": rising, "gold": [100.0] * n}, first=4,
                                       targets={"stocks": 0.6, "gold": 0.3, "rf": 0.1},
                                       returns={"stocks": [0.02] * (n - 4), "gold": [0.001] * (n - 4)},
                                       params=QUIET, costs=CostModel(10.0, 5.0), rf=0.0005), 0.0004)
    hooks, fh = foundation_hooks(inp, profile, mode, distribution_tax_base=base)
    res = run_engine(inp, hooks)
    t = settle_foundation_terminal(res.final_snapshot, fh.params, fh.state, 1_000_000.0,
                                   inp.weeks[0] - dt.timedelta(days=7))
    return inp, res, fh, t


def test_distribution_tax_by_profile():
    """TEST-016 / FND-003, FND-004, FND-005, FND-006, Q-032: terminal settlement liquidates
    everything (foundation_distribution_liquidation, costs), pays the final-year admin cost,
    and taxes distributed_amount at 15% or 19%; after-tax wealth = distributed - tax; no CG or
    solidarity for foundations."""
    out = {}
    for profile, rate in (("family_foundation_15", 0.15), ("family_foundation_19", 0.19)):
        inp, res, fh, t = _dist_run(profile)
        out[profile] = (res, t)
        assert {x.reason for x in t.liquidation_trades} == {TradeReason.FOUNDATION_DISTRIBUTION_LIQUIDATION}
        assert all(x.phase == "terminal" and x.pipeline_step is None for x in t.liquidation_trades)
        assert t.distributed_amount == pytest.approx(
            t.pre_terminal_nav - t.terminal_trading_costs - t.final_admin_cost.amount, rel=1e-12)
        assert t.distribution_tax_base == t.distributed_amount
        assert t.distribution_tax == pytest.approx(t.distributed_amount * rate, rel=1e-15)
        assert t.after_tax_terminal_wealth == pytest.approx(t.distributed_amount * (1 - rate), rel=1e-12)
        assert (t.terminal_capital_gains_tax, t.terminal_solidarity_tax) == (0.0, 0.0)
        assert t.terminal_foundation_tax == t.terminal_tax_total == t.distribution_tax
        ev = [e for e in t.terminal_tax_events if e.event_type == "foundation_distribution_tax"]
        assert [(e.category, e.settlement, e.phase, e.rate) for e in ev] == [("tax", "terminal", "terminal", rate)]
        assert not any(e.event_type in ("capital_gains_tax", "solidarity_tax") for e in t.final_tax_state.tax_events)
    (r15, t15), (r19, t19) = out["family_foundation_15"], out["family_foundation_19"]
    assert t15.distributed_amount == t19.distributed_amount and t19.distribution_tax > t15.distribution_tax


def test_internal_trading_tax_zero():
    """TEST-015 / FND-002 (default 0), Q-047: realized gains of signal trades, rebalances and
    sell_to_pay sales are audited per Friday year but create no internal trading tax; a
    non-zero rate is refused (Q-047 open), never silently treated as 0."""
    from fixtures.builders import annual_tax_inputs
    inp = annual_tax_inputs()
    for mode in ("signal-only", "annually"):
        hooks, fh = foundation_hooks(inp, mode=mode)
        res = run_engine(inp, hooks)
        reasons = {t.reason for t in res.trades if t.side == "sell"}
        assert TradeReason.SIGNAL_EXIT in reasons
        assert (TradeReason.SELL_TO_PAY if mode == "signal-only" else TradeReason.CALENDAR_REBALANCE) in reasons
        gains = [r.realized_gain for r in res.realizations]
        assert gains and max(gains) > 0
        audited = [(a, g, w) for rs in fh.state.realizations.values() for a, g, w in rs]
        assert sorted(audited) == sorted((r.asset, r.realized_gain, r.week_key) for r in res.realizations)
        assert fh.state.internal_trading_tax_paid == 0.0
        assert {e.event_type for e in fh.state.tax_events} <= {
            "foundation_setup_cost", "foundation_annual_admin_cost", "dividend_tax", "rf_interest_tax"}
    with pytest.raises(NotImplementedCommand, match="Q-047"):
        FoundationParams("family_foundation_15", internal_trading_tax_rate=0.05)
