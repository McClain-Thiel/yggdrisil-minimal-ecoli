#!/usr/bin/env python3
"""Audit terminal search reports and freeze nominations before any validation."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.plan_search_replication import (
    digest,
    encoded,
    gene_ids,
    gene_set_sha256,
    read_bytes,
    relative_path,
    state_id,
    verify_bundle,
)

TERMINAL = {"completed", "model_budget", "deadline", "exhausted", "failed"}


def decode_graph_value(value: Any) -> Any:
    """Read the frozen framework's data tags without importing stored classes."""
    if isinstance(value, list):
        return [decode_graph_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get("__yggdrisil__")
    if tag == "dict":
        pairs = value["items"]
        keys = [item[0] for item in pairs]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("Invalid graph mapping keys")
        return {key: decode_graph_value(item) for key, item in pairs}
    if tag in {"frozenset", "set", "tuple"}:
        return [decode_graph_value(item) for item in value["items"]]
    if tag == "object" and value.get("qualname") == "yggdrisil_ecoli.state:GenomeState":
        fields = value["fields"]
        if len(fields) != 1 or fields[0][0] != "deleted_genes":
            raise ValueError("Unexpected genome-state fields")
        return {"deleted_genes": decode_graph_value(fields[0][1])}
    if tag is not None:
        raise ValueError(f"Unsupported graph serialization tag: {tag}")
    return {key: decode_graph_value(item) for key, item in value.items()}


def export_graph_report(
    bundle: Path,
    job_index: int,
    graph_path: Path,
    terminal: dict[str, Any],
) -> dict[str, Any]:
    """Normalize a terminal graph through a read-only, consistent SQLite view."""
    spec, jobs = verify_bundle(bundle)
    if type(job_index) is not int or not 0 <= job_index < len(jobs):
        raise ValueError("Invalid job index")
    job = jobs[job_index]
    if terminal["status"] not in TERMINAL:
        raise ValueError("Cannot export a running search as a terminal result")
    root = json.loads(read_bytes(bundle / "inputs/starting-genotype.json"))
    protocol_hash = digest(read_bytes(bundle / "protocol.json"))
    with closing(
        sqlite3.connect(graph_path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if [r[0] for r in db.execute("PRAGMA quick_check")] != ["ok"]:
            raise ValueError("Graph failed SQLite quick_check")
        runs = db.execute("SELECT run_id, status, metadata_json FROM runs").fetchall()
        expected_runs = {job["run_id"]}
        if spec.get("study_kind") == "baseline_depth_continuation_v1":
            expected_runs.add(job["parent"]["run_id"])
            parent = next((r for r in runs if r["run_id"] == job["parent"]["run_id"]), None)
            if parent is None or parent["status"] != "completed":
                raise ValueError("Inherited parent run is missing or changed")
            parent_meta = decode_graph_value(json.loads(parent["metadata_json"]))
            if (parent_meta["evaluators"] != spec["evaluator_ids"]
                    or parent_meta["protocol_sha256"] != job["parent"]["protocol_sha256"]):
                raise ValueError("Inherited parent provenance changed")
        if len(runs) != len(expected_runs) or {r["run_id"] for r in runs} != expected_runs:
            raise ValueError("Graph contains a different or inherited run")
        current = next(r for r in runs if r["run_id"] == job["run_id"])
        if current["status"] not in {"completed", "failed"}:
            raise ValueError(
                "Graph is still running; inspect live process and lock before export"
            )
        meta = decode_graph_value(json.loads(current["metadata_json"]))
        if (
            meta["evaluators"] != spec["evaluator_ids"]
            or meta["protocol_sha256"] != protocol_hash
        ):
            raise ValueError("Graph evaluator/protocol provenance mismatch")
        # rowid records actual insert order when timestamps coincide. State hashes
        # sort lexically and must not determine evaluation-budget checkpoints.
        states = db.execute(
            "SELECT state_id, state_json FROM states ORDER BY rowid"
        ).fetchall()
        if not states or states[0]["state_id"] != root["state_id"]:
            raise ValueError("Graph root differs from the frozen starting genotype")
        records = []
        for index, node in enumerate(states):
            ids = gene_ids(
                decode_graph_value(json.loads(node["state_json"]))["deleted_genes"]
            )
            if state_id(ids) != node["state_id"]:
                raise ValueError("Graph genotype and state identity disagree")
            if index == 0:
                continue
            raw = db.execute(
                "SELECT evaluator_id, metrics_json FROM evaluations WHERE state_id=?",
                (node["state_id"],),
            ).fetchall()
            by_id = {r["evaluator_id"]: r["metrics_json"] for r in raw}
            if set(by_id) - set(spec["evaluator_ids"].values()):
                raise ValueError(
                    "Graph contains evaluations from an unexpected runtime"
                )
            evidence = {
                name: {
                    "evaluator_id": identity,
                    "metrics": decode_graph_value(json.loads(by_id[identity])),
                }
                for name, identity in spec["evaluator_ids"].items()
                if identity in by_id
            }
            complete = len(evidence) == len(spec["evaluator_ids"])
            records.append(
                {
                    "candidate_index": index,
                    "state_id": node["state_id"],
                    "deleted_gene_ids": ids,
                    "evaluations": evidence,
                    "evaluation_complete": complete,
                    "incomplete_reason": None
                    if complete
                    else "Missing evaluator records in terminal graph; inspect preserved failure evidence.",
                }
            )
    report = {
        **terminal,
        "run_id": job["run_id"],
        "method": job["method"],
        "seed": job["seed"],
        "protocol_sha256": protocol_hash,
        "starting_deletion_set_sha256": root["deletion_set_sha256"],
        "evaluator_ids": spec["evaluator_ids"],
        "candidates": records,
    }
    if spec.get("study_kind") == "baseline_depth_continuation_v1":
        prefix = meta["parent_checkpoint"]["candidate_prefix_sha256"]
        if digest(encoded(records[:100])) != prefix:
            raise ValueError("Inherited candidate records changed during continuation")
        report.update(
            inherited_candidates=100, additional_attempted_candidates=len(records) - 100,
            parent_run_id=job["parent"]["run_id"],
        )
    audit_report(spec, job, report, root["deleted_gene_ids"], protocol_hash)
    return report


def nonnegative(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid {label}: expected a finite nonnegative number")
    return float(value)


def audit_report(
    spec: dict[str, Any],
    job: dict[str, Any],
    report: dict[str, Any],
    root_ids: list[str],
    protocol_sha256: str,
) -> dict[str, Any]:
    for key in ("run_id", "method", "seed"):
        if report[key] != job[key]:
            raise ValueError(f"Report belongs to a different {key}")
    if report["protocol_sha256"] != protocol_sha256:
        raise ValueError("Result belongs to a different protocol")
    if report["starting_deletion_set_sha256"] != gene_set_sha256(root_ids):
        raise ValueError("Result has a different starting genotype")
    if report["evaluator_ids"] != spec["evaluator_ids"]:
        raise ValueError("Evaluator identities changed")
    if report["status"] not in TERMINAL:
        raise ValueError("Every search must be terminal before nomination")
    if report.get("validation_started") is not False:
        raise ValueError("Validation must not precede finalist freezing")
    if report["status"] == "failed" and not report.get("error"):
        raise ValueError("Failed attempts require explicit error evidence")
    spend = nonnegative(report["model_spend_usd"], "model spend")
    if spend > job["model_cap_usd"]:
        raise ValueError("Reported model spend exceeded the frozen job cap")
    wall = nonnegative(report["wall_time_s"], "wall time")
    cpu = nonnegative(report["cpu_hours"], "CPU hours")
    records = report["candidates"]
    if len(records) > job["new_candidate_slots"]:
        raise ValueError("Candidate budget exceeded")
    if report["status"] == "completed" and len(records) != job["new_candidate_slots"]:
        raise ValueError("An early stop cannot be labelled budget-complete")
    root = set(root_ids)
    seen, feasible, curves = set(), [], []
    best, complete = len(root), 0
    for index, row in enumerate(records, start=1):
        if type(row["candidate_index"]) is not int or row["candidate_index"] != index:
            raise ValueError("Candidate order must be contiguous and exclude the root")
        ids = gene_ids(row["deleted_gene_ids"])
        key = state_id(ids)
        if row["state_id"] != key or key in seen:
            raise ValueError("Duplicate candidate or incorrect canonical identity")
        if (
            not root < set(ids)
            or len(ids) > spec["starting_genotype"]["registry_genes"]
        ):
            raise ValueError("Candidate must preserve and extend the common root")
        seen.add(key)
        if type(row["evaluation_complete"]) is not bool:
            raise ValueError("Evaluation completeness must be explicit")
        accepted = False
        if row["evaluation_complete"]:
            complete += 1
            evidence = row["evaluations"]
            if set(evidence) != set(spec["evaluator_ids"]):
                raise ValueError("Complete candidate lacks the required evaluations")
            if any(
                evidence[n]["evaluator_id"] != h
                for n, h in spec["evaluator_ids"].items()
            ):
                raise ValueError("Candidate evaluator identity mismatch")
            metrics = {name: record["metrics"] for name, record in evidence.items()}
            fba, rba = metrics["fba"], metrics["resource_allocation"]
            growth = fba["growth_rate"]
            if growth is not None and (
                type(growth) not in (int, float) or not math.isfinite(growth)
            ):
                raise ValueError("Nonfinite or nonnumeric growth result")
            if (
                type(fba["feasible"]) is not bool
                or type(rba["feasible_at_growth_floor"]) is not bool
            ):
                raise ValueError("Growth feasibility must be explicitly boolean")
            if (
                rba["growth_rate_floor_h"]
                != spec["growth_gates"]["rba_growth_floor_per_hour"]
            ):
                raise ValueError("RBA growth floor changed")
            if metrics["genome_size"]["genes_deleted"] != len(ids):
                raise ValueError("Deletion count disagrees with the exact genotype")
            accepted = (
                fba["feasible"]
                and growth is not None
                and growth > 0
                and rba["feasible_at_growth_floor"]
            )
            if accepted:
                feasible.append(
                    {
                        "state_id": key,
                        "deleted_gene_ids": ids,
                        "deletion_set_sha256": gene_set_sha256(ids),
                        "genes_deleted": len(ids),
                        "additional_deletions": len(ids) - len(root),
                        "fba_growth": growth,
                        "candidate_index": index,
                    }
                )
                best = max(best, len(ids))
        elif not row.get("incomplete_reason"):
            raise ValueError("Incomplete evaluations require a recorded reason")
        curves.append(
            {
                "candidate_index": index,
                "state_id": key,
                "evaluation_complete": row["evaluation_complete"],
                "growth_gates_passed": accepted,
                "best_additional_deletions": best - len(root),
            }
        )
    if report["status"] == "completed" and complete != len(records):
        raise ValueError("Incomplete evaluations cannot be labelled completed")
    finalist = min(
        feasible,
        key=lambda row: (
            -row["additional_deletions"],
            -row["fba_growth"],
            row["state_id"],
        ),
        default=None,
    )
    # Missing checkpoints remain missing; early stopping never fabricates data.
    checkpoints = {
        str(n): curves[n - 1] if len(curves) >= n else None
        for n in spec["search"]["checkpoints"]
    }
    return {
        "run_id": job["run_id"],
        "method": job["method"],
        "seed": job["seed"],
        "status": report["status"],
        "stop_reason": report["stop_reason"],
        "attempted_unique_candidates": len(records),
        "completed_evaluations": complete,
        "full_evaluation_budget_reached": complete == job["new_candidate_slots"],
        "growth_gate_passing_candidates": len(feasible),
        "model_spend_usd": spend,
        "wall_time_s": wall,
        "cpu_hours": cpu,
        "checkpoints": checkpoints,
        "finalist": finalist,
        "search_improved": finalist is not None,
        "error": report.get("error"),
    }


def collect(bundle: Path) -> dict[str, Any]:
    spec, jobs = verify_bundle(bundle)
    protocol_hash = digest(read_bytes(bundle / "protocol.json"))
    root_ids = json.loads(read_bytes(bundle / "inputs/starting-genotype.json"))[
        "deleted_gene_ids"
    ]
    rows, nominations = [], {}
    for job in jobs:
        folder = relative_path(bundle, job["directory"])
        raw = read_bytes(folder / "run-report.json")
        receipt = json.loads(read_bytes(folder / "report-verification.json"))
        if (
            receipt.get("report_sha256") != digest(raw)
            or receipt.get("protocol_sha256") != protocol_hash
            or receipt.get("full_s3_get_verified") is not True
            or receipt.get("wandb_download_verified") is not True
            or receipt.get("wandb_state") != "COMMITTED"
            or receipt.get("source_and_inputs_verified") is not True
            or not str(receipt.get("s3_uri", "")).startswith(
                spec["tracking"]["s3_prefix"]
            )
            or not receipt.get("wandb_artifact")
        ):
            raise ValueError(f"Missing or mismatched verified report: {job['run_id']}")
        row = audit_report(spec, job, json.loads(raw), root_ids, protocol_hash)
        row.update(report_sha256=digest(raw), verification=receipt)
        if spec.get("study_kind") == "baseline_depth_continuation_v1":
            row.update(
                parent_run_id=job["parent"]["run_id"], inherited_candidates=100,
                additional_attempted_candidates=row["attempted_unique_candidates"] - 100,
                additional_completed_evaluations=row["completed_evaluations"] - 100,
                cumulative_wall_time_s=row["wall_time_s"] + job["parent"]["prior_wall_time_s"],
                cumulative_cpu_hours=row["cpu_hours"] + job["parent"]["prior_cpu_hours"],
            )
            if row["finalist"]:
                row["finalist"]["unchanged_parent_finalist"] = (
                    row["finalist"]["state_id"] == job["parent"]["previous_finalist_state_id"]
                )
        rows.append(row)
        candidate = row["finalist"]
        if candidate:
            nomination = nominations.setdefault(
                candidate["state_id"],
                {
                    **candidate,
                    "source_run_ids": [],
                    "validation_status": "not_launched",
                },
            )
            nomination["source_run_ids"].append(job["run_id"])
    return {
        "schema_version": 1,
        "experiment_id": spec["experiment_id"],
        "protocol_sha256": protocol_hash,
        "status": "nominations_frozen_pending_validation",
        "finalist_selection_frozen": True,
        "validation_launch_authorized": False,
        "runs": rows,
        "nominations": sorted(nominations.values(), key=lambda n: n["state_id"]),
        "controls": spec["validation"]["controls"],
        "maximum_validation_lineages": (len(nominations) + 2)
        * len(spec["validation"]["seeds"]),
        "interpretation": "Search feasibility is not whole-cell viability. Replication unit is the independent search run; identical genotypes may share validation, never independent validation evidence.",
        "verification_scope": "This offline collector validates receipts and hashes; the remote verifier must actually download S3 and W&B contents before issuing receipts.",
    }


def validation_outcome(
    records: list[dict[str, Any]], seeds: list[int]
) -> dict[str, Any]:
    if seeds != [101, 211, 307]:
        raise ValueError("Validation must use the three frozen simulation seeds")
    if len(records) != len(seeds) or {r["seed"] for r in records} != set(seeds):
        raise ValueError(
            "Exactly one validation outcome per planned simulation seed is required"
        )
    results = []
    reasons = {
        "reached_max_generations",
        "nondivision_max_duration",
        "model_exception",
        "deadline",
        "infrastructure_failure",
    }
    for row in records:
        reason, generations = row["terminal_reason"], row["generations"]
        if (
            reason not in reasons
            or type(generations) is not int
            or not 0 <= generations <= 20
        ):
            raise ValueError("Unknown terminal reason or invalid generation count")
        for key in ("finite_output", "exact_knockouts", "numerical_policy_passed"):
            if type(row[key]) is not bool:
                raise ValueError(f"Validation check must be explicit: {key}")
        if (reason == "reached_max_generations") != (generations == 20):
            raise ValueError("Endpoint count and terminal reason disagree")
        results.append(
            {
                **row,
                "passed": reason == "reached_max_generations"
                and all(
                    row[k]
                    for k in (
                        "finite_output",
                        "exact_knockouts",
                        "numerical_policy_passed",
                    )
                ),
            }
        )
    return {
        "all_three_seeds_passed": all(r["passed"] for r in results),
        "passed_lineages": sum(r["passed"] for r in results),
        "lineages": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--job-index", type=int)
    parser.add_argument("--terminal", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Refusing to replace a frozen finalist manifest")
    if any(x is not None for x in (args.graph, args.job_index, args.terminal)):
        if any(x is None for x in (args.graph, args.job_index, args.terminal)):
            parser.error("Graph export requires --graph, --job-index and --terminal")
        result = export_graph_report(
            args.bundle,
            args.job_index,
            args.graph,
            json.loads(read_bytes(args.terminal)),
        )
    else:
        result = collect(args.bundle)
    with args.output.open("xb") as handle:
        handle.write(encoded(result))
    if args.graph:
        print(
            f"Exported {len(result['candidates'])} candidate records; backup verification still required"
        )
    else:
        print(
            f"Frozen {len(result['nominations'])} unique finalists from {len(result['runs'])} terminal searches"
        )


if __name__ == "__main__":
    main()
