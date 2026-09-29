"""Staged clean-room data under the canonical/proxy adapters (realdata)."""
import datetime as dt
import json

from src.app import run_signals
from src.config import ResolvedConfig
from src.data_loader import load_role
from src.validation import alignment_correlation

D = dt.date.fromisoformat


def test_ff_alignment_correlation():
    """NORM-018: FF returns correlate with the stock price index on the same week key, not on
    a week-shifted index."""
    cfg = ResolvedConfig()
    ff = load_role(cfg, "stocks_return")
    stocks = load_role(cfg, "stocks_price")
    assert alignment_correlation(ff.stock_total.points, stocks) > 0.98
    shifted = stocks.replace_points([p.__class__(p.week_key + dt.timedelta(days=7), p.price,
                                                 p.available_at, p.source_date) for p in stocks.points])
    assert abs(alignment_correlation(ff.stock_total.points, shifted)) < 0.1


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
    """SEM-001 evidence status + REP-008: declared segments and the unmet provenance are in
    data_manifest.json; alias use is reported."""
    cfg = ResolvedConfig("signals", cli_layer={"run": {"asset": "stocks", "as_of_date": "2026-09-29",
                                                       "start": "2020-01-03"},
                                               "report": {"output_dir": str(tmp_path)}})
    res = run_signals(cfg)
    m = json.loads((res.output_dir / "data_manifest.json").read_text())
    src = m["sources"][0]
    assert src["resolved_via_alias"] and src["canonical"] is False
    assert [s["source"] for s in src["segments"]] == ["Schwert", "SPX (rebased, inferred)"]
    assert "SEM-001" in src["warnings"][0]
    assert m["as_of_date"] == "2026-09-29" and len(src["sha256"]) == 64
    assert src["raw_rows"] == 5568 and src["first_key"] == "1920-01-02"      # REPRO-002/003
    assert len(m["code_version"]["git_commit"]) == 40                       # REPRO-004
    assert m["timezone"] == "Europe/Warsaw" and m["run_timestamp"].endswith(("+01:00", "+02:00"))
    rows = (res.output_dir / "validation_report.csv").read_text()
    assert "default_file_alias" in rows and "missing_week" in rows


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
