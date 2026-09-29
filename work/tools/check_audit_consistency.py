#!/usr/bin/env python3
"""Cross-check the audit deliverables against the specification and against each other.

Checks:
  * work/compliance_matrix.csv covers every specification row once, in order, with a
    mapping (module, state/data, test, dependencies) for every MUST row, valid statuses,
    no PASS during the audit phase, and every explicit/implicit cross reference found by
    audit_spec.py present in depends_on.
  * work/implementation_questions.csv has the required columns, valid enums, existing
    requirement ids, existing source files and existing audit check ids; links are
    bidirectional with the compliance matrix.
  * every FAIL/WARN row of work/audit/input_checks.csv points to an existing question.
  * work/TEST_PLAN.md plans TEST-001..TEST-054 in the same files as the compliance matrix.
  * work/IMPLEMENTATION_PLAN.md covers all required concerns and every planned module.
  * the AUDIT_COUNTS block in work/AUDIT_REPORT.md matches recomputed numbers and every
    BLOCKER/MAJOR question is discussed in the report body.

Usage: python work/tools/check_audit_consistency.py [--allow-pass]
Exit code 0 when consistent, 1 otherwise.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work"
SPEC = ROOT / "input" / "python_backtest_specification_v3_1.csv"
MATRIX = WORK / "compliance_matrix.csv"
QUESTIONS = WORK / "implementation_questions.csv"
CHECKS = WORK / "audit" / "input_checks.csv"
SCENARIOS = WORK / "audit" / "scenario_feasibility.csv"
XREF = WORK / "audit" / "spec_cross_references.csv"
TEST_PLAN = WORK / "TEST_PLAN.md"
IMPL_PLAN = WORK / "IMPLEMENTATION_PLAN.md"
REPORT = WORK / "AUDIT_REPORT.md"

MATRIX_BASE_COLS = ["section", "requirement_id", "priority", "requirement",
                    "implementation", "test", "status", "notes"]      # tools/init_compliance.py
MATRIX_EXTRA_COLS = ["state_data", "depends_on", "question_ids", "verification"]
STATUSES = {"NOT_STARTED", "MAPPED", "BLOCKED", "PROPOSED_DEFERRAL", "DEFERRED",
            "IN_PROGRESS", "FAIL", "PASS"}
Q_COLS = ["question_id", "requirement_id", "severity", "source_file", "specification_requirement",
          "observed_data", "issue", "proposed_interpretation", "blocks_implementation",
          "confidence", "status"]
Q_STATUSES = {"OPEN", "ANSWERED", "RESOLVED", "WITHDRAWN", "DATA_BLOCKER"}
TEST_DIRS = ("tests/spec/", "tests/unit/", "tests/property/", "tests/integration/")
PLAN_CONCERNS = [
    "loading", "normalization", "calendar", "information availability", "validation", "signals",
    "confirmation", "delay", "allocation", "RF reserves", "ledger", "FIFO", "tax", "rebalancing",
    "sell_to_pay", "weekly engine", "metrics", "reporting", "optimization", "walk-forward", "PORT-011",
]


def read(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def split_ids(text: str):
    return [x.strip() for x in text.split(";") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--allow-pass", action="store_true",
                    help="permit PASS statuses (implementation phase)")
    args = ap.parse_args()
    errors: list[str] = []
    err = errors.append

    _, spec = read(SPEC)
    spec_ids = [r["requirement_id"] for r in spec]
    spec_by_id = {r["requirement_id"]: r for r in spec}

    # ------------------------------------------------------------------ matrix
    mcols, matrix = read(MATRIX)
    if mcols[:len(MATRIX_BASE_COLS)] != MATRIX_BASE_COLS:
        err(f"matrix: first columns must be {MATRIX_BASE_COLS}, got {mcols[:8]}")
    for c in MATRIX_EXTRA_COLS:
        if c not in mcols:
            err(f"matrix: missing column {c}")
    if [r["requirement_id"] for r in matrix] != spec_ids:
        err("matrix: requirement ids differ from specification (set or order)")
    m_by_id = {r["requirement_id"]: r for r in matrix}
    for r in matrix:
        rid = r["requirement_id"]
        s = spec_by_id.get(rid)
        if s is None:
            continue
        if r["priority"] != s["priority"] or r["requirement"] != s["requirement"]:
            err(f"matrix {rid}: priority/requirement text differs from specification")
        if r["status"] not in STATUSES:
            err(f"matrix {rid}: unknown status {r['status']}")
        if r["status"] == "PASS" and not args.allow_pass:
            err(f"matrix {rid}: PASS not allowed during the audit phase")
        needs_map = r["priority"] == "MUST" or r["status"] not in {"DEFERRED", "PROPOSED_DEFERRAL"}
        if needs_map:
            for c in ("implementation", "test", "state_data", "verification"):
                if not r.get(c, "").strip():
                    err(f"matrix {rid}: empty {c}")
        if r["priority"] == "MUST" and not split_ids(r.get("depends_on", "")):
            err(f"matrix {rid}: MUST row without depends_on")
        for dep in split_ids(r.get("depends_on", "")):
            if dep not in spec_by_id:
                err(f"matrix {rid}: unknown dependency {dep}")
            if dep == rid:
                err(f"matrix {rid}: self dependency")
        for t in [x.strip() for x in r["test"].split(";") if x.strip()]:
            if not t.startswith(TEST_DIRS):
                err(f"matrix {rid}: test path outside tests/{{spec,unit,property,integration}}: {t}")
        if r["status"] == "PASS":
            for t in [x.strip() for x in r["test"].split(";") if x.strip()]:
                path, _, func = t.partition("::")
                f = WORK / path
                if not f.is_file():
                    err(f"matrix {rid}: PASS test file missing: {path}")
                elif func and f"def {func.split('[')[0]}(" not in f.read_text(encoding="utf-8"):
                    err(f"matrix {rid}: PASS test function missing: {t}")
        if rid.startswith("TEST-") and not r["test"].startswith("tests/spec/"):
            err(f"matrix {rid}: specification test must live in tests/spec/")

    # cross references discovered by audit_spec.py must be dependencies
    _, xref = read(XREF)
    for x in xref:
        a, b = x["from_id"], x["to_id"]
        if a in m_by_id and b not in split_ids(m_by_id[a]["depends_on"]):
            err(f"matrix {a}: depends_on lacks {b} ({x['kind']}: {x['evidence']})")
        if x["from_priority"] == "MUST" and x["to_priority"] == "SHOULD":
            if m_by_id.get(b, {}).get("status") in {"DEFERRED", "PROPOSED_DEFERRAL"}:
                err(f"matrix {b}: SHOULD needed by MUST {a} cannot be deferred")

    # ------------------------------------------------------------------ questions
    qcols, questions = read(QUESTIONS)
    for c in Q_COLS:
        if c not in qcols:
            err(f"questions: missing column {c}")
    _, checks = read(CHECKS)
    check_ids = {c["check_id"] for c in checks}
    qids = [q["question_id"] for q in questions]
    if len(set(qids)) != len(qids):
        err("questions: duplicate question_id")
    expected = [f"Q-{i:03d}" for i in range(1, len(qids) + 1)]
    if qids != expected:
        err("questions: ids must be sequential Q-001..")
    q_by_id = {q["question_id"]: q for q in questions}
    for q in questions:
        qid = q["question_id"]
        if q["severity"] not in {"BLOCKER", "MAJOR", "MINOR"}:
            err(f"{qid}: bad severity {q['severity']}")
        if q["confidence"] not in {"HIGH", "MEDIUM", "LOW"}:
            err(f"{qid}: bad confidence {q['confidence']}")
        if q["status"] not in Q_STATUSES:
            err(f"{qid}: bad status {q['status']}")
        if not re.match(r"^(YES|NO|PARTIAL)\b", q["blocks_implementation"]):
            err(f"{qid}: blocks_implementation must start with YES/NO/PARTIAL")
        for c in Q_COLS:
            if not q[c].strip():
                err(f"{qid}: empty {c}")
        for rid in split_ids(q["requirement_id"]):
            if rid not in spec_by_id:
                err(f"{qid}: unknown requirement {rid}")
            elif qid not in split_ids(m_by_id[rid]["question_ids"]):
                err(f"{qid}: matrix row {rid} does not reference it")
        for p in q["source_file"].split("|"):
            if not (ROOT / p).is_file():
                err(f"{qid}: source_file not found: {p}")
        for ev in split_ids(q.get("audit_evidence", "")):
            if ev.startswith("A-") and ev not in check_ids:
                err(f"{qid}: audit_evidence {ev} not in input_checks.csv")
    for r in matrix:
        for qid in split_ids(r.get("question_ids", "")):
            if qid not in q_by_id:
                err(f"matrix {r['requirement_id']}: unknown question {qid}")
            elif r["requirement_id"] not in split_ids(q_by_id[qid]["requirement_id"]):
                err(f"matrix {r['requirement_id']}: {qid} does not list this requirement")
        if r["status"] == "BLOCKED":
            if not any(q_by_id.get(x, {}).get("severity") == "BLOCKER"
                       and q_by_id[x]["status"] in {"OPEN", "DATA_BLOCKER"}
                       for x in split_ids(r["question_ids"])):
                err(f"matrix {r['requirement_id']}: BLOCKED without an unresolved BLOCKER question")
    for c in checks:
        if c["result"] in {"FAIL", "WARN"}:
            refs = split_ids(c["question_ids"])
            if not refs:
                err(f"input_checks {c['check_id']}: {c['result']} without question_ids")
            for qid in refs:
                if qid not in q_by_id:
                    err(f"input_checks {c['check_id']}: unknown question {qid}")

    # ------------------------------------------------------------------ test plan
    tp = TEST_PLAN.read_text(encoding="utf-8") if TEST_PLAN.is_file() else ""
    if not tp:
        err("TEST_PLAN.md missing")
    for n in range(1, 55):
        tid = f"TEST-{n:03d}"
        rows = [ln for ln in tp.splitlines() if ln.startswith("|") and f"| {tid} |" in ln]
        if len(rows) != 1:
            err(f"TEST_PLAN.md: {tid} must appear in exactly one table row (found {len(rows)})")
            continue
        planned = re.findall(r"tests/spec/[\w/]+\.py", rows[0])
        mfile = m_by_id[tid]["test"].split("::")[0]
        if mfile not in planned:
            err(f"TEST_PLAN.md: {tid} planned file {planned} differs from matrix {mfile}")

    # ------------------------------------------------------------------ implementation plan
    ip = IMPL_PLAN.read_text(encoding="utf-8") if IMPL_PLAN.is_file() else ""
    if not ip:
        err("IMPLEMENTATION_PLAN.md missing")
    low = ip.lower()
    for concern in PLAN_CONCERNS:
        if concern.lower() not in low:
            err(f"IMPLEMENTATION_PLAN.md: concern '{concern}' not covered")
    modules = set()
    for r in matrix:
        modules |= set(re.findall(r"(?:src/[\w]+\.py|backtest\.py)", r["implementation"]))
    for mod in sorted(modules):
        if mod not in ip:
            err(f"IMPLEMENTATION_PLAN.md: module {mod} used in matrix but not described")

    # ------------------------------------------------------------------ report counts
    rep = REPORT.read_text(encoding="utf-8") if REPORT.is_file() else ""
    m = re.search(r"<!-- AUDIT_COUNTS\n(.*?)-->", rep, re.S)
    if not m:
        err("AUDIT_REPORT.md: AUDIT_COUNTS block missing")
    else:
        claimed = dict(line.split("=", 1) for line in m.group(1).strip().splitlines())
        sev = Counter(q["severity"] for q in questions)
        res = Counter(c["result"] for c in checks)
        _, scen = read(SCENARIOS)
        st = Counter(s["status"] for s in scen)
        actual = {
            "requirements_total": len(spec),
            "must": sum(1 for r in spec if r["priority"] == "MUST"),
            "should": sum(1 for r in spec if r["priority"] == "SHOULD"),
            "questions_total": len(questions),
            "blockers": sev["BLOCKER"], "major": sev["MAJOR"], "minor": sev["MINOR"],
            "must_mapped": sum(1 for r in matrix if r["priority"] == "MUST" and r["status"] == "MAPPED"),
            "must_blocked": sum(1 for r in matrix if r["priority"] == "MUST" and r["status"] == "BLOCKED"),
            "must_pass": sum(1 for r in matrix if r["priority"] == "MUST" and r["status"] == "PASS"),
            "should_proposed_deferral": sum(1 for r in matrix if r["priority"] == "SHOULD"
                                            and r["status"] == "PROPOSED_DEFERRAL"),
            "input_checks": len(checks), "checks_fail": res["FAIL"], "checks_warn": res["WARN"],
            "checks_pass": res["PASS"], "checks_info": res["INFO"],
            "scenarios_total": len(scen), "scenarios_feasible": st["FEASIBLE"],
            "scenarios_needs_decision": st["NEEDS_DECISION"],
            "blocker_ids": ",".join(q["question_id"] for q in questions if q["severity"] == "BLOCKER"),
            "data_blocker_ids": ",".join(q["question_id"] for q in questions if q["status"] == "DATA_BLOCKER"),
            "resolved_ids": ",".join(q["question_id"] for q in questions if q["status"] == "RESOLVED"),
            "open_questions": sum(1 for q in questions if q["status"] == "OPEN"),
            "must_fail": sum(1 for r in matrix if r["priority"] == "MUST" and r["status"] == "FAIL"),
            "must_in_progress": sum(1 for r in matrix if r["priority"] == "MUST" and r["status"] == "IN_PROGRESS"),
            "should_pass": sum(1 for r in matrix if r["priority"] == "SHOULD" and r["status"] == "PASS"),
        }
        for k, v in actual.items():
            if k not in claimed:
                err(f"AUDIT_REPORT.md: AUDIT_COUNTS lacks {k} (actual {v})")
            elif claimed[k].strip() != str(v):
                err(f"AUDIT_REPORT.md: {k}={claimed[k].strip()} but recomputed {v}")
        body = rep.split("<!-- AUDIT_COUNTS")[0]
        for q in questions:
            if q["severity"] in {"BLOCKER", "MAJOR"} and q["question_id"] not in body:
                err(f"AUDIT_REPORT.md: {q['severity']} {q['question_id']} not discussed in the report body")

    if errors:
        print(f"Audit consistency FAILED: {len(errors)} problem(s)")
        for e in errors:
            print(" - " + e)
        return 1
    print(f"Audit consistency PASS: {len(matrix)} matrix rows, {len(questions)} questions, "
          f"{len(checks)} input checks, TEST-001..TEST-054 planned, report counts verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
