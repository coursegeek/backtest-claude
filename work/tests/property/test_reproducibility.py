"""REPRO-006 for the implemented signal-only command: identical inputs/config give identical
output files; only the manifest timestamp and the run directory name differ."""
import json

from src.app import run_signals
from src.config import ResolvedConfig


def test_signal_outputs_identical_except_timestamp(tmp_path):
    dirs = []
    for i in range(2):
        cfg = ResolvedConfig("signals", cli_layer={
            "run": {"asset": "btc", "as_of_date": "2026-09-29"},
            "report": {"output_dir": str(tmp_path / f"o{i}"), "run_name": "repro"}})
        dirs.append(run_signals(cfg).output_dir)
    names = sorted(p.name for p in dirs[0].iterdir())
    assert names == sorted(p.name for p in dirs[1].iterdir())
    assert names == ["config_resolved.yaml", "data_manifest.json", "signals.csv",
                     "validation_report.csv", "weekly_normalized.csv"]
    for n in names:
        a, b = (dirs[0] / n).read_bytes(), (dirs[1] / n).read_bytes()
        if n == "data_manifest.json":
            ja, jb = json.loads(a), json.loads(b)
            ja.pop("run_timestamp"), jb.pop("run_timestamp")
            ja["code_version"].pop("working_tree_dirty"), jb["code_version"].pop("working_tree_dirty")
            assert ja == jb
        elif n == "config_resolved.yaml":
            assert a.replace(str(tmp_path / "o0").encode(), b"X") == b.replace(str(tmp_path / "o1").encode(), b"X")
        else:
            assert a == b, n
