#!/usr/bin/env python3
"""Run one authorized frozen baseline on Linux, with verified data checkpoints."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import socket
import sys
import time
from dataclasses import asdict
from importlib.metadata import distributions, version
from pathlib import Path
from typing import Any

import yggdrisil
from yggdrisil import EvaluatorSuite, RunLimits, Runner, SQLiteStateGraph

import yggdrisil_ecoli
from scripts.plan_deletion_baselines import validate_protocol
from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.analysis import summarize_run
from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets, screen
from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.data.io import atomic_json, file_sha256
from yggdrisil_ecoli.policies import BlockDeletionPolicy
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import active_evaluator_ids, passes_growth_gates
from yggdrisil_ecoli.state import GenomeState, genome_state_key


def load_bundle(bundle: Path, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((bundle / "manifest.json").read_text())
    required = {
        "protocol.json",
        "jobs.json",
        "inputs/starting-genotype.json",
        "source-manifest.json",
    }
    if set(manifest["files"]) != required:
        raise ValueError("Incomplete frozen bundle manifest")
    for name, digest in manifest["files"].items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle.resolve()) or file_sha256(path) != digest:
            raise ValueError(f"Changed or unsafe frozen input: {name}")
    spec = json.loads((bundle / "protocol.json").read_text())
    validate_protocol(spec)
    start = json.loads((bundle / "inputs/starting-genotype.json").read_text())
    ids = start["deleted_gene_ids"]
    digest = hashlib.sha256(
        json.dumps(sorted(ids), separators=(",", ":")).encode()
    ).hexdigest()
    if (
        len(set(ids)) != spec["starting_genotype"]["deletion_count"]
        or digest != spec["starting_genotype"]["deletion_set_sha256"]
    ):
        raise ValueError("Starting genotype differs from frozen protocol")
    jobs = json.loads((bundle / "jobs.json").read_text())
    if not 0 <= index < len(jobs):
        raise ValueError("Job index outside frozen plan")
    job = jobs[index]
    expected = (
        f"{spec['experiment_id']}-{job['kind'].replace('_', '-')}-seed-{job['seed']}"
    )
    if job["run_id"] != expected or job["directory"] != f"search/{expected}":
        raise ValueError("Unsafe job identity/path")
    return spec, job


def verify_sources(bundle: Path, repo: Path) -> dict[str, str]:
    expected = json.loads((bundle / "source-manifest.json").read_text())["files"]
    for name, digest in expected.items():
        if name.startswith("application/src/yggdrisil_ecoli/"):
            actual = (
                Path(yggdrisil_ecoli.__file__).parent
                / name.split("src/yggdrisil_ecoli/", 1)[1]
            )
        elif name.startswith("framework/src/yggdrisil/"):
            actual = (
                Path(yggdrisil.__file__).parent / name.split("src/yggdrisil/", 1)[1]
            )
        elif name.startswith("application/"):
            actual = repo / name.removeprefix("application/")
        else:
            actual = bundle / "source" / name
        if file_sha256(actual) != digest:
            raise ValueError(f"Runtime source differs from frozen bundle: {name}")
    return expected


def validate_approval(
    bundle: Path, spec: dict[str, Any], approval: dict[str, Any]
) -> None:
    if (
        approval.get("experiment_id") != spec["experiment_id"]
        or approval.get("protocol_sha256") != file_sha256(bundle / "protocol.json")
        or approval.get("bundle_manifest_sha256")
        != file_sha256(bundle / "manifest.json")
        or approval.get("paid_launch") is not True
        or approval.get("model_calls_allowed") is not False
        or not isinstance(approval.get("user_authorization"), str)
        or not approval["user_authorization"].strip()
        or type(approval.get("total_cap_usd")) not in (int, float)
        or not 0
        < approval["total_cap_usd"]
        <= spec["cost_proposal"]["maximum_approval_usd"]
    ):
        raise ValueError("Missing experiment-specific paid-launch approval")
    now = time.time()
    deadlines = [
        approval.get(k) for k in ("science_deadline_unix", "stop_deadline_unix")
    ]
    if not all(type(t) in (int, float) and math.isfinite(t) for t in deadlines):
        raise ValueError("Approval deadlines must be finite timestamps")
    if not now < deadlines[0] < deadlines[1]:
        raise ValueError("Approval deadline is expired or invalid")
    if deadlines[1] - now > spec["cost_proposal"]["maximum_worker_hours"] * 3600:
        raise ValueError("Worker deadline exceeds the proposed spending window")


def curves(
    graph: Any, identities: dict[str, str], initial_count: int
) -> list[dict[str, Any]]:
    rows = []
    best = initial_count
    # Graph.states sorts within each batch by hash, not evaluation order.
    for i, node in enumerate(
        sorted(graph.states(), key=lambda n: (n.created_at, n.state_id))
    ):
        by_id = {r.evaluator_id: r for r in graph.evaluations(node.state_id)}
        complete = all(v in by_id for v in identities.values())
        evidence = {k: by_id[v] for k, v in identities.items() if v in by_id}
        accepted = complete and passes_growth_gates(evidence)
        if accepted:
            best = max(best, len(node.state.deleted_genes))
        rows.append(
            {
                "evaluation_index_including_root": i,
                "state_id": node.state_id,
                "created_step": node.created_step,
                "created_at": node.created_at,
                "genes_deleted": len(node.state.deleted_genes),
                "evaluation_complete": complete,
                "growth_gates_passed": accepted,
                "best_additional_deletions": best - initial_count,
                "fba_growth": evidence["fba"].metrics.get("growth_rate")
                if "fba" in evidence
                else None,
            }
        )
    return rows


class CheckpointPolicy:
    def __init__(
        self,
        policy: Any,
        archive: Any,
        tracked: Any,
        bundle: Path,
        folder: Path,
        identities: dict[str, str],
        initial_count: int,
    ) -> None:
        self.policy, self.archive, self.tracked = policy, archive, tracked
        self.bundle, self.folder, self.identities = bundle, folder, identities
        self.initial_count = initial_count
        self.last_count, self.last_time = -1, 0.0

    async def step(self, graph: Any, status: Any) -> Any:
        rows = curves(graph, self.identities, self.initial_count)
        progress = {
            "status": "running",
            "step": status.step,
            "states": len(graph),
            "new_evaluated_states": sum(r["evaluation_complete"] for r in rows) - 1,
            "best_additional_deletions": max(
                r["best_additional_deletions"] for r in rows
            ),
            "elapsed_s_this_process": status.elapsed_s,
        }
        atomic_json(self.folder / "progress.json", progress)
        atomic_json(self.folder / "curves.json", rows)
        self.tracked.log(progress)
        if (
            self.last_count < 0
            or len(graph) - self.last_count >= 10
            or time.monotonic() - self.last_time >= 120
        ):
            task = asyncio.create_task(
                asyncio.to_thread(self.archive.checkpoint, self.bundle, self.folder)
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A cancelled policy must not leave an upload racing the final
                # checkpoint or allow science to advance before verification.
                await task
                raise
            self.last_count, self.last_time = len(graph), time.monotonic()
        return await self.policy.step(graph, status)


async def run_job(
    bundle: Path, index: int, repo: Path, approval_path: Path, resume: bool
) -> None:
    if sys.platform != "linux":
        raise RuntimeError("Scientific execution is remote-only on Linux")
    spec, job = load_bundle(bundle, index)
    approval = json.loads(approval_path.read_text())
    validate_approval(bundle, spec, approval)
    sources = verify_sources(bundle, repo)
    folder = bundle / job["directory"]
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await execute(bundle, folder, spec, job, repo, approval, sources, resume)


async def execute(
    bundle: Path,
    folder: Path,
    spec: dict[str, Any],
    job: dict[str, Any],
    repo: Path,
    approval: dict[str, Any],
    sources: dict[str, str],
    resume: bool,
) -> None:
    # Imports that can initialize scientific solvers occur only after the Linux,
    # approval, provenance and exclusive-run checks.
    import boto3
    import wandb

    from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
    from yggdrisil_ecoli.scorers.fba import FBAScorer
    from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
    from yggdrisil_ecoli.scorers.rba import RBAScorer
    from yggdrisil_ecoli.scorers.size import GenomeSizeScorer

    inputs = {k: repo / v["path"] for k, v in spec["inputs"].items()}
    for name, path in inputs.items():
        if file_sha256(path) != spec["inputs"][name]["sha256"]:
            raise ValueError(f"Scientific input changed: {name}")
    versions = {name: version(name) for name in spec["required_versions"]}
    if versions != spec["required_versions"]:
        raise ValueError("Scientific runtime versions differ from frozen comparison")
    genes = load_genes(inputs["genes"])
    start = json.loads((bundle / "inputs/starting-genotype.json").read_text())
    initial = GenomeState(frozenset(start["deleted_gene_ids"]))
    scorers = [
        GenomeSizeScorer(genes),
        EssentialityScorer(genes=genes, artifact_hash=file_sha256(inputs["genes"])),
        ModuleEvaluator.from_json(inputs["modules"], genes),
        FBAScorer(genes=genes, model_path=inputs["fba_model"]),
        RBAScorer(genes=genes, artifact_dir=inputs["rba_manifest"].parent),
    ]
    identities = active_evaluator_ids(scorers)
    if identities != spec["evaluator_ids"]:
        raise ValueError("Evaluator identities differ from frozen agent experiment")
    metadata = {
        "job": job,
        "policy": job["kind"],
        "evaluators": identities,
        "protocol_sha256": file_sha256(bundle / "protocol.json"),
        "bundle_manifest_sha256": file_sha256(bundle / "manifest.json"),
        "sources": sources,
        "versions": versions,
        "approval": approval,
        "inputs": spec["inputs"],
        "initial_state_id": genome_state_key(initial),
    }
    config = folder / "config.json"
    if config.exists():
        if not resume or json.loads(config.read_text()) != metadata:
            raise ValueError(
                "Resume requires identical inputs, code, settings and approval"
            )
        if (folder / "result.json").exists():
            raise ValueError("Run is already complete; do not extend its budget")
    else:
        if resume or (folder / "graph.sqlite").exists():
            raise ValueError("Cannot resume without the original configuration")
        atomic_json(config, metadata)
        atomic_json(
            folder / "launch.json",
            {
                "started_at_unix": time.time(),
                "deadline_unix": min(
                    time.time() + spec["limits"]["max_wall_time_s"],
                    approval["science_deadline_unix"],
                ),
                "wandb_id": hashlib.sha256(job["run_id"].encode()).hexdigest()[:16],
            },
        )
    launch = json.loads((folder / "launch.json").read_text())
    if launch["deadline_unix"] <= time.time():
        raise ValueError("Original run deadline expired; resume cannot extend it")
    environment = {
        "python": sys.version,
        "platform": sys.platform,
        "packages": sorted(
            [
                {"name": d.metadata["Name"], "version": d.version}
                for d in distributions()
            ],
            key=lambda d: d["name"].lower(),
        ),
        "native_thread_limits": {
            k: os.environ.get(k)
            for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
        },
    }
    environment_path = folder / "environment.json"
    if (
        environment_path.exists()
        and json.loads(environment_path.read_text()) != environment
    ):
        raise ValueError("Installed runtime changed since the original launch")
    atomic_json(environment_path, environment)
    atomic_json(
        folder / "process.json",
        {
            "pid": os.getpid(),
            "start_ticks": Path(f"/proc/{os.getpid()}/stat").read_text().split()[21],
            "host": socket.gethostname(),
            "remote_path": str(folder),
        },
    )
    secrets = configured_secrets(repo, repo.parent / "credentials")
    screen(config, secrets)
    with wandb.init(
        project=spec["tracking"]["project"],
        entity=spec["tracking"]["entity"],
        id=launch["wandb_id"],
        name=job["run_id"],
        resume="allow" if resume else "never",
        dir=str(folder),
        config=metadata,
        tags=["yggdrisil", "baseline", job["kind"]],
        settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False),
    ) as tracked:
        atomic_json(
            folder / "tracking.json", {"wandb_url": tracked.url, "wandb_id": tracked.id}
        )
        archive = BaselineArchive(
            s3=boto3.client("s3", region_name=spec["tracking"]["region"]),
            wandb=wandb,
            tracked=tracked,
            owner=boto3.client("sts").get_caller_identity()["Account"],
            bucket=spec["tracking"]["s3_bucket"],
            prefix=spec["tracking"]["s3_prefix"],
            run_id=job["run_id"],
            secrets=secrets,
        )
        try:
            with SQLiteStateGraph[GenomeState, DeleteGenes](
                folder / "graph.sqlite"
            ) as graph:
                policy = BlockDeletionPolicy(
                    gene_ids=list(genes.index),
                    evaluator_ids=identities,
                    initial_state=initial,
                    kind=job["kind"],
                    seed=job["seed"],
                    block_sizes=spec["policy"]["block_sizes"],
                    neighborhood_size=spec["policy"]["greedy_neighborhood_size"],
                )
                wrapped = CheckpointPolicy(
                    policy,
                    archive,
                    tracked,
                    bundle,
                    folder,
                    identities,
                    len(initial.deleted_genes),
                )
                result = await Runner(
                    EcoliProblem(
                        genes, initial_state=initial, max_genes_per_action=100
                    ),
                    wrapped,
                    graph,
                    RunLimits(
                        max_states=spec["limits"]["new_evaluated_states"] + 1,
                        max_steps=spec["limits"]["max_steps"],
                        max_wall_time_s=max(0, launch["deadline_unix"] - time.time()),
                    ),
                    evaluators=EvaluatorSuite(scorers),
                    run_id=job["run_id"],
                    metadata=metadata,
                    resume=resume,
                ).run()
                rows = curves(graph, identities, len(initial.deleted_genes))
                atomic_json(folder / "curves.json", rows)
            summary = summarize_run(folder / "graph.sqlite")
            candidate = summary["deepest_viable_candidate"]
            if candidate and candidate["genes_deleted"] == len(initial.deleted_genes):
                candidate = None
            report = {
                "status": "search_completed_pending_backup",
                "result": asdict(result),
                "summary": summary,
                "finalist": candidate,
                "new_evaluated_states": sum(r["evaluation_complete"] for r in rows) - 1,
                "stop_reason": result.stop_reason,
                "convergence_claim": False,
                "model_calls": 0,
                "provider_spend_usd": 0,
                "remote_path": str(folder),
            }
            atomic_json(folder / "result.json", report)
            atomic_json(
                folder / "progress.json",
                {
                    k: report[k]
                    for k in ("status", "new_evaluated_states", "stop_reason")
                },
            )
            tracked.summary.update(
                {
                    "status": "search_completed_pending_backup",
                    "stop_reason": result.stop_reason,
                    "new_evaluated_states": report["new_evaluated_states"],
                }
            )
        except Exception as exc:
            record_failure(folder, "search", exc)
            tracked.summary["status"] = "failed"
            raise
        finally:
            try:
                checkpoint = archive.checkpoint(bundle, folder)
            except Exception as exc:
                record_failure(folder, "backup", exc)
                tracked.summary["status"] = "backup_failed"
                raise
        atomic_json(
            folder / "completion.json",
            {
                "status": "completed_verified",
                "result_sha256": file_sha256(folder / "result.json"),
                "checkpoint": checkpoint,
            },
        )
        tracked.summary["status"] = "completed_verified"


def record_failure(folder: Path, phase: str, error: Exception) -> None:
    record = {
        "time_unix": time.time(),
        "phase": phase,
        "type": type(error).__name__,
        "message": str(error),
    }
    atomic_json(folder / "failure.json", record)
    with (folder / "failures.jsonl").open("a") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    atomic_json(folder / "progress.json", {"status": f"{phase}_failed", **record})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--job-index", type=int, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Scientific execution is remote-only on Linux")
    asyncio.run(
        run_job(
            args.bundle.resolve(),
            args.job_index,
            args.repo_root.resolve(),
            args.approval.resolve(),
            args.resume,
        )
    )


if __name__ == "__main__":
    main()
