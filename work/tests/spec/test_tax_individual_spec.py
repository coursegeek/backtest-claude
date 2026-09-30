"""TEST-010, TEST-011, TEST-014, TEST-032, TEST-034 (individual_pl). TEST-033 needs terminal
settlement, which is not implemented in this build."""
import dataclasses
import datetime as dt
import math

import pytest

from fixtures.builders import engine_inputs, params_for, tax_hooks, with_dividends, write_csv
from src.engine import run_engine
from src.models import State, TradeReason
from src.tax import LossBucket, TaxParams, TaxState, close_tax_year

K0 = dt.date(2000, 1, 7)
QUIET = {"stocks": params_for("stocks", threshold_off=0.9, threshold_on=0.9)}


def idx(day):
    return (day - K0).days // 7


def events(tax, event_type, **kw):
    return [e for e in tax.state.tax_events if e.event_type == event_type
            and all(getattr(e, k) == v for k, v in kw.items())]


def test_cg_only_realized():
    """TEST-010 / IND-001, IND-012, IND-006: 10 000 PLN in stocks at 100; the price rises to
    150 in 2000 without a sale -> no tax for 2000 (no mark-to-market); a signal exit sells half
    in 2001 -> realized 7 500 - 5 000 = 2 500 -> 19% = 475, determined in step 2 of the first
    week of 2002 and paid in step 3 of that week."""
    n = idx(dt.date(2002, 3, 1))
    exit_at = idx(dt.date(2001, 6, 1))
    hist = [100.0] * exit_at + [50.0] * (n - exit_at)
    rets = [0.0] * (n - 4)
    rets[idx(dt.date(2000, 3, 17)) - 4] = 0.5
    inp = engine_inputs({"stocks": hist}, first=4, targets={"stocks": 1.0}, returns={"stocks": rets},
                        capital=10_000.0)
    hooks, tax = tax_hooks()
    res = run_engine(inp, hooks)
    sells = [t for t in res.trades if t.side == "sell"]
    assert [(t.reason, t.week_key) for t in sells] == [(TradeReason.SIGNAL_EXIT, dt.date(2001, 6, 8))]
    assert sells[0].realized_gain == 2500.0
    cg = {e.tax_year: e for e in events(tax, "capital_gains_tax")}
    assert cg[2000].amount == 0.0 and cg[2000].week_key == dt.date(2001, 1, 5)   # unrealized 5 000
    assert cg[2001].gross_base == 2500.0 and cg[2001].taxable_base == 2500.0
    assert cg[2001].amount == pytest.approx(475.0, abs=1e-9) and cg[2001].rate == 0.19
    assert cg[2001].week_key == dt.date(2002, 1, 4) and cg[2001].pipeline_step == 2
    w = next(x for x in res.weeks if x.week_key == dt.date(2002, 1, 4))
    assert w.amounts_due.items == (("capital_gains_tax", cg[2001].amount),)
    assert sum(p.amount for p in w.payments) == pytest.approx(475.0, abs=1e-9)
    assert {p.pipeline_step for p in w.payments} == {3}
    assert all(e.amount == 0 for e in tax.state.tax_events if e.event_type not in ("capital_gains_tax",)
               and e.settlement == "annual")
    # no tax before the payment week: the NAV path of 2001 is unaffected by the 2001 tax
    assert all(not x.payments for x in res.weeks if x.week_key < dt.date(2002, 1, 4))


