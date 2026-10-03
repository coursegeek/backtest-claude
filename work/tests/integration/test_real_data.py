"""Clean-room data under the canonical/proxy adapters (realdata): canonical stock signal and
dividend files (Q-004/Q-008 resolved), staged gold proxy (Q-002), raw BTC (Q-005)."""
import datetime as dt
import json

from fixtures.builders import STAGED
from src.app import run_signals
from src.config import ResolvedConfig
from src.data_loader import load_role, load_stocks_signal, sem001_check
from src.validation import alignment_correlation

D = dt.date.fromisoformat
STOCKS = STAGED / "US_STOCK_PRICE_WEEKLY_1885_2026.csv"


def _shifted(series, days=7):
    return series.replace_points([p.__class__(p.week_key + dt.timedelta(days=days), p.price,
                                              p.available_at, p.source_date) for p in series.points])


def _window(series, lo, hi):
    return series.replace_points([p for p in series.points if D(lo) <= p.week_key <= D(hi)])


def test_ff_alignment_correlation():
    """NORM-018: FF returns correlate with the stock price index on the same week key, not on
    a week-shifted index. The canonical index is the S&P composite from 1928 (Friday closes;
    FF/CRSP weeks end on Saturday while Saturday sessions existed, before 1953), so the
    same-week correlation is lower before 1953 than with the CRSP-based Schwert series and
    above 0.98 from 1963; the supplemental Schwert file shows the 1926-1962 alignment."""
    cfg = ResolvedConfig()
    ff = load_role(cfg, "stocks_return").stock_total.points
    stocks = load_role(cfg, "stocks_price")
    assert alignment_correlation(ff, stocks) > 0.95
    assert alignment_correlation(ff, _window(stocks, "1963-01-04", "2026-09-25")) > 0.98
    for days in (7, -7):
        assert abs(alignment_correlation(ff, _shifted(stocks, days))) < 0.1
    schwert = load_stocks_signal(STAGED / "US_STOCK_PRICE_WEEKLY_schwert_1919_1962.csv")
    assert alignment_correlation(ff, schwert) > 0.98
    assert abs(alignment_correlation(ff, _shifted(schwert))) < 0.1


def test_ff_first_last_records():
    """SEM-002."""
    ff = load_role(ResolvedConfig(), "stocks_return")
    assert ff.stock_total.points[0].week_key == D("1926-07-02")
    assert ff.stock_total.points[-1].week_key == D("2026-08-28")


def test_gold_source_segments_reported():
    """SEM-003 evidence status: the staged gold file is a non-canonical proxy (Q-002)."""
    g = load_role(ResolvedConfig(), "gold")
    assert not g.provenance.canonical and g.provenance.adapter == "tvc_oanda_proxy"
    assert {s.source for s in g.provenance.segments} == {"TVC", "OANDA", "FOREXCOM_FILL"}
    assert sum("fill" in p.flags for p in g.points) == 26


def test_stocks_segments_in_manifest(tmp_path):
    """SEM-001 + REP-008: the canonical default stock signal file is used directly (no alias)
    and data_manifest.json keeps the source and range of every segment, the rebase anchor and
    factor and the raw-input SHA-256 values of the build (provenance sidecar)."""
    cfg = ResolvedConfig("signals", cli_layer={"run": {"asset": "stocks", "as_of_date": "2026-09-29",
                                                       "start": "2020-01-03"},
                                               "report": {"output_dir": str(tmp_path)}})
    res = run_signals(cfg)
    m = json.loads((res.output_dir / "data_manifest.json").read_text())
    src = m["sources"][0]
    assert src["role"] == "stocks_price" and src["path"].endswith("US_STOCK_PRICE_WEEKLY_1885_2026.csv")
    assert src["canonical"] is True and src["resolved_via_alias"] is False
    assert src["adapter"] == "week_end" and src["warnings"] == []
    assert [(s["source"], s["first_key"], s["last_key"], s["rows"], s["verified"]) for s in src["segments"]] == [
        ("Schwert", "1920-01-02", "1927-12-30", 418, True), ("SPX", "1928-01-06", "2026-09-25", 5151, True)]
    assert src["segments"][1]["rebase_factor"] == 410.4598610098 / 17.66
    bp = src["extra"]["build_provenance"]
    assert bp["splice"]["rebase_anchor_week"] == "1927-12-30"
    assert bp["splice"]["rebase_factor"].startswith("23.24234773554926")
    assert {x["name"] for x in bp["raw_inputs"]} == {"stkdatd.zip", "SP_DLY_SPX, 1W(1).csv"}
    assert m["as_of_date"] == "2026-09-29" and len(src["sha256"]) == 64
    assert src["sha256"] == "af1da27aa7d8dc689cb5d2f57f4a9c37e70f95fab18514a5f3087b98078987bd"
    assert src["raw_rows"] == 5569 and src["first_key"] == "1920-01-02"      # REPRO-002/003
    assert src["last_key"] == "2026-09-25"
    assert len(m["code_version"]["git_commit"]) == 40                       # REPRO-004
    assert m["timezone"] == "Europe/Warsaw" and m["run_timestamp"].endswith(("+01:00", "+02:00"))
    rows = (res.output_dir / "validation_report.csv").read_text()
    assert "default_file_alias" not in rows and "missing_week" in rows     # 1933-03-10 (Q-012)
    assert "SEM-001 provenance not satisfied" not in rows


