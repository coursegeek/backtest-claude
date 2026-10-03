"""Source parsers and canonical normalisation (DATA-*, SCHEMA-*, NORM-001/002/006/008/009/
013/014/015, PORT-001..004, SEM-001..011).

Every loader returns an immutable series whose ``Provenance`` records the file hash, the
adapter used, the date convention, all transformations, excluded rows, source segments,
warnings and validation issues. Non-canonical proxy adapters (staged clean-room files that
do not satisfy the specification's data contract) set ``canonical=False``.
"""
from __future__ import annotations

import csv
import datetime as dt
import fnmatch
import hashlib
import io
import json
import math
import re
import zipfile
from pathlib import Path
from typing import Optional

import yaml

from .calendar import WEEK, aggregate_daily_last, friday_key, week_start_to_key
from .errors import (DataFileNotFound, DataValidationError, DividendModeError, MissingColumns,
                     ConfigError)
from .models import (CpiSeries, DistributionSchedule, DividendPoint, DividendSeries, FFData,
                     PricePoint, PriceSeries, ScheduledDistribution,
                     Provenance, ReturnPoint, ReturnSeries, Severity, SourceSegment,
                     ValidationIssue)

ROLE_CONFIG_KEYS = {
    "stocks_price": "data.stocks_price_file",
    "stocks_return": "data.stocks_return_file",
    "gold": "data.gold_file",
    "btc": "data.btc_file",
    "dividend": "data.dividend_file",
    "cpi": "data.cpi_file",
}
TOL_RETURN = 1e-8
TOL_DIVIDEND_REL = 1e-9
SEM001_SPLICE_YEAR = 1928
FF_MEMBER_PATTERN = "F-F_Research_Data_Factors_weekly*.csv"
DIVIDEND_CANONICAL_COLUMNS = ["date", "dividend_return", "dividend_points", "spx_close_prev",
                              "spx_close", "year", "annual_yield_pct", "trailing_dps_points",
                              "status"]                                          # SCHEMA-005
DIVIDEND_PROXY_COLUMNS = ["date", "dividend_return", "dividend_points", "spx_close_prev", "status"]
DIVIDEND_STATUSES = {"actual", "estimate"}
BTC_COLUMNS = ["date", "price", "weekly_return", "close_date", "source_week_start"]  # SCHEMA-004


# ============================================================================ raw reading
def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_bytes(path, config_key: str) -> bytes:
    p = Path(path)
    if not p.is_file():
        raise DataFileNotFound(p, config_key)
    return p.read_bytes()


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def read_table(path, config_key: str):
    raw = read_bytes(path, config_key)
    text = decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]
    return header, rows, raw


def require(header, required, path, config_key, hint=""):
    missing = [c for c in required if c not in header]
    if missing:
        raise MissingColumns(path, missing, required, config_key, hint)


def to_date(value: str, column: str, path) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise DataValidationError(f"{path}: column {column}: invalid date {value!r}") from exc


