#!/usr/bin/env python3
from pathlib import Path
import csv, hashlib, json, datetime, sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
COMPLIANCE = WORK / "compliance_matrix.csv"
OUT = ROOT / "V2_FREEZE_MANIFEST.json"

EXCLUDE_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "results"}
EXCLUDE_FILES = {".DS_Store"}

def eligible(p: Path) -> bool:
    rel = p.relative_to(WORK)
    if any(part in EXCLUDE_PARTS for part in rel.parts):
        return False
    if p.name in EXCLUDE_FILES:
        return False
    return p.is_file()

def main():
    if not COMPLIANCE.is_file():
        raise SystemExit("work/compliance_matrix.csv is missing.")
    with COMPLIANCE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    blockers = [
        r for r in rows
        if r.get("priority") == "MUST" and r.get("status", "").strip().upper() != "PASS"
    ]
    if blockers:
        print(f"Freeze blocked: {len(blockers)} MUST requirements are not PASS.", file=sys.stderr)
        for r in blockers[:25]:
            print(f" - {r.get('requirement_id')}: {r.get('status')}", file=sys.stderr)
        if len(blockers) > 25:
            print(f" ... and {len(blockers)-25} more", file=sys.stderr)
        raise SystemExit(2)

    files = []
    tree = hashlib.sha256()
    for p in sorted(WORK.rglob("*")):
        if not eligible(p):
            continue
        data = p.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        rel = str(p.relative_to(ROOT))
        files.append({"path": rel, "bytes": len(data), "sha256": digest})
        tree.update(rel.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")

    if not files:
        raise SystemExit("No V2 work files found.")

    manifest = {
        "freeze_version": 1,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tree_sha256": tree.hexdigest(),
        "must_requirements": sum(1 for r in rows if r.get("priority") == "MUST"),
        "must_pass": sum(1 for r in rows if r.get("priority") == "MUST" and r.get("status", "").strip().upper() == "PASS"),
        "files": files,
        "rule": "Do not inspect V1 outputs before this manifest exists.",
    }
    OUT.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"V2 frozen: {OUT.name}")
    print(f"tree_sha256={manifest['tree_sha256']}")

if __name__ == "__main__":
    main()
