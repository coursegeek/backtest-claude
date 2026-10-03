"""Output files of a taxed run (REP-003, REP-005, REP-015, TAX-004, PORT-007, PORT-008) on the
clean-room data: canonical stock signal and dividend files (Q-004/Q-008 resolved); gold is the
staged proxy (Q-002)."""
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
                                      "pipeline_step", "phase", "source_status", "notes"]
    for r in rows:
        assert r["date"] == r["week_key"] and r["tax_base"] == r["taxable_base"]
        assert r["tax_due"] == r["amount"] and r["category"] in ("tax", "cost")
        assert r["settlement"] in ("weekly", "annual", "terminal")
        assert float(r["amount"]) == pytest.approx(float(r["taxable_base"]) * float(r["rate"]), rel=1e-12) \
            or r["event_type"] == "solidarity_tax"
        if r["settlement"] == "annual":
            assert int(r["tax_year"]) == int(r["week_key"][:4]) - 1 and r["pipeline_step"] == "2"
            assert r["phase"] == "weekly"
        elif r["settlement"] == "terminal":                 # final year, after the last week
            assert r["week_key"] == res.engine.weeks[-1].week_key.isoformat()
            assert int(r["tax_year"]) == int(r["week_key"][:4])
            assert r["pipeline_step"] == "" and r["phase"] == "terminal"
        else:
            assert int(r["tax_year"]) == int(r["week_key"][:4]) and r["pipeline_step"] == "5"
            assert r["phase"] == "weekly"
    assert [r["event_type"] for r in rows if r["settlement"] == "terminal"] == [
        "capital_gains_tax", "solidarity_tax"]


def test_tax_event_types(taxed):
    """REP-015 (individual_pl part): distinct types dividend_tax, rf_interest_tax,
    capital_gains_tax, solidarity_tax; dividend events keep the file status (DIV-011);
    annual taxes leave the ledger as step-3 payments with the same event type."""
    res, read = taxed
    rows = read("tax_events.csv")
    assert {r["event_type"] for r in rows} == {"dividend_tax", "rf_interest_tax", "capital_gains_tax",
                                               "solidarity_tax"}
    div = [r for r in rows if r["event_type"] == "dividend_tax"]
    assert {r["source_status"] for r in div if r["week_key"] <= "2025-12-26"} == {"actual"}
    assert {r["source_status"] for r in div if r["week_key"] >= "2026-01-02"} == {"estimate"}
    pays = read("payments.csv")
    for t in ("capital_gains_tax", "solidarity_tax"):
        due = math.fsum(float(r["amount"]) for r in rows if r["event_type"] == t)
        assert math.fsum(float(p["amount"]) for p in pays if p["event_type"] == t) == pytest.approx(due, rel=1e-12)
    st = json.loads((res.output_dir / "tax_state.json").read_text())
    total = math.fsum(float(r["amount"]) for r in rows if r["category"] == "tax")
    assert st["after_terminal"]["total_tax_paid"] == pytest.approx(total, rel=1e-12)
    weekly = math.fsum(float(r["amount"]) for r in rows if r["phase"] == "weekly")
    assert st["before_terminal"]["total_tax_paid"] == pytest.approx(weekly, rel=1e-12)
    assert st["parameters"]["capital_gains_rate"] == 0.19
    assert st["before_terminal"]["open_year"] == 2026 and st["after_terminal"]["open_year"] is None
    assert [p["context"] for p in pays if p["phase"] == "terminal"] == ["terminal_settlement"] * len(
        [p for p in pays if p["phase"] == "terminal"])


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
    weekly = math.fsum(float(r["taxes_paid"]) for r in rows)        # terminal taxes excluded
    assert weekly == pytest.approx(math.fsum(float(e["amount"]) for e in ev if e["phase"] == "weekly"),
                                   rel=1e-12)
    assert weekly + res.terminal.terminal_tax_total == pytest.approx(
        math.fsum(float(e["amount"]) for e in ev), rel=1e-12)
    assert len(rows) == len(res.engine.weeks)                       # no terminal weekly record
    assert float(rows[-1]["nav_end"]) == res.terminal.pre_terminal_nav
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


