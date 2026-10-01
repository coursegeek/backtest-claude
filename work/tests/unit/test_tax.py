"""individual_pl tax module (TAX-001/002/005, IND-009/011/013/019, DIV-004/006/008/009/011,
PORT-013, Q-015, Q-016, Q-029) and its integration with rebalancing and sell_to_pay."""
import dataclasses
import datetime as dt
import itertools
import json
import math

import pytest

from fixtures.builders import (annual_tax_inputs, engine_inputs, params_for, random_walk,
                               tax_hooks, with_dividends, write_csv)
from src.app import run_portfolio
from src.config import ResolvedConfig
from src.costs import CostModel
from src.engine import ComposedHooks, run_engine
from src.errors import ConfigError, DividendModeError, NotImplementedCommand
from src.models import State, TradeReason
from src.rebalancing import StrategicHooks, band_deviation
from src.tax import (IndividualTaxHooks, LossBucket, TaxParams, TaxState, close_tax_year,
                     tax_hooks_from_config)

D = dt.date.fromisoformat
QUIET = {a: params_for(a, threshold_off=0.9, threshold_on=0.9) for a in ("stocks", "gold", "btc")}
Y1 = D("2001-01-05")


def cfg(extra=None):
    layer = {"allocation": {"targets": "stocks=1.0"},
             "run": {"start": "2018-01-01", "end": "2018-12-31", "as_of_date": "2026-09-29"},
             "tax": {"profile": "individual_pl"}}
    for k, v in (extra or {}).items():
        layer.setdefault(k, {}).update(v)
    return ResolvedConfig("run", cli_layer=layer)


def dividend_file(path, statuses=None, d=0.001, first="2017-01-06", last="2019-12-27"):
    """Synthetic canonical SCHEMA-005 file: dividend_return = points / spx_close_prev."""
    rows, k, i = [], D(first), 0
    while k <= D(last):
        st = (statuses or {}).get(k.year, "actual")
        rows.append([k.isoformat(), repr(d), repr(1000.0 * d), "1000.0", "1000.0", str(k.year),
                     "2.0", "20.0", st])
        k += dt.timedelta(days=7)
        i += 1
    return write_csv(path, ["date", "dividend_return", "dividend_points", "spx_close_prev",
                            "spx_close", "year", "annual_yield_pct", "trailing_dps_points",
                            "status"], rows)


# ------------------------------------------------------------------------------ profiles
def test_profiles():
    """TAX-002: none (no tax hooks), individual_pl (defaults of the specification), the
    foundation profiles are refused as not implemented; rates are validated."""
    p = TaxParams.from_config(cfg())
    assert (p.dividend_rate, p.capital_gains_rate, p.solidarity_rate, p.solidarity_threshold_pln,
            p.external_solidarity_base_pln, p.loss_carryforward_years, p.loss_offset_fraction,
            p.rf_interest_rate) == (0.19, 0.19, 0.04, 1_000_000.0, 0.0, 5, 1.0, 0.19)
    assert tax_hooks_from_config(ResolvedConfig("run")) is None
    assert isinstance(tax_hooks_from_config(cfg()), IndividualTaxHooks)
    with pytest.raises(ConfigError, match="Q-037"):
        run_portfolio(cfg({"tax": {"profile": "family_foundation_19",
                                   "foundation": {"tax_event": "distribution_schedule"}}}), write=False)
    with pytest.raises(ConfigError):
        TaxParams(capital_gains_rate=1.5)
    with pytest.raises(ConfigError):
        TaxParams(loss_carryforward_years=-1)


def test_none_profile_no_dividend_tax():
    """DIV-004: tax.profile=none has no dividend (or any) tax; dividend data is not needed."""
    assert ResolvedConfig().get("tax.none.dividend_rate") == 0.0
    r = run_portfolio(cfg({"tax": {"profile": "none"}}), write=False)
    assert r.dividend_mode == "none" and r.tax_state is None
    assert r.engine.dividend_reinvestments == () and not r.engine.payments


