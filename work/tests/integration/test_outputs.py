"""Output files of a taxed run (REP-003, REP-005, REP-015, TAX-004, PORT-007, PORT-008) on the
staged data - mechanics only: the staged dividend file is a non-canonical proxy (Q-008), gold
and stocks are staged proxies (Q-002, Q-004)."""
import csv
import json
import math

import pytest

from src.app import run_portfolio
from src.config import ResolvedConfig

RUN = {"allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"},
       "run": {"start": "2018-01-01", "end": "2026-07-31", "as_of_date": "2026-09-29"},
       "tax": {"profile": "individual_pl"},
       "portfolio": {"rebalance": "band", "rebalance_band_pp": 1, "transaction_cost_bps": 10.0}}


@pytest.fixture(scope="module")
def taxed(tmp_path_factory):
    layer = json.loads(json.dumps(RUN))
    layer["report"] = {"output_dir": str(tmp_path_factory.mktemp("out")), "run_name": "tax"}
    res = run_portfolio(ResolvedConfig("run", cli_layer=layer))
    read = lambda n: list(csv.DictReader((res.output_dir / n).open(encoding="utf-8")))   # noqa: E731
    return res, read


def test_tax_events_columns(taxed):
    """TAX-004, REP-005, Q-035: every tax event has date, event_type, asset, tax_base, rate,
    tax_due plus category, settlement, tax_year, gross/taxable base, amount and notes."""
    res, read = taxed
    rows = read("tax_events.csv")
    assert rows and list(rows[0]) == ["week_key", "date", "event_type", "category", "settlement",
                                      "tax_year", "asset", "component", "gross_base",
                                      "taxable_base", "tax_base", "rate", "amount", "tax_due",
                                      "pipeline_step", "source_status", "notes"]
    for r in rows:
        assert r["date"] == r["week_key"] and r["tax_base"] == r["taxable_base"]
        assert r["tax_due"] == r["amount"] and r["category"] in ("tax", "cost")
        assert r["settlement"] in ("weekly", "annual", "terminal")
        assert float(r["amount"]) == pytest.approx(float(r["taxable_base"]) * float(r["rate"]), rel=1e-12) \
            or r["event_type"] == "solidarity_tax"
        if r["settlement"] == "annual":
            assert int(r["tax_year"]) == int(r["week_key"][:4]) - 1 and r["pipeline_step"] == "2"
        else:
            assert int(r["tax_year"]) == int(r["week_key"][:4]) and r["pipeline_step"] == "5"


def test_tax_event_types(taxed):
    """REP-015 (individual_pl part): distinct types dividend_tax, rf_interest_tax,
    capital_gains_tax, solidarity_tax; dividend events keep the file status (DIV-011);
    annual taxes leave the ledger as step-3 payments with the same event type."""
    res, read = taxed
    rows = read("tax_events.csv")
    assert {r["event_type"] for r in rows} == {"dividend_tax", "rf_interest_tax", "capital_gains_tax",
                                               "solidarity_tax"}
    assert {r["source_status"] for r in rows if r["event_type"] == "dividend_tax"} == {"estimate"}
    pays = read("payments.csv")
    for t in ("capital_gains_tax", "solidarity_tax"):
        due = math.fsum(float(r["amount"]) for r in rows if r["event_type"] == t)
        assert math.fsum(float(p["amount"]) for p in pays if p["event_type"] == t) == pytest.approx(due, rel=1e-12)
    st = json.loads((res.output_dir / "tax_state.json").read_text())
    total = math.fsum(float(r["amount"]) for r in rows if r["category"] == "tax")
    assert st["state"]["total_tax_paid"] == pytest.approx(total, rel=1e-12)
    assert st["parameters"]["capital_gains_rate"] == 0.19 and st["unsettled_open_year"] == 2026


def test_weekly_portfolio_columns(taxed):
    """REP-003: weekly NAV path at every pipeline boundary, weights, returns, dividends and the
    actual taxes paid in the week (annual payments + immediate withholdings)."""
    res, read = taxed
    rows = read("weekly_portfolio.csv")
    for k in ("nav_start", "nav_after_signal", "nav_before_returns", "nav_after_returns", "nav_end",
              "dividend_return", "gross_dividend", "dividend_reinvested", "annual_tax_paid",
              "dividend_tax", "rf_interest_tax", "taxes_paid", "weight_start_stocks", "target_stocks"):
        assert k in rows[0], k
    ev = read("tax_events.csv")
    assert math.fsum(float(r["taxes_paid"]) for r in rows) == pytest.approx(
        math.fsum(float(e["amount"]) for e in ev), rel=1e-12)
    for r in rows:
        assert float(r["taxes_paid"]) == pytest.approx(
            float(r["annual_tax_paid"]) + float(r["dividend_tax"]) + float(r["rf_interest_tax"]), abs=1e-9)
        assert float(r["nav_end"]) == pytest.approx(
            float(r["nav_after_returns"]) - float(r["dividend_tax"]) - float(r["rf_interest_tax"]), rel=1e-12)


def test_weekly_portfolio_reconstructs_sleeves(taxed):
    """PORT-007: start/end value, return, trades, taxes and reserve of every sleeve can be
    rebuilt from the per-component ledger of each week."""
    res, read = taxed
    for w in res.engine.weeks:
        comps = w.ledger_end.components()
        assert math.fsum(comps.values()) == pytest.approx(w.nav_end, rel=1e-12)
        for a in ("stocks", "gold", "btc"):
            assert w.ledger_end.sleeve(a) == comps[a] + comps[f"rf_reserve_{a}"]


def test_realizations_and_lots(taxed):
    """PORT-008, IND-011: every sale has a realization (cost basis, gain, lots consumed, tax
    year of its Friday week_key); dividend reinvestments open lots but are not trades."""
    res, read = taxed
    sells = [t for t in read("trades.csv") if t["side"] == "sell"]
    reals = read("realizations.csv")
    assert len(sells) == len(reals)
    for t, r in zip(sells, reals):
        assert (t["week_key"], t["asset"], t["realized_gain"]) == (r["week_key"], r["asset"], r["realized_gain"])
        assert r["tax_year"] == r["week_key"][:4] and r["lots_consumed"]
    divs = read("dividend_reinvestments.csv")
    assert len(divs) == sum(1 for w in res.engine.weeks if w.ledger_before_returns.stocks > 0)
    assert not [t for t in read("trades.csv") if "dividend" in t["reason"]]


def test_taxed_run_is_deterministic(taxed, tmp_path):
    """REPRO: identical config and inputs give byte-identical tax outputs."""
    res, _ = taxed
    layer = json.loads(json.dumps(RUN))
    layer["report"] = {"output_dir": str(tmp_path), "run_name": "tax"}
    again = run_portfolio(ResolvedConfig("run", cli_layer=layer)).output_dir
    for name in ("tax_events.csv", "weekly_portfolio.csv", "trades.csv", "payments.csv",
                 "realizations.csv", "dividend_reinvestments.csv", "tax_state.json"):
        assert (res.output_dir / name).read_bytes() == (again / name).read_bytes(), name
