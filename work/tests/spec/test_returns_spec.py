"""TEST-007, TEST-008 (TEST-013 needs the tax module and is not implemented yet)."""
from fixtures.builders import ff_text, write_csv
from src.data_loader import load_ff, load_gold


def test_ff_percent_to_decimal(tmp_path):
    """TEST-007 / PORT-001, PORT-002: R_stock=(Mkt-RF+RF)/100 and R_rf=RF/100."""
    p = tmp_path / "ff.csv"
    p.write_text(ff_text([("19260702", 1.58, -0.61, -0.90, 0.06)]), encoding="utf-8")
    ff = load_ff(p)
    assert abs(ff.stock_total.points[0].value - 0.0164) < 1e-15
    assert abs(ff.rf.points[0].value - 0.0006) < 1e-15


def test_gold_return_price_ratio(tmp_path):
    """TEST-008 / PORT-003, SCHEMA-003: returns are LBMA price ratios; a missing weekly_return
    column is computed from prices."""
    p = write_csv(tmp_path / "gold.csv", ["week_end", "gold_pm_usd"],
                  [["2020-01-03", "100"], ["2020-01-10", "110"], ["2020-01-17", "99"]])
    rets = [r.value for r in load_gold(p).returns()]
    assert [round(x, 15) for x in rets] == [0.1, -0.1]