def to_float(value: str, column: str, path, where: str = "") -> float:
    """NORM-006: NaN, inf and non-numeric values are errors unless a source policy says
    otherwise (handled by the caller)."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"{path}: column {column}{where}: non-numeric value {value!r}") from exc
    if math.isnan(f) or math.isinf(f):
        raise DataValidationError(f"{path}: column {column}{where}: non-finite value {value!r}")
    return f


def issue(sev, code, role, msg, key=None, reqs=""):
    return ValidationIssue(sev, code, role, msg, key, reqs)


def normalise_points(points, role: str, path, duplicates: str, issues: list):
    """NORM-001 ascending sort (reported when needed) and NORM-002 duplicate policy per key."""
    points = list(points)
    keys = [p.week_key for p in points]
    if keys != sorted(keys):
        issues.append(issue(Severity.INFO, "input_unsorted", role,
                            f"{path}: rows were not in ascending date order; sorted", None,
                            "NORM-001"))
    points.sort(key=lambda p: (p.week_key, getattr(p, "source_date", p.available_at)))
    out = []
    for p in points:
        if out and out[-1].week_key == p.week_key:
            if duplicates == "error":
                raise DataValidationError(
                    f"{path}: duplicate week key {p.week_key} (validation.duplicates=error, NORM-002)")
            issues.append(issue(Severity.WARNING, "duplicate_week", role,
                                f"duplicate week key {p.week_key}; kept {duplicates}", p.week_key,
                                "NORM-002"))
            if duplicates == "last":
                out[-1] = p
            continue
        out.append(p)
    return out


def segments_from_sources(points) -> tuple:
    segs = []
    for p in points:
        if segs and segs[-1][0] == p.source:
            segs[-1][2] = p.week_key
            segs[-1][3] += 1
        else:
            segs.append([p.source, p.week_key, p.week_key, 1])
    return tuple(SourceSegment(s, a, b, n) for s, a, b, n in segs)


def check_price_returns(points, file_returns: dict, role: str, issues: list, reqs: str):
    """Verify a file weekly_return column against price ratios between consecutive weeks."""
    by_key = {p.week_key: p for p in points}
    bad = 0
    for p in points:
        r = file_returns.get(p.week_key)
        prev = by_key.get(p.week_key - WEEK)
        if r is None or prev is None:
            continue
        if abs(r - (p.price / prev.price - 1.0)) > TOL_RETURN:
            bad += 1
            issues.append(issue(Severity.ERROR, "weekly_return_mismatch", role,
                                f"weekly_return {r!r} != price ratio at {p.week_key}", p.week_key, reqs))
    return bad


def provenance_sidecar(path, sha256: str) -> tuple:
    """The build metadata ``<file>.provenance.json`` next to a data file (REP-008): returns
    (document or None, warning or None). A sidecar is used only when its ``final.sha256``
    describes exactly this file; it never changes values, only what the manifest records."""
    p = Path(path)
    side = p.with_name(p.name + ".provenance.json")
    if not side.is_file():
        return None, None
    raw = side.read_bytes()
    try:
        doc = json.loads(decode(raw))
    except ValueError:
        return None, f"provenance sidecar {side.name} is not valid JSON; ignored"
    if (doc.get("final") or {}).get("sha256") != sha256:
        return None, (f"provenance sidecar {side.name} describes a different file "
                      f"(final.sha256 {(doc.get('final') or {}).get('sha256')}); ignored")
    raw_inputs = tuple({k: x[k] for k in ("name", "sha256", "archive_member", "archive_member_sha256")
                        if k in x} for x in doc.get("raw_inputs") or ())
    record = {"file": side.name, "sha256": sha256_bytes(raw), "data_status": doc.get("data_status"),
              "raw_inputs": raw_inputs, "summary": doc.get("summary") or {}}
    splice = (doc.get("construction") or {}).get("splice")
    if splice:
        record["splice"] = {k: splice[k] for k in ("last_schwert_week", "first_spx_week",
                                                   "rebase_anchor_week", "anchor_schwert_value",
                                                   "anchor_spx_raw_close", "rebase_factor",
                                                   "rebase_factor_float") if k in splice}
    return record, None


def _provenance(role, path, raw, config_key, adapter, canonical, raw_rows, points, convention,
                raw_dates, sidecar=None, **kw) -> Provenance:
    digest = sha256_bytes(raw)
    doc, warning = sidecar if sidecar is not None else provenance_sidecar(path, digest)
    if doc:
        kw["extra"] = tuple(kw.get("extra", ())) + (("build_provenance", doc),)
    if warning:
        kw["warnings"] = tuple(kw.get("warnings", ())) + (warning,)
    return Provenance(
        role=role, path=str(path), sha256=digest, config_key=config_key,
        adapter=adapter, canonical=canonical, raw_rows=raw_rows, used_rows=len(points),
        raw_first_date=min(raw_dates) if raw_dates else None,
        raw_last_date=max(raw_dates) if raw_dates else None,
        first_key=points[0].week_key if points else None,
        last_key=points[-1].week_key if points else None,
        date_convention=convention, **kw)


# ============================================================================ stocks signal
def parse_declared_segments(declared) -> tuple:
    out = []
    for s in declared or ():
        try:
            out.append(SourceSegment(source=str(s["source"]),
                                     first_key=dt.date.fromisoformat(str(s["from"])),
                                     last_key=dt.date.fromisoformat(str(s["to"])), rows=0,
                                     verified=bool(s.get("verified", False)),
                                     note=str(s.get("note", ""))))
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"invalid stock signal segment declaration {s!r}") from exc
    return tuple(out)


def sem001_check(segments) -> tuple:
    """SEM-001: Schwert before 1928, SPX from 1928 after rebasing; every segment verified and
    contiguous. Returns (satisfied, reason)."""
    if not segments:
        return False, "no source segments recorded"
    if not all(s.verified for s in segments):
        return False, "segment provenance not verified: " + ", ".join(
            s.source for s in segments if not s.verified)
    first_1928 = next(k for k in (dt.date(SEM001_SPLICE_YEAR, 1, d) for d in range(1, 8))
                      if k.weekday() == 4)
    schwert = [s for s in segments if "schwert" in s.source.lower()]
    spx = [s for s in segments if "spx" in s.source.lower() or "s&p" in s.source.lower()]
    if not schwert or not spx or len(schwert) + len(spx) != len(segments):
        return False, "segments must be Schwert and SPX only"
    if max(s.last_key for s in schwert) >= first_1928:
        return False, f"Schwert segment extends to {max(s.last_key for s in schwert)} (>= 1928)"
    if min(s.first_key for s in spx) != first_1928:
        return False, f"SPX segment starts {min(s.first_key for s in spx)}, expected {first_1928}"
    ordered = sorted(segments, key=lambda s: s.first_key)
    for a, b in zip(ordered, ordered[1:]):
        if b.first_key <= a.last_key:
            return False, "overlapping segments"
    return True, "Schwert before 1928, SPX from 1928"


def load_stocks_signal(path, config_key="data.stocks_price_file", adapter="auto",
                       segments=None, duplicates="error", role="stocks_price",
                       resolved_via_alias=False) -> PriceSeries:
    header, rows, raw = read_table(path, config_key)
    missing = ([] if {"week_start", "week_end"} & set(header) else ["week_start|week_end"]) + \
              ([] if "price_index_continuous" in header else ["price_index_continuous"])
    if missing:
        raise MissingColumns(path, missing, ["week_start|week_end", "price_index_continuous"],
                             config_key)
    issues, points, raw_dates = [], [], []
    use_end = "week_end" in header
    has_source_date = "source_date" in header
    for i, r in enumerate(rows, start=2):
        if use_end:
            d = to_date(r["week_end"], "week_end", path)
            key = friday_key(d)
            if "week_start" in header and r.get("week_start"):
                ws = to_date(r["week_start"], "week_start", path)
                if ws + dt.timedelta(days=4) != key:
                    raise DataValidationError(f"{path}: row {i}: week_start {ws} and week_end {d} "
                                              "are not the same calendar week")
        else:
            d = to_date(r["week_start"], "week_start", path)
            try:
                key = week_start_to_key(d)
            except ValueError as exc:
                raise DataValidationError(f"{path}: row {i}: {exc}") from exc
        price = to_float(r["price_index_continuous"], "price_index_continuous", path, f" row {i}")
        if not price > 0:
            raise DataValidationError(f"{path}: row {i}: price must be > 0")
        observed = d
        if has_source_date and r.get("source_date"):      # metadata only: availability stays key
            observed = to_date(r["source_date"], "source_date", path)
            if friday_key(observed) != key:
                raise DataValidationError(f"{path}: row {i}: source_date {observed} is not in the "
                                          f"calendar week of {key} (NORM-013)")
        raw_dates.append(d)
        points.append(PricePoint(key, price, key, observed, r.get("source", "")))
    points = normalise_points(points, role, path, duplicates, issues)
    sidecar = provenance_sidecar(path, sha256_bytes(raw))
    if "source" in header:
        splice = (sidecar[0] or {}).get("splice") or {}
        segs = []
        for s in segments_from_sources(points):
            note, factor = "from source column", None
            if splice.get("first_spx_week") == s.first_key.isoformat():
                factor = splice.get("rebase_factor_float")
                note += f"; rebased at {splice.get('rebase_anchor_week')} (provenance sidecar)"
            segs.append(SourceSegment(s.source, s.first_key, s.last_key, s.rows, True, note, factor))
        segs = tuple(segs)
    elif segments:
        segs = parse_declared_segments(segments)
    else:
        segs = (SourceSegment("unknown", points[0].week_key, points[-1].week_key, len(points),
                              False, "no source column and no declared segments"),)
    ok, reason = sem001_check(segs)
    warnings = () if ok else (f"SEM-001 provenance not satisfied: {reason}",)
    convention = "week_end" if use_end else "week_start_plus_4"
    transformations = () if use_end else ("week_key = week_start + 4 days (NORM-008)",)
    if has_source_date:
        transformations += ("source_date column kept as observation metadata; information "
                            "available at the Friday week_key (NORM-019)",)
    return PriceSeries(role, tuple(points), _provenance(
        role, path, raw, config_key, adapter if adapter != "auto" else convention, ok, len(rows),
        points, convention, raw_dates, sidecar=sidecar, transformations=transformations,
        segments=segs, warnings=warnings, issues=tuple(issues),
        resolved_via_alias=resolved_via_alias, source_label="US stock price index (signal)"))


def compose_stock_signal(schwert: PriceSeries, spx: PriceSeries,
                         splice_key: Optional[dt.date] = None) -> PriceSeries:
    """Canonical SEM-001 construction: Schwert for weeks before the splice, SPX rebased to the
    Schwert level at the last common week before the splice for weeks from the splice on."""
    if splice_key is None:
        splice_key = next(k for k in (dt.date(SEM001_SPLICE_YEAR, 1, d) for d in range(1, 8))
                          if k.weekday() == 4)
    s_by, x_by = schwert.by_key(), spx.by_key()
    common = sorted(k for k in s_by if k in x_by and k < splice_key)
    if not common:
        raise DataValidationError("cannot rebase SPX: no common week with Schwert before splice")
    anchor = common[-1]
    factor = s_by[anchor].price / x_by[anchor].price
    pts = [p for p in schwert.points if p.week_key < splice_key]
    pts += [PricePoint(p.week_key, p.price * factor, p.available_at, p.source_date, "SPX", p.flags)
            for p in spx.points if p.week_key >= splice_key]
    pts = [PricePoint(p.week_key, p.price, p.available_at, p.source_date,
                      p.source or "Schwert", p.flags) if p.week_key < splice_key else p for p in pts]
    if not pts or pts[0].week_key >= splice_key:
        raise DataValidationError("Schwert series has no weeks before the splice")
    segs = (SourceSegment("Schwert", pts[0].week_key,
                          max(p.week_key for p in pts if p.week_key < splice_key),
                          sum(1 for p in pts if p.week_key < splice_key), True,
                          f"component {schwert.provenance.path}"),
            SourceSegment("SPX", splice_key if splice_key in x_by else
                          min(p.week_key for p in pts if p.week_key >= splice_key),
                          pts[-1].week_key, sum(1 for p in pts if p.week_key >= splice_key), True,
                          f"component {spx.provenance.path}; rebased at {anchor}", factor))
    ok, reason = sem001_check(segs)
    prov = Provenance(
        role="stocks_price", path=f"{schwert.provenance.path}+{spx.provenance.path}",
        sha256=hashlib.sha256((schwert.provenance.sha256 + spx.provenance.sha256).encode()).hexdigest(),
        config_key="data.stocks_signal_components", adapter="schwert_spx_splice", canonical=ok,
        raw_rows=schwert.provenance.raw_rows + spx.provenance.raw_rows, used_rows=len(pts),
        raw_first_date=schwert.provenance.raw_first_date, raw_last_date=spx.provenance.raw_last_date,
        first_key=pts[0].week_key, last_key=pts[-1].week_key, date_convention="composed",
        transformations=(f"SPX * {factor!r} from {splice_key} (anchor {anchor})",),
        segments=segs, warnings=() if ok else (f"SEM-001 provenance not satisfied: {reason}",),
        issues=schwert.provenance.issues + spx.provenance.issues,
        source_label="Schwert + SPX (SEM-001)",
        components=(schwert.provenance, spx.provenance),
        extra=(("rebase_anchor", anchor), ("rebase_factor", factor), ("splice_key", splice_key)))
    return PriceSeries("stocks_price", tuple(pts), prov)


# ============================================================================ Fama-French
def _ff_text(path, config_key) -> tuple:
    raw = read_bytes(path, config_key)
    if str(path).lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = sorted(n for n in zf.namelist()
                           if fnmatch.fnmatch(Path(n).name.lower(), FF_MEMBER_PATTERN.lower()))
            if not names:
                raise MissingColumns(path, [FF_MEMBER_PATTERN], [FF_MEMBER_PATTERN], config_key,
                                     "ZIP contains no Fama-French weekly CSV member")
            member = zf.read(names[0])
        return raw, member, names[0]
    return raw, raw, None


def load_ff(path, config_key="data.stocks_return_file", duplicates="error",
            resolved_via_alias=False) -> FFData:
    """SCHEMA-002/NORM-014: accept only rows whose first field matches ^\\d{8}$ and whose
    required fields are numeric; locate Mkt-RF and RF by name; map dates with NORM-013."""
    raw, content, member = _ff_text(path, config_key)
    lines = decode(content).splitlines()
    date_re, num_re = re.compile(r"^\d{8}$"), re.compile(r"^-?\d+(\.\d+)?$")
    header_idx, first_record = None, None
    nondata, records, issues = [], [], []
    col = None
    for n, line in enumerate(lines, start=1):
        fields = [x.strip() for x in line.split(",")]
        if fields and date_re.match(fields[0]):
            if col is None:
                raise MissingColumns(path, ["Mkt-RF", "RF"], ["date", "Mkt-RF", "RF"], config_key,
                                     "no column header before the first record")
            if (len(fields) > max(col.values())
                    and all(num_re.match(fields[i]) for i in col.values())):
                records.append((n, fields))
                first_record = first_record or n
                continue
            issues.append(issue(Severity.WARNING, "ff_malformed_record", "stocks_return",
                                f"line {n} starts with a date but has non-numeric required fields",
                                None, "NORM-014"))
        nondata.append(n)
        if "Mkt-RF" in fields and "RF" in fields and first_record is None:
            header_idx = n
            col = {"Mkt-RF": fields.index("Mkt-RF"), "RF": fields.index("RF")}
    if not records:
        raise MissingColumns(path, ["Mkt-RF", "RF"], ["date", "Mkt-RF", "RF"], config_key)
    stock_pts, rf_pts, raw_dates = [], [], []
    for n, f in records:
        d = dt.datetime.strptime(f[0], "%Y%m%d").date()
        key = friday_key(d)                                   # NORM-013
        mkt, rf = float(f[col["Mkt-RF"]]), float(f[col["RF"]])
        raw_dates.append(d)
        stock_pts.append(ReturnPoint(key, (mkt + rf) / 100.0, d, d))   # PORT-001
        rf_pts.append(ReturnPoint(key, rf / 100.0, d, d))              # PORT-002
    stock_pts = normalise_points(stock_pts, "stocks_return", path, duplicates, issues)
    rf_pts = normalise_points(rf_pts, "rf", path, duplicates, [])
    prov = _provenance(
        "stocks_return", path, raw, config_key, "fama_french_csv" if member is None else "fama_french_zip",
        True, len(records), stock_pts, "yyyymmdd_same_calendar_week_friday", raw_dates,
        transformations=("week_key = d - weekday(d) + 4 (NORM-013)", "R_stock=(Mkt-RF+RF)/100",
                         "R_rf=RF/100"),
        issues=tuple(issues), resolved_via_alias=resolved_via_alias,
        source_label="Kenneth R. French data library, weekly factors",
        extra=(("nondata_lines", tuple(nondata)), ("header_line", header_idx),
               ("zip_member", member), ("records", len(records))))
    return FFData(ReturnSeries("stocks_return", tuple(stock_pts), prov),
                  ReturnSeries("rf", tuple(rf_pts), prov), prov)


# ============================================================================ gold
def load_gold(path, config_key="data.gold_file", adapter="auto", duplicates="error",
              source_label="LBMA Gold Price PM", resolved_via_alias=False) -> PriceSeries:
    header, rows, raw = read_table(path, config_key)
    if adapter == "auto":
        if "week_end" in header:
            adapter = "lbma_weekly"
        elif "date" in header:
            adapter = "lbma_daily"
        else:
            raise MissingColumns(path, ["week_end"], ["week_end", "gold_pm_usd"], config_key,
                                 "staged week_start files need the explicit non-canonical "
                                 "adapter tvc_oanda_proxy (Q-002)")
    date_col = {"lbma_weekly": "week_end", "lbma_daily": "date",
                "tvc_oanda_proxy": "week_start"}.get(adapter)
    if date_col is None:
        raise ConfigError(f"unknown gold adapter {adapter!r}")
    require(header, [date_col, "gold_pm_usd"], path, config_key)
    issues, raw_dates, file_returns, obs = [], [], {}, []
    for i, r in enumerate(rows, start=2):
        d = to_date(r[date_col], date_col, path)
        price = to_float(r["gold_pm_usd"], "gold_pm_usd", path, f" row {i}")
        if not price > 0:
            raise DataValidationError(f"{path}: row {i}: gold price must be > 0")
        raw_dates.append(d)
        wr = r.get("weekly_return", "")
        obs.append((d, (price, r.get("source", ""), wr)))
    transformations, warnings = [], []
    points = []
    if adapter == "lbma_daily":
        for key, d, (price, src, _) in aggregate_daily_last(sorted(obs, key=lambda x: x[0])):
            points.append(PricePoint(key, price, d, d, src))
        transformations.append("daily -> weekly: last fixing of each Monday-Sunday week (NORM-009)")
    else:
        for d, (price, src, wr) in obs:
            if adapter == "tvc_oanda_proxy":
                try:
                    key = week_start_to_key(d)
                except ValueError as exc:
                    raise DataValidationError(f"{path}: {exc}") from exc
                avail = key
            else:
                key, avail = friday_key(d), d
            flags = ("fill",) if "FILL" in src.upper() else ()
            points.append(PricePoint(key, price, avail, d, src, flags))
            if wr != "":
                file_returns[key] = to_float(wr, "weekly_return", path)
        if adapter == "tvc_oanda_proxy":
            transformations.append("week_key = week_start + 4 days (Q-003)")
    points = normalise_points(points, "gold", path, duplicates, issues)
    check_price_returns(points, file_returns, "gold", issues, "SCHEMA-003;PORT-003")
    canonical = adapter in ("lbma_weekly", "lbma_daily")
    if "source" in header:
        segs = segments_from_sources(points)
        non_lbma = sorted({s.source for s in segs if "LBMA" not in s.source.upper()})
        if non_lbma:
            canonical = False
            warnings.append(f"gold sources are not LBMA PM: {', '.join(non_lbma)}")
    else:
        segs = (SourceSegment(source_label, points[0].week_key, points[-1].week_key,
                              len(points), False, "declared, no source column"),)
    if adapter == "tvc_oanda_proxy":
        warnings.append("non-canonical gold proxy (TVC/OANDA); not LBMA Gold Price PM "
                        "(DATA-003, SIG-009, SEM-003; data blocker Q-002)")
    fills = sum(1 for p in points if "fill" in p.flags)
    if fills:
        warnings.append(f"{fills} rows come from a secondary fill source")
    return PriceSeries("gold", tuple(points), _provenance(
        "gold", path, raw, config_key, adapter, canonical, len(rows), points,
        {"lbma_weekly": "week_end", "lbma_daily": "daily_last_in_week",
         "tvc_oanda_proxy": "week_start_plus_4"}[adapter], raw_dates,
        transformations=tuple(transformations), segments=segs, warnings=tuple(warnings),
        issues=tuple(issues), resolved_via_alias=resolved_via_alias,
        source_label=source_label if canonical else "gold proxy (non-LBMA)"))


# ============================================================================ bitcoin
def load_btc(path, config_key="data.btc_file", adapter="auto", date_range=None,
             duplicates="error", resolved_via_alias=False) -> PriceSeries:
    """SCHEMA-004/NORM-016/SEM-008 with the Q-005 canonical normalisation:
    raw Monday-Sunday rows (date == source_week_start, Monday) map to
    week_key = source_week_start + 4 (Friday of the same calendar week); available_at =
    close_date = week_key + 2 (Sunday). Canonical Friday-keyed files are accepted as is."""
    header, rows, raw = read_table(path, config_key)
    require(header, BTC_COLUMNS, path, config_key)
    parsed = []
    for i, r in enumerate(rows, start=2):
        parsed.append((i, to_date(r["date"], "date", path), to_date(r["close_date"], "close_date", path),
                       to_date(r["source_week_start"], "source_week_start", path), r))
    if adapter == "auto":
        if all(d.weekday() == 4 for _, d, *_ in parsed):
            adapter = "canonical_friday"
        elif all(d.weekday() == 0 and d == sws for _, d, _, sws, _ in parsed):
            adapter = "raw_monday_week_start"
        else:
            raise DataValidationError(f"{path}: BTC date column is neither Friday keys nor raw "
                                      "Monday week starts (SCHEMA-004)")
    if adapter not in ("canonical_friday", "raw_monday_week_start"):
        raise ConfigError(f"unknown btc adapter {adapter!r}")
    lo = dt.date.fromisoformat(str(date_range[0])) if date_range else None
    hi = dt.date.fromisoformat(str(date_range[1])) if date_range else None
    issues, points, excluded, file_returns = [], [], [], {}
    for i, d, close, sws, r in parsed:
        if adapter == "raw_monday_week_start":
            if d.weekday() != 0 or d != sws:
                raise DataValidationError(f"{path}: row {i}: raw adapter needs date == "
                                          "source_week_start on a Monday")
            key = sws + dt.timedelta(days=4)
        else:
            key = d
            if d.weekday() != 4 or sws != d - dt.timedelta(days=4):
                raise DataValidationError(f"{path}: row {i}: canonical date must be a Friday and "
                                          "source_week_start its Monday")
        if close != key + dt.timedelta(days=2):
            raise DataValidationError(f"{path}: row {i}: close_date {close} is not the Sunday "
                                      f"week_key+2 of week {key} (SCHEMA-004)")
        if (lo and key < lo) or (hi and key > hi):
            excluded.append((d.isoformat(), "outside canonical range"))
            continue
        price = to_float(r["price"], "price", path, f" row {i}")
        if not price > 0:
            raise DataValidationError(f"{path}: row {i}: price must be > 0")
        points.append(PricePoint(key, price, close, d, r.get("source", "")))
        if r["weekly_return"] != "":
            file_returns[key] = to_float(r["weekly_return"], "weekly_return", path, f" row {i}")
    points = normalise_points(points, "btc", path, duplicates, issues)
    check_price_returns(points, file_returns, "btc", issues, "SCHEMA-004;PORT-004")
    transformations = ["information_available_at = close_date (NORM-016)"]
    if adapter == "raw_monday_week_start":
        transformations.insert(0, "source_week_start = raw date (Monday); week_key = "
                                  "source_week_start + 4 days (Friday, same calendar week) (Q-005)")
    if date_range:
        transformations.append(f"canonical range {lo}..{hi}; {len(excluded)} raw rows excluded")
    return PriceSeries("btc", tuple(points), _provenance(
        "btc", path, raw, config_key, adapter, True, len(rows), points,
        "raw_monday_week_start" if adapter == "raw_monday_week_start" else "friday_key",
        [p[1] for p in parsed], transformations=tuple(transformations), excluded=tuple(excluded),
        issues=tuple(issues), resolved_via_alias=resolved_via_alias,
        source_label=", ".join(sorted({p.source for p in points if p.source})) or "BTC price",
        extra=(("canonical_range", (lo, hi) if date_range else None),)))


# ============================================================================ dividends
def load_dividend(path, config_key="data.dividend_file", adapter="canonical", duplicates="error",
                  resolved_via_alias=False) -> DividendSeries:
    """Smoothed weekly dividend series (DIV-001, DIV-012). ``canonical`` enforces the full
    SCHEMA-005 column set; ``shiller_proxy`` is the explicit non-canonical adapter for the
    staged file (Q-008) and can never count as canonical evidence."""
    header, rows, raw = read_table(path, config_key)
    if adapter == "canonical":
        require(header, DIVIDEND_CANONICAL_COLUMNS, path, config_key,
                "the staged Shiller file needs the explicit adapter shiller_proxy (Q-008)")
    elif adapter == "shiller_proxy":
        require(header, DIVIDEND_PROXY_COLUMNS, path, config_key)
    else:
        raise ConfigError(f"unknown dividend adapter {adapter!r}")
    issues, points, raw_dates = [], [], []
    prev_close = {}
    for i, r in enumerate(rows, start=2):
        d = to_date(r["date"], "date", path)
        raw_dates.append(d)
        if d.weekday() != 4:
            issues.append(issue(Severity.ERROR, "dividend_not_friday", "dividend",
                                f"row {i}: date {d} is not a Friday", friday_key(d), "DIV-012"))
        dr = to_float(r["dividend_return"], "dividend_return", path, f" row {i}")
        pts = to_float(r["dividend_points"], "dividend_points", path, f" row {i}")
        prev = to_float(r["spx_close_prev"], "spx_close_prev", path, f" row {i}")
        if prev <= 0 or abs(dr - pts / prev) > TOL_DIVIDEND_REL * max(abs(dr), 1e-12):
            issues.append(issue(Severity.ERROR, "dividend_formula", "dividend",
                                f"row {i}: dividend_return != dividend_points/spx_close_prev",
                                friday_key(d), "SCHEMA-005;DIV-012"))
        status = r["status"]
        if status not in DIVIDEND_STATUSES:
            issues.append(issue(Severity.ERROR, "dividend_status", "dividend",
                                f"row {i}: status {status!r} not in {sorted(DIVIDEND_STATUSES)}",
                                friday_key(d), "DIV-012"))
        if "year" in header and r.get("year", "") and int(float(r["year"])) != d.year:
            issues.append(issue(Severity.ERROR, "dividend_year", "dividend",
                                f"row {i}: year {r['year']} != {d.year}", friday_key(d), "SCHEMA-005"))
        if "spx_close" in header and r.get("spx_close", ""):
            prev_close[friday_key(d)] = to_float(r["spx_close"], "spx_close", path, f" row {i}")
        values = {"dividend_return": dr, "dividend_points": pts}
        if adapter == "canonical":
            values.update((c, to_float(r[c], c, path, f" row {i}")) for c in
                          ("spx_close", "annual_yield_pct", "trailing_dps_points"))
        negative = [c for c, v in values.items() if v < 0 or (c == "spx_close" and v <= 0)]
        if negative:
            issues.append(issue(Severity.ERROR, "dividend_value", "dividend",
                                f"row {i}: invalid {', '.join(negative)} (negative dividend or "
                                "non-positive price)", friday_key(d), "SCHEMA-005;NORM-006"))
        key = friday_key(d)
        points.append(DividendPoint(key, dr, pts, prev, status, key))
    points = normalise_points(points, "dividend", path, duplicates, issues)
    for p in points:
        c = prev_close.get(p.week_key - WEEK)
        if c is not None and abs(c / p.spx_close_prev - 1) > 1e-9:
            issues.append(issue(Severity.WARNING, "dividend_close_chain", "dividend",
                                f"spx_close of previous week != spx_close_prev at {p.week_key}",
                                p.week_key, "SCHEMA-005"))
    warnings = []
    if adapter == "shiller_proxy":
        warnings.append("non-canonical dividend proxy: SCHEMA-005 columns spx_close, year, "
                        "annual_yield_pct, trailing_dps_points absent (data blocker Q-008)")
    statuses = sorted({p.status for p in points})
    blocks = []                                   # contiguous status blocks (SEM-007, DIV-011)
    for p in points:
        if blocks and blocks[-1][0] == p.status:
            blocks[-1][2] = p.week_key
        else:
            blocks.append([p.status, p.week_key, p.week_key])
    if any(a[0] == "estimate" and b[0] == "actual" for a, b in zip(blocks, blocks[1:])):
        first = next(b for a, b in zip(blocks, blocks[1:]) if a[0] == "estimate" and b[0] == "actual")
        issues.append(issue(Severity.WARNING, "dividend_status_order", "dividend",
                            f"status=actual from {first[1]} follows status=estimate rows",
                            first[1], "SEM-007;DIV-011"))
    return DividendSeries(tuple(points), _provenance(
        "dividend", path, raw, config_key, adapter, adapter == "canonical", len(rows), points,
        "friday", raw_dates, warnings=tuple(warnings), issues=tuple(issues),
        resolved_via_alias=resolved_via_alias, source_label="smoothed SPX dividend return",
        extra=(("status_values", tuple(statuses)),
               ("status_counts", tuple((s, sum(1 for p in points if p.status == s)) for s in statuses)),
               ("status_blocks", tuple((s, a, b) for s, a, b in blocks)))))


def load_dividend_cash(path, config_key="data.dividend_cash_file") -> DividendSeries:
    """Exact dividend mode input (DIV-010, Q-036): pay_date,dividend_return. A smoothed file
    is never accepted as exact."""
    header, rows, raw = read_table(path, config_key)
    if {"dividend_points", "spx_close_prev", "status"} & set(header):
        raise DividendModeError(f"{path} looks like a smoothed dividend file; exact mode needs "
                                "actual cash dividend dates (DIV-010)")
    require(header, ["pay_date", "dividend_return"], path, config_key)
    points = []
    for i, r in enumerate(rows, start=2):
        d = to_date(r["pay_date"], "pay_date", path)
        dr = to_float(r["dividend_return"], "dividend_return", path, f" row {i}")
        points.append(DividendPoint(friday_key(d), dr, float("nan"), float("nan"), "actual", d))
    points = normalise_points(points, "dividend_cash", path, "error", [])
    return DividendSeries(tuple(points), _provenance(
        "dividend_cash", path, raw, config_key, "cash_dates", True, len(rows), points,
        "pay_date_same_calendar_week", [p.available_at for p in points]))


# ============================================================================ CPI
def load_cpi(path, config_key="data.cpi_file", missing_policy="previous_available",
             resolved_via_alias=False) -> CpiSeries:
    """SCHEMA-007, NORM-015/REAL-002: an empty month uses the last earlier non-empty value
    (flagged) under previous_available, or fails under error."""
    header, rows, raw = read_table(path, config_key)
    require(header, ["observation_date", "CPIAUCNS"], path, config_key)
    values, imputed, issues, raw_dates = {}, set(), [], []
    months = []
    for i, r in enumerate(rows, start=2):
        d = to_date(r["observation_date"], "observation_date", path)
        raw_dates.append(d)
        months.append(((d.year, d.month), r["CPIAUCNS"]))
    months.sort()
    if not months:
        raise DataValidationError(f"{path}: empty CPI file")
    (y, m), _ = months[0]
    expected = []
    last = months[-1][0]
    while (y, m) <= last:
        expected.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    given = dict(months)
    prev = None
    for ym in expected:
        v = given.get(ym, "")
        if v.strip() == "":
            if missing_policy == "error" or prev is None:
                raise DataValidationError(f"{path}: CPI missing for {ym[0]}-{ym[1]:02d} "
                                          f"(cpi.missing_policy={missing_policy})")
            values[ym] = prev
            imputed.add(ym)
            issues.append(issue(Severity.WARNING, "cpi_imputed", "cpi",
                                f"CPI {ym[0]}-{ym[1]:02d} missing; previous_available value {prev} used",
                                None, "NORM-015;REAL-002;SEM-010"))
        else:
            values[ym] = to_float(v, "CPIAUCNS", path, f" {ym}")
            prev = values[ym]
    prov = Provenance(
        role="cpi", path=str(path), sha256=sha256_bytes(raw), config_key=config_key,
        adapter="monthly", canonical=True, raw_rows=len(rows), used_rows=len(values),
        raw_first_date=min(raw_dates), raw_last_date=max(raw_dates), first_key=None, last_key=None,
        date_convention="first_day_of_month", issues=tuple(issues),
        resolved_via_alias=resolved_via_alias, source_label="US CPI-U / CPIAUCNS",
        extra=(("imputed_months", tuple(f"{a}-{b:02d}" for a, b in sorted(imputed))),))
    return CpiSeries(values, frozenset(imputed), prov)


# ============================================================================ resolution
DISTRIBUTION_KEY = "tax.foundation.distribution_file"


def load_distribution_schedule(cfg) -> DistributionSchedule:
    """FND-009 / Q-037: the foundation distribution schedule - CSV ``date,amount`` and/or
    ``date,percent_nav``; in every row exactly one of amount (gross PLN, > 0) and percent_nav
    (decimal fraction of NAV_after_signal, 0 < p <= 1) is filled. A missing key, a missing file
    or an invalid row is a ConfigError (never a fallback to tax_event=terminal). The schedule
    is an exogenous plan: it does not take part in the market calendar intersection."""
    requested = cfg.get(DISTRIBUTION_KEY)
    if not requested:
        raise ConfigError(f"tax.foundation.tax_event=distribution_schedule requires "
                          f"{DISTRIBUTION_KEY} (--distribution-file) (FND-009, Q-037)")
    path = Path(requested)
    if not path.is_file() and not path.is_absolute():
        path = Path(cfg.get("data.dir")) / requested
    if not path.is_file():
        raise ConfigError(f"{DISTRIBUTION_KEY}: distribution schedule file {requested!r} not "
                          f"found (FND-009, Q-037)")
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(decode(raw)))
    header = [h.strip() for h in (reader.fieldnames or [])]
    if "date" not in header or not {"amount", "percent_nav"} & set(header):
        raise ConfigError(f"{path}: distribution schedule needs the columns date and amount and/or "
                          f"percent_nav, got {header} (FND-009)")
    rows = []
    for i, r in enumerate(reader, start=1):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        where = f"{path}: row {i}"
        try:
            day = dt.date.fromisoformat(r["date"])
        except ValueError:
            raise ConfigError(f"{where}: invalid date {r['date']!r} (YYYY-MM-DD)") from None
        amount, pct = r.get("amount", ""), r.get("percent_nav", "")
        if bool(amount) == bool(pct):
            raise ConfigError(f"{where}: exactly one of amount and percent_nav must be filled "
                              f"(amount={amount!r}, percent_nav={pct!r})")
        try:
            value = float(amount or pct)
        except ValueError:
            raise ConfigError(f"{where}: non-numeric {'amount' if amount else 'percent_nav'} "
                              f"{amount or pct!r}") from None
        if not math.isfinite(value):
            raise ConfigError(f"{where}: non-finite value {amount or pct!r}")
        if amount and not value > 0:
            raise ConfigError(f"{where}: amount must be > 0 PLN, got {value!r}")
        if pct and not 0.0 < value <= 1.0:
            raise ConfigError(f"{where}: percent_nav is a decimal fraction with 0 < p <= 1, got "
                              f"{value!r} (not a CLI percent)")
        rows.append(ScheduledDistribution(i, day, value if amount else None, value if pct else None))
    if not rows:
        raise ConfigError(f"{path}: the distribution schedule has no rows (FND-009)")
    dates = [x.scheduled_date for x in rows]
    prov = Provenance(
        role="distribution_schedule", path=str(path), sha256=sha256_bytes(raw),
        config_key=DISTRIBUTION_KEY, adapter="distribution_schedule", canonical=True,
        raw_rows=len(rows), used_rows=len(rows), raw_first_date=min(dates),
        raw_last_date=max(dates), first_key=None, last_key=None,
        date_convention="scheduled date -> Friday key of its Monday-Sunday week; a removed "
                        "week -> the next retained run week (Q-037)",
        extra=(("amount_rows", sum(1 for x in rows if x.amount is not None)),
               ("percent_nav_rows", sum(1 for x in rows if x.percent_nav is not None)),
               ("exogenous_schedule", "not part of the market calendar intersection")))
    return DistributionSchedule(tuple(rows), prov)


def load_profile(path) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"data profile not found: {p} (config key: data.profile)")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _normalise_role(key: str) -> str:
    from .config import normalize_data_key
    return normalize_data_key(key)


def resolve_source(cfg, role: str) -> dict:
    """Resolve the file, adapter and options of one data role (DATA-001..008, Q-001)."""
    from .config import DEFAULTS, get_path
    config_key = ROLE_CONFIG_KEYS[role]
    data_dir = Path(cfg.get("data.dir"))
    overrides = {_normalise_role(k): v for k, v in (cfg.get("data.overrides") or {}).items()}
    if role in overrides:
        requested, explicit, used_key = overrides[role], True, f"data.overrides.{role}"
    else:
        requested = cfg.get(config_key)
        explicit = cfg.source_of(config_key) in ("cli", "file")
        used_key = config_key
    spec_default = get_path(DEFAULTS, config_key)

    def locate(name):
        p = Path(name)
        if p.is_absolute():
            return p
        return p if p.is_file() else data_dir / p

    path = locate(requested)
    alias, via_alias = {}, False
    if not path.is_file() and Path(str(requested)).name == spec_default:
        alias = (load_profile(cfg.get("data.profile")).get("aliases") or {}).get(role) or {}
        if alias:
            path, via_alias = locate(alias["file"]), True
    if not path.is_file():
        raise DataFileNotFound(path, used_key)
    adapters = cfg.get("data.adapters") or {}
    return {"role": role, "path": path, "config_key": used_key, "explicit": explicit,
            "resolved_via_alias": via_alias, "requested": str(requested),
            "adapter": adapters.get(role) or alias.get("adapter") or
                       ("canonical" if role == "dividend" else "auto"),
            "alias": alias}


def load_role(cfg, role: str):
    src = resolve_source(cfg, role)
    dup = cfg.get("validation.duplicates")
    kw = dict(config_key=src["config_key"], resolved_via_alias=src["resolved_via_alias"])
    if role == "stocks_price":
        comps = cfg.get("data.stocks_signal_components")
        if comps:
            schwert = load_stocks_signal(comps["schwert_file"], "data.stocks_signal_components.schwert_file",
                                         duplicates=dup)
            spx = load_stocks_signal(comps["spx_file"], "data.stocks_signal_components.spx_file",
                                     duplicates=dup)
            splice = comps.get("splice_week")
            return compose_stock_signal(schwert, spx, dt.date.fromisoformat(splice) if splice else None)
        segs = cfg.get("data.stocks_signal_segments") or src["alias"].get("segments")
        adapter = "auto" if src["adapter"] in ("auto", "single_series") else src["adapter"]
        return load_stocks_signal(src["path"], adapter=adapter, segments=segs, duplicates=dup, **kw)
    if role == "stocks_return":
        return load_ff(src["path"], duplicates=dup, **kw)
    if role == "gold":
        return load_gold(src["path"], adapter=src["adapter"], duplicates=dup, **kw)
    if role == "btc":
        rng = cfg.get("data.btc_range") or src["alias"].get("range")
        return load_btc(src["path"], adapter=src["adapter"], date_range=rng, duplicates=dup, **kw)
    if role == "dividend":
        return load_dividend(src["path"], adapter=src["adapter"], duplicates=dup, **kw)
    if role == "cpi":
        return load_cpi(src["path"], missing_policy=cfg.get("cpi.missing_policy"), **kw)
    raise ConfigError(f"unknown data role {role!r}")
