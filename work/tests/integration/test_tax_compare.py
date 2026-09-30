"""tax-compare (TAX-003, FND-008, CLI-006, ALLOC-001, REP-002, REP-012, REP-017, REP-019,
REPRO-006, Q-023) on the frozen clean-room S06 config and the staged data - mechanics
validation only: stocks, gold and the dividend file are staged proxies (Q-002, Q-004, Q-008).

The command is an orchestrator of the production run pipeline: one shared prepared input
(superset of the data requirements of all profiles), one run_prepared per profile, one
summary.csv with one SUMMARY_FIELDS row per profile; no ranking."""
import copy
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src import app
from src import tax_compare as tc
from src.cli import resolve
from src.config import ResolvedConfig
from src.errors import ConfigError, InsolvencyError, NotImplementedCommand
from src.reporting import SUMMARY_FIELDS, fmt, summary_row

WORK = Path(__file__).resolve().parents[2]
ROOT = WORK.parent
BT = str(WORK / "backtest.py")
S06 = WORK / "configs" / "tax_compare_s06.yaml"
ALL = "none,individual_pl,family_foundation_15,family_foundation_19"
PROFILES = tuple(ALL.split(","))


def base_cfg(out, *extra, profiles=ALL):
    args = ["tax-compare", "--config", str(S06), "--output-dir", str(out)]
    if profiles is not None:
        args += ["--tax-profile", profiles]
    return resolve(args + list(extra))


def read_csv(path):
    with Path(path).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def s06(tmp_path_factory):
    out = tmp_path_factory.mktemp("s06")
    res = tc.run_tax_compare(base_cfg(out))
    rows = {r["tax_profile"]: r for r in read_csv(res.output_dir / "summary.csv")}
    manifest = json.loads((res.output_dir / tc.SHARED_MANIFEST).read_text(encoding="utf-8"))
    return res, rows, manifest