def test_dividend_tax_19(tmp_path):
    """TEST-011 / DIV-002, DIV-005, IND-007: stock value 1 000 000 at the start of the week and
    d = 0.0005 -> gross dividend 500, tax 95, withheld in step 5 of the same week (smoothed);
    exact mode applies the same formula in the pay-date week only."""
    inp = with_dividends(engine_inputs({"stocks": [100.0] * 8}, first=4, targets={"stocks": 1.0},
                                       returns={"stocks": [0.01] * 4}, params=QUIET), 0.0005)
    hooks, tax = tax_hooks()
    res = run_engine(inp, hooks)
    first = events(tax, "dividend_tax")[0]
    assert (first.week_key, first.gross_base, first.rate, first.component, first.pipeline_step,
            first.settlement, first.source_status) == (inp.weeks[0], 500.0, 0.19, "stocks", 5,
                                                       "weekly", "actual")
    assert first.amount == pytest.approx(95.0, abs=1e-12)
    assert res.weeks[0].dividends[0].gross_dividend == 500.0
    for w in res.weeks:                         # every week: gross = value_before_returns * d
        e = next(e for e in events(tax, "dividend_tax") if e.week_key == w.week_key)
        assert e.gross_base == w.ledger_before_returns.stocks * 0.0005
        assert e.amount == pytest.approx(e.gross_base * 0.19, rel=1e-15)

    # exact: synthetic cash-date file, staged stock/FF files (mechanics only)
    from src.app import run_portfolio
    from src.config import ResolvedConfig
    cash = write_csv(tmp_path / "cash.csv", ["pay_date", "dividend_return"],
                     [["2018-03-14", "0.004"], ["2018-09-12", "0.0035"]])
    cfg = ResolvedConfig("run", cli_layer={
        "allocation": {"targets": "stocks=1.0"},
        "run": {"start": "2018-01-01", "end": "2018-12-31", "as_of_date": "2026-09-29"},
        "tax": {"profile": "individual_pl", "dividend_tax_mode": "exact"},
        "data": {"dividend_cash_file": str(cash)}})
    r = run_portfolio(cfg, write=False)
    divs = [e for e in r.tax_state.tax_events if e.event_type == "dividend_tax"]
    assert [e.week_key for e in divs] == [dt.date(2018, 3, 16), dt.date(2018, 9, 14)]
    for e, d in zip(divs, (0.004, 0.0035)):
        w = next(x for x in r.engine.weeks if x.week_key == e.week_key)
        assert e.gross_base == w.ledger_before_returns.stocks * d
        assert e.amount == pytest.approx(e.gross_base * 0.19, rel=1e-15)


def test_solidarity():
    """TEST-014 / IND-002..005, IND-018: solidarity = 4% of (net realized CG after loss offsets
    + external base) above 1 000 000 PLN; dividends and RF income are excluded."""
    def close(gain, external=0.0, loss=0.0):
        st = TaxState()
        if loss:
            st.loss_buckets.append(LossBucket(2009, loss, loss))
        st.realizations[2010] = [("stocks", gain * 0.75, dt.date(2010, 5, 7)),
                                 ("btc", gain * 0.25, dt.date(2010, 9, 3))]
        return close_tax_year(st, TaxParams(external_solidarity_base_pln=external), 2010,
                              dt.date(2011, 1, 7))

    l = close(1_200_000.0, external=100_000.0)
    assert l.solidarity_base == 1_300_000.0
    assert l.solidarity_tax == pytest.approx(12_000.0, abs=1e-9)
    assert l.capital_gains_tax == pytest.approx(228_000.0, abs=1e-9)
    assert close(900_000.0).solidarity_tax == 0.0
    l = close(1_200_000.0, loss=300_000.0)                      # after the loss offset
    assert l.taxable_gain == 900_000.0 and l.solidarity_tax == 0.0

    # in the engine: large dividends and RF income, no realized gain, external base exactly at
    # the threshold -> solidarity 0 (it would be > 0 if dividends or RF income were included)
    n = idx(dt.date(2001, 2, 2))
    inp = with_dividends(engine_inputs({"stocks": [100.0] * n}, first=4,
                                       targets={"stocks": 0.5, "rf": 0.5},
                                       returns={"stocks": [0.0] * (n - 4)}, rf=0.002,
                                       params=QUIET, capital=5_000_000.0), 0.002)
    hooks, tax = tax_hooks(external_solidarity_base_pln=1_000_000.0)
    run_engine(inp, hooks)
    assert sum(e.amount for e in events(tax, "dividend_tax")) > 0
    assert sum(e.amount for e in events(tax, "rf_interest_tax")) > 0
    sol = events(tax, "solidarity_tax", tax_year=2000)[0]
    assert sol.gross_base == 1_000_000.0 and sol.amount == 0.0


def test_rf_tax_individual():
    """TEST-032 / IND-014, IND-015, PORT-013: weekly RF income max(0, value*R_rf) of rf_base and
    of every RF reserve is taxed 19% from that component; a negative RF return creates no tax
    and no credit: R_rf_net = R_rf - max(R_rf, 0)*rate."""
    hist = [100.0] * 4 + [70.0, 69.0, 68.0, 67.0]          # reconstructed RISK_OFF: 50/50 split
    inp = engine_inputs({"stocks": hist}, first=6, targets={"stocks": 0.8, "rf": 0.2},
                        returns={"stocks": [0.0, 0.0]}, rf=[0.001, -0.0002], capital=500_000.0)
    hooks, tax = tax_hooks()
    res = run_engine(inp, hooks)
    assert res.pre_start["stocks"].effective_state == State.RISK_OFF
    w1, w2 = res.weeks
    assert (w1.ledger_before_returns.rf_base, w1.ledger_before_returns.rf_reserve_stocks) == (100_000.0, 200_000.0)
    rf = events(tax, "rf_interest_tax")
    assert [(e.week_key, e.component) for e in rf] == [(w1.week_key, "rf_base"),
                                                       (w1.week_key, "rf_reserve_stocks")]
    assert rf[0].amount == pytest.approx(19.0, abs=1e-9) and rf[1].amount == pytest.approx(38.0, abs=1e-9)
    assert rf[0].taxable_base == pytest.approx(100.0, abs=1e-9) and rf[0].rate == 0.19
    for c in ("rf_base", "rf_reserve_stocks"):
        v0 = getattr(w1.ledger_before_returns, c)
        assert getattr(w1.ledger_end, c) == pytest.approx(v0 * (1 + 0.001 - 0.001 * 0.19), rel=1e-15)
        v1 = getattr(w2.ledger_before_returns, c)
        assert getattr(w2.ledger_end, c) == v1 * (1 - 0.0002)       # no tax, no credit
    assert not [e for e in rf if e.week_key == w2.week_key]


