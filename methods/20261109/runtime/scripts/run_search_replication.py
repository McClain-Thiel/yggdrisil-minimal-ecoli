#!/usr/bin/env python3
"""Run one frozen replication job on its approved Linux worker."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import resource
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from scripts.plan_search_replication import digest, read_bytes, verify_bundle


def worker_identity() -> str:
    request = Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    with urlopen(request, timeout=5) as response:
        token = response.read().decode()
    request = Request(
        "http://169.254.169.254/latest/meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with urlopen(request, timeout=5) as response:
        return response.read().decode()


def validate_launch(
    bundle: Path, repo: Path, approval: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if sys.platform != "linux":
        raise RuntimeError(
            "Scientific execution is restricted to the approved remote Linux worker"
        )
    spec, jobs = verify_bundle(bundle)
    if (
        approval.get("paid_launch") is not True
        or approval.get("science_dispatch_ready") is not True
        or approval.get("experiment_id") != spec["experiment_id"]
        or approval.get("total_cap_usd") != 550
        or approval.get("protocol_sha256")
        != digest(read_bytes(bundle / "protocol.json"))
        or approval.get("manifest_sha256")
        != digest(read_bytes(bundle / "manifest.json"))
        or approval.get("runtime_manifest_sha256")
        != digest(read_bytes(bundle / "runtime-manifest.json"))
        or not approval.get("user_authorization")
    ):
        raise ValueError("Missing or mismatched study-specific launch approval")
    if worker_identity() != approval.get("instance_id"):
        raise ValueError("This is not the approved dedicated worker")
    launch, science, stop = [
        approval.get(k)
        for k in (
            "authorized_start_unix",
            "science_deadline_unix",
            "stop_deadline_unix",
        )
    ]
    if not all(
        type(v) in (int, float) and math.isfinite(v) for v in (launch, science, stop)
    ):
        raise ValueError("Invalid absolute deadlines")
    if (
        not launch <= time.time() < science < stop
        or stop - launch > 72 * 3600
        or stop - science < 2 * 3600
    ):
        raise ValueError("Expired or extended study window")
    runtime = json.loads(read_bytes(bundle / "runtime-manifest.json"))
    if approval.get("operational_repair_sha256"):
        if (
            digest(read_bytes(repo.parent / "operational-repair.json"))
            != approval["operational_repair_sha256"]
        ):
            raise ValueError("Operational repair provenance changed")
    if approval.get("transport_recovery_sha256"):
        if (
            digest(read_bytes(repo.parent / "transport-recovery.json"))
            != approval["transport_recovery_sha256"]
        ):
            raise ValueError("Transport recovery provenance changed")
    if approval.get("queue_timing_correction_sha256"):
        if (
            digest(read_bytes(repo.parent / "queue-timing-correction.json"))
            != approval["queue_timing_correction_sha256"]
        ):
            raise ValueError("Queue timing provenance changed")
    for name, expected in runtime["files"].items():
        path = (repo.parent / name).resolve()
        if (
            not path.is_relative_to(repo.parent.resolve())
            or digest(read_bytes(path)) != expected
        ):
            raise ValueError(f"Changed deployed runtime: {name}")
    if (
        spec["agent"]["provider_route"] != spec["execution"]["provider_route"]
        or spec["agent"]["provider_fallback_allowed"]
    ):
        raise ValueError("Unfrozen or fallback-enabled model route")
    return spec, jobs


def save(path: Path, value: Any) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def cpu_seconds() -> float:
    return sum(
        r.ru_utime + r.ru_stime
        for r in (
            resource.getrusage(resource.RUSAGE_SELF),
            resource.getrusage(resource.RUSAGE_CHILDREN),
        )
    )


def remaining_job_window(
    now: float, spent_seconds: float, study_deadline: float
) -> float:
    """Queue time consumes no execution allowance; prior attempts still count."""
    if (
        not all(math.isfinite(x) for x in (now, spent_seconds, study_deadline))
        or spent_seconds < 0
    ):
        raise ValueError("Invalid recorded execution time")
    return min(study_deadline, now + max(0.0, 21600 - spent_seconds))


def transient_transport_error(error: dict[str, Any]) -> bool:
    """Never retry biological, numerical, input, or budget failures."""
    return error.get("type") in {
        "RateLimitError",
        "Timeout",
        "APIConnectionError",
        "ServiceUnavailableError",
    } or (
        error.get("type") == "RuntimeError"
        and error.get("message", "").startswith("ADK TimeoutError:")
    )


class CheckpointPolicy:
    def __init__(
        self,
        policy: Any,
        archive: Any,
        tracked: Any,
        bundle: Path,
        folder: Path,
        identities: dict[str, str],
        model_cap: int,
    ) -> None:
        self.policy, self.archive, self.tracked = policy, archive, tracked
        self.bundle, self.folder, self.identities, self.model_cap = (
            bundle,
            folder,
            identities,
            model_cap,
        )
        self.last_count, self.last_time = 0, time.monotonic()
        self.stopped_for_budget = False

    def drain_interrupted_decisions(self) -> Any:
        drain = getattr(self.policy, "drain_interrupted_decisions", None)
        return drain() if drain is not None else []

    async def step(self, graph: Any, status: Any) -> Any:
        from decimal import Decimal

        from yggdrisil import Decision

        from scripts.run_deletion_baseline import curves
        from scripts.run_literature_ablation import check_key_budget, key_usage

        rows = curves(graph, self.identities, 1030)
        progress = {
            "status": "running",
            "step": status.step,
            "attempted_candidates": max(0, len(graph) - 1),
            "completed_candidates": sum(x["evaluation_complete"] for x in rows[1:]),
            "best_additional_deletions": max(
                (x["best_additional_deletions"] for x in rows), default=0
            ),
            "updated_at_unix": time.time(),
        }
        if self.model_cap:
            usage = await asyncio.to_thread(key_usage)
            spent = check_key_budget(usage, Decimal(self.model_cap))
            save(self.folder / "model-usage.json", usage)
            with (self.folder / "provider-usage.jsonl").open("a") as handle:
                handle.write(
                    json.dumps({"time_unix": time.time(), "usage": usage}) + "\n"
                )
            progress["model_spend_usd"] = float(spent)
            if Decimal(self.model_cap) - spent < Decimal("1.25"):
                self.stopped_for_budget = True
        save(self.folder / "progress.json", progress)
        self.tracked.log(progress)
        if (
            len(graph) - self.last_count >= 10
            or time.monotonic() - self.last_time >= 120
        ):
            task = asyncio.create_task(
                asyncio.to_thread(self.archive.checkpoint, self.bundle, self.folder)
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            self.last_count, self.last_time = len(graph), time.monotonic()
        if self.stopped_for_budget:
            return [
                Decision(role="budget", output={"reason": "model_budget", **progress})
            ]
        return await self._step_with_transport_recovery(graph, status, progress)

    async def _step_with_transport_recovery(
        self, graph: Any, status: Any, progress: dict[str, Any]
    ) -> Any:
        from decimal import Decimal

        from yggdrisil import Decision

        from scripts.run_literature_ablation import check_key_budget, key_usage

        delays = (
            (60, 120, 300, 600, 1200, 1200)
            if os.environ.get("YGGDRISIL_RECOVER_TRANSIENT_STEPS") == "1"
            else ()
        )
        for attempt in range(len(delays) + 1):
            try:
                return await self.policy.step(graph, status)
            except Exception as exc:
                cause = getattr(exc, "cause", exc)
                error = {"type": type(cause).__name__, "message": str(cause)}
                if not transient_transport_error(error) or attempt == len(delays):
                    raise
                event = {
                    "time_unix": time.time(),
                    "event": "transient_step_retry",
                    "error": error,
                    "retry": attempt + 1,
                    "delay_seconds": delays[attempt],
                    "step": status.step,
                    "candidate_count": len(graph) - 1,
                    "same_graph_session_and_request": True,
                }
                with (self.folder / "transport-events.jsonl").open("a") as handle:
                    handle.write(json.dumps(event) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                print(json.dumps(event), file=sys.stderr, flush=True)
                # The Runner's original wall-time context also bounds these waits.
                # Re-entering with the same graph/status reuses durable ADK output
                # and invocation budgets, including successful sibling responses.
                progress.update(
                    status="waiting_for_service",
                    transport_retry=event,
                    updated_at_unix=time.time(),
                )
                save(self.folder / "progress.json", progress)
                task = asyncio.create_task(
                    asyncio.to_thread(self.archive.checkpoint, self.bundle, self.folder)
                )
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
                await asyncio.sleep(delays[attempt])
                if self.model_cap:
                    usage = await asyncio.to_thread(key_usage)
                    spent = check_key_budget(usage, Decimal(self.model_cap))
                    if Decimal(self.model_cap) - spent < Decimal("1.25"):
                        self.stopped_for_budget = True
                        return [
                            Decision(role="budget", output={"reason": "model_budget"})
                        ]
                progress.update(status="running", updated_at_unix=time.time())
                save(self.folder / "progress.json", progress)
        raise AssertionError("unreachable")


async def execute(
    bundle: Path,
    repo: Path,
    index: int,
    approval: dict[str, Any],
    spec: dict[str, Any],
    job: dict[str, Any],
    folder: Path,
) -> None:
    # Nothing above this point imports solvers, initializes model clients, or
    # touches a scientific input. The worker/approval/lock checks come first.
    from decimal import Decimal
    from importlib.metadata import distributions, version

    import boto3
    import wandb
    from yggdrisil import EvaluatorSuite, RunLimits, Runner, SQLiteStateGraph

    from scripts.collect_search_replication import export_graph_report
    from scripts.run_literature_ablation import check_key_budget, key_usage, seed_memory
    from yggdrisil_ecoli.agent_policy import AgentSearchConfig, make_agent_policy
    from yggdrisil_ecoli.baseline_storage import (
        BaselineArchive,
        configured_secrets,
        screen,
    )
    from yggdrisil_ecoli.data.evidence import load_genes
    from yggdrisil_ecoli.policies import BlockDeletionPolicy
    from yggdrisil_ecoli.problem import EcoliProblem
    from yggdrisil_ecoli.scorers.base import active_evaluator_ids
    from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
    from yggdrisil_ecoli.scorers.fba import FBAScorer
    from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
    from yggdrisil_ecoli.scorers.rba import RBAScorer
    from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
    from yggdrisil_ecoli.state import GenomeState

    transport = (
        json.loads(read_bytes(repo.parent / "transport-recovery.json"))
        if approval.get("transport_recovery_sha256")
        else None
    )
    continuation = transport["resumes"].get(job["run_id"]) if transport else None
    if continuation:
        for name, expected in continuation["database_hashes"].items():
            if digest(read_bytes(folder / name)) != expected:
                raise ValueError("Saved graph/session changed before transport resume")
    elif (folder / "launch.json").exists() or (folder / "graph.sqlite").exists():
        raise RuntimeError(
            "Attempt already exists; no automatic scientific retry or budget reset"
        )
    credentials = repo.parent / "credentials"
    repair = (
        json.loads(read_bytes(repo.parent / "operational-repair.json"))
        if approval.get("operational_repair_sha256")
        else None
    )
    retry = repair["retries"].get(job["run_id"]) if repair else None
    tracking_id = job["run_id"] + (
        transport.get("tracking_suffix", "-transport-resume1")
        if continuation
        else "-dependency-retry1"
        if retry
        else ""
    )
    if transport:
        os.environ["YGGDRISIL_RATE_LIMIT_RETRIES"] = "6"
        os.environ["YGGDRISIL_TRANSPORT_JOURNAL"] = str(
            folder / "transport-events.jsonl"
        )
        if transport.get("recover_transient_steps"):
            os.environ["YGGDRISIL_RECOVER_TRANSIENT_STEPS"] = "1"
    tracking = json.loads(read_bytes(credentials / "tracking.json"))
    os.environ.update(tracking)
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("EDISON_PLATFORM_API_KEY", None)
    if job["model_cap_usd"]:
        # LiteLLM imports this lazily on its first real completion. Check the
        # same path before creating a search graph or making a paid request.
        from litellm.responses.mcp.chat_completions_handler import acompletion_with_mcp

        assert callable(acompletion_with_mcp)
        scoped = json.loads(read_bytes(credentials / (job["run_id"] + ".json")))
        os.environ["OPENROUTER_API_KEY"] = scoped["OPENROUTER_API_KEY"]
        usage = key_usage()
        if (
            check_key_budget(usage, Decimal(job["model_cap_usd"])) != 0
            and not continuation
        ) or usage["limit"] != job["model_cap_usd"]:
            raise ValueError(
                "New job requires an unused dedicated key with the exact frozen cap"
            )
    if job["live_literature"]:
        os.environ["YGGDRISIL_LITERATURE_CUTOFF"] = job["publication_cutoff"]
        os.environ["YGGDRISIL_LITERATURE_ARCHIVE"] = str(folder / "literature")
    else:
        os.environ.pop("YGGDRISIL_LITERATURE_CUTOFF", None)
        os.environ.pop("YGGDRISIL_LITERATURE_ARCHIVE", None)
    inputs = {k: repo / v["path"] for k, v in spec["inputs"].items()}
    for name, path in inputs.items():
        if digest(read_bytes(path)) != spec["inputs"][name]["sha256"]:
            raise ValueError("Changed scientific input: " + name)
    versions = {name: version(name) for name in spec["required_versions"]}
    if versions != spec["required_versions"]:
        raise ValueError("Scientific dependency versions changed")
    genes = load_genes(inputs["genes"])
    root = json.loads(read_bytes(bundle / "inputs/starting-genotype.json"))
    initial = GenomeState(frozenset(root["deleted_gene_ids"]))
    modules = ModuleEvaluator.from_json(inputs["modules"], genes)
    scorers = [
        GenomeSizeScorer(genes),
        EssentialityScorer(
            genes=genes, artifact_hash=digest(read_bytes(inputs["genes"]))
        ),
        modules,
        FBAScorer(genes=genes, model_path=inputs["fba_model"]),
        RBAScorer(genes=genes, artifact_dir=inputs["rba_manifest"].parent),
    ]
    identities = active_evaluator_ids(scorers)
    if identities != spec["evaluator_ids"]:
        raise ValueError("Scientific evaluator identity changed")
    dossier = None
    if job["dossier"]:
        dossier = json.loads(read_bytes(bundle / job["dossier"]))
        if (
            hashlib.sha256(dossier["content"].encode()).hexdigest()
            != spec["literature"]["dossier_answer_sha256"]
        ):
            raise ValueError("Immutable pre-search dossier changed")
    timing = (
        json.loads(read_bytes(repo.parent / "queue-timing-correction.json"))["jobs"][
            job["run_id"]
        ]
        if approval.get("queue_timing_correction_sha256")
        else None
    )
    started, cpu_started = time.time(), cpu_seconds()
    if continuation and not timing:
        started = continuation["started_at_unix"]
    launch = {
        "started_at_unix": started,
        "deadline_unix": min(
            started + 21600,
            approval["science_deadline_unix"],
            retry["original_deadline_unix"] if retry else float("inf"),
            continuation["deadline_unix"] if continuation else float("inf"),
        ),
        "wandb_id": hashlib.sha256(tracking_id.encode()).hexdigest()[:16],
    }
    if timing:
        launch.update(
            deadline_unix=remaining_job_window(
                started,
                timing["prior_active_wall_time_s"],
                approval["science_deadline_unix"],
            ),
            resumed_at_unix=started,
            prior_active_wall_time_s=timing["prior_active_wall_time_s"],
            timing_basis="Six hours of cumulative execution, including prior failed attempts; queue time excluded",
        )
    metadata = {
        "job": job,
        "evaluators": identities,
        "protocol_sha256": digest(read_bytes(bundle / "protocol.json")),
        "approval_sha256": digest(read_bytes(bundle / "approval.json")),
        "runtime_manifest_sha256": approval["runtime_manifest_sha256"],
        "versions": versions,
        "inputs": spec["inputs"],
        "initial_state_id": root["state_id"],
        "operational_retry": retry,
        "transport_continuation": continuation,
        "execution_time_accounting": timing,
    }
    save(folder / "config.json", metadata)
    save(folder / "launch.json", launch)
    save(
        folder / "environment.json",
        {
            "python": sys.version,
            "packages": sorted(
                (d.metadata["Name"], d.version) for d in distributions()
            ),
            "threads": {
                k: os.environ.get(k)
                for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
            },
        },
    )
    save(
        folder / "process.json",
        {
            "pid": os.getpid(),
            "start_ticks": Path(f"/proc/{os.getpid()}/stat").read_text().split()[21],
            "host": socket.gethostname(),
            "remote_path": str(folder),
        },
    )
    secrets = configured_secrets(repo, credentials)
    screen(folder / "config.json", secrets)
    location = urlparse(spec["tracking"]["s3_prefix"])
    with wandb.init(
        project=spec["tracking"]["project"],
        entity=spec["tracking"]["entity"],
        name=tracking_id,
        id=launch["wandb_id"],
        resume="never",
        dir=str(folder),
        config=metadata,
        tags=[*spec["tracking"]["tags"], job["method"]],
        settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False),
    ) as tracked:
        save(
            folder / "tracking.json", {"wandb_url": tracked.url, "wandb_id": tracked.id}
        )
        archive = BaselineArchive(
            s3=boto3.client("s3", region_name="us-east-1"),
            wandb=wandb,
            tracked=tracked,
            owner=boto3.client("sts").get_caller_identity()["Account"],
            bucket=location.netloc,
            prefix=location.path.lstrip("/"),
            run_id=tracking_id,
            secrets=secrets,
        )
        archive.checkpoint(bundle, folder)
        error, result, wrapped = None, None, None
        try:
            with SQLiteStateGraph(folder / "graph.sqlite") as graph:
                if job["agent_config"]:
                    config = AgentSearchConfig.model_validate(
                        {
                            **job["agent_config"],
                            "session_path": folder / "agents.sqlite",
                            "edison_cache_path": folder / "edison-disabled.sqlite",
                        }
                    )
                    await seed_memory(config.session_path, job["run_id"], dossier)
                    goal = (
                        f"Continue minimizing the fixed common-1030-deletion-root coding-gene projection on MG1655 (NC_000913.3), already lacking {len(initial.deleted_genes)} genes. "
                        "Keep all starting deletions and propose additional deletions. Aerobic M9 glucose at 37 C; positive FBA and feasible RBA at 0.1/h are required. "
                        "Seek useful additional targets; distinguish model-covered from unmodeled functions and do not claim laboratory viability or global novelty. "
                        "Use variable bundles of 1 to 100 genes. Increase bundle size when evaluated bundles succeed, and use smaller bundles to investigate failed combinations."
                    )
                    policy = make_agent_policy(
                        genes=genes,
                        modules=modules,
                        config=config,
                        evaluator_ids=identities,
                        graph=graph.readonly(),
                        run_id=job["run_id"],
                        goal=goal,
                    )
                else:
                    policy = BlockDeletionPolicy(
                        gene_ids=list(genes.index),
                        evaluator_ids=identities,
                        initial_state=initial,
                        kind=job["method"],
                        seed=job["seed"],
                        block_sizes=spec["baseline_policy"]["block_sizes"],
                        neighborhood_size=spec["baseline_policy"][
                            "greedy_neighborhood_size"
                        ],
                    )
                wrapped = CheckpointPolicy(
                    policy,
                    archive,
                    tracked,
                    bundle,
                    folder,
                    identities,
                    job["model_cap_usd"],
                )
                result = await Runner(
                    EcoliProblem(
                        genes, initial_state=initial, max_genes_per_action=100
                    ),
                    wrapped,
                    graph,
                    RunLimits(
                        max_states=101,
                        max_steps=750,
                        max_wall_time_s=max(0, launch["deadline_unix"] - time.time()),
                    ),
                    evaluators=EvaluatorSuite(scorers),
                    run_id=job["run_id"],
                    metadata=metadata,
                ).run()
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            save(
                folder / "failure.json",
                {"phase": "search", "time_unix": time.time(), **error},
            )
            print(
                json.dumps({"search_failed": job["run_id"], **error}),
                file=sys.stderr,
                flush=True,
            )
        usage = key_usage() if job["model_cap_usd"] else {"usage": 0}
        if job["model_cap_usd"]:
            check_key_budget(usage, Decimal(job["model_cap_usd"]))
        save(folder / "model-usage.json", usage)
        reason = "exception" if error else result.stop_reason
        status = (
            "failed"
            if error
            else "model_budget"
            if wrapped.stopped_for_budget
            else "completed"
            if reason == "max_states"
            else "deadline"
            if reason == "max_wall_time_s"
            else "exhausted"
        )
        terminal = {
            "status": status,
            "stop_reason": reason,
            "error": error,
            "validation_started": False,
            "model_spend_usd": float(usage["usage"]),
            "wall_time_s": time.time()
            - started
            + (timing["prior_active_wall_time_s"] if timing else 0),
            "elapsed_since_first_attempt_s": time.time()
            - (timing["first_attempt_started_at_unix"] or started)
            if timing
            else time.time() - started,
            "cpu_hours": (cpu_seconds() - cpu_started) / 3600
            + (
                timing["prior_cpu_hours"]
                if timing
                else continuation["cpu_hours"]
                if continuation
                else 0
            ),
        }
        save(folder / "terminal.json", terminal)
        report = export_graph_report(bundle, index, folder / "graph.sqlite", terminal)
        save(folder / "run-report.json", report)
        checkpoint = archive.checkpoint(bundle, folder)
        member = next(x for x in checkpoint["files"] if x["path"] == "run-report.json")
        if member["sha256"] != digest(read_bytes(folder / "run-report.json")):
            raise RuntimeError("Terminal report changed during verified backup")
        receipt = {
            "report_sha256": member["sha256"],
            "protocol_sha256": metadata["protocol_sha256"],
            "full_s3_get_verified": checkpoint["full_get_verified"],
            "wandb_download_verified": checkpoint["wandb_download_verified"],
            "wandb_state": "COMMITTED",
            "source_and_inputs_verified": True,
            "s3_uri": checkpoint["archive"]["uri"],
            "archive_member": "run-report.json",
            "wandb_artifact": checkpoint["wandb_artifact"],
            "checkpoint": checkpoint,
        }
        save(folder / "report-verification.json", receipt)
        save(
            folder / "completion.json",
            {
                "status": "terminal_verified",
                "scientific_status": status,
                "report_sha256": member["sha256"],
                "checkpoint": checkpoint,
            },
        )
        tracked.summary.update(
            {
                "status": status,
                "model_spend_usd": float(usage["usage"]),
                "attempted_candidates": len(report["candidates"]),
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--job-index", type=int, required=True)
    args = parser.parse_args()
    bundle, repo = args.bundle.resolve(), args.repo_root.resolve()
    approval = json.loads(read_bytes(bundle / "approval.json"))
    spec, jobs = validate_launch(bundle, repo, approval)
    if not 0 <= args.job_index < len(jobs):
        raise ValueError("Invalid frozen job index")
    job = jobs[args.job_index]
    folder = bundle / job["directory"]
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(execute(bundle, repo, args.job_index, approval, spec, job, folder))


if __name__ == "__main__":
    main()
