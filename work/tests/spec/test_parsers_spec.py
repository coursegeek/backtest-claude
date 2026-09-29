"""TEST-027, TEST-028, TEST-038, TEST-039, TEST-044."""
import datetime as dt
import zipfile

import pytest

from fixtures.builders import STAGED, ff_text, write_csv
from src.config import ResolvedConfig
from src.data_loader import load_btc, load_cpi, load_dividend, load_ff, load_role
from src.errors import DataValidationError, MissingColumns
from src.models import Severity
from src.validation import validate_btc_canonical, validate_dividend_series

D = dt.date.fromisoformat
FF = STAGED / "F-F_Research_Data_Factors_weekly.csv"


def test_ff_parser_nondata(tmp_path):
    """TEST-027 / NORM-014: only YYYYMMDD records are read; preamble, header and copyright
    footer are ignored, whatever their length."""
    ff = load_ff(FF)
    pts = ff.stock_total.points
    extra = dict(ff.provenance.extra)
    assert len(pts) == 5226 and extra["records"] == 5226
    assert pts[0].source_date == D("1926-07-02") and pts[-1].source_date == D("2026-08-28")
    assert extra["nondata_lines"] == (1, 2, 3, 4, 5, 5232, 5233)   # preamble, header, footer
    p = tmp_path / "ff.csv"
    text = ff_text([("20200103", 1.0, 0, 0, 0.01), ("20200110", -2.0, 0, 0, 0.01)])
    p.write_text("extra title line\n" + text + "Some note 2026\n12345\n", encoding="utf-8")
    assert [x.source_date for x in load_ff(p).stock_total.points] == [D("2020-01-03"), D("2020-01-10")]


def test_cpi_missing_policy(tmp_path):
    """TEST-028 / NORM-015, SEM-010: empty 2025-10 uses the previous available value with an
    imputation flag; the error policy fails."""
    cpi = load_cpi(STAGED / "CPIAUCNS.csv")
    assert cpi.values[(2025, 10)] == cpi.values[(2025, 9)] == 324.8
    assert (2025, 10) in cpi.imputed and len(cpi.imputed) == 1
    assert any(i.code == "cpi_imputed" for i in cpi.provenance.issues)
    with pytest.raises(DataValidationError):
        load_cpi(STAGED / "CPIAUCNS.csv", missing_policy="error")


def canonical_dividend_rows(n=6, start="1970-01-02"):
    rows, close = [], 90.0
    for i in range(n):
        day = D(start) + dt.timedelta(days=7 * i)
        prev, close = close, close * 1.001
        pts = 0.05
        rows.append([day.isoformat(), repr(pts / prev), repr(pts), repr(prev), repr(close),
                     str(day.year), "3.1", "2.6", "actual"])
    return rows


def test_dividend_input_file(tmp_path):
    """TEST-038 / DIV-012, SCHEMA-005: a canonical file validates as continuous Friday weekly
    series with correct formula. The staged Shiller file is only a non-canonical proxy (Q-008):
    the canonical loader rejects it; its proxy load is continuous from 1970-01-02 but is never
    canonical evidence."""
    cols = ["date", "dividend_return", "dividend_points", "spx_close_prev", "spx_close", "year",
            "annual_yield_pct", "trailing_dps_points", "status"]
    good = load_dividend(write_csv(tmp_path / "div.csv", cols, canonical_dividend_rows()))
    assert good.provenance.canonical
    assert validate_dividend_series(good) == []
    bad_rows = canonical_dividend_rows()
    bad_rows[3][1] = "0.5"
    bad = load_dividend(write_csv(tmp_path / "bad.csv", cols, bad_rows))
    assert [i.code for i in validate_dividend_series(bad)] == ["dividend_formula"]
    gap_rows = canonical_dividend_rows()
    del gap_rows[2]
    gap = load_dividend(write_csv(tmp_path / "gap.csv", cols, gap_rows))
    assert [i.code for i in validate_dividend_series(gap)] == ["missing_week"]
    staged = STAGED / "SPX_dividend_return_weekly_shiller.csv"
    with pytest.raises(MissingColumns):
        load_dividend(staged)
    proxy = load_dividend(staged, adapter="shiller_proxy")
    assert proxy.provenance.canonical is False
    assert validate_dividend_series(proxy, from_date=D("1970-01-02")) == []


def test_btc_input_file():
    """TEST-039 / SCHEMA-004, SEM-008 on the canonical normalised representation (Q-005):
    Friday keys, Sunday close_date = key + 2, 794 rows 2011-07-08..2026-09-18, no gaps,
    weekly_return = price_t / price_t-1 - 1."""
    s = load_role(ResolvedConfig(), "btc")
    assert s.provenance.canonical and s.provenance.adapter == "raw_monday_week_start"
    assert validate_btc_canonical(s, (D("2011-07-08"), D("2026-09-18")), 794) == []
    rets = s.returns()
    assert len(rets) == 793
    raw = {r[0]: r for r in (line.split(",") for line in
                             (STAGED / "BTC_REAL_weekly_2010_2026.csv").read_text().splitlines()[1:])}
    for r in rets:
        assert abs(r.value - float(raw[(r.week_key - dt.timedelta(days=4)).isoformat()][2])) < 1e-8
    assert len(s.provenance.excluded) == 51 and s.provenance.transformations[0].startswith("source_week_start")


def test_btc_raw_monday_adapter(tmp_path):
    """Q-005 raw adapter: Monday date == source_week_start -> Friday key (+4) in the same
    calendar week, close_date Sunday (+6 raw / +2 key); raw date kept as source_date."""
    rows = [["2020-01-06", "100", "", "2020-01-12", "2020-01-06"],
            ["2020-01-13", "110", "0.1", "2020-01-19", "2020-01-13"]]
    s = load_btc(write_csv(tmp_path / "raw.csv", ["date", "price", "weekly_return", "close_date",
                                                  "source_week_start"], rows))
    assert [p.week_key for p in s.points] == [D("2020-01-10"), D("2020-01-17")]
    assert [p.source_date for p in s.points] == [D("2020-01-06"), D("2020-01-13")]
    assert [p.available_at for p in s.points] == [D("2020-01-12"), D("2020-01-19")]
    canon = [["2020-01-10", "100", "", "2020-01-12", "2020-01-06"],
             ["2020-01-17", "110", "0.1", "2020-01-19", "2020-01-13"]]
    c = load_btc(write_csv(tmp_path / "canon.csv", ["date", "price", "weekly_return",
                                                    "close_date", "source_week_start"], canon))
    assert c.provenance.adapter == "canonical_friday"
    strip = lambda pts: [(p.week_key, p.price, p.available_at) for p in pts]
    assert strip(c.points) == strip(s.points)
    rows[1][3] = "2020-01-18"
    with pytest.raises(DataValidationError):
        load_btc(write_csv(tmp_path / "bad.csv", ["date", "price", "weekly_return", "close_date",
                                                  "source_week_start"], rows))


def test_ff_csv_zip_equivalence(tmp_path):
    """TEST-044 / DATA-010: the ZIP holding the same CSV yields identical normalised records."""
    z = tmp_path / "F-F_Research_Data_Factors_weekly_CSV.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(FF, "F-F_Research_Data_Factors_weekly.csv")
    a, b = load_ff(FF), load_ff(z)
    assert a.stock_total.points == b.stock_total.points and a.rf.points == b.rf.points
    assert b.provenance.adapter == "fama_french_zip"