def test_stock_signal_splice_real_data():
    """SEM-001 on the real canonical file around the splice: 1927-12-30 is the last Schwert
    week (the rebase anchor stays Schwert), 1928-01-06 and 1928-01-13 are SPX; the anchor value
    equals raw SPX 17.66 times the rebase factor; the known 1933-03-10 gap is not filled."""
    s = load_role(ResolvedConfig(), "stocks_price")
    assert s.provenance.canonical and sem001_check(s.provenance.segments) == (
        True, "Schwert before 1928, SPX from 1928")
    by = s.by_key()
    assert [by[D(k)].source for k in ("1927-12-23", "1927-12-30", "1928-01-06", "1928-01-13")] == [
        "Schwert", "Schwert", "SPX", "SPX"]
    factor = s.provenance.segments[1].rebase_factor
    assert by[D("1927-12-30")].price == 410.4598610098
    assert abs(by[D("1928-01-06")].price / factor - 17.66) < 1e-12           # raw SPX close
    assert abs(by[D("1928-01-13")].price / factor - 17.58) < 1e-12
    assert by[D("1927-12-30")].source_date == D("1927-12-31")              # Saturday session
    assert by[D("1928-01-06")].source_date == D("1928-01-02")              # weekly bar timestamp
    assert all(p.available_at == p.week_key for p in s.points)             # metadata only
    assert D("1933-03-10") not in by and D("1933-03-03") in by and D("1933-03-17") in by


def test_run_manifest_canonical_and_proxy_sources(tmp_path):
    """Default resolution after Q-004/Q-008: stocks and dividends canonical, not via alias,
    canonical adapters; gold still the non-canonical TVC/OANDA proxy with the Q-002 warning."""
    cfg = ResolvedConfig("run", cli_layer={
        "allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"}, "tax": {"profile": "individual_pl"},
        "run": {"start": "2025-06-06", "end": "2026-02-27", "as_of_date": "2026-09-29"},
        "report": {"output_dir": str(tmp_path)}})
    from src.app import run_portfolio
    res = run_portfolio(cfg)
    src = {s["role"]: s for s in json.loads((res.output_dir / "data_manifest.json").read_text())["sources"]}
    st = src["stocks_price"]
    assert st["canonical"] and not st["resolved_via_alias"] and st["adapter"] not in ("single_series",)
    dv = src["dividend"]
    assert dv["canonical"] and not dv["resolved_via_alias"] and dv["adapter"] == "canonical"
    assert dv["extra"]["status_counts"] == [["actual", 2922], ["estimate", 38]]
    assert "Q3 2026 estimate = 21.13 index points" in dv["extra"]["build_provenance"]["summary"]["estimate_2026"]
    gd = src["gold"]
    assert gd["canonical"] is False and gd["adapter"] == "tvc_oanda_proxy" and gd["resolved_via_alias"]
    assert any("Q-002" in w for w in gd["warnings"])


def test_partial_last_stock_week_real_file():
    """SEM-011 / NORM-019 / TEST-051 on the real canonical file: its last row is the week
    ending 2026-09-25 (source_date 2026-09-21); with as_of 2026-09-22 it never reaches the
    signal calculation, from as_of 2026-09-25 it is a completed week."""
    weeks = {}
    for as_of in ("2026-09-22", "2026-09-29"):
        cfg = ResolvedConfig("signals", cli_layer={"run": {"asset": "stocks", "as_of_date": as_of,
                                                           "start": "2026-01-02"}})
        res = run_signals(cfg, write=False)
        weeks[as_of] = ([r.week_key for r in res.records], res.dropped_incomplete_weeks)
    assert weeks["2026-09-22"][0][-1] == D("2026-09-18") and weeks["2026-09-22"][1] == 1
    assert weeks["2026-09-29"][0][-1] == D("2026-09-25") and weeks["2026-09-29"][1] == 0


