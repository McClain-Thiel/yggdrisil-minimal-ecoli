#!/usr/bin/env python3
"""Prepare isolated, fixed-budget baseline continuations without launching science."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import tarfile
import tempfile
import time
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.plan_search_replication import (
    digest,
    encoded,
    gene_set_sha256,
    read_bytes,
    relative_path,
)

KIND = "baseline_depth_continuation_v1"
EXPERIMENT = "baseline-depth-extension-20261110"
METHODS = {"greedy_block", "randomized_block"}
SEEDS = {103, 227, 349, 461, 587}
INHERITED_FIELDS = (
    "starting_genotype",
    "inputs",
    "input_restore_index",
    "evaluator_ids",
    "required_versions",
    "growth_gates",
    "baseline_policy",
    "validation",
)


def build_jobs(spec: dict[str, Any]) -> list[dict[str, Any]]:
    if (
        spec.get("study_kind") != KIND
        or spec.get("experiment_id") != EXPERIMENT
        or spec.get("search")
        != {
            "new_candidate_slots": 2000,
            "inherited_candidate_slots": 100,
            "checkpoints": [250, 500, 1000, 2000],
            "max_steps": 2000,
            "max_wall_time_s": 21600,
            "maximum_genes_per_action": 100,
            "validation_feedback": False,
        }
        or spec.get("execution")
        != {
            "maximum_concurrent_jobs": 4,
            "auto_validation": False,
            "model_calls": False,
            "maximum_worker_hours": 24,
            "maximum_cap_usd": 25,
        }
        or spec.get("tracking", {}).get("s3_prefix")
        != f"s3://phd-research-storage-1758274488/yggdrisil/experiments/{EXPERIMENT}/"
    ):
        raise ValueError("Changed continuation budget, identity or execution policy")
    parents = spec["parents"]
    if len(parents) != 10 or {(p["method"], p["seed"]) for p in parents} != {
        (m, s) for m in METHODS for s in SEEDS
    }:
        raise ValueError("Expected all ten frozen parent baseline searches")
    jobs = []
    for parent in parents:
        if parent["previous_candidate_budget"] != 100:
            raise ValueError("Parent accounting differs from the frozen comparison")
        run_id = (
            f"{EXPERIMENT}-{parent['method'].replace('_', '-')}-seed-{parent['seed']}"
        )
        jobs.append(
            {
                "run_id": run_id,
                "method": parent["method"],
                "seed": parent["seed"],
                "directory": f"search/{run_id}",
                "graph_path": f"search/{run_id}/graph.sqlite",
                "new_candidate_slots": 2000,
                "max_states_including_root": 2001,
                "inherited_candidate_slots": 100,
                "additional_candidate_slots": 1900,
                "model_cap_usd": 0,
                "agent_config": None,
                "dossier": None,
                "live_literature": False,
                "parent": parent,
            }
        )
    return jobs


def verify_bundle(bundle: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(read_bytes(bundle / "manifest.json"))
    required = {
        "protocol.json",
        "jobs.json",
        "inputs/starting-genotype.json",
        "source-manifest.json",
        "parent-protocol.json",
    }
    if set(manifest["files"]) != required:
        raise ValueError("Incomplete continuation manifest")
    for name, expected in manifest["files"].items():
        if digest(read_bytes(relative_path(bundle, name))) != expected:
            raise ValueError(f"Changed continuation input: {name}")
    spec = json.loads(read_bytes(bundle / "protocol.json"))
    original = json.loads(read_bytes(bundle / "parent-protocol.json"))
    if any(spec[key] != original[key] for key in INHERITED_FIELDS):
        raise ValueError("Continuation changed frozen science or evaluator identities")
    if (
        digest(read_bytes(bundle / "parent-protocol.json"))
        != spec["parent_protocol_sha256"]
    ):
        raise ValueError("Parent protocol identity changed")
    jobs = build_jobs(spec)
    if json.loads(read_bytes(bundle / "jobs.json")) != jobs:
        raise ValueError("Changed continuation jobs")
    root = json.loads(read_bytes(bundle / "inputs/starting-genotype.json"))
    if (
        gene_set_sha256(root["deleted_gene_ids"])
        != spec["starting_genotype"]["deletion_set_sha256"]
    ):
        raise ValueError("Changed continuation starting genotype")
    if manifest["experiment_id"] != spec["experiment_id"]:
        raise ValueError("Continuation manifest identity mismatch")
    return spec, jobs


def prepare(root: Path, output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"Refusing to replace prepared continuation: {output}")
    prior = root / "runs/search-replication-20261109"
    catalog = json.loads(
        read_bytes(prior / "parallel-validation/finalization/catalog/nominations.json")
    )
    protocol_raw = read_bytes(
        prior / "parallel-validation/finalization/active-protocol.json"
    )
    if digest(protocol_raw) != catalog["protocol_sha256"]:
        raise ValueError("Parent catalog/protocol mismatch")
    original = json.loads(protocol_raw)
    spec = {k: deepcopy(original[k]) for k in INHERITED_FIELDS}
    parents = []
    for run in catalog["runs"]:
        if run["method"] not in METHODS:
            continue
        proof = run["verification"]
        if (
            run["status"] != "completed"
            or run["completed_evaluations"] != 100
            or proof["wandb_state"] != "COMMITTED"
            or not proof["full_s3_get_verified"]
            or not proof["wandb_download_verified"]
        ):
            raise ValueError("Parent lacks completed, preserved scientific results")
        parents.append(
            {
                "method": run["method"],
                "seed": run["seed"],
                "run_id": run["run_id"],
                "previous_candidate_budget": 100,
                "protocol_sha256": proof["protocol_sha256"],
                "report_sha256": run["report_sha256"],
                "checkpoint": proof["checkpoint"],
                "prior_wall_time_s": run["wall_time_s"],
                "prior_cpu_hours": run["cpu_hours"],
                "previous_finalist_state_id": run["finalist"]["state_id"],
            }
        )
    spec.update(
        schema_version=1,
        study_kind=KIND,
        experiment_id=EXPERIMENT,
        status="prepared_pending_cost_approval",
        parent_protocol_sha256=digest(protocol_raw),
        parents=parents,
        search={
            "new_candidate_slots": 2000,
            "inherited_candidate_slots": 100,
            "checkpoints": [250, 500, 1000, 2000],
            "max_steps": 2000,
            "max_wall_time_s": 21600,
            "maximum_genes_per_action": 100,
            "validation_feedback": False,
        },
        execution={
            "maximum_concurrent_jobs": 4,
            "auto_validation": False,
            "model_calls": False,
            "maximum_worker_hours": 24,
            "maximum_cap_usd": 25,
        },
        tracking={
            **original["tracking"],
            "s3_prefix": f"s3://phd-research-storage-1758274488/yggdrisil/experiments/{EXPERIMENT}/",
        },
        interpretation="Exploratory higher-budget continuation; retain the original equal-budget comparison separately. Candidate totals include100 inherited attempts; no model calls or whole-cell feedback.",
        selection={
            "finalists_per_run": 1,
            "order": ["additional_deletions_desc", "fba_growth_desc", "state_id_asc"],
            "deduplicate": True,
            "freeze_before_validation": True,
            "revalidate_unchanged_finalists": False,
        },
    )
    tools = [
        "scripts/baseline_depth_extension.py",
        "scripts/run_search_replication.py",
        "scripts/collect_search_replication.py",
        "scripts/supervise_search_replication.py",
        "scripts/plan_search_replication.py",
        "src/yggdrisil_ecoli/baseline_storage.py",
        "src/yggdrisil_ecoli/policies.py",
    ]
    frozen_runtime = json.loads(
        read_bytes(root / "methods/20261109/runtime-manifest.json")
    )["files"]
    science_files = {}
    for name, expected in frozen_runtime.items():
        if not name.startswith("application/src/yggdrisil_ecoli/"):
            continue
        local = name.removeprefix("application/")
        if "/scorers/" in name or Path(local).name in {
            "policies.py",
            "problem.py",
            "state.py",
            "actions.py",
        }:
            if digest(read_bytes(root / local)) != expected:
                raise ValueError(
                    f"Scientific source differs from the executed parent: {local}"
                )
            science_files[local] = expected
    if len(science_files) < 10:
        raise ValueError("Incomplete scientific-source provenance")
    payloads = {
        "protocol.json": encoded(spec),
        "parent-protocol.json": protocol_raw,
        "jobs.json": encoded(build_jobs(spec)),
        "inputs/starting-genotype.json": read_bytes(
            prior / "inputs/starting-genotype.json"
        ),
        "source-manifest.json": encoded(
            {
                "study_tools": {p: digest(read_bytes(root / p)) for p in tools},
                "frozen_scientific_sources": science_files,
                "runtime_snapshot_status": "freeze_complete_deployed_runtime_before_launch",
            }
        ),
    }
    manifest = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT,
        "launch_ready": False,
        "status": "prepared_pending_cost_approval",
        "search_runs": 10,
        "additional_candidate_slots": 19000,
        "files": {p: digest(raw) for p, raw in payloads.items()},
    }
    for name, raw in {**payloads, "manifest.json": encoded(manifest)}.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(raw)
    verify_bundle(output)
    return manifest


def validate_approval(approval: dict[str, Any]) -> None:
    start, science, stop = [
        approval.get(k)
        for k in (
            "authorized_start_unix",
            "science_deadline_unix",
            "stop_deadline_unix",
        )
    ]
    cap = approval.get("total_cap_usd")
    if (
        not all(
            type(v) in (int, float) and math.isfinite(v)
            for v in (start, science, stop, cap)
        )
        or not 0 < cap <= 25
        or not start <= time.time() < science < stop
        or stop - start > 24 * 3600
        or stop - science < 2 * 3600
        or approval.get("model_calls_allowed") is not False
        or approval.get("validation_launch_authorized") is not False
    ):
        raise ValueError("Invalid baseline-only continuation approval or deadlines")


def verify_archive(archive: Path, index: dict[str, Any], target: Path) -> None:
    """Stream every indexed member, exposing only the verified graph at the end."""
    expected = {f["path"]: f for f in index["files"]}
    if len(expected) != len(index["files"]) or "graph.sqlite" not in expected:
        raise ValueError("Duplicate or missing archive member")
    seen = set()
    with tempfile.TemporaryDirectory(
        prefix="parent-member-", dir=target.parent
    ) as temp:
        graph = Path(temp) / "graph.sqlite"
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                name = member.name
                if (
                    not member.isfile()
                    or name not in expected
                    or name in seen
                    or Path(name).is_absolute()
                    or ".." in Path(name).parts
                ):
                    raise ValueError("Unexpected or unsafe parent archive member")
                ref = expected[name]
                if member.size != ref["bytes"]:
                    raise ValueError("Parent member length mismatch")
                h, count = hashlib.sha256(), 0
                body = tar.extractfile(member)
                if body is None:
                    raise ValueError("Unreadable parent member")
                with (
                    body,
                    (
                        graph.open("xb")
                        if name == "graph.sqlite"
                        else tempfile.TemporaryFile()
                    ) as out,
                ):
                    while chunk := body.read(1024 * 1024):
                        h.update(chunk)
                        count += len(chunk)
                        if name == "graph.sqlite":
                            out.write(chunk)
                if count != ref["bytes"] or h.hexdigest() != ref["sha256"]:
                    raise ValueError("Parent member checksum mismatch")
                seen.add(name)
        if seen != set(expected):
            raise ValueError("Missing indexed parent archive members")
        if target.exists():
            raise FileExistsError("Refusing to replace an existing scientific graph")
        from yggdrisil_ecoli.baseline_storage import sqlite_snapshot

        sqlite_snapshot(graph, target)


def check_parent_graph(
    path: Path, parent: dict[str, Any], identities: dict[str, str]
) -> None:
    from scripts.collect_search_replication import decode_graph_value

    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        runs = db.execute("SELECT run_id, status, metadata_json FROM runs").fetchall()
        if (
            len(runs) != 1
            or runs[0][0] != parent["run_id"]
            or runs[0][1] != "completed"
        ):
            raise ValueError("Parent graph is not the expected closed run")
        meta = decode_graph_value(json.loads(runs[0][2]))
        if (
            meta["evaluators"] != identities
            or meta["protocol_sha256"] != parent["protocol_sha256"]
        ):
            raise ValueError("Parent graph scientific provenance changed")
        if db.execute("SELECT COUNT(*) FROM states").fetchone()[0] != 101:
            raise ValueError("Parent graph must contain root plus100 candidates")
        for (key,) in db.execute("SELECT state_id FROM states"):
            found = {
                r[0]
                for r in db.execute(
                    "SELECT evaluator_id FROM evaluations WHERE state_id=?", (key,)
                )
            }
            if found != set(identities.values()):
                raise ValueError("Parent contains incomplete or changed evaluations")


def restore_parent(
    folder: Path, job: dict[str, Any], spec: dict[str, Any], archive: Any, wandb: Any
) -> dict[str, Any]:
    """Require full S3 and uncached committed W&B verification before science."""
    parent, target = job["parent"], folder / "graph.sqlite"
    if target.exists():
        raise FileExistsError(
            "Continuation graph already exists; inspect before resuming"
        )
    checkpoint = parent["checkpoint"]
    with tempfile.TemporaryDirectory(prefix="parent-download-", dir=folder) as temp:
        scratch = Path(temp)
        fetched = {}
        for category in ("index", "archive"):
            ref = checkpoint[category]
            uri = urlparse(ref["uri"])
            if uri.scheme != "s3" or uri.netloc != archive.bucket:
                raise ValueError("Parent is outside the verified private bucket")
            path = scratch / category
            h, count = hashlib.sha256(), 0
            with (
                closing(
                    archive.s3.get_object(
                        Bucket=uri.netloc,
                        Key=uri.path.lstrip("/"),
                        ExpectedBucketOwner=archive.owner,
                    )["Body"]
                ) as body,
                path.open("xb") as out,
            ):
                while chunk := body.read(1024 * 1024):
                    h.update(chunk)
                    count += len(chunk)
                    out.write(chunk)
            if h.hexdigest() != ref["sha256"] or count != ref["bytes"]:
                raise ValueError("Parent S3 content verification failed")
            fetched[category] = path
        index = json.loads(read_bytes(fetched["index"]))
        if (
            index["archive"] != checkpoint["archive"]
            or index["files"] != checkpoint["files"]
        ):
            raise ValueError("Parent archive index disagrees with frozen references")
        remote = wandb.Api(timeout=120).artifact(checkpoint["wandb_artifact"])
        if remote.state != "COMMITTED":
            raise ValueError("Parent W&B artifact is not committed")
        for logical, category in (
            ("checkpoint.tar.gz", "archive"),
            ("index.json", "index"),
        ):
            downloaded = Path(
                remote.get_entry(logical).download(
                    root=str(scratch / "wandb"), skip_cache=True
                )
            )
            ref = checkpoint[category]
            if (
                downloaded.stat().st_size != ref["bytes"]
                or digest(read_bytes(downloaded)) != ref["sha256"]
            ):
                raise ValueError("Parent W&B content verification failed")
        staging = scratch / "graph.sqlite"
        verify_archive(fetched["archive"], index, staging)
        check_parent_graph(staging, parent, spec["evaluator_ids"])
        with tarfile.open(fetched["archive"], "r:gz") as tar:
            body = tar.extractfile("run-report.json")
            if body is None:
                raise ValueError("Missing parent report")
            with body:
                report_raw = body.read()
        if digest(report_raw) != parent["report_sha256"]:
            raise ValueError("Changed parent report")
        report = json.loads(report_raw)
        if len(report["candidates"]) != 100:
            raise ValueError("Parent report does not contain100 candidates")
        proof = {
            "status": "verified",
            "verified_at_unix": time.time(),
            "parent": parent,
            "candidate_prefix_sha256": digest(encoded(report["candidates"])),
            "full_s3_get_verified": True,
            "wandb_download_verified": True,
            "every_indexed_member_verified": True,
            "graph_sha256": digest(read_bytes(staging)),
        }
        from scripts.run_search_replication import save

        save(folder / "parent-checkpoint.json", proof)
        staging.rename(target)
        return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        spec, jobs = verify_bundle(args.verify)
        print(
            json.dumps(
                {
                    "experiment_id": spec["experiment_id"],
                    "jobs": len(jobs),
                    "verified": True,
                }
            )
        )
    elif args.output:
        print(json.dumps(prepare(args.root.resolve(), args.output.resolve()), indent=2))
    else:
        parser.error(
            "Provide --output or --verify; this command never launches science"
        )


if __name__ == "__main__":
    main()
