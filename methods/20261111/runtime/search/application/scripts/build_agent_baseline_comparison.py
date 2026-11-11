#!/usr/bin/env python3
"""Join verified search nominees to held-out trajectories without running science."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

AGENT_SEARCH = "literature-ablation-kimi-continuation-20261106"
AGENT_VALIDATION = "vecoli-kimi-continuation-20gen-3seed-20261106"
BASELINE_SEARCH = "deletion-baselines-20261106"
BASELINE_VALIDATION = "vecoli-deletion-baselines-20gen-3seed-20261106"


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def checked(path: Path, ref: dict[str, Any]) -> Any:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref["sha256"] or len(data) != ref["bytes"]:
        raise ValueError(f"Analysis differs from verified ledger reference: {path}")
    return json.loads(data)


def build(root: Path) -> dict[str, Any]:
    ledger = {e["id"]: e for e in read(root / "experiments.json")["experiments"]}
    aroot = root / "runs" / AGENT_SEARCH
    broot = root / "runs" / BASELINE_SEARCH
    vroot = root / "runs" / BASELINE_VALIDATION
    refs = {
        "agent_validation": ledger[AGENT_VALIDATION]["data_backup"][
            "completion_records"
        ]["final-analysis.json"],
        "baseline_search": ledger[BASELINE_SEARCH]["results"],
        "baseline_validation": ledger[BASELINE_VALIDATION]["final_analysis"],
        "functional_comparison": ledger[AGENT_SEARCH]["deletion_comparability"][
            "report"
        ],
    }
    av = checked(aroot / "final-analysis.json", refs["agent_validation"])
    bs = checked(broot / "final/analysis.json", refs["baseline_search"])
    bv = checked(vroot / "final/final-analysis.json", refs["baseline_validation"])
    functional = checked(
        aroot / "functional-gene-comparison.json", refs["functional_comparison"]
    )
    amap = {e["state_id"]: e for e in functional["finalists"]}
    bmap = {e["state_id"]: e for e in bs["rows"]}
    if len(bmap) != 10:
        raise ValueError("Expected ten distinct frozen baseline nominees")
    rows = []
    for study, trajectories in (
        (AGENT_VALIDATION, av["original_lineages"]),
        (BASELINE_VALIDATION, bv["lineages"]),
    ):
        groups: dict[str, list[dict[str, Any]]] = {}
        for lineage in trajectories:
            groups.setdefault(lineage["state_id"], []).append(lineage)
        for state, group in groups.items():
            if sorted(e["seed"] for e in group) != [101, 211, 307]:
                raise ValueError(f"Missing or duplicated simulation seeds: {state}")
            first = group[0]
            for row in group:
                for key in ("name", "deletion_count", "deletion_set_sha256"):
                    if row[key] != first[key]:
                        raise ValueError(
                            f"Genotype identity changes across seeds: {state}"
                        )
            control = state in amap and amap[state]["name"].endswith("control")
            if study == AGENT_VALIDATION or control:
                source = amap[state]
                if source["deletion_set_sha256"] != first["deletion_set_sha256"]:
                    raise ValueError(f"Functional mapping genotype mismatch: {state}")
                denominators = {
                    key: {
                        "deleted": source[name]["deleted"],
                        "total": source[name]["denominator"],
                        "percent": source[name]["percent"],
                    }
                    for key, name in (
                        ("registry", "full_registry"),
                        ("current_functional", "current_functional_set"),
                        ("shared_functional", "shared_functional_set"),
                    )
                }
                method = (
                    "control" if control else "agent_" + first["name"].replace("-", "_")
                )
                search_id = None if control else AGENT_SEARCH
                search_seed = None if control else 59
            else:
                source = bmap[state]
                denominators = source["denominators"]
                method, search_id, search_seed = (
                    source["method"],
                    BASELINE_SEARCH,
                    source["seed"],
                )
            if denominators["registry"]["deleted"] != first["deletion_count"]:
                raise ValueError(
                    f"Search and validation deletion counts differ: {state}"
                )
            for value in denominators.values():
                if (
                    abs(value["percent"] - 100 * value["deleted"] / value["total"])
                    > 1e-9
                ):
                    raise ValueError("Inconsistent deletion percentage")
            rows.append(
                {
                    "name": first["name"],
                    "method": method,
                    "is_control": control,
                    "search_experiment_id": search_id,
                    "validation_experiment_id": study,
                    "search_seed": search_seed,
                    "state_id": state,
                    "deletion_set_sha256": first["deletion_set_sha256"],
                    "denominators": denominators,
                    "lineages": [
                        {
                            "seed": row["seed"],
                            "generations_completed": row["generations_completed"],
                            "terminal_reason": row["terminal_reason"],
                            "solver_assisted_generations": row.get(
                                "solver_assisted_generations",
                                row.get("solver_assisted_completed_generations", []),
                            ),
                        }
                        for row in sorted(group, key=lambda r: r["seed"])
                    ],
                }
            )
    summary = {}
    for group_name, selected in (
        ("agent", [r for r in rows if r["method"].startswith("agent_")]),
        (
            "baseline",
            [r for r in rows if r["method"] in {"randomized_block", "greedy_block"}],
        ),
        ("controls", [r for r in rows if r["is_control"]]),
    ):
        lines = [line for row in selected for line in row["lineages"]]
        summary[group_name] = {
            "distinct_genotypes": len({r["state_id"] for r in selected}),
            "lineages": len(lines),
            "lineages_at_20": sum(
                line["generations_completed"] == 20 for line in lines
            ),
            "completed_divisions": sum(line["generations_completed"] for line in lines),
            "terminal_reasons": dict(
                Counter(line["terminal_reason"] for line in lines)
            ),
        }
    return {
        "schema_version": 1,
        "comparison_id": "agent-baselines-20261106",
        "analysis_type": "Read-only join of independently verified final analyses; no new simulations.",
        "sources": refs,
        "summary": summary,
        "genotypes": rows,
        "methods_manifest": "methods/20261106/manifest.json",
        "limitations": [
            "Agent conditions each have one search seed and inherited search graphs; baselines have five search seeds per method from the common 1030-deletion root.",
            "Simulation seeds repeat each genome; they are not independent genome-search replicates. Controls are repeated in both validation studies.",
            "Baseline searches used 500 new evaluated states each; agent searches stopped on model budgets. This is not a matched-cost or matched-evaluation comparison.",
            "Functional percentages use the same five-process accounting rule and 1915-gene current-model set; the shared historical panel contains 1197 genes.",
            "Model exceptions remain separate from nondivision at the 10800-second simulated cell-time cap.",
            "Expression-knockout projection in aerobic M9 glucose; no physical chromosome reduction or laboratory viability claim.",
            "The same strict v2 numerical fallback policy applies to both validation studies; assisted generations remain explicitly marked.",
            "These results do not independently establish a benefit from literature retrieval or a statistically replicated agent-method advantage.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
