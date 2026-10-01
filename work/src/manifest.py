"""data_manifest.json (REP-008, REPRO-001..007)."""
from __future__ import annotations

import datetime as dt
import subprocess

from . import SPEC_VERSION
from .calendar import TZ
from .config import WORK_DIR


def code_version() -> dict:
    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(WORK_DIR), *args], capture_output=True,
                                  text=True, check=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    commit = git("rev-parse", "HEAD") or "unknown"
    dirty = bool(git("status", "--porcelain", "--", "src", "backtest.py"))
    return {"git_commit": commit, "working_tree_dirty": dirty}


def run_timestamp() -> dt.datetime:
    return dt.datetime.now(TZ).replace(microsecond=0)


def build_manifest(provenances, as_of: dt.date, timestamp: dt.datetime, command: str) -> dict:
    """Only ``run_timestamp`` (and the output directory name) differ between identical runs."""
    return {
        "spec_version": SPEC_VERSION,
        "command": command,
        "code_version": code_version(),
        "run_timestamp": timestamp.isoformat(),
        "timezone": "Europe/Warsaw",
        "as_of_date": as_of.isoformat(),
        "sources": [p.to_dict() for p in sorted(provenances, key=lambda p: p.role)],
        # DATA-008: every --data-file / data.overrides source actually used (path and SHA-256)
        "data_overrides": {p.role: {"config_key": p.config_key, "path": p.path, "sha256": p.sha256}
                           for p in sorted(provenances, key=lambda p: p.role)
                           if p.config_key.startswith("data.overrides.")},
    }