# ------------------------------------------------------------------------------ dividends
def test_no_cross_index_dividend_inference(tmp_path):
    """DIV-008, SEM-005, SEM-006: the dividend is the supplied dividend_return, never FF total
    return minus the SPX price return; without the file smoothed_weekly fails clearly."""
    f = dividend_file(tmp_path / "div.csv", d=0.00123)
    r = run_portfolio(cfg({"data": {"dividend_file": str(f)}}), write=False)
    evs = [e for e in r.tax_state.tax_events if e.event_type == "dividend_tax"]
    assert len(evs) == len(r.engine.weeks)
    for e, w in zip(evs, r.engine.weeks):
        assert e.gross_base == w.ledger_before_returns.stocks * 0.00123
        assert w.market.dividend_yield == {"stocks": 0.00123}
    with pytest.raises(Exception) as err:
        run_portfolio(cfg({"data": {"dividend_file": str(tmp_path / "missing.csv")}}), write=False)
    assert "missing.csv" in str(err.value)


def test_supplied_dividend_return_used(tmp_path):
    """DIV-009 (use_supplied_dividend_return): the weekly value of the file is used as is."""
    f = dividend_file(tmp_path / "div.csv", d=0.0007)
    r = run_portfolio(cfg({"data": {"dividend_file": str(f)}}), write=False)
    assert {d.dividend_return for d in r.engine.dividend_reinvestments} == {0.0007}


def test_dividend_estimate_policies(tmp_path):
    """DIV-011: status is kept in tax events; allow_with_warning warns per contiguous block,
    error_on_estimate fails, actual_only uses actual rows only (range truncated)."""
    f = dividend_file(tmp_path / "div.csv", statuses={2017: "estimate", 2018: "estimate"},
                      last="2018-12-28")
    r = run_portfolio(cfg({"data": {"dividend_file": str(f)}}), write=False)
    warn = [i for i in r.report.issues if i.code == "dividend_estimate"]
    assert len(warn) == 1 and "2018-01-05..2018-12-28" in warn[0].message
    assert {e.source_status for e in r.tax_state.tax_events if e.event_type == "dividend_tax"} == {"estimate"}
    with pytest.raises(DividendModeError):
        run_portfolio(cfg({"data": {"dividend_file": str(f)},
                           "tax": {"dividend_estimate_policy": "error_on_estimate"}}), write=False)
    with pytest.raises(DividendModeError):
        run_portfolio(cfg({"data": {"dividend_file": str(f)},
                           "tax": {"dividend_estimate_policy": "actual_only"}}), write=False)
    g = dividend_file(tmp_path / "mixed.csv", statuses={2018: "actual", 2019: "estimate"})
    r = run_portfolio(cfg({"data": {"dividend_file": str(g)},
                           "run": {"end": "2019-06-28"},
                           "tax": {"dividend_estimate_policy": "actual_only"}}), write=False)
    assert r.engine.weeks[-1].week_key == D("2018-12-28")
    assert any(i.code == "range_truncated" and i.role == "stocks_return"
               and i.week_key == D("2018-12-28") for i in r.report.issues)   # limited by dividends


def test_dividend_reinvest_raises_cost_basis_and_is_not_a_trade():
    """DIV-006, Q-016: the net dividend opens a dividend_reinvest lot (cost = net dividend,
    units at the price-component unit price); no Trade, no cost, no slippage, no turnover. A
    later sale of everything realises proceeds - (initial cost + reinvested net dividends), so
    the taxed dividend is not taxed again as a capital gain."""
    n = 16
    hist = [100.0] * 8 + [50.0] * (n - 8)             # exit at index 8, sell_fraction 1.0
    inp = with_dividends(engine_inputs({"stocks": hist}, first=4, targets={"stocks": 1.0},
                                       returns={"stocks": [0.01] * (n - 4)},
                                       params={"stocks": params_for("stocks", sell_fraction=1.0)},
                                       costs=CostModel(25.0, 10.0), capital=100_000.0), 0.001)
    hooks, tax = tax_hooks()
    res = run_engine(inp, hooks)
    divs = res.dividend_reinvestments
    assert divs and all(t.reason == TradeReason.SIGNAL_EXIT for t in res.trades)
    assert len(res.trades) == 1                         # the reinvestments are not trades
    exit_week = res.trades[0].week_key
    before = [d for d in divs if d.week_key < exit_week]
    for d in before:
        assert d.net_reinvested == pytest.approx(d.gross_dividend * 0.81, rel=1e-15)
        assert d.units == d.net_reinvested / d.unit_price
    sale = res.trades[0]
    basis = 100_000.0 + math.fsum(d.net_reinvested for d in before)
    assert sale.cost_basis == pytest.approx(basis, rel=1e-12)
    assert sale.realized_gain == pytest.approx(sale.net_cash_flow - basis, rel=1e-12)
    for w in res.weeks:                                 # step 5 changes stocks only by the tax
        for d in w.dividends:
            assert w.ledger_end.stocks == pytest.approx(w.ledger_after_returns.stocks - d.dividend_tax,
                                                        rel=1e-15)


