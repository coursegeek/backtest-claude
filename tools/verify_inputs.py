#!/usr/bin/env python3
from pathlib import Path
import hashlib, json, sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "input" / "source_manifest.json"
STATUSES = ("specification", "canonical", "provenance", "proxy", "supplemental")
# Specification default file names (DATA-001..DATA-007) and the role each one serves.
SPEC_DEFAULTS = {
    "DATA-001 stocks signal": "US_STOCK_PRICE_WEEKLY_1885_2026.csv",
    "DATA-002 stocks return": "F-F_Research_Data_Factors_weekly.csv",
    "DATA-003 gold": "GOLD_LBMA_PM_weekly_backtest_ready.csv",
    "DATA-004 CPI": "CPIAUCNS.csv",
    "DATA-005 BTC": "BTC_weekly_date_price_2011_2026.csv",
    "DATA-007 dividend": "SPX_dividend_return_weekly_1970_2026.csv",
}

def main():
    if not MANIFEST.is_file():
        raise SystemExit("input/source_manifest.json not found. Run stage_cleanroom.py first.")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = []
    by_name = {}
    for rec in manifest["files"]:
        p = ROOT / rec["staged_path"]
        status = rec.get("data_status", "unclassified")
        by_name[p.name] = rec
        if status not in STATUSES:
            bad.append(f"UNCLASSIFIED {rec['staged_path']} data_status={status!r}")
        if not p.is_file():
            bad.append(f"MISSING {rec['staged_path']}")
            continue
        data = p.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != rec["sha256"]:
            bad.append(f"HASH MISMATCH {rec['staged_path']} expected={rec['sha256']} actual={actual}")
            continue
        if status == "canonical" and p.with_name(p.name + ".provenance.json").is_file():
            meta = json.loads(p.with_name(p.name + ".provenance.json").read_text(encoding="utf-8"))
            if meta.get("final", {}).get("sha256") != actual:
                bad.append(f"PROVENANCE MISMATCH {rec['staged_path']}: sidecar final.sha256 "
                           f"{meta.get('final', {}).get('sha256')} != {actual}")
    if bad:
        print("Input verification FAILED", file=sys.stderr)
        for x in bad:
            print(" - " + x, file=sys.stderr)
        raise SystemExit(2)
    print(f"Input verification PASS: {len(manifest['files'])} immutable files.")
    for status in STATUSES:
        names = [Path(r["staged_path"]).name for r in manifest["files"] if r["data_status"] == status]
        if names:
            print(f"  {status}: {', '.join(names)}")
    for role, name in SPEC_DEFAULTS.items():
        rec = by_name.get(name)
        if rec is not None:
            print(f"  {role}: default {name} present ({rec['data_status']})")
        else:
            print(f"  {role}: default {name} absent; the staged alias is used (see work/config/cleanroom_data.yaml)")

if __name__ == "__main__":
    main()