def test_terminal_settlement_breakout(taxed):
    """PORT-014, REP-017 (individual_pl part), IND-016: terminal_settlement.json holds the
    breakout separate from the weekly path; terminal trades, payments and transfers are in
    the audit files with phase=terminal and no pipeline step."""
    res, read = taxed
    doc = json.loads((res.output_dir / "terminal_settlement.json").read_text())
    for k in ("pre_terminal_nav", "terminal_liquidation_costs", "terminal_transaction_costs",
              "terminal_slippage", "terminal_capital_gains_tax", "terminal_solidarity_tax",
              "terminal_foundation_tax", "terminal_tax_total", "after_tax_terminal_wealth"):
        assert k in doc, k
    rows = read("weekly_portfolio.csv")
    assert float(rows[-1]["nav_end"]) == doc["pre_terminal_nav"]
    assert doc["after_tax_terminal_wealth"] == pytest.approx(
        doc["pre_terminal_nav"] - doc["terminal_liquidation_costs"] - doc["terminal_tax_total"], rel=1e-12)
    term = [t for t in read("trades.csv") if t["phase"] == "terminal"]
    assert term and all(t["reason"] == "terminal_liquidation" and t["pipeline_step"] == ""
                        and t["week_key"] == rows[-1]["week_key"] for t in term)
    assert all(t["phase"] == "weekly" and t["pipeline_step"] in "123" for t in read("trades.csv")
               if t["reason"] != "terminal_liquidation")
    assert [t["asset"] for t in term] == ["stocks", "gold", "btc"]
    assert {r["phase"] for r in read("realizations.csv")} == {"weekly", "terminal"}
    assert {p["phase"] for p in read("payments.csv") if p["context"] == "terminal_settlement"} == {"terminal"}


def _summary(out):
    rows = list(csv.DictReader((out / "summary.csv").open(encoding="utf-8")))
    assert len(rows) == 1                                       # one wide row per run
    return rows[0]


