#!/usr/bin/env python3
from pathlib import Path
import hashlib, json, sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "input" / "source_manifest.json"

def main():
    if not MANIFEST.is_file():
        raise SystemExit("input/source_manifest.json not found. Run stage_cleanroom.py first.")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = []
    for rec in manifest["files"]:
        p = ROOT / rec["staged_path"]
        if not p.is_file():
            bad.append(f"MISSING {rec['staged_path']}")
            continue
        data = p.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != rec["sha256"]:
            bad.append(f"HASH MISMATCH {rec['staged_path']} expected={rec['sha256']} actual={actual}")
    if bad:
        print("Input verification FAILED", file=sys.stderr)
        for x in bad:
            print(" - " + x, file=sys.stderr)
        raise SystemExit(2)
    print(f"Input verification PASS: {len(manifest['files'])} immutable files.")

if __name__ == "__main__":
    main()
