import datetime as dt

import pytest

from fixtures.builders import STAGED, ff_text, price_series, staged_proxy_layer, write_csv
from src.config import ResolvedConfig
from src.data_loader import (compose_stock_signal, load_cpi, load_ff, load_gold, load_role,
                             load_stocks_signal, resolve_source, sem001_check,
                             parse_declared_segments)
from src.errors import ConfigError, DataFileNotFound, DataValidationError, MissingColumns

D = dt.date.fromisoformat


def weekly_rows(n, first_monday="1927-10-03", price=100.0, source=None):
    rows = []
    for i in range(n):
        r = [(D(first_monday) + dt.timedelta(days=7 * i)).isoformat(), repr(price + i)]
        if source:
            r.append(source(i))
        rows.append(r)
    return rows


def test_stocks_signal_accepts_week_start_or_week_end(tmp_path):
    """SCHEMA-001, NORM-008."""
    a = load_stocks_signal(write_csv(tmp_path / "a.csv", ["week_start", "price_index_continuous"],
                                     weekly_rows(3)))
    b = load_stocks_signal(write_csv(tmp_path / "b.csv", ["week_end", "price_index_continuous"],
                                     [[(D("1927-10-07") + dt.timedelta(days=7 * i)).isoformat(),
                                       repr(100.0 + i)] for i in range(3)]))
    assert a.keys() == b.keys() == (D("1927-10-07"), D("1927-10-14"), D("1927-10-21"))
    with pytest.raises(DataValidationError):
        load_stocks_signal(write_csv(tmp_path / "c.csv", ["week_start", "price_index_continuous"],
                                     [["1927-10-04", "1"]]))


def test_stocks_signal_segments_and_sem001(tmp_path):
    """SEM-001: segments from a source column; Schwert before 1928 and SPX from 1928 satisfy
    the rule, the staged 1962 splice does not."""
    rows = weekly_rows(20, source=lambda i: "Schwert" if i < 13 else "SPX")
    s = load_stocks_signal(write_csv(tmp_path / "s.csv", ["week_start", "price_index_continuous",
                                                           "source"], rows))
    assert [(x.source, x.first_key, x.last_key) for x in s.provenance.segments] == [
        ("Schwert", D("1927-10-07"), D("1927-12-30")), ("SPX", D("1928-01-06"), D("1928-02-17"))]
    assert s.provenance.canonical and not s.provenance.warnings
    staged = load_role(ResolvedConfig(cli_layer=staged_proxy_layer()), "stocks_price")
    assert staged.provenance.path.endswith("US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv")
    assert not staged.provenance.canonical
    assert "SEM-001 provenance not satisfied" in staged.provenance.warnings[0]
    canonical = load_role(ResolvedConfig(), "stocks_price")          # default resolution
    assert canonical.provenance.canonical and not canonical.provenance.warnings
    late = parse_declared_segments([{"source": "Schwert", "from": "1920-01-02", "to": "1962-06-29",
                                     "verified": True},
                                    {"source": "SPX", "from": "1962-07-06", "to": "2026-09-18",
                                     "verified": True}])
    assert sem001_check(late)[0] is False


def test_compose_schwert_spx_rebased():
    """SEM-001 canonical composition: SPX rebased to Schwert at the last common pre-1928 week."""
    schwert = price_series([10.0, 11.0, 12.0], first_key="1927-12-16")
    spx = price_series([20.0, 24.0, 26.0, 28.0], first_key="1927-12-23")
    c = compose_stock_signal(schwert, spx)
    assert [p.price for p in c.points] == [10.0, 11.0, 12.0, 13.0, 14.0]
    assert [p.source for p in c.points] == ["Schwert"] * 3 + ["SPX"] * 2
    assert c.provenance.canonical and c.provenance.segments[1].rebase_factor == 0.5
    assert dict(c.provenance.extra)["rebase_anchor"] == D("1927-12-30")


def test_ff_malformed_and_missing_header(tmp_path):
    p = tmp_path / "ff.csv"
    p.write_text(ff_text([("20200103", 1.0, 0, 0, 0.01)]) + "20200110, x, 1, 1, 1\n", encoding="utf-8")
    ff = load_ff(p)
    assert len(ff.stock_total.points) == 1
    assert [i.code for i in ff.provenance.issues] == ["ff_malformed_record"]
    q = tmp_path / "nohdr.csv"
    q.write_text("20200103, 1, 0, 0, 0.01\n", encoding="utf-8")
    with pytest.raises(MissingColumns):
        load_ff(q)