# ------------------------------------------------------------------ TAX-003 / FND-008 / REP-002
def test_tax_003_end_to_end(s06):
    """TAX-003: one command, one coherent compare result, one top-level summary.csv with one
    row per profile in the SUMMARY_FIELDS schema of 'run' (tax_profile first)."""
    res, rows, manifest = s06
    lines = (res.output_dir / "summary.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == list(SUMMARY_FIELDS) and SUMMARY_FIELDS[0] == "tax_profile"
    assert len(lines) == 1 + 4 and len(SUMMARY_FIELDS) >= 147
    assert tuple(rows) == PROFILES == res.profiles
    for p in PROFILES:                                   # the full run pipeline per profile
        r = res.results[p]
        assert r.engine is not None and r.metrics is not None and r.pre_tax is not None
        assert (r.terminal is None) == (p == "none")
        assert rows[p]["tax_profile"] == p
    assert manifest["profiles"] == list(PROFILES) and manifest["shared_input"] is True
    assert manifest["command"] == "tax-compare"
    for p in PROFILES:
        own = read_csv(res.output_dir / "profiles" / p / "summary.csv")
        assert own == [rows[p]]                          # per-profile summary == top-level row


def test_fnd_008_both_foundations_side_by_side(s06):
    """FND-008: family_foundation_15 and _19 in the same summary.csv with their own
    distribution tax, after-tax wealth and after-tax CAGR."""
    _, rows, _ = s06
    f15, f19 = rows["family_foundation_15"], rows["family_foundation_19"]
    assert float(f15["applied_distribution_rate"]) == 0.15
    assert float(f19["applied_distribution_rate"]) == 0.19
    d15, d19 = float(f15["terminal_foundation_tax"]), float(f19["terminal_foundation_tax"])
    assert d15 > 0 and d19 > d15
    assert float(f15["foundation_distribution_tax_paid"]) == d15
    base = float(f15["distribution_tax_base"])
    assert base == float(f19["distribution_tax_base"]) == float(f15["distributed_amount"])
    assert d15 == pytest.approx(0.15 * base, rel=1e-12) and d19 == pytest.approx(0.19 * base, rel=1e-12)
    w15, w19 = float(f15["after_tax_terminal_wealth"]), float(f19["after_tax_terminal_wealth"])
    assert w15 == pytest.approx(base - d15, rel=1e-12) and w19 == pytest.approx(base - d19, rel=1e-12)
    assert float(f15["after_tax_cagr"]) > float(f19["after_tax_cagr"])


def test_foundation_terminal_results_differ_only_at_distribution(s06):
    """15 vs 19: identical up to the terminal distribution rate; the differing columns are the
    distribution tax and what is computed from it (after-tax wealth, after-tax CAGR/real
    CAGR/Calmar, tax totals)."""
    _, rows, _ = s06
    a, b = rows["family_foundation_15"], rows["family_foundation_19"]
    diff = {k for k in a if a[k] != b[k]}
    assert diff == {"tax_profile", "applied_distribution_rate", "terminal_foundation_tax",
                    "foundation_distribution_tax_paid", "terminal_tax_total", "total_tax_paid",
                    "after_tax_terminal_wealth", "after_tax_cagr", "after_tax_real_cagr",
                    "after_tax_calmar"}
    for k in ("pre_terminal_nav", "distributed_amount", "foundation_setup_cost_paid",
              "foundation_admin_cost_paid", "dividend_tax_paid", "rf_interest_tax_paid",
              "after_tax_max_drawdown", "trade_count", "turnover"):
        assert a[k] == b[k]


# ------------------------------------------------------------------ shared input
def test_shared_effective_calendar_across_profiles(s06):
    """Q-023 common calendar rule: the dividend source bounds the calendar of every profile,
    tax.profile=none included (it gets no longer period than the taxed profiles)."""
    res, rows, manifest = s06
    keys = ("effective_first_week", "effective_last_week", "weeks", "inception_date",
            "elapsed_days", "common_data_start", "common_data_end", "range_truncation_detail",
            "dropped_incomplete_weeks")
    for k in keys:
        assert len({rows[p][k] for p in PROFILES}) == 1, k
    weeks = {p: tuple(w.week_key for w in res.results[p].engine.weeks) for p in PROFILES}
    assert len(set(weeks.values())) == 1
    dropped = {p: res.results[p].calendar.dropped for p in PROFILES}
    assert len(set(dropped.values())) == 1
    assert manifest["data_requirements"] == {"none": "none", "individual_pl": "smoothed_weekly",
                                             "family_foundation_15": "smoothed_weekly",
                                             "family_foundation_19": "smoothed_weekly"}
    assert manifest["shared_dividend_mode"] == "smoothed_weekly"
    assert manifest["effective_last_week"] == rows["none"]["effective_last_week"]
    assert manifest["weeks"] == int(rows["none"]["weeks"]) == len(weeks["none"])
    # the dividend proxy ends before the requested end: the common range is truncated for all
    assert "dividend" in rows["none"]["range_truncation_detail"] or \
        rows["none"]["common_data_end"] < "2026-07-31"
    assert all(manifest["checks"][k] for k in ("same_effective_first_week",
                                                "same_effective_last_week", "same_weeks",
                                                "same_dropped_weeks"))


def test_none_alone_would_run_longer(s06, tmp_path):
    """Why the superset matters: a standalone 'run' of tax.profile=none needs no dividend file
    and keeps a longer calendar; inside tax-compare it shares the common calendar."""
    _, rows, _ = s06
    alone = app.run_portfolio(ResolvedConfig("run", file_layer=_s06_layer(),
                                             cli_layer={"tax": {"profile": "none"}}), write=False)
    assert alone.engine.weeks[-1].week_key.isoformat() > rows["none"]["effective_last_week"]
    assert len(alone.engine.weeks) > int(rows["none"]["weeks"])


def _s06_layer():
    import yaml
    return yaml.safe_load(S06.read_text(encoding="utf-8"))


def test_shared_source_hashes_across_profiles(s06):
    """Profile manifests repeat the shared hashes (never a second load): every profile's
    sources and prepared_input_sha256 equal the shared manifest."""
    res, _, manifest = s06
    shared = json.loads((res.output_dir / "data_manifest.json").read_text(encoding="utf-8"))
    compact = [{"role": s["role"], "path": s["path"], "sha256": s["sha256"]}
               for s in shared["sources"]]
    assert manifest["sources"] == compact and len(compact) >= 5
    assert shared["prepared_input_sha256"] == manifest["prepared_input_sha256"] == res.fingerprint
    for p in PROFILES:
        pm = json.loads((res.output_dir / "profiles" / p / "data_manifest.json")
                        .read_text(encoding="utf-8"))
        assert pm["sources"] == shared["sources"]
        assert pm["prepared_input_sha256"] == manifest["prepared_input_sha256"]
        assert pm["shared_input"] is True and pm["shared_manifest"] == "../../tax_compare_manifest.json"
        assert pm["as_of_date"] == manifest["as_of_date"] == "2026-09-29"
        assert pm["run_calendar_weeks"] == manifest["weeks"]
        assert tc.prepared_fingerprint(res.results[p].prepared) == res.fingerprint
    assert manifest["checks"]["same_source_hashes"] is True
    assert "all profiles used the same prepared market/calendar inputs" in manifest["shared_input_statement"]


def test_inputs_prepared_once_no_second_alignment(tmp_path, monkeypatch):
    """One data preparation for the whole compare: build_run is called once, every source is
    loaded once (the same number of loads as one prepare_run with the superset requirement),
    and every profile run gets the identical PreparedRun / EngineInputs object."""
    calls = {"build_run": 0, "load_role": []}
    real_build, real_load = app.build_run, app.load_role

    def build(cfg, *args, **kw):
        calls["build_run"] += 1
        return real_build(cfg, *args, **kw)

    def load(cfg, role):
        calls["load_role"].append(role)
        return real_load(cfg, role)

    monkeypatch.setattr(app, "build_run", build)
    monkeypatch.setattr(app, "load_role", load)
    res = tc.run_tax_compare(base_cfg(tmp_path), write=False)
    assert calls["build_run"] == 1
    compare_loads = sorted(calls["load_role"])
    calls["load_role"].clear()
    app.prepare_run(base_cfg(tmp_path), dividend_mode="smoothed_weekly")
    assert compare_loads == sorted(calls["load_role"])
    assert len(set(compare_loads)) == len(compare_loads)          # no role loaded twice
    for p in PROFILES:
        assert res.results[p].prepared is res.prepared
        assert res.results[p].inputs is res.prepared.inputs
        assert res.results[p].pre_tax.inputs is res.prepared.inputs   # shadow on the same object


# ------------------------------------------------------------------ pre-tax invariants
PRE_TAX_COLUMNS = ("final_wealth_pre_tax", "cagr", "real_cagr", "volatility", "sharpe", "sortino",
                   "max_drawdown", "calmar", "best_year", "best_year_return", "worst_year",
                   "worst_year_return")


def test_none_pre_tax_equals_individual_pre_tax(s06):
    """Same strategy, same shared input: the zero-tax shadow of individual_pl is exactly the
    actual path of none (no foundation costs). Actual weekly paths may differ (taxes,
    sell_to_pay, band triggers) - that is the profile effect."""
    res, rows, manifest = s06
    n, i = res.results["none"], res.results["individual_pl"]
    assert n.pre_tax.final_wealth == i.pre_tax.final_wealth
    assert [w.nav_end for w in n.pre_tax.engine.weeks] == [w.nav_end for w in i.pre_tax.engine.weeks]
    for k in PRE_TAX_COLUMNS:
        assert rows["none"][k] == rows["individual_pl"][k], k
    assert manifest["checks"]["final_wealth_pre_tax_none_equals_individual_pl"] is True
    assert float(rows["individual_pl"]["after_tax_terminal_wealth"]) < float(
        rows["none"]["after_tax_terminal_wealth"])


def test_foundation_15_pre_tax_equals_19_pre_tax(s06):
    """Both foundations: identical setup/admin costs, strategy and zero-tax shadow."""
    res, rows, manifest = s06
    a, b = res.results["family_foundation_15"], res.results["family_foundation_19"]
    assert a.pre_tax.final_wealth == b.pre_tax.final_wealth
    assert a.pre_tax.terminal_cost.admin_cost == b.pre_tax.terminal_cost.admin_cost
    for k in PRE_TAX_COLUMNS + ("pre_tax_final_admin_cost",):
        assert rows["family_foundation_15"][k] == rows["family_foundation_19"][k], k
    assert manifest["checks"]["final_wealth_pre_tax_foundation_15_equals_19"] is True
    # not expected: a foundation pays setup/admin costs in its pre-tax shadow too
    assert float(rows["family_foundation_15"]["final_wealth_pre_tax"]) < float(
        rows["none"]["final_wealth_pre_tax"])


def test_foundation_15_weekly_path_equals_19(s06):
    """Retained weeks, weekly NAV (every pipeline ledger), trades, payments, RF transfers,
    rebalancing, dividend taxes, RF taxes, setup/admin costs and pre_terminal_nav are
    identical; the difference starts at the terminal distribution."""
    res, _, manifest = s06
    a, b = res.results["family_foundation_15"], res.results["family_foundation_19"]
    assert tc.weekly_path(a) == tc.weekly_path(b)
    assert a.engine.final_ledger.nav == b.engine.final_ledger.nav
    assert a.tax_state.totals() == b.tax_state.totals()
    assert a.terminal.distributed_amount == b.terminal.distributed_amount
    assert a.terminal.distribution_tax != b.terminal.distribution_tax
    assert manifest["checks"]["weekly_path_foundation_15_equals_19"] is True
    for f in ("weekly_portfolio.csv", "trades.csv", "payments.csv", "rf_transfers.csv",
              "rebalance_events.csv", "dividend_reinvestments.csv", "signals.csv"):
        pa = res.output_dir / "profiles" / "family_foundation_15" / f
        pb = res.output_dir / "profiles" / "family_foundation_19" / f
        if f in ("trades.csv", "payments.csv", "rf_transfers.csv"):
            # identical except the terminal rows (the distribution tax payment differs)
            wa = [r for r in read_csv(pa) if r.get("phase", "weekly") != "terminal"]
            wb = [r for r in read_csv(pb) if r.get("phase", "weekly") != "terminal"]
            assert wa == wb, f
        else:
            assert pa.read_bytes() == pb.read_bytes(), f


# ------------------------------------------------------------------ comparability / REP-012
STRATEGY_COLUMNS = (["requested_start", "requested_end", "effective_first_week",
                     "effective_last_week", "weeks", "initial_capital", "rebalance_mode",
                     "rebalance_band_pp", "transaction_cost_bps", "slippage_bps", "as_of_date",
                     "sortino_mar_annual", "run_name", "spec_version"]
                    + [f"target_{s}" for s in ("stocks", "gold", "btc", "rf")]
                    + [c for c in SUMMARY_FIELDS if c.startswith("signal_")]
                    + [c for c in SUMMARY_FIELDS if c.startswith("tax_") and c != "tax_profile"])


def test_all_rows_use_identical_strategic_config(s06):
    res, rows, manifest = s06
    for k in STRATEGY_COLUMNS:
        assert len({rows[p][k] for p in PROFILES}) == 1, k
    r = rows["none"]
    assert (r["requested_start"], r["requested_end"], r["as_of_date"]) == (
        "2018-01-01", "2026-07-31", "2026-09-29")
    assert [float(r[f"target_{s}"]) for s in ("stocks", "gold", "btc", "rf")] == [0.6, 0.2, 0.2, 0.0]
    assert (r["rebalance_mode"], float(r["rebalance_band_pp"])) == ("band", 1.0)
    assert float(r["transaction_cost_bps"]) == float(r["slippage_bps"]) == 0.0
    assert float(r["initial_capital"]) == 1_000_000.0
    # profile configs differ only in tax.profile; the base config is not mutated
    views = [tc.strategy_view(res.configs[p]) for p in PROFILES]
    assert all(v == views[0] for v in views)
    assert [res.configs[p].get("tax.profile") for p in PROFILES] == list(PROFILES)
    assert res.base.source_of("tax.profile") == "defaults"
    assert len({manifest["resolved_strategy_sha256"]}) == 1


def test_profile_configs_do_not_mutate_base(tmp_path):
    base = base_cfg(tmp_path)
    before = copy.deepcopy(base.data), copy.deepcopy(base.layers)
    cfgs = {p: tc.profile_config(base, p) for p in PROFILES}
    assert (base.data, base.layers) == before
    assert len({id(c) for c in cfgs.values()}) == 4
    for p, c in cfgs.items():
        assert c.get("tax.profile") == p and c.command == "tax-compare"


def test_rep_012_tax_assumptions_per_row(s06):
    """REP-012: each row shows its active profile parameters (applied_*: 0.0 or
    not_applicable where the profile does not apply them) next to the configured scenario
    assumptions (tax_*, shared by every row)."""
    _, rows, manifest = s06
    n, i, f15, f19 = (rows[p] for p in PROFILES)
    na = "not_applicable"
    assert [float(n[k]) for k in ("applied_dividend_tax_rate", "applied_capital_gains_rate",
                                  "applied_solidarity_rate", "applied_rf_interest_rate",
                                  "applied_distribution_rate", "applied_internal_trading_tax_rate",
                                  "applied_foundation_setup_cost_pln",
                                  "applied_foundation_annual_admin_cost_pln")] == [0.0] * 8
    assert n["applied_foundation_tax_event"] == n["applied_distribution_tax_base"] == na
    assert n["applied_solidarity_threshold_pln"] == na
    assert (float(i["applied_dividend_tax_rate"]), float(i["applied_capital_gains_rate"]),
            float(i["applied_solidarity_rate"]), float(i["applied_rf_interest_rate"]),
            float(i["applied_solidarity_threshold_pln"]), float(i["applied_distribution_rate"]),
            float(i["applied_foundation_setup_cost_pln"])) == (0.19, 0.19, 0.04, 0.19, 1e6, 0.0, 0.0)
    assert i["applied_foundation_tax_event"] == na
    for f, rate in ((f15, 0.15), (f19, 0.19)):
        assert float(f["applied_distribution_rate"]) == rate
        assert float(f["applied_dividend_tax_rate"]) == 0.15
        assert float(f["applied_capital_gains_rate"]) == 0.0
        assert float(f["applied_foundation_setup_cost_pln"]) == 40000.0
        assert float(f["applied_foundation_annual_admin_cost_pln"]) == 40000.0
        assert (f["applied_foundation_tax_event"], f["applied_distribution_tax_base"],
                f["applied_foundation_admin_cost_proration"]) == ("terminal", "distributed_amount",
                                                                  "prorated")
        assert f["applied_solidarity_threshold_pln"] == na
    for r in rows.values():                       # configured assumptions, identical per row
        assert float(r["tax_foundation_15_distribution_rate"]) == 0.15
        assert float(r["tax_foundation_19_distribution_rate"]) == 0.19
        assert float(r["tax_individual_capital_gains_rate"]) == 0.19
        assert r["tax_foundation_tax_event"] == "terminal"
    assert set(manifest["summary_column_semantics"]) == {"applied_*", "tax_*"}


def test_rep_017_rep_019_fields_in_compare_summary(s06):
    """REP-017 terminal breakout and REP-019 foundation cost breakout in every compare row."""
    _, rows, _ = s06
    for r in rows.values():
        for k in ("pre_terminal_nav", "terminal_liquidation_costs", "terminal_capital_gains_tax",
                  "terminal_solidarity_tax", "terminal_foundation_tax", "after_tax_terminal_wealth",
                  "foundation_setup_cost_paid", "foundation_admin_cost_paid"):
            assert r[k] != ""
    n, i = rows["none"], rows["individual_pl"]
    assert n["after_tax_terminal_wealth"] == n["pre_terminal_nav"]
    assert float(n["terminal_tax_total"]) == float(n["total_tax_paid"]) == 0.0
    assert float(i["terminal_capital_gains_tax"]) > 0 and float(i["terminal_foundation_tax"]) == 0.0
    for p in ("family_foundation_15", "family_foundation_19"):
        f = rows[p]
        assert float(f["foundation_setup_cost_paid"]) == 40000.0
        assert float(f["foundation_admin_cost_paid"]) > 40000.0
        assert float(f["foundation_admin_cost_paid"]) == pytest.approx(
            float(f["foundation_admin_cost_weekly"]) + float(f["foundation_admin_cost_terminal"]))
        assert float(f["terminal_capital_gains_tax"]) == 0.0


def test_profile_audit_outputs(s06):
    """Each profile keeps its standard run artifacts; none has no tax state, terminal
    settlement or tax events (no fake tax events), foundations write foundation_state.json."""
    res, _, _ = s06
    common = {"summary.csv", "weekly_portfolio.csv", "trades.csv", "tax_events.csv",
              "payments.csv", "rf_transfers.csv", "rebalance_events.csv", "signals.csv",
              "config_resolved.yaml", "validation_report.csv", "data_manifest.json",
              "realizations.csv", "dividend_reinvestments.csv"}
    for p in PROFILES:
        files = {f.name for f in (res.output_dir / "profiles" / p).iterdir()}
        assert common <= files, p
        assert ("terminal_settlement.json" in files) == (p != "none")
        if p.startswith("family_foundation"):
            assert "foundation_state.json" in files and "tax_state.json" not in files
        else:
            assert "tax_state.json" in files and "foundation_state.json" not in files
    none_dir = res.output_dir / "profiles" / "none"
    assert read_csv(none_dir / "tax_events.csv") == []
    assert read_csv(none_dir / "payments.csv") == []
    assert all(float(r["dividend_tax"]) == 0.0 for r in read_csv(none_dir / "dividend_reinvestments.csv"))
    doc = json.loads((none_dir / "tax_state.json").read_text(encoding="utf-8"))
    assert doc["tax_profile"] == "none" and "no tax events" in doc["dividend_note"]
    top = {f.name for f in res.output_dir.iterdir()}
    assert {"summary.csv", "tax_compare_manifest.json", "data_manifest.json", "config_resolved.yaml",
            "validation_report.csv", "weekly_normalized.csv", "profiles"} <= top
    import yaml
    conf = yaml.safe_load((res.output_dir / "config_resolved.yaml").read_text(encoding="utf-8"))
    assert "profile" not in conf["tax"] and conf["tax"]["compare_profiles"] == list(PROFILES)
    for p in PROFILES:
        pc = yaml.safe_load((res.output_dir / "profiles" / p / "config_resolved.yaml")
                            .read_text(encoding="utf-8"))
        assert pc["tax"]["profile"] == p


def test_taxed_rows_equal_standalone_run(s06):
    """The compare is not a second backtester: on the common calendar a standalone 'run' of a
    taxed profile gives the same row (apart from the run name)."""
    _, rows, _ = s06
    for p in ("individual_pl", "family_foundation_19"):
        cfg = ResolvedConfig("run", file_layer=_s06_layer(), cli_layer={"tax": {"profile": p}})
        alone = app.run_portfolio(cfg, write=False)
        row = {k: fmt(v) for k, v in summary_row(cfg, alone).items()}
        diff = {k for k in SUMMARY_FIELDS if row.get(k, "") != rows[p][k]}
        assert diff == {"run_name"}, diff


def test_no_ranking_or_recommendation(s06):
    res, _, manifest = s06
    header = (res.output_dir / "summary.csv").read_text(encoding="utf-8").splitlines()[0].lower()
    for word in ("rank", "winner", "best_profile", "recommend"):
        assert word not in header
    text = json.dumps(manifest).lower()
    assert "winner" not in text.replace("no ranking, winner or recommendation", "")
    assert "recommended" not in text


# ------------------------------------------------------------------ ordering / reproducibility
def test_deterministic_profile_order_and_reproducibility(s06, tmp_path):
    """Canonical row order whatever the requested order; identical inputs/config give a
    byte-identical summary.csv and a compare manifest identical apart from run_timestamp."""
    res, _, manifest = s06
    again = tc.run_tax_compare(base_cfg(tmp_path / "a"))
    shuffled = tc.run_tax_compare(base_cfg(
        tmp_path / "b", profiles="family_foundation_19,none,family_foundation_15,individual_pl"))
    ref = (res.output_dir / "summary.csv").read_bytes()
    assert (again.output_dir / "summary.csv").read_bytes() == ref
    assert (shuffled.output_dir / "summary.csv").read_bytes() == ref
    assert shuffled.profiles == PROFILES
    assert shuffled.requested_profiles == ("family_foundation_19", "none", "family_foundation_15",
                                           "individual_pl")
    m2 = json.loads((again.output_dir / tc.SHARED_MANIFEST).read_text(encoding="utf-8"))
    strip = lambda m: {k: v for k, v in m.items() if k not in ("run_timestamp", "code_version")}  # noqa: E731
    assert strip(m2) == strip(manifest)
    assert json.loads((shuffled.output_dir / tc.SHARED_MANIFEST).read_text(encoding="utf-8"))[
        "resolved_strategy_sha256"] == manifest["resolved_strategy_sha256"]
    for p in PROFILES:                      # every profile artifact except the manifest timestamp
        for f in (res.output_dir / "profiles" / p).iterdir():
            g = again.output_dir / "profiles" / p / f.name
            if f.name == "data_manifest.json":
                a, b = (json.loads(x.read_text(encoding="utf-8")) for x in (f, g))
                assert strip(a) == strip(b), p
            elif f.name == "config_resolved.yaml":      # differs only in report.output_dir
                a, b = (x.read_text(encoding="utf-8").splitlines() for x in (f, g))
                assert [x for x, y in zip(a, b) if x != y] == [
                    f"  output_dir: {res.output_dir.parent}"]
            else:
                assert f.read_bytes() == g.read_bytes(), (p, f.name)
    assert "run_timestamp" not in ref.decode("utf-8").splitlines()[0]


def test_parse_profiles_validation():
    assert tc.parse_profiles("none, family_foundation_19") == ("none", "family_foundation_19")
    assert tc.canonical_order(("family_foundation_19", "none")) == ("none", "family_foundation_19")
    for bad, msg in (("none,foo", "unknown"), ("none,none", "duplicate"), ("", "empty"),
                     ("none,,individual_pl", "empty"), ([], "empty"), (None, "no tax profiles")):
        with pytest.raises(ConfigError, match=msg):
            tc.parse_profiles(bad)


def test_single_tax_profile_in_config_is_rejected(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(S06.read_text(encoding="utf-8") + "  profile: individual_pl\n", encoding="utf-8")
    cfg = resolve(["tax-compare", "--config", str(f), "--tax-profile", "none",
                   "--output-dir", str(tmp_path / "o")])
    with pytest.raises(ConfigError, match="profile axis"):
        tc.run_tax_compare(cfg)
    assert not (tmp_path / "o").exists()


# ------------------------------------------------------------------ Q-023 / ALLOC-001
def test_q023_no_weights_is_alloc_001(tmp_path):
    cfg = resolve(["tax-compare", "--tax-profile", ALL, "--output-dir", str(tmp_path / "o")])
    with pytest.raises(ConfigError, match="ALLOC-001"):
        tc.run_tax_compare(cfg)
    assert not (tmp_path / "o").exists()


# ------------------------------------------------------------------ failure atomicity
def _assert_nothing_written(out):
    assert not Path(out).exists() or not any(Path(out).iterdir())


def test_atomic_failure_q037_distribution_schedule(tmp_path):
    cfg = base_cfg(tmp_path / "o", "--foundation-tax-event", "distribution_schedule")
    with pytest.raises(tc.TaxCompareError) as e:
        tc.run_tax_compare(cfg)
    assert e.value.profile == "family_foundation_15" and "Q-037" in str(e.value)
    assert isinstance(e.value.cause, NotImplementedCommand) and e.value.exit_code == 3
    _assert_nothing_written(tmp_path / "o")


def test_atomic_failure_q047_internal_trading_tax(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(S06.read_text(encoding="utf-8") + "    internal_trading_tax_rate: 0.1\n",
                 encoding="utf-8")
    cfg = resolve(["tax-compare", "--config", str(f), "--tax-profile", "individual_pl,family_foundation_19",
                   "--output-dir", str(tmp_path / "o")])
    with pytest.raises(tc.TaxCompareError) as e:
        tc.run_tax_compare(cfg)
    assert e.value.profile == "family_foundation_19" and "Q-047" in str(e.value)
    _assert_nothing_written(tmp_path / "o")


def test_atomic_failure_insolvency_of_one_profile(tmp_path, monkeypatch):
    """A profile failing inside the engine (e.g. TAX-006 insolvency) fails the whole compare
    and names the profile; no summary.csv of the other profiles is written."""
    real = tc.run_prepared

    def run(cfg, prepared, **kw):
        if cfg.get("tax.profile") == "family_foundation_19":
            raise InsolvencyError(prepared.inputs.weeks[3], 1.0, 0.5)
        return real(cfg, prepared, **kw)

    monkeypatch.setattr(tc, "run_prepared", run)
    with pytest.raises(tc.TaxCompareError) as e:
        tc.run_tax_compare(base_cfg(tmp_path / "o"))
    assert e.value.profile == "family_foundation_19" and "insolvency" in str(e.value)
    _assert_nothing_written(tmp_path / "o")


def test_atomic_failure_while_writing_removes_directory(tmp_path, monkeypatch):
    real = tc.write_portfolio_outputs
    seen = []

    def write(cfg, res, **kw):
        seen.append(cfg.get("tax.profile"))
        if len(seen) == 3:
            raise OSError("disk full")
        return real(cfg, res, **kw)

    monkeypatch.setattr(tc, "write_portfolio_outputs", write)
    with pytest.raises(OSError):
        tc.run_tax_compare(base_cfg(tmp_path / "o"))
    _assert_nothing_written(tmp_path / "o")


# ------------------------------------------------------------------ CLI-006
def cli(*args):
    return subprocess.run([sys.executable, BT, *args], capture_output=True, text=True,
                          timeout=300, cwd=str(ROOT))


def test_cli_006_real_command(tmp_path):
    """CLI-006 with the frozen clean-room S06 config (AB_SCENARIOS S06)."""
    r = cli("tax-compare", "--config", "work/configs/tax_compare_s06.yaml",
            "--tax-profile", ALL, "--output-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = next(tmp_path.iterdir())
    assert out.name.endswith("_tax_compare")
    rows = read_csv(out / "summary.csv")
    assert [x["tax_profile"] for x in rows] == list(PROFILES)
    assert (out / "tax_compare_manifest.json").is_file()
    ab = read_csv(ROOT / "AB_SCENARIOS.csv")
    s06 = next(x for x in ab if x["scenario_id"] == "S06")
    assert "--config work/configs/tax_compare_s06.yaml" in s06["v2_command_template"]
    assert f"--tax-profile {ALL}" in s06["v2_command_template"]


def test_cli_006_failures(tmp_path):
    r = cli("tax-compare")                                     # literal: no config, no weights
    assert r.returncode == 2 and "ALLOC-001" in r.stderr
    r = cli("tax-compare", "--config", "configs/portfolio.yaml", "--tax-profile", ALL)
    assert r.returncode == 2 and "config file not found" in r.stderr     # no V1 config exists
    r = cli("tax-compare", "--config", str(S06), "--tax-profile", "none,individual_pl,none",
            "--output-dir", str(tmp_path))
    assert r.returncode == 2 and "duplicate" in r.stderr
    r = cli("tax-compare", "--config", str(S06), "--foundation-tax-event", "distribution_schedule",
            "--output-dir", str(tmp_path))
    assert r.returncode == 3 and "family_foundation_15" in r.stderr and "Q-037" in r.stderr
    assert not any(tmp_path.iterdir())


def test_s06_config_is_v2_owned_and_frozen():
    import yaml
    conf = yaml.safe_load(S06.read_text(encoding="utf-8"))
    assert conf["allocation"]["targets"] == {"stocks": 0.60, "gold": 0.20, "btc": 0.20, "rf": 0.00}
    assert conf["portfolio"] == {"initial_capital_pln": 1000000, "rebalance": "band",
                                 "rebalance_band_pp": 1, "transaction_cost_bps": 0,
                                 "slippage_bps": 0}
    assert {k: str(v) for k, v in conf["run"].items()} == {
        "start": "2018-01-01", "end": "2026-07-31", "as_of_date": "2026-09-29"}
    assert conf["tax"] == {"dividend_tax_mode": "smoothed_weekly",
                           "foundation": {"tax_event": "terminal",
                                          "distribution_tax_base": "distributed_amount"}}
    assert "profile" not in conf["tax"]


# ------------------------------------------------------------------ superset rule per selection
def test_calendar_is_superset_of_the_selected_profiles(s06, tmp_path):
    """The common calendar follows the data requirements of the selected profiles only:
    none alone needs no dividend file (its own longer calendar, identical to 'run'); none with
    one foundation shares the dividend-bound calendar of the full compare."""
    _, rows, _ = s06
    only_none = tc.run_tax_compare(base_cfg(tmp_path, profiles="none"), write=False)
    alone = app.run_portfolio(ResolvedConfig("run", file_layer=_s06_layer(),
                                             cli_layer={"tax": {"profile": "none"}}), write=False)
    assert only_none.prepared.dividend_mode == "none"
    assert [w.week_key for w in only_none.results["none"].engine.weeks] == \
        [w.week_key for w in alone.engine.weeks]
    assert only_none.results["none"].engine.final_ledger.nav == alone.engine.final_ledger.nav
    pair = tc.run_tax_compare(base_cfg(tmp_path, profiles="family_foundation_15,none"), write=False)
    assert pair.profiles == ("none", "family_foundation_15")
    assert pair.prepared.dividend_mode == "smoothed_weekly"
    assert pair.row("none")["effective_last_week"].isoformat() == rows["none"]["effective_last_week"]
    assert fmt(pair.row("none")["final_wealth_pre_tax"]) == rows["none"]["final_wealth_pre_tax"]


def test_profile_cannot_run_on_input_without_its_data(tmp_path):
    """run_prepared refuses a profile whose data requirement the prepared input lacks."""
    base = base_cfg(tmp_path)
    prepared = app.prepare_run(base, dividend_mode="none")
    with pytest.raises(ConfigError, match="needs dividend data"):
        app.run_prepared(tc.profile_config(base, "individual_pl"), prepared, write=False)
