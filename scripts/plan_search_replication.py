#!/usr/bin/env python3
"""Prepare and verify the replication study offline; never dispatch experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

METHODS = (
    "no_retrieval",
    "frozen_dossier",
    "adaptive_literature",
    "randomized_block",
    "greedy_block",
)
AGENT_METHODS = METHODS[:3]
EVALUATORS = {
    "essentiality",
    "fba",
    "genome_size",
    "module_retention",
    "resource_allocation",
}


def encoded(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path) -> bytes:
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if len(data) != before.st_size or (
        before.st_size,
        before.st_mtime_ns,
        before.st_ino,
    ) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise OSError(f"Short read or changing input: {path}")
    return data


def relative_path(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe relative path: {name}")
    result = (root / path).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes bundle: {name}")
    return result


def gene_ids(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(g, str) or not re.fullmatch(r"b[0-9]{4}", g) for g in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("Expected unique canonical gene IDs")
    return sorted(value)


def gene_set_sha256(ids: list[str]) -> str:
    return digest(json.dumps(gene_ids(ids), separators=(",", ":")).encode())


def state_id(ids: list[str]) -> str:
    return digest(
        json.dumps(
            {"deleted_genes": gene_ids(ids)}, separators=(",", ":"), sort_keys=True
        ).encode()
    )


def positive_number(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return float(value)


def validate_protocol(spec: dict[str, Any]) -> None:
    if spec["schema_version"] != 1 or spec["status"] != "draft_not_authorized":
        raise ValueError("Expected a version 1 unapproved draft protocol")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", spec["experiment_id"]):
        raise ValueError("Unsafe experiment ID")
    if spec["methods"] != list(METHODS):
        raise ValueError("All five methods must participate")
    seeds = spec["search_seeds"]
    if (
        len(seeds) != 5
        or len(set(seeds)) != 5
        or any(type(s) is not int or s < 0 for s in seeds)
    ):
        raise ValueError("Five distinct independent search seeds are required")
    if type(spec["schedule_seed"]) is not int:
        raise ValueError("Schedule seed must be an integer")
    search = spec["search"]
    for name in ("new_candidate_slots", "max_steps", "max_wall_time_s"):
        if type(search[name]) is not int or search[name] <= 0:
            raise ValueError(f"Invalid search limit: {name}")
    if search["new_candidate_slots"] != 100 or search["checkpoints"] != [25, 50, 100]:
        raise ValueError("Expected the scoped 100-candidate study and checkpoints")
    if search["max_steps"] < search["new_candidate_slots"]:
        raise ValueError("Step limit is smaller than the candidate budget")
    if (
        search["fresh_sessions"] is not True
        or search["validation_feedback"] is not False
    ):
        raise ValueError("Searches must start fresh without validation feedback")
    if spec["growth_gates"] != {
        "fba_growth_strictly_positive": True,
        "rba_growth_floor_per_hour": 0.1,
    }:
        raise ValueError("Growth gates differ from the frozen comparator")
    if set(spec["evaluator_ids"]) != EVALUATORS:
        raise ValueError("All five evaluator identities must be frozen")
    refs = [*spec["inputs"].values(), *spec["source_references"].values()]
    for ref in refs:
        if not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
            raise ValueError("Missing SHA256 input/source identity")
        if Path(ref["path"]).is_absolute() or ".." in Path(ref["path"]).parts:
            raise ValueError("Unsafe scientific reference path")
    for checksum in spec["evaluator_ids"].values():
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Invalid evaluator identity")
    if spec["selection"] != {
        "finalists_per_run": 1,
        "order": ["additional_deletions_desc", "fba_growth_desc", "state_id_asc"],
        "replacement_after_validation": False,
        "freeze_all_before_validation": True,
    }:
        raise ValueError("Finalist selection or validation separation changed")
    validation = spec["validation"]
    if (
        validation["generations"] != 20
        or validation["seeds"] != [101, 211, 307]
        or validation["controls"] != ["MG1655", "common-1030-deletion-root"]
        or validation["followed_daughters"] != 1
        or validation["max_cell_time_s"] != 10800
    ):
        raise ValueError("Whole-cell validation differs from the scoped study")
    if not re.fullmatch(r"[0-9a-f]{64}", validation["solver_patch_sha256"]):
        raise ValueError("Invalid numerical-policy patch identity")
    if not re.fullmatch(r"[0-9a-f]{40}", validation["native_commit"]):
        raise ValueError("Invalid native model commit")
    if (
        spec["agent"]["edison"] is not False
        or spec["agent"]["orchestration"] != "manager"
    ):
        raise ValueError("Expected the same manager with no new Edison jobs")
    forbidden = {"mode", "seed", "session_path", "edison_cache_path", "continuation"}
    if forbidden & spec["agent"].keys():
        raise ValueError("Run-specific memory/configuration belongs to individual jobs")
    date.fromisoformat(spec["literature"]["publication_cutoff"])
    if spec["authorization"] != {
        "preparation": True,
        "paid_launch": False,
        "approved_total_usd": None,
    }:
        raise ValueError("A planning file cannot grant paid-run authorization")
    budget = spec["budget_proposal"]
    per_run = positive_number(budget["model_per_run_cap_usd"], "Model cap")
    reserve = positive_number(
        budget["infrastructure_reserve_usd"], "Infrastructure reserve"
    )
    total = positive_number(budget["total_cap_usd"], "Study cap")
    if len(AGENT_METHODS) * len(seeds) * per_run + reserve > total:
        raise ValueError("Per-run budgets exceed the study ceiling")
    if spec["execution"]["platform"] != "remote_linux_only":
        raise ValueError("Scientific execution must remain remote-only")
    if spec["tracking"]["s3_prefix"] != (
        "s3://phd-research-storage-1758274488/yggdrisil/experiments/"
        + spec["experiment_id"]
        + "/"
    ):
        raise ValueError("Unexpected experiment archive destination")


def build_jobs(spec: dict[str, Any]) -> list[dict[str, Any]]:
    validate_protocol(spec)
    jobs = []
    for block, seed in enumerate(spec["search_seeds"]):
        # Hash ordering is independent of Python's random implementation.
        order = sorted(
            METHODS,
            key=lambda method: digest(
                f"{spec['schedule_seed']}:{block}:{method}".encode()
            ),
        )
        for position, method in enumerate(order):
            run_id = f"{spec['experiment_id']}-{method.replace('_', '-')}-seed-{seed}"
            folder = f"search/{run_id}"
            agent = method in AGENT_METHODS
            jobs.append(
                {
                    "run_id": run_id,
                    "method": method,
                    "seed": seed,
                    "block": block,
                    "position_in_block": position,
                    "directory": folder,
                    "graph_path": f"{folder}/graph.sqlite",
                    "starting_deletion_set_sha256": spec["starting_genotype"][
                        "deletion_set_sha256"
                    ],
                    "new_candidate_slots": spec["search"]["new_candidate_slots"],
                    "max_states_including_root": spec["search"]["new_candidate_slots"]
                    + 1,
                    "model_cap_usd": spec["budget_proposal"]["model_per_run_cap_usd"]
                    if agent
                    else 0,
                    "agent_config": {
                        **deepcopy(spec["agent"]),
                        "seed": seed,
                        "mode": "research"
                        if method == "adaptive_literature"
                        else "tool-rich",
                        "require_initial_research": method == "adaptive_literature",
                        "session_path": f"{folder}/agents.sqlite",
                        "edison_cache_path": f"{folder}/edison-disabled.sqlite",
                    }
                    if agent
                    else None,
                    "dossier": "inputs/dossier.json"
                    if method in METHODS[1:3]
                    else None,
                    "live_literature": method == "adaptive_literature",
                    "publication_cutoff": spec["literature"]["publication_cutoff"]
                    if method == "adaptive_literature"
                    else None,
                    "status": "not_launched",
                }
            )
    return jobs


def prepare(protocol: Path, root: Path, output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"Refusing to replace prepared study: {output}")
    spec = json.loads(read_bytes(protocol))
    jobs = build_jobs(spec)
    start_ref = spec["starting_genotype"]["local_reference"]
    start_raw = read_bytes(relative_path(root, start_ref["path"]))
    if digest(start_raw) != start_ref["sha256"]:
        raise ValueError("Starting-genotype file changed")
    start = json.loads(start_raw)
    ids = gene_ids(start["deleted_gene_ids"])
    if len(ids) != spec["starting_genotype"]["deletion_count"] or (
        gene_set_sha256(ids) != spec["starting_genotype"]["deletion_set_sha256"]
    ):
        raise ValueError("Starting genotype differs from the common root")
    source_refs = spec["source_references"]
    for label, ref in source_refs.items():
        if digest(read_bytes(relative_path(root, ref["path"]))) != ref["sha256"]:
            raise ValueError(f"Changed source reference: {label}")
    previous = json.loads(
        read_bytes(relative_path(root, source_refs["validation_protocol"]["path"]))
    )
    if (
        spec["validation"]["solver_patch_sha256"] != previous["solver_patch_sha256"]
        or spec["validation"]["native_commit"]
        != previous["native_runtime"]["git_commit"]
    ):
        raise ValueError("Validation runtime differs from the historical comparison")
    local_sources = {
        "scripts/plan_search_replication.py": read_bytes(Path(__file__)),
        "scripts/collect_search_replication.py": read_bytes(
            root / "scripts/collect_search_replication.py"
        ),
    }
    payloads = {
        "protocol.json": encoded(spec),
        "jobs.json": encoded(jobs),
        "inputs/starting-genotype.json": encoded(
            {
                "name": spec["starting_genotype"]["name"],
                "deleted_gene_ids": ids,
                "deletion_set_sha256": gene_set_sha256(ids),
                "state_id": state_id(ids),
            }
        ),
        "source-manifest.json": encoded(
            {
                "study_tools": {
                    name: digest(raw) for name, raw in local_sources.items()
                },
                "historical_method_references": source_refs,
                "runtime_snapshot_status": "must_be_frozen_before_launch",
            }
        ),
    }
    manifest = {
        "schema_version": 1,
        "experiment_id": spec["experiment_id"],
        "status": "prepared_not_authorized",
        "launch_ready": False,
        "search_runs": len(jobs),
        "agent_runs": sum(j["agent_config"] is not None for j in jobs),
        "maximum_new_candidates": len(jobs) * spec["search"]["new_candidate_slots"],
        "maximum_validation_lineages": (len(jobs) + 2) * 3,
        "maximum_validation_cell_cycles": (len(jobs) + 2) * 3 * 20,
        "calibration_cell_cycles": 2,
        "files": {name: digest(raw) for name, raw in payloads.items()},
        "launch_blockers": spec["launch_requirements"],
        "data_policy": "Metadata and root only; no live databases, credentials or source archives. Source hashes reference separately versioned code.",
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in {**payloads, "manifest.json": encoded(manifest)}.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(raw)
    verify_bundle(output)
    return manifest


def verify_bundle(bundle: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(read_bytes(bundle / "manifest.json"))
    required = {
        "protocol.json",
        "jobs.json",
        "inputs/starting-genotype.json",
        "source-manifest.json",
    }
    if set(manifest["files"]) != required:
        raise ValueError("Incomplete study bundle")
    for name, checksum in manifest["files"].items():
        if digest(read_bytes(relative_path(bundle, name))) != checksum:
            raise ValueError(f"Changed study input: {name}")
    spec = json.loads(read_bytes(bundle / "protocol.json"))
    jobs = json.loads(read_bytes(bundle / "jobs.json"))
    if jobs != build_jobs(spec):
        raise ValueError("Jobs do not match the deterministic protocol schedule")
    start = json.loads(read_bytes(bundle / "inputs/starting-genotype.json"))
    if (
        gene_set_sha256(start["deleted_gene_ids"])
        != spec["starting_genotype"]["deletion_set_sha256"]
    ):
        raise ValueError("Changed starting genotype")
    if manifest["experiment_id"] != spec["experiment_id"]:
        raise ValueError("Manifest study identity mismatch")
    return spec, jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol", type=Path, default=Path("benchmarks/search_replication.json")
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if (args.output is None) == (args.verify is None):
        parser.error("Choose exactly one of --output or --verify")
    if args.verify:
        _, jobs = verify_bundle(args.verify)
        print(f"Verified {len(jobs)} planned jobs; no execution authorized")
    else:
        print(json.dumps(prepare(args.protocol, args.repo_root, args.output), indent=2))


if __name__ == "__main__":
    main()