def test_gold_parser_week_start_or_week_end(tmp_path):
    """DATA-003/SCHEMA-003/SIG-009 canonical LBMA adapters; the proxy needs explicit opt-in."""
    wk = load_gold(write_csv(tmp_path / "w.csv", ["week_end", "gold_pm_usd", "source"],
                             [["2020-01-03", "1500", "LBMA PM"], ["2020-01-09", "1510", "LBMA PM"]]))
    assert wk.provenance.canonical and wk.keys() == (D("2020-01-03"), D("2020-01-10"))
    assert wk.points[1].available_at == D("2020-01-09")         # Thursday fixing in holiday week
    daily = load_gold(write_csv(tmp_path / "d.csv", ["date", "gold_pm_usd"],
                                [["2020-01-06", "1"], ["2020-01-09", "2"], ["2020-01-10", "3"],
                                 ["2020-01-13", "4"]]))
    assert [(p.week_key, p.price) for p in daily.points] == [(D("2020-01-10"), 3.0), (D("2020-01-17"), 4.0)]
    proxy_path = write_csv(tmp_path / "p.csv", ["week_start", "gold_pm_usd", "weekly_return", "source"],
                           [["2020-01-06", "100", "", "OANDA"], ["2020-01-13", "110", "0.1", "OANDA"]])
    with pytest.raises(MissingColumns, match="tvc_oanda_proxy"):
        load_gold(proxy_path)
    proxy = load_gold(proxy_path, adapter="tvc_oanda_proxy")
    assert not proxy.provenance.canonical and "Q-002" in proxy.provenance.warnings[-1]
    non_lbma = load_gold(write_csv(tmp_path / "n.csv", ["week_end", "gold_pm_usd", "source"],
                                   [["2020-01-03", "1500", "TVC"]]))
    assert not non_lbma.provenance.canonical


def test_gold_return_computed_when_missing(tmp_path):
    """SCHEMA-003: weekly_return optional; if present it must match the price ratio."""
    ok = load_gold(write_csv(tmp_path / "a.csv", ["week_end", "gold_pm_usd", "weekly_return"],
                             [["2020-01-03", "100", ""], ["2020-01-10", "110", "0.1"]]))
    assert ok.provenance.issues == ()
    bad = load_gold(write_csv(tmp_path / "b.csv", ["week_end", "gold_pm_usd", "weekly_return"],
                              [["2020-01-03", "100", ""], ["2020-01-10", "110", "0.2"]]))
    assert [i.code for i in bad.provenance.issues] == ["weekly_return_mismatch"]


def test_btc_return_price_ratio(tmp_path):
    """PORT-004."""
    rows = [["2020-01-10", "100", "", "2020-01-12", "2020-01-06"],
            ["2020-01-17", "125", "0.25", "2020-01-19", "2020-01-13"]]
    from src.data_loader import load_btc
    s = load_btc(write_csv(tmp_path / "b.csv", ["date", "price", "weekly_return", "close_date",
                                                "source_week_start"], rows))
    assert s.returns()[0].value == 0.25


def test_cpi_parser(tmp_path):
    """SCHEMA-007."""
    cpi = load_cpi(STAGED / "CPIAUCNS.csv")
    assert cpi.values[(1913, 1)] == 9.8 and cpi.values[(2026, 8)] == 334.98
    with pytest.raises(MissingColumns):
        load_cpi(write_csv(tmp_path / "c.csv", ["date", "cpi"], [["2020-01-01", "1"]]))


def test_missing_file_message(tmp_path):
    """ERR-001: message names the path and the config key."""
    with pytest.raises(DataFileNotFound) as exc:
        load_gold(tmp_path / "nope.csv", config_key="data.gold_file")
    assert "nope.csv" in str(exc.value) and "data.gold_file" in str(exc.value)


def test_duplicates_unsorted_and_numeric(tmp_path):
    """NORM-001, NORM-002, NORM-006."""
    rows = [["1927-10-10", "2"], ["1927-10-03", "1"], ["1927-10-10", "3"]]
    p = write_csv(tmp_path / "s.csv", ["week_start", "price_index_continuous"], rows)
    with pytest.raises(DataValidationError, match="NORM-002"):
        load_stocks_signal(p)
    last = load_stocks_signal(p, duplicates="last")
    first = load_stocks_signal(p, duplicates="first")
    assert [x.price for x in last.points] == [1.0, 3.0] and [x.price for x in first.points] == [1.0, 2.0]
    assert {i.code for i in last.provenance.issues} == {"input_unsorted", "duplicate_week"}
    for bad in ("nan", "inf", "abc", ""):
        with pytest.raises(DataValidationError):
            load_stocks_signal(write_csv(tmp_path / f"x{len(bad)}.csv",
                                         ["week_start", "price_index_continuous"], [["1927-10-03", bad]]))


