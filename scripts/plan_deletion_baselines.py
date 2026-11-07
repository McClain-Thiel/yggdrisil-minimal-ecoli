#!/usr/bin/env python3
"""Freeze non-LLM deletion controls without loading models or launching jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from yggdrisil_ecoli.data.io import atomic_json, file_sha256

KINDS = ("randomized_block", "greedy_block")


def validate_protocol(spec: dict[str, Any]) -> None:
    if spec["schema_version"] != 1 or spec["status"] != "prepared_not_launched":
        raise ValueError("Expected a version 1 unlaunched protocol")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", spec["experiment_id"]):
        raise ValueError("Unsafe experiment ID")
    if spec["methods"] != list(KINDS):
        raise ValueError("Expected the two frozen non-LLM methods")
    seeds = spec["seeds"]
    if not seeds or len(set(seeds)) != len(seeds) or any(
        type(s) is not int or s < 0 for s in seeds
    ):
        raise ValueError("Seeds must be distinct nonnegative integers")
    for k in ("new_evaluated_states", "max_steps", "max_wall_time_s"):
        if type(spec["limits"][k]) is not int or spec["limits"][k] < 1:
            raise ValueError(f"Invalid limit: {k}")
    if spec["limits"]["max_steps"] < spec["limits"]["new_evaluated_states"]:
        raise ValueError("Step cap must not penalize first-improvement search")
    if spec["growth_gates"] != {
        "fba_growth_strictly_positive": True, "rba_growth_floor_per_hour": 0.1
    }:
        raise ValueError("Growth gates differ from the agent experiment")
    sizes = spec["policy"]["block_sizes"]
    if (
        not sizes or sizes[-1] != 1 or sizes != sorted(set(sizes), reverse=True)
        or any(type(n) is not int or not 1 <= n <= 100 for n in sizes)
        or type(spec["policy"]["greedy_neighborhood_size"]) is not int
        or spec["policy"]["greedy_neighborhood_size"] < 2
    ):
        raise ValueError("Invalid block neighborhood")
    for ref in spec["inputs"].values():
        p = Path(ref["path"])
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("Inputs must be relative to the remote application")
        if not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
            raise ValueError("Input lacks a SHA256")
    if spec["tracking"]["s3_bucket"] != "phd-research-storage-1758274488":
        raise ValueError("Unexpected scientific archive destination")
    if spec["tracking"]["s3_prefix"] != f"yggdrisil/experiments/{spec['experiment_id']}/":
        raise ValueError("Archive prefix must be scoped to this experiment")
    if spec["authorization"]["paid_launch"] is not False:
        raise ValueError("Planning cannot authorize paid execution")


def source_files(root: Path, framework: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for label, base in (("application", root), ("framework", framework)):
        for p in sorted((base / "src").rglob("*")):
            # Partial packaging metadata becomes a nameless installed distribution
            # when the frozen source tree is added to PYTHONPATH.
            if any(part.endswith((".egg-info", ".dist-info")) for part in p.parts):
                continue
            if p.is_file() and p.suffix in {".py", ".txt", ".typed"}:
                files[f"{label}/{p.relative_to(base)}"] = p
        files[f"{label}/pyproject.toml"] = base / "pyproject.toml"
    for name in ("plan_deletion_baselines.py", "run_deletion_baseline.py", "collect_deletion_baselines.py"):
        files[f"application/scripts/{name}"] = root / "scripts" / name
    for name in ("conftest.py", "test_deletion_baselines.py"):
        files[f"application/tests/{name}"] = root / "tests" / name
    return files


def prepare(
    protocol: Path, starting_genotype: Path, output: Path,
    root: Path, framework: Path,
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    spec = json.loads(protocol.read_text())
    validate_protocol(spec)
    start = json.loads(starting_genotype.read_text())
    ids = start["deleted_gene_ids"]
    if (
        len(set(ids)) != len(ids)
        or any(not isinstance(g, str) or not re.fullmatch(r"b\d{4}", g) for g in ids)
        or len(ids) != spec["starting_genotype"]["deletion_count"]
    ):
        raise ValueError("Invalid starting genotype")
    digest = hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest()
    if digest != spec["starting_genotype"]["deletion_set_sha256"]:
        raise ValueError("Starting genotype differs from frozen agent root")
    jobs = []
    for i, seed in enumerate(spec["seeds"]):
        for kind in KINDS[i % 2:] + KINDS[:i % 2]:
            run_id = f"{spec['experiment_id']}-{kind.replace('_', '-')}-seed-{seed}"
            jobs.append({"run_id": run_id, "kind": kind, "seed": seed,
                         "directory": f"search/{run_id}"})
    # Source is retained locally as source, never included in data artifacts.
    sources = source_files(root, framework)
    source_bytes = {name: path.read_bytes() for name, path in sources.items()}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in source_bytes.items()}
    output.mkdir(parents=True, exist_ok=False)
    for name, data in source_bytes.items():
        p = output / "source" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    atomic_json(output / "protocol.json", spec)
    atomic_json(output / "jobs.json", jobs)
    atomic_json(output / "inputs/starting-genotype.json", {
        **start, "deleted_gene_ids": sorted(ids), "deletion_set_sha256": digest,
    })
    atomic_json(output / "source-manifest.json", {"files": hashes})
    files = ["protocol.json", "jobs.json", "inputs/starting-genotype.json", "source-manifest.json"]
    manifest = {
        "schema_version": 1, "experiment_id": spec["experiment_id"],
        "status": "prepared_not_launched", "launch_authorized": False,
        "jobs": len(jobs),
        "maximum_new_evaluated_states": len(jobs) * spec["limits"]["new_evaluated_states"],
        "files": {name: file_sha256(output / name) for name in files},
        "data_policy": "Only allowlisted data files enter S3/W&B. source/ stays source-only; credentials are never bundled.",
    }
    atomic_json(output / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--starting-genotype", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--framework-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.protocol, args.starting_genotype, args.output,
                             args.repo_root, args.framework_root), indent=2))


if __name__ == "__main__":
    main()
