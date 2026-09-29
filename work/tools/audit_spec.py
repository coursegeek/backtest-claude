#!/usr/bin/env python3
"""Static audit of input/python_backtest_specification_v3_1.csv (diagnostic tool).

Produces an inventory used by the requirement map and the question log:
  work/audit/spec_inventory.json        counts, outputs, CLI options, commands
  work/audit/spec_config_keys.csv       config keys shared by several rows / conflicting defaults
  work/audit/spec_cross_references.csv  explicit and implicit requirement-to-requirement links,
                                        including MUST rows that depend on SHOULD rows

Usage: python work/tools/audit_spec.py
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "input" / "python_backtest_specification_v3_1.csv"
OUT_DIR = ROOT / "work" / "audit"
TEXT_FIELDS = ["requirement", "default_value", "cli_option", "config_key", "allowed_values",
               "logic_formula", "output", "acceptance_criteria", "notes"]
ID_RE = re.compile(r"\b([A-Z]{2,6}-\d{3})\b")
OPT_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")
FILE_RE = re.compile(r"\b([A-Za-z_]+\.(?:csv|json|yaml))\b")


def load_spec():
    with SPEC.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    rows = load_spec()
    by_id = {r["requirement_id"]: r for r in rows}
    assert len(by_id) == len(rows), "duplicate requirement_id in specification"

    # ---- counts
    prio = Counter(r["priority"] for r in rows)
    sections = OrderedDict()
    for r in rows:
        s = sections.setdefault(r["section"], {"MUST": 0, "SHOULD": 0})
        s[r["priority"]] += 1

    # ---- outputs
    outputs = defaultdict(list)
    for r in rows:
        for name in FILE_RE.findall(r["output"]) + FILE_RE.findall(r["default_value"] if r["section"] == "20_REPORTING" else ""):
            if r["requirement_id"] not in outputs[name]:
                outputs[name].append(r["requirement_id"])

    # ---- CLI options and commands
    defined, used_in_examples = defaultdict(list), defaultdict(list)
    for r in rows:
        opts = OPT_RE.findall(r["cli_option"])
        target = used_in_examples if r["section"] == "23_CLI_EXAMPLES" else defined
        for o in opts:
            if r["requirement_id"] not in target[o]:
                target[o].append(r["requirement_id"])
    example_only = sorted(o for o in used_in_examples if o not in defined)
    commands = sorted({r["default_value"] for r in rows if r["data_type"] == "command"})
    example_commands = sorted({m.group(1) for r in rows if r["section"] == "23_CLI_EXAMPLES"
                               for m in [re.search(r"backtest\.py\s+([a-z-]+)", r["cli_option"])] if m})

    # ---- config keys
    keys = defaultdict(list)
    for r in rows:
        if r["config_key"] and r["data_type"] != "test":
            keys[r["config_key"]].append(r)
    key_rows = []
    for k in sorted(keys):
        rs = keys[k]
        if len(rs) < 2:
            continue
        defaults = sorted({r["default_value"] for r in rs if r["default_value"]})
        key_rows.append(OrderedDict([
            ("config_key", k), ("requirement_ids", ";".join(r["requirement_id"] for r in rs)),
            ("priorities", ";".join(r["priority"] for r in rs)),
            ("distinct_defaults", " || ".join(defaults)),
            ("conflict", "YES" if len(defaults) > 1 and k != "report.files" else "no"),
        ]))

    # ---- cross references
    xref = []
    for r in rows:
        rid = r["requirement_id"]
        seen = set()
        for fld in TEXT_FIELDS:
            for ref in ID_RE.findall(r[fld]):
                if ref != rid and ref in by_id and ref not in seen:
                    seen.add(ref)
                    xref.append(OrderedDict([
                        ("from_id", rid), ("from_priority", r["priority"]), ("to_id", ref),
                        ("to_priority", by_id[ref]["priority"]), ("kind", "explicit_id"),
                        ("evidence", f"{fld} mentions {ref}")]))
    # implicit: a MUST row shares a config key / option / enum value with a SHOULD row
    should_rows = [r for r in rows if r["priority"] == "SHOULD"]
    for s in should_rows:
        tokens = set()
        tokens |= set(OPT_RE.findall(s["cli_option"]))
        for v in re.split(r"[,|]", s["allowed_values"]):
            v = v.strip()
            if "_" in v and len(v) > 8:
                tokens.add(v)
        for m in rows:
            if m["priority"] != "MUST" or m["requirement_id"] == s["requirement_id"]:
                continue
            ev = []
            if s["config_key"] and s["config_key"] == m["config_key"]:
                ev.append(f"same config_key {s['config_key']}")
            text = " ".join(m[f] for f in TEXT_FIELDS)
            for t in sorted(tokens):
                if t.startswith("--") and t in OPT_RE.findall(text):
                    ev.append(f"mentions option {t}")
                elif not t.startswith("--") and re.search(rf"\b{re.escape(t)}\b", text):
                    ev.append(f"mentions value {t}")
            sk = s["config_key"].split(".")[-1].split("/")[0] if s["config_key"] else ""
            if sk and len(sk) > 8 and re.search(rf"\b{re.escape(sk)}\b", text) and not ev:
                ev.append(f"mentions key fragment {sk}")
            if ev:
                xref.append(OrderedDict([
                    ("from_id", m["requirement_id"]), ("from_priority", "MUST"),
                    ("to_id", s["requirement_id"]), ("to_priority", "SHOULD"),
                    ("kind", "implicit_should_dependency"), ("evidence", "; ".join(ev))]))
    # Semantic links that no token match can find; each carries its justification.
    semantic_links = [
        ("TEST-044", "DATA-010", "TEST-044 verifies the FF ZIP behaviour defined only by DATA-010"),
        ("TEST-034", "IND-010", "TEST-034 verifies the 5-year carry-forward defined by IND-010"),
        ("FND-005", "FND-009", "allowed value distribution_schedule needs the schedule input defined only by FND-009"),
        ("REPRO-006", "OPT-009", "identical optimizer output requires a deterministic tie-break"),
    ]
    for a, b, why in semantic_links:
        xref.append(OrderedDict([
            ("from_id", a), ("from_priority", by_id[a]["priority"]), ("to_id", b),
            ("to_priority", by_id[b]["priority"]), ("kind", "implicit_should_dependency"),
            ("evidence", why)]))
    xref.sort(key=lambda x: (x["from_id"], x["to_id"], x["kind"]))

    must_on_should = sorted({(x["from_id"], x["to_id"]) for x in xref
                             if x["from_priority"] == "MUST" and x["to_priority"] == "SHOULD"})
    test_ids = sorted(r["requirement_id"] for r in rows if r["section"] == "24_TESTS")

    inventory = OrderedDict(
        spec_file=str(SPEC.relative_to(ROOT)), rows=len(rows), priority_counts=dict(prio),
        sections=sections, test_rows=len(test_ids), test_ids_first_last=[test_ids[0], test_ids[-1]],
        commands_declared=commands, commands_in_examples=example_commands,
        output_files=OrderedDict(sorted(outputs.items())),
        cli_options_defined=OrderedDict(sorted(defined.items())),
        cli_options_only_in_examples=example_only,
        config_keys_with_conflicting_defaults=[k["config_key"] for k in key_rows if k["conflict"] == "YES"],
        must_depends_on_should=[f"{a}->{b}" for a, b in must_on_should],
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "spec_inventory.json").write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (OUT_DIR / "spec_config_keys.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(key_rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(key_rows)
    with (OUT_DIR / "spec_cross_references.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(xref[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(xref)
    print(f"Spec audit: {len(rows)} rows {dict(prio)}; {len(outputs)} output files; "
          f"{len(inventory['config_keys_with_conflicting_defaults'])} config keys with conflicting defaults; "
          f"{len(must_on_should)} MUST->SHOULD dependencies; {len(xref)} cross references")


if __name__ == "__main__":
    main()