def test_default_file_resolution(tmp_path):
    """DATA-001..007/Q-001: an existing spec default file is used directly (the canonical
    stock signal and dividend files, Q-004/Q-008); an absent default resolves through the
    explicit staged alias (gold, BTC), recorded as resolved_via_alias."""
    cfg = ResolvedConfig()
    for role, name in (("stocks_price", "US_STOCK_PRICE_WEEKLY_1885_2026.csv"),
                       ("dividend", "SPX_dividend_return_weekly_1970_2026.csv"),
                       ("stocks_return", "F-F_Research_Data_Factors_weekly.csv"),
                       ("cpi", "CPIAUCNS.csv")):
        src = resolve_source(cfg, role)
        assert src["path"].name == name and not src["resolved_via_alias"] and not src["alias"]
    assert resolve_source(cfg, "dividend")["adapter"] == "canonical"
    for role, staged in (("gold", "GOLD_REAL_weekly_1970_2026.csv"),
                         ("btc", "BTC_REAL_weekly_2010_2026.csv")):
        src = resolve_source(cfg, role)
        assert src["path"].name == staged and src["resolved_via_alias"]
    # CLI-005 passes the spec default dividend file name explicitly -> the canonical file
    cli = ResolvedConfig(cli_layer={"data": {"dividend_file": "SPX_dividend_return_weekly_1970_2026.csv"}})
    assert not resolve_source(cli, "dividend")["resolved_via_alias"]
    # the alias table stays a fallback for a data.dir without the canonical defaults
    for name in ("US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv", "SPX_dividend_return_weekly_shiller.csv"):
        (tmp_path / name).symlink_to(STAGED / name)
    old = ResolvedConfig(cli_layer={"data": {"dir": str(tmp_path)}})
    for role, staged, adapter in (("stocks_price", "US_STOCK_PRICE_WEEKLY_REAL_1919_2026.csv", "single_series"),
                                  ("dividend", "SPX_dividend_return_weekly_shiller.csv", "shiller_proxy")):
        src = resolve_source(old, role)
        assert src["path"].name == staged and src["resolved_via_alias"] and src["adapter"] == adapter


def test_data_file_override(tmp_path):
    """DATA-008: generic override by role key; unknown keys fail."""
    p = write_csv(tmp_path / "moj_spx.csv", ["week_start", "price_index_continuous"], weekly_rows(3))
    cfg = ResolvedConfig(cli_layer={"data": {"overrides": {"stocks_price": str(p)}}})
    src = resolve_source(cfg, "stocks_price")
    assert src["path"] == p and src["config_key"] == "data.overrides.stocks_price"
    with pytest.raises(ConfigError):
        resolve_source(ResolvedConfig(cli_layer={"data": {"overrides": {"spx": str(p)}}}), "stocks_price")
    with pytest.raises(DataFileNotFound, match="data.overrides.gold"):
        load_role(ResolvedConfig(cli_layer={"data": {"overrides": {"gold_file": "missing.csv"}}}), "gold")


def test_missing_columns_message(tmp_path):
    """ERR-002: the message names the file, the missing columns and the config key."""
    p = write_csv(tmp_path / "g.csv", ["week_end", "price"], [["2020-01-03", "1"]])
    with pytest.raises(MissingColumns) as exc:
        load_gold(p, config_key="data.gold_file")
    msg = str(exc.value)
    assert "gold_pm_usd" in msg and "g.csv" in msg and "data.gold_file" in msg


def test_stocks_missing_columns_lists_only_absent(tmp_path):
    p = write_csv(tmp_path / "s.csv", ["week_end", "price"], [["2020-01-03", "1"]])
    with pytest.raises(MissingColumns) as exc:
        load_stocks_signal(p)
    assert exc.value.missing == ["price_index_continuous"]