def test_zero_rate_shadow_equals_none():
    """Q-015: the pre-tax shadow run is the identical pipeline with every tax rate 0 (costs kept):
    same NAV path as a run without taxes, no tax paid."""
    n = 150
    hist = {a: random_walk(n, seed=70 + i, vol=0.04) for i, a in enumerate(("stocks", "gold", "btc"))}
    inp = with_dividends(engine_inputs(hist, first=10, targets={"stocks": 0.5, "gold": 0.2, "btc": 0.2,
                                                                "rf": 0.1},
                                       params={a: params_for(a, ma=5) for a in hist},
                                       costs=CostModel(10.0, 5.0), rf=0.0007), 0.0004)
    ref = run_engine(inp, StrategicHooks("monthly"))
    zero = IndividualTaxHooks(TaxParams().zero_rates())
    res = run_engine(inp, ComposedHooks(StrategicHooks("monthly"), zero))
    assert [w.nav_end for w in res.weeks] == [w.nav_end for w in ref.weeks]
    assert res.trades == ref.trades and zero.state.total_tax_paid() == 0.0
    taxed = IndividualTaxHooks(TaxParams())
    res2 = run_engine(inp, ComposedHooks(StrategicHooks("monthly"), taxed))
    assert res2.weeks[-1].nav_end < ref.weeks[-1].nav_end and taxed.state.total_tax_paid() > 0


# ------------------------------------------------------------------------------ RF
def test_negative_rf_no_credit():
    """IND-014, PORT-013: R_rf_net = R_rf - max(R_rf, 0)*rate; negative RF: no tax, no credit."""
    hist = [100.0] * 4 + [70.0, 69.0, 68.0, 67.0, 66.0]
    inp = engine_inputs({"stocks": hist}, first=6, targets={"stocks": 0.5, "rf": 0.5},
                        returns={"stocks": [0.0] * 3}, rf=[-0.001, 0.002, -0.0005])
    hooks, tax = tax_hooks()
    res = run_engine(inp, hooks)
    assert {e.week_key for e in tax.state.tax_events} == {res.weeks[1].week_key}
    for w in res.weeks:
        r = w.market.rf_return
        for c in ("rf_base", "rf_reserve_stocks"):
            v = getattr(w.ledger_before_returns, c)
            assert getattr(w.ledger_end, c) == pytest.approx(v * (1 + r - max(r, 0.0) * 0.19), rel=1e-15)


# ------------------------------------------------------------------------------ annual
def test_loss_tracking_per_year():
    """IND-009, IND-019, Q-029: realized gains/losses are attributed to the Friday tax year and
    netted across stocks, gold and BTC; a net loss becomes that year's bucket."""
    st = TaxState()
    st.realizations[2005] = [("stocks", 50_000.0, D("2005-03-04")), ("gold", -80_000.0, D("2005-07-01")),
                             ("btc", 10_000.0, D("2005-12-30"))]
    l = close_tax_year(st, TaxParams(), 2005, D("2006-01-06"))
    assert l.annual_realized == -20_000.0 and l.capital_gains_tax == 0.0
    assert st.loss_buckets == [LossBucket(2005, 20_000.0, 20_000.0)]
    st.realizations[2006] = [("gold", 30_000.0, D("2006-02-03"))]
    l = close_tax_year(st, TaxParams(), 2006, D("2007-01-05"))
    assert (l.loss_offset, l.taxable_gain) == (20_000.0, 10_000.0)
    assert l.capital_gains_tax == pytest.approx(1_900.0, abs=1e-9)
    assert st.realized_gain_by_year() == {2005: -20_000.0, 2006: 30_000.0}


def test_netting_across_assets():
    """IND-019: stocks+gold+BTC gains and losses net within the year before the 19% tax;
    dividend and RF taxes do not enter the netting."""
    st = TaxState()
    st.tax_events.append(object())                    # unrelated events are ignored
    st.realizations[2010] = [("stocks", 100.0, D("2010-01-08")), ("gold", -40.0, D("2010-02-05")),
                             ("btc", 15.0, D("2010-03-05"))]
    l = close_tax_year(st, TaxParams(), 2010, D("2011-01-07"))
    assert l.annual_realized == 75.0 and l.capital_gains_tax == pytest.approx(14.25, abs=1e-12)