def test_btc_manifest_keeps_raw_dates_and_transformation(tmp_path):
    """Q-005: raw dates and applied transformation recorded."""
    cfg = ResolvedConfig("signals", cli_layer={"run": {"asset": "btc", "as_of_date": "2026-09-29"},
                                               "report": {"output_dir": str(tmp_path)}})
    res = run_signals(cfg)
    m = json.loads((res.output_dir / "data_manifest.json").read_text())["sources"][0]
    assert m["raw_first_date"] == "2010-07-12" and m["first_key"] == "2011-07-08"
    assert m["date_convention"] == "raw_monday_week_start" and len(m["excluded"]) == 51
    assert "source_week_start + 4" in m["transformations"][0]
    norm = (res.output_dir / "weekly_normalized.csv").read_text().splitlines()
    assert norm[1].startswith("btc,2011-07-08,") and ",2011-07-04," in norm[1]


def test_dividend_status_boundary_real_file():
    """SEM-007 / DIV-011 on the canonical dividend file: a taxed stock run across the status
    boundary keeps 2025-12-26 actual and 2026-01-02 estimate in tax_events; the estimate block
    is one allow_with_warning warning; actual_only ends the dividend source at 2025-12-26
    (reported NORM-011 truncation); error_on_estimate fails."""
    import pytest
    from src.app import run_portfolio
    from src.errors import DividendModeError
    base = {"allocation": {"targets": "stocks=0.6,gold=0.2,btc=0.2"}, "tax": {"profile": "individual_pl"},
            "run": {"start": "2025-06-06", "end": "2026-02-27", "as_of_date": "2026-09-29"}}
    res = run_portfolio(ResolvedConfig("run", cli_layer=base), write=False)
    status = {e.week_key: e.source_status for e in res.tax_state.tax_events if e.event_type == "dividend_tax"}
    assert status[D("2025-12-26")] == "actual" and status[D("2026-01-02")] == "estimate"
    assert {v for k, v in status.items() if k <= D("2025-12-26")} == {"actual"}
    assert {v for k, v in status.items() if k >= D("2026-01-02")} == {"estimate"}
    warn = [i for i in res.report.issues if i.code == "dividend_estimate"]
    assert [w.message for w in warn] == ["dividend status=estimate used for 2026-01-02..2026-02-27 "
                                         "(tax.dividend_estimate_policy=allow_with_warning)"]
    only = run_portfolio(ResolvedConfig("run", cli_layer={
        **base, "tax": {"profile": "individual_pl", "dividend_estimate_policy": "actual_only"}}), write=False)
    assert only.engine.weeks[-1].week_key == D("2025-12-26")
    assert any(i.code == "range_truncated" and i.role == "dividend" for i in only.report.issues)
    assert {e.source_status for e in only.tax_state.tax_events if e.event_type == "dividend_tax"} == {"actual"}
    with pytest.raises(DividendModeError):
        run_portfolio(ResolvedConfig("run", cli_layer={
            **base, "tax": {"profile": "individual_pl", "dividend_estimate_policy": "error_on_estimate"}}),
            write=False)


def test_dividend_range_follows_norm019_completion():
    """Regression (session 15): the dividend source takes part in NORM-011 after the same
    NORM-019 completion as every other source - weeks after min(run.end, as_of) are not part of
    its range. A canonical file reaching past run.end no longer reports a false end
    truncation; rows after as_of are dropped as incomplete weeks (no numerical effect)."""
    from src.app import run_portfolio
    base = {"allocation": {"targets": "stocks=1.0"}, "tax": {"profile": "individual_pl"}}
    res = run_portfolio(ResolvedConfig("run", cli_layer={
        **base, "run": {"start": "2025-01-03", "end": "2026-07-31", "as_of_date": "2026-09-29"}}), write=False)
    trunc = [i.message for i in res.report.issues if i.code == "range_truncated" and i.role == "dividend"]
    assert not any("end truncated" in m for m in trunc)
    assert res.engine.weeks[-1].week_key == D("2026-07-31")
    late = run_portfolio(ResolvedConfig("run", cli_layer={
        **base, "run": {"start": "2025-01-03", "as_of_date": "2026-09-15"}}), write=False)
    dropped = [i.week_key for i in late.report.issues if i.code == "incomplete_week_dropped"
               and i.role == "dividend"]
    assert dropped == [D("2026-09-18")]
    assert late.engine.weeks[-1].week_key == D("2026-08-28")              # FF end, unchanged
    trunc = [i.message for i in late.report.issues if i.code == "range_truncated" and i.role == "dividend"]
    assert any("end truncated from 2026-09-11 to common 2026-08-28" in m for m in trunc)