def test_loss_carryforward_5y():
    """TEST-034 / IND-009, IND-010, IND-013: a net loss of year Y is a bucket usable in
    Y+1..Y+5, consumed oldest first, expired after Y+5; loss_offset_fraction limits the offset
    to fraction * available losses."""
    def run(gains, params=TaxParams()):
        st = TaxState()
        out = {}
        for y in sorted(gains):
            st.realizations[y] = [("stocks", gains[y], dt.date(y, 6, 2))]
            out[y] = close_tax_year(st, params, y, dt.date(y + 1, 1, 6))
        return st, out

    gains = {2010: -200_000.0, 2011: 30_000.0, 2012: -20_000.0, 2013: 30_000.0, 2014: 30_000.0,
             2015: 30_000.0, 2016: 30_000.0, 2017: 30_000.0}
    st, out = run(gains)
    assert [out[y].loss_offset for y in (2011, 2013, 2014, 2015)] == [30_000.0] * 4
    assert all(out[y].buckets_used == ((2010, 30_000.0),) for y in (2011, 2013, 2014, 2015))
    assert out[2012].new_loss_bucket == 20_000.0 and out[2012].taxable_gain == 0.0
    # the 2010 bucket (80 000 left) expires after 2015; 2016 uses the 2012 bucket
    assert st.expired_losses == [(2010, 80_000.0, 2015)]
    assert out[2016].buckets_used == ((2012, 20_000.0),) and out[2016].taxable_gain == 10_000.0
    assert out[2017].loss_offset == 0.0 and out[2017].taxable_gain == 30_000.0
    assert out[2017].capital_gains_tax == pytest.approx(5_700.0, abs=1e-9)
    assert st.loss_buckets == []

    # a bucket is usable in Y+5 but not in Y+6
    st, out = run({2010: -50_000.0, 2015: 20_000.0, 2016: 40_000.0})
    assert out[2015].loss_offset == 20_000.0 and out[2016].loss_offset == 0.0
    assert st.expired_losses == [(2010, 30_000.0, 2015)]

    # loss_offset_fraction = 0.5: offset <= 0.5 * available losses
    st, out = run({2010: -40_000.0, 2011: 30_000.0, 2012: 30_000.0},
                  TaxParams(loss_offset_fraction=0.5))
    assert out[2011].loss_offset == 20_000.0 and out[2011].taxable_gain == 10_000.0
    assert out[2012].loss_offset == 10_000.0 and out[2012].taxable_gain == 20_000.0
    assert st.loss_buckets == [LossBucket(2010, 40_000.0, 10_000.0)]

    # loss_carryforward_years = 2
    st, out = run({2010: -50_000.0, 2013: 10_000.0}, TaxParams(loss_carryforward_years=2))
    assert out[2013].loss_offset == 0.0 and st.expired_losses == [(2010, 50_000.0, 2012)]