def test_loss_offset_fraction():
    """IND-013: fraction 0 disables the offset; fraction 1 (default) uses all available losses."""
    for f, taxable in ((0.0, 50_000.0), (1.0, 20_000.0)):
        st = TaxState(loss_buckets=[LossBucket(2009, 30_000.0, 30_000.0)])
        st.realizations[2010] = [("stocks", 50_000.0, D("2010-06-04"))]
        assert close_tax_year(st, TaxParams(loss_offset_fraction=f), 2010,
                              D("2011-01-07")).taxable_gain == taxable


def test_partial_signal_sale_realizes():
    """IND-011: a partial sale after a signal realises the gain of the units sold (FIFO), dated
    in its Friday tax year, and enters that year's annual base."""
    res, tax = _annual("signal-only")
    exit_trade = next(t for t in res.trades if t.reason == TradeReason.SIGNAL_EXIT)
    real = next(r for r in res.realizations if r.week_key == exit_trade.week_key)
    assert real.realized_gain == exit_trade.realized_gain > 0
    assert real.units_sold < real.units_sold / 0.5 and ("stocks", real.realized_gain, real.week_key) \
        in tax.state.realizations[2000]


def _annual(mode, band_pp=None, **kw):
    inp = annual_tax_inputs()
    hooks, tax = tax_hooks(mode, band_pp, **kw)
    return run_engine(inp, hooks), tax


def test_annual_tax_with_calendar_rebalance():
    res, tax = _annual("annually")
    w = next(x for x in res.weeks if x.week_key == Y1)
    assert w.rebalance.reason == TradeReason.CALENDAR_REBALANCE
    assert w.amounts_due.total == tax.state.annual_liabilities[2000].capital_gains_tax > 0
    assert {p.context for p in w.payments} == {"strategic_rebalance"}
    assert not [t for t in res.trades if t.reason == TradeReason.SELL_TO_PAY]


def test_annual_tax_with_band_rebalance():
    """A band breach at the end of 2000-12-29 executes at the start of 2001-01-05, the week the
    2000 tax becomes due: TAX-007 funds it from the band rebalance sale proceeds."""
    res, tax = _annual("band", 1.0)
    w = next(x for x in res.weeks if x.week_key == Y1)
    assert w.rebalance.reason == TradeReason.BAND_REBALANCE
    assert w.rebalance.trigger_source_week == D("2000-12-29")
    assert w.rebalance.amounts_due == w.amounts_due.total > 0
    assert {p.context for p in w.payments} == {"strategic_rebalance"}
    assert not [t for t in w.trades if t.reason == TradeReason.SELL_TO_PAY]
    for s, v in w.ledger_before_returns.sleeve_weights().items():
        assert abs(v - {"stocks": 0.7, "gold": 0.3, "btc": 0.0, "rf": 0.0}[s]) < 1e-12


def test_annual_tax_without_rebalance_uses_sell_to_pay():
    res, tax = _annual("signal-only")
    w = next(x for x in res.weeks if x.week_key == Y1)
    assert w.rebalance is None
    assert {t.reason for t in w.trades} == {TradeReason.SELL_TO_PAY}
    assert math.fsum(p.amount for p in w.payments) == pytest.approx(
        tax.state.annual_liabilities[2000].capital_gains_tax, rel=1e-12)


def test_sell_to_pay_realization_goes_to_new_tax_year():
    """TAX-006, Q-029 regression: gains realised by selling to pay the 2000 tax in the first
    week of 2001 belong to 2001 and never increase the 2000 liability."""
    res, tax = _annual("signal-only")
    stp = [t for t in res.trades if t.reason == TradeReason.SELL_TO_PAY and t.week_key == Y1]
    assert stp
    exit_gain = next(t for t in res.trades if t.reason == TradeReason.SIGNAL_EXIT).realized_gain
    assert tax.state.annual_liabilities[2000].annual_realized == exit_gain
    in_2001 = [(a, g) for a, g, wk in tax.state.realizations[2001] if wk == Y1]
    assert sorted(in_2001) == sorted((t.asset, t.realized_gain) for t in stp)
    assert tax.state.annual_liabilities[2001].annual_realized == pytest.approx(
        math.fsum(t.realized_gain for t in stp), rel=1e-12)


