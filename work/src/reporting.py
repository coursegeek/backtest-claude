"""Deterministic output writers (REP-001, REP-006, REP-007, REP-009, REP-014, NORM-007).
Floats use the shortest round-trip repr, so identical inputs give identical bytes."""
from __future__ import annotations

import csv
import datetime as dt
import json
from enum import Enum
from pathlib import Path

SIGNAL_FIELDS = ["asset", "week_key", "available_at", "price", "sma", "lower_band", "upper_band",
                 "raw_condition", "exit_counter", "entry_counter", "confirmed_state", "state_basis",
                 "confirmed_signal", "scheduled_execution_week", "effective_state",
                 "executed_target", "flags"]
VALIDATION_FIELDS = ["severity", "code", "role", "week_key", "requirement_ids", "message"]
NORMALIZED_FIELDS = ["role", "week_key", "value", "value_kind", "available_at", "source_date",
                     "source", "flags"]


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (tuple, list)):
        return "|".join(fmt(x) for x in v)
    return str(v)


def write_csv(path: Path, fields, rows) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(fields)
        for r in rows:
            w.writerow([fmt(r.get(k)) for k in fields])


def write_json(path: Path, obj) -> None:
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def run_directory(output_dir, run_name: str, timestamp: dt.datetime) -> Path:
    """REP-001: results/<timestamp>_<run_name>/ (never reused)."""
    base = Path(output_dir) / f"{timestamp.strftime('%Y%m%dT%H%M%S')}_{run_name}"
    path, n = base, 1
    while path.exists():
        n += 1
        path = base.with_name(f"{base.name}_{n}")
    path.mkdir(parents=True)
    return path


def signal_rows(records) -> list:
    return [{"asset": r.asset, "week_key": r.week_key, "available_at": r.available_at,
             "price": r.price, "sma": r.sma, "lower_band": r.lower_band, "upper_band": r.upper_band,
             "raw_condition": r.condition, "exit_counter": r.exit_counter,
             "entry_counter": r.entry_counter, "confirmed_state": r.confirmed_state,
             "state_basis": r.state_basis, "confirmed_signal": r.confirmation,
             "scheduled_execution_week": r.scheduled_execution_week,
             "effective_state": r.effective_state, "executed_target": r.executed_target,
             "flags": r.flags} for r in records]


def normalized_price_rows(series) -> list:
    return [{"role": series.role, "week_key": p.week_key, "value": p.price, "value_kind": "price",
             "available_at": p.available_at, "source_date": p.source_date, "source": p.source,
             "flags": p.flags} for p in series.points]