def test_stocks_source_date_is_metadata_only(tmp_path):
    """A source_date column (canonical stock file) is kept as observation metadata: the week
    key and the information availability stay the Friday week_end (NORM-019); a source_date
    outside the calendar week of its row is an error (NORM-013)."""
    cols = ["week_end", "price_index_continuous", "source", "source_date"]
    rows = [["1927-12-30", "410.0", "Schwert", "1927-12-31"], ["1928-01-06", "410.0", "SPX", "1928-01-02"],
            ["1928-01-13", "408.0", "SPX", "1928-01-09"]]
    s = load_stocks_signal(write_csv(tmp_path / "s.csv", cols, rows))
    assert [p.source_date for p in s.points] == [D("1927-12-31"), D("1928-01-02"), D("1928-01-09")]
    assert [p.available_at for p in s.points] == [p.week_key for p in s.points]
    assert [(g.source, g.first_key, g.last_key, g.rows) for g in s.provenance.segments] == [
        ("Schwert", D("1927-12-30"), D("1927-12-30"), 1), ("SPX", D("1928-01-06"), D("1928-01-13"), 2)]
    rows[1][3] = "1928-01-09"                                       # Monday of the next week
    with pytest.raises(DataValidationError, match="NORM-013"):
        load_stocks_signal(write_csv(tmp_path / "bad.csv", cols, rows))


def test_provenance_sidecar(tmp_path):
    """REP-008: a <file>.provenance.json whose final.sha256 describes the file is carried into
    the provenance (raw-input hashes, summary, splice; SPX segment rebase factor) without
    changing any value; a sidecar for other content is ignored with a warning."""
    import hashlib
    import json
    cols = ["week_end", "price_index_continuous", "source", "source_date"]
    path = write_csv(tmp_path / "s.csv", cols, [["1927-12-30", "410.0", "Schwert", "1927-12-31"],
                                                ["1928-01-06", "410.0", "SPX", "1928-01-02"]])
    plain = load_stocks_signal(path)
    side = path.with_name(path.name + ".provenance.json")
    doc = {"final": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}, "data_status": "canonical",
           "raw_inputs": [{"name": "raw.zip", "sha256": "ab" * 32, "other": "dropped"}],
           "summary": {"segments": "Schwert..SPX"},
           "construction": {"splice": {"first_spx_week": "1928-01-06", "rebase_anchor_week": "1927-12-30",
                                       "rebase_factor": "2.5", "rebase_factor_float": 2.5}}}
    side.write_text(json.dumps(doc), encoding="utf-8")
    s = load_stocks_signal(path)
    bp = dict(s.provenance.extra)["build_provenance"]
    assert bp["file"] == side.name and bp["data_status"] == "canonical"
    assert bp["raw_inputs"] == ({"name": "raw.zip", "sha256": "ab" * 32},)
    assert bp["summary"] == {"segments": "Schwert..SPX"} and bp["splice"]["rebase_factor"] == "2.5"
    assert [g.rebase_factor for g in s.provenance.segments] == [None, 2.5]
    assert s.points == plain.points and s.provenance.canonical
    doc["final"]["sha256"] = "0" * 64
    side.write_text(json.dumps(doc), encoding="utf-8")
    other = load_stocks_signal(path)
    assert "build_provenance" not in dict(other.provenance.extra)
    assert any("describes a different file" in w for w in other.provenance.warnings)


def test_dividend_values_and_status_blocks(tmp_path):
    """SCHEMA-005 / SEM-007 / DIV-011: negative dividend fields are validation errors, the
    status blocks are recorded and an actual block after an estimate block is reported."""
    from src.data_loader import load_dividend
    cols = ["date", "dividend_return", "dividend_points", "spx_close_prev", "spx_close", "year",
            "annual_yield_pct", "trailing_dps_points", "status"]
    rows = []
    for i, st in enumerate(["actual", "actual", "estimate", "estimate", "actual"]):
        day = D("1970-01-02") + dt.timedelta(days=7 * i)
        rows.append([day.isoformat(), repr(0.05 / 90.0), "0.05", "90.0", "90.0", "1970", "3.0", "2.6", st])
    s = load_dividend(write_csv(tmp_path / "d.csv", cols, rows))
    extra = dict(s.provenance.extra)
    assert extra["status_blocks"] == (("actual", D("1970-01-02"), D("1970-01-09")),
                                      ("estimate", D("1970-01-16"), D("1970-01-23")),
                                      ("actual", D("1970-01-30"), D("1970-01-30")))
    assert [(i.code, i.week_key) for i in s.provenance.issues] == [("dividend_status_order", D("1970-01-30"))]
    rows = rows[:4]
    rows[1][1], rows[1][2] = repr(-0.05 / 90.0), "-0.05"
    bad = load_dividend(write_csv(tmp_path / "neg.csv", cols, rows))
    assert [(i.code, i.severity.value) for i in bad.provenance.issues] == [("dividend_value", "ERROR")]