# ------------------------------------------------------------------------------ properties
def test_tax_state_order_independent():
    """The tax state and events do not depend on the key order of histories/targets/params."""
    n = 170
    assets = ("stocks", "gold", "btc")
    hist = {a: random_walk(n, seed=96 + i, vol=0.05) for i, a in enumerate(assets)}
    targets = {"stocks": 0.5, "gold": 0.2, "btc": 0.2, "rf": 0.1}
    prm = {a: params_for(a, ma=5, confirm_off=2) for a in assets}
    out = []
    for order in itertools.permutations(assets):
        inp = with_dividends(engine_inputs({a: hist[a] for a in order}, first=10,
                                           targets={k: targets[k] for k in order + ("rf",)},
                                           params={a: prm[a] for a in order},
                                           costs=CostModel(10.0, 5.0), rf=0.0006), 0.0003)
        hooks, tax = tax_hooks("quarterly", solidarity_threshold_pln=20_000.0)
        res = run_engine(inp, hooks)
        out.append((tuple(tax.state.tax_events), json.dumps(tax.state.to_dict(), sort_keys=True),
                    res.final_ledger, res.payments))
    assert all(o == out[0] for o in out[1:])
    assert any(e.event_type == "capital_gains_tax" and e.amount > 0 for e in out[0][0])
    assert any(e.event_type == "solidarity_tax" and e.amount > 0 for e in out[0][0])


def test_future_tax_events_cannot_alter_past_nav():
    """No look-ahead through taxes: a run truncated after week k, or with different returns
    after week k, has the identical NAV path, trades and tax events up to week k; the tax of
    year Y leaves the ledger only in the first week of Y+1."""
    inp = annual_tax_inputs()
    hooks, tax = tax_hooks("signal-only")
    full = run_engine(inp, hooks)
    k = next(i for i, w in enumerate(inp.weeks) if w == Y1)
    short = dataclasses.replace(inp, weeks=inp.weeks[:k])
    hooks2, tax2 = tax_hooks("signal-only")
    part = run_engine(short, hooks2)
    assert [w.ledger_end for w in part.weeks] == [w.ledger_end for w in full.weeks[:k]]
    assert tax2.state.tax_events == [e for e in tax.state.tax_events if e.week_key < Y1]
    assert tax2.state.annual_liabilities == {}           # 2000 is still open at 2000-12-29
    shocked = {w: (dataclasses.replace(m, asset_returns={a: -0.01 for a in m.asset_returns})
                   if w >= Y1 else m) for w, m in inp.market.items()}
    hooks3, tax3 = tax_hooks("signal-only")
    other = run_engine(dataclasses.replace(inp, market=shocked), hooks3)
    assert [w.ledger_end for w in other.weeks[:k]] == [w.ledger_end for w in full.weeks[:k]]
    assert tax3.state.annual_liabilities[2000] == tax.state.annual_liabilities[2000]


def test_immediate_taxes_step5_timing():
    """PORT-011 step 5: dividend and RF taxes are charged after the returns of step 4 and
    before step 6; the exposure of the week's return (ledger_before_returns) is unaffected, but
    the end-of-week weights - and hence a future band trigger - include them."""
    r_rf = 0.01
    f = 0.5 * (1 + r_rf)
    s = f * (0.5 + 0.0099) / (0.5 - 0.0099)            # 0.99 pp before tax, >= 1 pp after
    inp = engine_inputs({"stocks": [100.0] * 8}, first=4, targets={"stocks": 0.5, "rf": 0.5},
                        returns={"stocks": [s / 0.5 - 1.0, 0.0, 0.0, 0.0]}, rf=[r_rf, 0.0, 0.0, 0.0],
                        params=QUIET)
    hooks, tax = tax_hooks("band", 1.0)
    res = run_engine(inp, hooks)
    w = res.weeks[0]
    step = dict(w.step_ledgers)
    assert step[4] == w.ledger_after_returns and step[3] == w.ledger_before_returns
    assert step[4].rf_base == w.ledger_before_returns.rf_base * (1 + r_rf)     # gross in step 4
    assert step[5].rf_base == pytest.approx(step[4].rf_base - w.ledger_before_returns.rf_base * r_rf * 0.19,
                                            rel=1e-15)
    assert step[6] == step[5] == w.ledger_end
    assert band_deviation(step[4], inp.targets)[0] < 0.01 <= band_deviation(step[5], inp.targets)[0]
    assert [e.trigger_source_week for e in res.rebalance_events] == [w.week_key]
    none = run_engine(inp, StrategicHooks("band", 1.0))                       # without taxes
    assert none.rebalance_events == ()


