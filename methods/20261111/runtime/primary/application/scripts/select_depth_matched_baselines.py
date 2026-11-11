#!/usr/bin/env python3
"""Freeze a separately labelled depth-matched panel from verified search reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.collect_search_replication import collect
from scripts.plan_search_replication import digest, encoded

METHODS = ("no_retrieval", "frozen_dossier", "adaptive_literature")
SEEDS = (103, 227, 349, 461, 587)


def reference_rule(reference: dict[str, Any], reference_sha256: str) -> dict[str, Any]:
    if (
        reference.get("experiment_id") != "search-replication-20261109"
        or reference.get("finalist_selection_frozen") is not True
        or len(reference_sha256) != 64
        or any(c not in "0123456789abcdef" for c in reference_sha256)
    ):
        raise ValueError("Expected the frozen original agent nominations")
    rows = [r for r in reference["runs"] if r["method"] in METHODS]
    expected = {(method, seed) for method in METHODS for seed in SEEDS}
    if len(rows) != 15 or {(r["method"], r["seed"]) for r in rows} != expected:
        raise ValueError("All fifteen assigned agent searches must enter the target")
    cohort = []
    for row in sorted(rows, key=lambda r: (r["method"], r["seed"])):
        finalist = row.get("finalist")
        if not finalist or type(finalist["genes_deleted"]) is not int:
            raise ValueError("Missing agent finalist; never silently omit a search")
        count = finalist["genes_deleted"]
        if not 1030 < count <= 4290:
            raise ValueError("Invalid reference deletion count")
        cohort.append({
            "run_id": row["run_id"], "method": row["method"], "seed": row["seed"],
            "search_status": row["status"], "state_id": finalist["state_id"],
            "genes_deleted": count,
        })
    total = sum(r["genes_deleted"] for r in cohort)
    return {
        "reference_experiment_id": reference["experiment_id"],
        "reference_nominations_sha256": reference_sha256,
        "cohort": cohort,
        "reference_count": len(cohort),
        "reference_total_deletions": total,
        "reference_mean_total_deletions": total / len(cohort),
        "maximum_total_deletions": total // len(cohort),
        "registry_genes": 4290,
        "common_root_deletions": 1030,
        "rule": "One deepest fully evaluated growth-gate-passing candidate at or below the integer floor of the mean total deletions across all fifteen original agent finalists; ties by higher finite FBA growth then ascending state_id.",
        "whole_cell_outcomes_used": False,
        "deduplication": "Validate identical genotypes once and retain every source-run link; shared outcomes are not independent replicates.",
        "missing_or_shallower_run": "Retain the run in the denominator; report the exact shortfall or missing nominee. Never synthesize a genotype or replace after validation.",
    }


def select(bundle: Path, amendment: dict[str, Any], reference_path: Path) -> dict[str, Any]:
    raw = reference_path.read_bytes()
    rule = reference_rule(json.loads(raw), digest(raw))
    if amendment.get("selection") != rule:
        raise ValueError("Depth target or reference cohort changed")
    if amendment.get("status") != "selection_frozen_before_new_validation":
        raise ValueError("Selection amendment must be frozen before validation")
    original = collect(bundle)
    if original["experiment_id"] != amendment.get("search_experiment_id"):
        raise ValueError("Amendment belongs to another search")
    if json.loads((bundle / "nominations.json").read_bytes()) != original:
        raise ValueError("Preserve the original unrestricted nominations first")
    result = collect(bundle, maximum_deletions=rule["maximum_total_deletions"])
    for row in result["runs"]:
        finalist = row["finalist"]
        row["deletion_shortfall_from_target"] = (
            rule["maximum_total_deletions"] - finalist["genes_deleted"]
            if finalist else None
        )
    result.update(
        selection_amendment_sha256=digest(encoded(amendment)),
        reference_nominations_sha256=digest(raw),
        unrestricted_nominations_sha256=digest((bundle / "nominations.json").read_bytes()),
        selection=rule,
        interpretation="Exploratory depth-matched comparison selected after observing baseline search depth, before any new whole-cell outcomes. Original search budgets, unrestricted endpoints and failures remain separate and unchanged. Search feasibility is not whole-cell viability.",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--amendment", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("Scientific report selection runs on remote Linux only")
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite a frozen selection")
    result = select(args.bundle, json.loads(args.amendment.read_bytes()), args.reference)
    with args.output.open("xb") as handle:
        handle.write(encoded(result))
    print(f"Selected {len(result['nominations'])} unique depth-matched genomes; backup verification and validation approval are required before launch")


if __name__ == "__main__":
    main()