def test_summary_schema(taxed):
    """REP-002, REP-017, REP-018: summary.csv has the stable column list, one row, the
    parameters, metrics, terminal breakout, as_of_date and dropped incomplete weeks."""
    from src.reporting import SUMMARY_FIELDS
    res, read = taxed
    header = (res.output_dir / "summary.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header == list(SUMMARY_FIELDS)
    row = _summary(res.output_dir)
    for k in ("spec_version", "run_name", "tax_profile", "requested_start", "requested_end",
              "effective_first_week", "effective_last_week", "inception_date", "elapsed_days",
              "weeks", "initial_capital", "nav_start", "final_wealth_pre_tax", "pre_terminal_nav",
              "after_tax_terminal_wealth", "cagr", "after_tax_cagr", "real_cagr", "volatility",
              "sharpe", "after_tax_sharpe", "sortino", "max_drawdown", "calmar", "after_tax_calmar",
              "best_year", "worst_year", "trade_count", "turnover", "total_tax_paid",
              "terminal_liquidation_costs", "terminal_capital_gains_tax",
              "terminal_solidarity_tax", "terminal_foundation_tax", "as_of_date",
              "dropped_incomplete_weeks", "target_stocks", "risk_on_share_stocks",
              "signal_stocks_ma", "cpi_label", "real_return_warning"):
        assert row[k] != "", k
    assert (row["spec_version"], row["tax_profile"], row["pre_tax_method"]) == (
        "3.1", "individual_pl", "shadow_zero_tax")
    assert row["as_of_date"] == "2026-09-29" and row["requested_start"] == "2018-01-01"
    assert float(row["pre_terminal_nav"]) == float(read("weekly_portfolio.csv")[-1]["nav_end"])
    doc = json.loads((res.output_dir / "terminal_settlement.json").read_text())
    for k in ("pre_terminal_nav", "terminal_liquidation_costs", "terminal_capital_gains_tax",
              "terminal_solidarity_tax", "after_tax_terminal_wealth", "terminal_tax_total"):
        assert float(row[k]) == doc[k], k
    assert float(row["terminal_foundation_tax"]) == 0.0
    assert int(row["terminal_trade_count"]) == 3 and int(row["trade_count"]) == len(res.engine.trades)
    m = json.loads((res.output_dir / "data_manifest.json").read_text())
    assert m["pre_tax_method"] == "shadow_zero_tax" and "same EngineInputs" in m["pre_tax_note"]
    assert "run_timestamp" not in row


def test_summary_lists_all_tax_rates_and_bases(taxed):
    """REP-012, META-006: the summary shows every tax and cost assumption of the configuration
    (individual, none and the foundation scenario parameters) and the rates applied in this
    run, as user scenario parameters."""
    res, _ = taxed
    row = _summary(res.output_dir)
    expected = {"tax_individual_dividend_rate": 0.19, "tax_individual_capital_gains_rate": 0.19,
                "tax_individual_solidarity_rate": 0.04,
                "tax_individual_solidarity_threshold_pln": 1_000_000.0,
                "tax_individual_rf_interest_rate": 0.19, "tax_foundation_dividend_rate": 0.15,
                "tax_foundation_15_distribution_rate": 0.15,
                "tax_foundation_19_distribution_rate": 0.19,
                "tax_foundation_setup_cost_pln": 40_000.0,
                "tax_foundation_annual_admin_cost_pln": 40_000.0,
                "applied_dividend_tax_rate": 0.19, "applied_capital_gains_rate": 0.19,
                "applied_solidarity_rate": 0.04, "applied_rf_interest_rate": 0.19,
                "transaction_cost_bps": 10.0, "slippage_bps": 0.0}
    for k, v in expected.items():
        assert float(row[k]) == v, k
    assert row["tax_individual_cost_basis"] == "FIFO" and row["tax_dividend_tax_mode"] == "smoothed_weekly"


def test_summary_none_profile(tmp_path):
    """Q-032 (none), REP-017: no terminal settlement; terminal fields 0 and
    after_tax_terminal_wealth = pre_terminal_nav = final_wealth_pre_tax; all tax fields 0;
    pre-tax and after-tax metrics identical."""
    layer = json.loads(json.dumps(RUN))
    layer["tax"] = {"profile": "none"}
    layer["report"] = {"output_dir": str(tmp_path), "run_name": "none"}
    res = run_portfolio(ResolvedConfig("run", cli_layer=layer))
    row = _summary(res.output_dir)
    assert row["pre_tax_method"] == "actual_run_no_taxes"
    assert row["after_tax_terminal_wealth"] == row["pre_terminal_nav"] == row["final_wealth_pre_tax"]
    for k in ("terminal_liquidation_costs", "terminal_capital_gains_tax", "terminal_solidarity_tax",
              "terminal_foundation_tax", "terminal_tax_total", "total_tax_paid", "dividend_tax_paid",
              "capital_gains_tax_paid", "solidarity_tax_paid", "applied_capital_gains_rate"):
        assert float(row[k]) == 0.0, k
    assert row["terminal_trade_count"] == "0" and not (res.output_dir / "terminal_settlement.json").exists()
    for k in ("cagr", "volatility", "sharpe", "sortino", "max_drawdown", "calmar", "real_cagr"):
        assert row[k] == row[f"after_tax_{k}"], k
    assert not [t for t in read_rows(res.output_dir, "trades.csv") if t["phase"] == "terminal"]


def read_rows(out, name):
    return list(csv.DictReader((out / name).open(encoding="utf-8")))


def test_summary_reproducible(taxed, tmp_path):
    """REPRO: identical config, as_of and inputs give a byte-identical summary.csv."""
    res, _ = taxed
    layer = json.loads(json.dumps(RUN))
    layer["report"] = {"output_dir": str(tmp_path), "run_name": "tax"}
    again = run_portfolio(ResolvedConfig("run", cli_layer=layer)).output_dir
    assert (res.output_dir / "summary.csv").read_bytes() == (again / "summary.csv").read_bytes()
    assert res.output_dir != again


@pytest.fixture(scope="module")
def foundation_runs(tmp_path_factory):
    out = {}
    for profile in ("family_foundation_15", "family_foundation_19"):
        layer = json.loads(json.dumps(RUN))
        layer["tax"] = {"profile": profile}
        layer["report"] = {"output_dir": str(tmp_path_factory.mktemp(profile)), "run_name": profile}
        out[profile] = run_portfolio(ResolvedConfig("run", cli_layer=layer))
    return out


def test_foundation_summary_and_events(foundation_runs):
    """TEST-024 (foundation), MET-019, REP-017, REP-019, Q-035: summary.total_tax_paid ==
    sum(tax_events category=tax) (dividend + RF + distribution tax; setup/admin costs and
    transaction costs excluded); foundation_distribution_tax_paid, setup and admin costs are
    separate fields; terminal breakout with a non-zero terminal_foundation_tax."""
    for profile, res in foundation_runs.items():
        out = res.output_dir
        row = _summary(out)
        ev = read_rows(out, "tax_events.csv")
        taxes = math.fsum(float(e["amount"]) for e in ev if e["category"] == "tax")
        assert float(row["total_tax_paid"]) == pytest.approx(taxes, rel=1e-12)
        dist = [e for e in ev if e["event_type"] == "foundation_distribution_tax"]
        assert len(dist) == 1 and dist[0]["settlement"] == "terminal" and dist[0]["phase"] == "terminal"
        assert float(row["foundation_distribution_tax_paid"]) == float(dist[0]["amount"]) > 0
        assert float(row["terminal_foundation_tax"]) == float(row["terminal_tax_total"]) == float(dist[0]["amount"])
        assert float(row["terminal_capital_gains_tax"]) == float(row["terminal_solidarity_tax"]) == 0.0
        costs = [e for e in ev if e["category"] == "cost"]
        assert {e["event_type"] for e in costs} == {"foundation_setup_cost", "foundation_annual_admin_cost"}
        assert float(row["foundation_setup_cost_paid"]) == 40_000.0
        admin = math.fsum(float(e["amount"]) for e in costs if e["event_type"] == "foundation_annual_admin_cost")
        assert float(row["foundation_admin_cost_paid"]) == pytest.approx(admin, rel=1e-12)
        assert float(row["foundation_admin_cost_weekly"]) + float(row["foundation_admin_cost_terminal"]) == \
            pytest.approx(admin, rel=1e-12)
        weekly = read_rows(out, "weekly_portfolio.csv")
        assert math.fsum(float(r["costs_paid"]) for r in weekly) == pytest.approx(
            float(row["foundation_admin_cost_weekly"]), rel=1e-12)
        assert math.fsum(float(r["taxes_paid"]) for r in weekly) + float(row["terminal_tax_total"]) == \
            pytest.approx(float(row["total_tax_paid"]), rel=1e-12)
        doc = json.loads((out / "terminal_settlement.json").read_text())
        assert float(row["pre_terminal_nav"]) == doc["pre_terminal_nav"] == float(weekly[-1]["nav_end"])
        assert float(row["after_tax_terminal_wealth"]) == doc["after_tax_terminal_wealth"] == pytest.approx(
            float(row["distributed_amount"]) - float(row["terminal_foundation_tax"]), rel=1e-12)
        assert float(row["distributed_amount"]) == pytest.approx(
            float(row["pre_terminal_nav"]) - float(row["terminal_liquidation_costs"])
            - float(row["foundation_admin_cost_terminal"]), rel=1e-12)
        term = [t for t in read_rows(out, "trades.csv") if t["phase"] == "terminal"]
        assert {t["reason"] for t in term} == {"foundation_distribution_liquidation"}
        assert (out / "foundation_state.json").is_file() and not (out / "tax_state.json").exists()
        st = json.loads((out / "foundation_state.json").read_text())
        assert "loss_buckets" not in json.dumps(st)
        assert row["pre_tax_method"] == "shadow_zero_tax" and float(row["pre_tax_final_admin_cost"]) == \
            float(row["foundation_admin_cost_terminal"])
        assert (float(row["initial_capital"]), float(row["investable_initial_capital"])) == (1_000_000.0, 960_000.0)


def test_foundation_15_vs_19_only_terminal_differs(foundation_runs):
    """Same configuration: identical weekly path (costs, dividend taxes, trades, weekly NAV,
    pre_terminal_nav) - only the distribution rate, terminal_foundation_tax, after-tax wealth
    and after-tax CAGR differ."""
    a, b = foundation_runs["family_foundation_15"], foundation_runs["family_foundation_19"]
    for name in ("weekly_portfolio.csv", "trades.csv", "payments.csv", "rf_transfers.csv",
                 "rebalance_events.csv", "signals.csv", "realizations.csv", "dividend_reinvestments.csv"):
        ra = [r for r in read_rows(a.output_dir, name) if r.get("phase", "weekly") == "weekly"]
        rb = [r for r in read_rows(b.output_dir, name) if r.get("phase", "weekly") == "weekly"]
        assert ra == rb, name
    wa = [e for e in read_rows(a.output_dir, "tax_events.csv") if e["settlement"] != "terminal"]
    wb = [e for e in read_rows(b.output_dir, "tax_events.csv") if e["settlement"] != "terminal"]
    assert wa == wb
    sa, sb = _summary(a.output_dir), _summary(b.output_dir)
    differ = {k for k in sa if sa[k] != sb[k]}
    assert differ == {"run_name", "tax_profile", "terminal_foundation_tax", "terminal_tax_total",
                      "foundation_distribution_tax_paid", "total_tax_paid", "after_tax_terminal_wealth",
                      "after_tax_cagr", "after_tax_real_cagr", "after_tax_calmar",
                      "applied_distribution_rate"}
    assert float(sb["terminal_foundation_tax"]) / float(sa["terminal_foundation_tax"]) == pytest.approx(19 / 15)