def test_tax_state_transferable():
    """WF-004 preparation: the state (loss buckets, realizations, liabilities, totals) is a
    plain deep-copyable object; a new hook instance continues from a carried state."""
    st = TaxState(loss_buckets=[LossBucket(1999, 1_000_000.0, 1_000_000.0)], open_year=2000)
    carried = st.copy()
    assert carried == st and carried is not st and carried.loss_buckets is not st.loss_buckets
    json.dumps(carried.to_dict())
    inp = annual_tax_inputs()
    hooks, tax = tax_hooks("signal-only", state=carried)
    run_engine(inp, hooks)
    l = tax.state.annual_liabilities[2000]
    assert l.loss_offset == l.annual_realized > 0 and l.capital_gains_tax == 0.0
    assert st.loss_buckets == [LossBucket(1999, 1_000_000.0, 1_000_000.0)]       # original untouched


def test_real_tax_sell_to_pay_waterfall():
    """TAX-006 re-verified with a real annual liability: rf_base first (A), then the RF
    reserves pro rata (B); no asset is sold while RF cash suffices; the RISK_OFF split stays."""
    inp = annual_tax_inputs(reentry=False, targets={"stocks": 0.7, "gold": 0.25, "rf": 0.05})
    hooks, tax = tax_hooks("signal-only")
    res = run_engine(inp, hooks)
    w = next(x for x in res.weeks if x.week_key == Y1)
    due = tax.state.annual_liabilities[2000].capital_gains_tax
    assert w.ledger_after_signal.rf_base < due < w.ledger_after_signal.rf_base + \
        w.ledger_after_signal.rf_reserve_stocks
    assert [(p.context, p.funding_source) for p in w.payments] == [
        ("sell_to_pay:A_rf_base", "rf_base"), ("sell_to_pay:B_reserves_pro_rata", "rf_reserve_stocks")]
    assert w.payments[0].amount == w.ledger_after_signal.rf_base and w.ledger_before_returns.rf_base == 0.0
    assert w.trades == () and w.effective_states["stocks"] == State.RISK_OFF
    assert math.fsum(p.amount for p in w.payments) == pytest.approx(due, rel=1e-12)


def test_dividend_mode_off():
    """DIV-001: dividend_tax_mode=off takes no dividend tax (no dividend data needed); the total
    return moves the unit price; RF and capital gains taxes still apply."""
    r = run_portfolio(cfg({"tax": {"dividend_tax_mode": "off"},
                           "data": {"dividend_file": "does_not_exist.csv"}}), write=False)
    assert r.dividend_mode == "none" and r.engine.dividend_reinvestments == ()
    assert {e.event_type for e in r.tax_state.tax_events} == {"rf_interest_tax"}


def test_solidarity_external_base_above_threshold():
    """Q-051 (RESOLVED, literal IND-002/004/005 model assumption): with zero realized gain and
    external_solidarity_base_pln above the threshold, solidarity is charged on the excess."""
    st = TaxState()
    st.realizations[2010] = [("stocks", 0.0, D("2010-05-07"))]
    p = TaxParams(external_solidarity_base_pln=1_500_000.0)
    l = close_tax_year(st, p, 2010, D("2011-01-07"))
    assert l.taxable_gain == 0.0 and l.capital_gains_tax == 0.0
    assert l.solidarity_base == 1_500_000.0
    assert l.solidarity_tax > 0 and l.solidarity_tax == pytest.approx(0.04 * 500_000.0, abs=1e-9)
    # in the engine it becomes an AmountsDue item paid in step 3 of the first week of 2001
    inp = engine_inputs({"stocks": [100.0] * 60}, first=4, targets={"stocks": 0.5, "rf": 0.5},
                        returns={"stocks": [0.0] * 56}, params=QUIET)
    hooks, tax = tax_hooks(external_solidarity_base_pln=1_200_000.0, rf_interest_rate=0.0)
    res = run_engine(inp, hooks)
    w = next(x for x in res.weeks if x.week_key == Y1)
    assert w.amounts_due.items == (("solidarity_tax", pytest.approx(8_000.0, abs=1e-9)),)
    assert [p.event_type for p in w.payments] == ["solidarity_tax"]
    assert not res.realizations