def test_terminal_liquidation():
    """TEST-033 / IND-016, IND-020, Q-032 (individual_pl): at the end of the backtest all
    stocks/gold/BTC are sold (costs, FIFO basis, realizations), the final tax year is netted
    with carried losses, CG and solidarity are charged and after-tax terminal wealth is cash
    after tax on the previously unrealized gains; the weekly path is untouched."""
    import math
    from fixtures.builders import annual_tax_inputs
    from src.settlement import settle_terminal

    # A) TEST_PLAN numbers: open lots +100 000 (stocks) and -30 000 (gold) -> 70 000 -> 13 300
    n = idx(dt.date(2001, 3, 30)) + 1
    exit_at = idx(dt.date(2000, 6, 2))
    rets_s, rets_g = [0.0] * (n - 4), [0.0] * (n - 4)
    rets_s[idx(dt.date(2000, 3, 3)) - 4] = 1.0            # stocks 200 000 -> 400 000
    rets_g[idx(dt.date(2000, 10, 6)) - 4] = -0.25         # gold 120 000 -> 90 000
    inp = engine_inputs({"stocks": [100.0] * exit_at + [50.0] * (n - exit_at), "gold": [100.0] * n},
                        first=4, targets={"stocks": 0.625, "gold": 0.375},
                        returns={"stocks": rets_s, "gold": rets_g}, capital=320_000.0,
                        params={"stocks": params_for("stocks"),
                                "gold": params_for("gold", threshold_off=0.9, threshold_on=0.9)})
    hooks, tax = tax_hooks()
    res = run_engine(inp, hooks)
    exit_trade = next(t for t in res.trades if t.reason == TradeReason.SIGNAL_EXIT)
    assert (exit_trade.week_key, exit_trade.realized_gain) == (dt.date(2000, 6, 9), 100_000.0)
    assert tax.state.annual_liabilities[2000].capital_gains_tax == pytest.approx(19_000.0, abs=1e-9)
    snap = res.final_snapshot
    assert (snap.ledger.stocks, snap.ledger.gold) == (200_000.0, 90_000.0)
    assert [l.cost for l in snap.lots_of("stocks")] == [100_000.0]           # remaining lot
    assert [l.cost for l in snap.lots_of("gold")] == [120_000.0]
    t = settle_terminal(snap, tax.params, tax.state)
    assert [(x.asset, x.reason, x.phase, x.pipeline_step) for x in t.liquidation_trades] == [
        ("stocks", TradeReason.TERMINAL_LIQUIDATION, "terminal", None),
        ("gold", TradeReason.TERMINAL_LIQUIDATION, "terminal", None)]
    assert [(r.cost_basis, r.realized_gain) for r in t.terminal_realizations] == [
        (100_000.0, 100_000.0), (120_000.0, -30_000.0)]
    assert t.final_tax_year == 2001 and t.final_year_realized_gain == 70_000.0
    assert t.terminal_capital_gains_tax == pytest.approx(13_300.0, abs=1e-9)
    assert t.terminal_solidarity_tax == 0.0 and t.terminal_trading_costs == 0.0
    assert t.pre_terminal_nav == res.weeks[-1].nav_end == 471_000.0
    assert t.after_tax_terminal_wealth == pytest.approx(471_000.0 - 13_300.0, abs=1e-9)
    assert t.final_cash_ledger.components() == {
        "stocks": 0.0, "gold": 0.0, "btc": 0.0, "rf_base": t.after_tax_terminal_wealth,
        "rf_reserve_stocks": 0.0, "rf_reserve_gold": 0.0, "rf_reserve_btc": 0.0}

    # B) with costs, a partial realization earlier in the final year (sell_to_pay of the 2001
    #    tax on 2002-01-04) and exact FIFO basis of the remaining lots
    inp = annual_tax_inputs()
    hooks, tax = tax_hooks("signal-only")
    res = run_engine(inp, hooks)
    snap = res.final_snapshot
    t = settle_terminal(snap, tax.params, tax.state)
    earlier = [r for r in res.realizations if r.week_key.year == t.final_tax_year]
    assert earlier and all(r.week_key == dt.date(2002, 1, 4) for r in earlier)
    for x, r in zip(t.liquidation_trades, t.terminal_realizations):
        v = snap.ledger.asset(x.asset)
        assert x.gross_traded_value == v and r.cost_basis == pytest.approx(
            math.fsum(l.cost for l in snap.lots_of(x.asset)), rel=1e-15)
        assert x.net_cash_flow == pytest.approx(v * (1 - 0.0015), rel=1e-15)
        assert r.realized_gain == pytest.approx(x.net_cash_flow - r.cost_basis, rel=1e-12)
    stock_basis = math.fsum(l.cost for l in snap.lots_of("stocks"))
    assert snap.ledger.stocks - stock_basis > 100_000.0             # unrealized before terminal
    assert t.final_year_realized_gain == pytest.approx(
        math.fsum([r.realized_gain for r in earlier] + [r.realized_gain for r in t.terminal_realizations]),
        rel=1e-12)
    assert t.terminal_capital_gains_tax == pytest.approx(
        0.19 * max(0.0, t.final_year_realized_gain - t.loss_offset), rel=1e-12)
    assert t.after_tax_terminal_wealth == pytest.approx(
        t.pre_terminal_nav - t.terminal_trading_costs - t.terminal_tax_total, rel=1e-12)
    assert t.after_tax_terminal_wealth < t.nav_after_liquidation < t.pre_terminal_nav
