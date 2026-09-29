#!/usr/bin/env python3
from pathlib import Path
import csv

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "input" / "python_backtest_specification_v3_1.csv"
OUT = ROOT / "work" / "compliance_matrix.csv"

def main():
    if not SPEC.is_file():
        raise SystemExit("Specification not found. Stage the clean-room package first.")
    with SPEC.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    fields = [
        "section", "requirement_id", "priority", "requirement",
        "implementation", "test", "status", "notes"
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({
                "section": r.get("section", ""),
                "requirement_id": r.get("requirement_id", ""),
                "priority": r.get("priority", ""),
                "requirement": r.get("requirement", ""),
                "implementation": "",
                "test": "",
                "status": "NOT_STARTED",
                "notes": "",
            })
    must = sum(1 for r in rows if r.get("priority") == "MUST")
    should = sum(1 for r in rows if r.get("priority") == "SHOULD")
    print(f"Wrote {OUT.relative_to(ROOT)} with {len(rows)} requirements ({must} MUST, {should} SHOULD).")

if __name__ == "__main__":
    main()