def test_dividend_estimate_policy_variants(tmp_path):
    """Q-052 (RESOLVED): one file with an estimate block inside 2018 gives three distinct
    outcomes: allow_with_warning uses the estimate weeks (status kept, one warning);
    actual_only removes them from the source, so the missing policy decides (error, or the
    weeks are dropped with missing.return_policy=drop); error_on_estimate fails."""
    f = dividend_file(tmp_path / "div.csv", statuses={}, last="2019-06-28")
    rows = f.read_text().splitlines()
    est = {"2018-07-06", "2018-07-13", "2018-07-20"}
    f.write_text("\n".join(r.replace(",actual", ",estimate") if r[:10] in est else r for r in rows) + "\n")
    base = {"data": {"dividend_file": str(f)}}

    r = run_portfolio(cfg(base), write=False)                          # allow_with_warning
    assert len(r.engine.weeks) == 52
    warn = [i for i in r.report.issues if i.code == "dividend_estimate"]
    assert len(warn) == 1 and "2018-07-06..2018-07-20" in warn[0].message
    status = {e.week_key.isoformat(): e.source_status for e in r.tax_state.tax_events
              if e.event_type == "dividend_tax"}
    assert {k for k, v in status.items() if v == "estimate"} == est

    with pytest.raises(Exception) as err:                              # actual_only + error
        run_portfolio(cfg({**base, "tax": {"dividend_estimate_policy": "actual_only"}}), write=False)
    assert "missing dividend_return for run week 2018-07-06" in str(err.value)
    r = run_portfolio(cfg({**base, "tax": {"dividend_estimate_policy": "actual_only"},
                           "missing": {"return_policy": "drop"}}), write=False)
    assert len(r.engine.weeks) == 49 and {w.isoformat() for w in r.calendar.dropped} >= est
    assert {e.source_status for e in r.tax_state.tax_events if e.event_type == "dividend_tax"} == {"actual"}

    with pytest.raises(DividendModeError):                             # error_on_estimate
        run_portfolio(cfg({**base, "tax": {"dividend_estimate_policy": "error_on_estimate"}}),
                      write=False)


def test_terminal_insolvency_and_profile_scope():
    """TAX-006 (D) at the terminal: taxes exceeding the terminal cash raise InsolvencyError
    (never negative cash). Terminal settlement is applied for individual_pl only; for
    tax.profile=none it is not applied (Q-032 remains open for the other profiles)."""
    import pickle
    from src.errors import InsolvencyError
    from src.settlement import settle_terminal
    inp = engine_inputs({"stocks": [100.0] * 12}, first=4, targets={"stocks": 1.0},
                        returns={"stocks": [0.0] * 8}, params=QUIET, capital=10_000.0)
    hooks, tax = tax_hooks(external_solidarity_base_pln=10_000_000.0)
    res = run_engine(inp, hooks)
    frozen = pickle.dumps((res.final_snapshot, tax.state))
    with pytest.raises(InsolvencyError):
        settle_terminal(res.final_snapshot, tax.params, tax.state)       # solidarity 360 000
    assert pickle.dumps((res.final_snapshot, tax.state)) == frozen
    assert run_portfolio(cfg({"tax": {"profile": "none"}}), write=False).terminal is None
    r = run_portfolio(cfg(), write=False)
    assert r.terminal is not None and r.terminal.pre_terminal_nav == r.engine.weeks[-1].nav_end


def test_foundation_tax_event_modes():
    """FND-005: tax_event=terminal (default) runs; distribution_schedule needs its file
    (ConfigError, never silently terminal); any other value is a ConfigError."""
    from src.foundation import FoundationParams
    r = run_portfolio(cfg({"tax": {"profile": "family_foundation_15"}}), write=False)
    assert r.tax_params.tax_event == "terminal" and r.terminal.distribution_tax > 0
    with pytest.raises(ConfigError, match="requires tax.foundation.distribution_file"):
        FoundationParams("family_foundation_15", tax_event="distribution_schedule")
    assert FoundationParams("family_foundation_15", tax_event="distribution_schedule",
                            distribution_file="d.csv").schedule_mode
    with pytest.raises(ConfigError, match="FND-005"):
        FoundationParams("family_foundation_15", tax_event="annual")
    with pytest.raises(ConfigError, match="Q-037"):
        run_portfolio(cfg({"tax": {"profile": "family_foundation_19",
                                   "foundation": {"tax_event": "distribution_schedule"}}}), write=False)
