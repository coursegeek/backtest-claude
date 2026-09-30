"""Reporting of the summary (REAL-005)."""
import csv
import shutil

from fixtures.builders import STAGED
from src.app import run_portfolio
from src.config import ResolvedConfig


def test_custom_cpi_label(tmp_path):
    """REAL-005: another CPI file (same SCHEMA-007 columns) and cpi.label replace the US CPI
    without code changes; the summary uses that label and no US-CPI warning."""
    f = tmp_path / "gus_cpi.csv"
    shutil.copy(STAGED / "CPIAUCNS.csv", f)
    cfg = ResolvedConfig("run", cli_layer={
        "allocation": {"targets": "stocks=1.0"},
        "run": {"start": "2019-01-01", "end": "2019-12-31", "as_of_date": "2026-09-29"},
        "data": {"cpi_file": str(f)}, "cpi": {"label": "GUS CPI (test fixture)"},
        "report": {"output_dir": str(tmp_path), "run_name": "cpi"}})
    out = run_portfolio(cfg).output_dir
    row = next(csv.DictReader((out / "summary.csv").open(encoding="utf-8")))
    assert row["cpi_label"] == "GUS CPI (test fixture)"
    assert row["real_return_warning"] == "Real returns deflated by GUS CPI (test fixture)"
    assert row["real_cagr"] != ""
