#!/usr/bin/env python3
"""Run one frozen ablation job with native Yggdrisil and ADK persistence."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import httpx
import wandb
import yggdrisil
from dotenv import load_dotenv
from google.adk.events import Event, EventActions
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from yggdrisil import (
    Decision,
    Evaluator,
    EvaluatorSuite,
    RunLimits,
    Runner,
    SQLiteStateGraph,
)

import yggdrisil_ecoli
from scripts.plan_literature_ablation import checked_file
from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.agent_policy import AgentSearchConfig, make_agent_policy
from yggdrisil_ecoli.analysis import summarize_run
from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.data.io import atomic_json, file_sha256
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import active_evaluator_ids
from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
from yggdrisil_ecoli.scorers.fba import FBAScorer
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.scorers.rba import RBAScorer
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.state import GenomeState


def load_job(bundle: Path, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((bundle / "manifest.json").read_text())
    for path, digest in manifest["bundle_sha256"].items():
        checked_file(bundle, {"path": path, "sha256": digest})
    spec = json.loads((bundle / "protocol.json").read_text())
    jobs = json.loads((bundle / "jobs.json").read_text())
    if not 0 <= index < len(jobs):
        raise ValueError(f"Job index must be in 0..{len(jobs) - 1}")
    return spec, jobs[index]


async def seed_memory(path: Path, run_id: str, dossier: dict[str, Any] | None) -> None:
    """A dossier is immutable input, separate from the agent's mutable documents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sessions = SqliteSessionService(str(path))
    try:
        session = await sessions.get_session(
            app_name="yggdrisil", user_id=run_id, session_id="navigator"
        )
        if session is None:
            await sessions.create_session(
                app_name="yggdrisil",
                user_id=run_id,
                session_id="navigator",
                state={
                    "user:initial_dossier": dossier,
                    "user:evidence_retrieved": dossier is not None,
                },
            )
        elif session.state.get("user:initial_dossier") != dossier:
            raise ValueError("Initial dossier differs from the saved ADK session")
    finally:
        await sessions.close()


async def continuation_memory(path: Path, run_id: str, execution_id: str) -> int:
    """Start a fresh research gate once while retaining all prior agent history."""
    sessions = SqliteSessionService(str(path))
    try:
        session = await sessions.get_session(
            app_name="yggdrisil", user_id=run_id, session_id="navigator"
        )
        if session is None:
            raise ValueError("Continuation requires the saved navigator session")
        if session.state.get("user:continuation_execution_id") != execution_id:
            await sessions.append_event(
                session=session,
                event=Event(
                    author="user",
                    invocation_id="continuation-" + execution_id,
                    actions=EventActions(
                        state_delta={
                            "user:continuation_execution_id": execution_id,
                            "user:continuation_parent_literature_search_count": session.state.get(
                                "user:literature_search_count", 0
                            ),
                            "user:literature_search_count": 0,
                        }
                    ),
                ),
            )
            session = await sessions.get_session(
                app_name="yggdrisil", user_id=run_id, session_id="navigator"
            )
            if session is None:
                raise ValueError("Continuation session disappeared")
        return int(session.state.get("user:literature_search_count", 0))
    finally:
        await sessions.close()


def initialize_continuation(
    bundle: Path, job: dict[str, Any], metadata: dict[str, Any]
) -> None:
    """Clone verified parent databases; never modify an expired parent run."""
    stage = job["continuation"]
    parent = bundle / stage["parent_path"]
    for name, digest in stage["member_sha256"].items():
        checked_file(parent, {"path": name, "sha256": digest})
    original = json.loads((parent / "config.json").read_text())
    result = json.loads((parent / "result.json").read_text())
    for key in ("evaluators", "versions", "protocol_sha256"):
        if original[key] != metadata[key]:
            raise ValueError("Continuation changed frozen " + key)
    for key in ("application", "framework"):
        if original["sources"][key] != metadata["sources"][key]:
            raise ValueError("Continuation changed scientific source: " + key)
    old_job = original["job"]
    for key in (
        "run_id",
        "arm",
        "seed",
        "starting_genotype",
        "starting_gene_set_sha256",
        "dossier",
        "live_literature",
    ):
        if old_job[key] != job[key]:
            raise ValueError("Continuation changed treatment: " + key)
    paths = {"session_path", "edison_cache_path"}
    if {k: v for k, v in old_job["agent_config"].items() if k not in paths} != {
        k: v for k, v in job["agent_config"].items() if k not in paths
    }:
        raise ValueError("Continuation changed agent parameters")
    if job["execution_id"] == job["run_id"]:
        raise ValueError("Continuation needs a separate execution identity")
    for limit, prior in (("max_states", "unique_states"), ("max_steps", "step")):
        if (
            job["limits"][limit]
            != result["result"][prior] + stage["additional_limits"][limit]
        ):
            raise ValueError("Continuation limits must be relative to the parent")
    if original["credential_sha256"] == metadata["credential_sha256"]:
        raise ValueError("Continuation requires a new dedicated provider key")
    for name, target in (
        ("graph.sqlite", bundle / job["graph_path"]),
        ("agents.sqlite", bundle / job["agent_config"]["session_path"]),
    ):
        if target.exists():
            raise ValueError("Partial continuation exists; inspect before recovery")
        source = (parent / name).resolve()
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("Continuation SQLite integrity check failed")
    atomic_json((bundle / job["graph_path"]).parent / "parent-checkpoint.json", stage)


def key_usage() -> dict[str, Any]:
    response = httpx.get(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        timeout=30,
    )
    response.raise_for_status()
    return dict(response.json()["data"])


def check_key_budget(usage: dict[str, Any], budget: Decimal) -> Decimal:
    """Require a dedicated non-resetting provider cap; invocation caps are soft."""
    if usage.get("limit") is None or usage.get("limit_reset") is not None:
        raise ValueError(
            "Use a dedicated OpenRouter key with a non-resetting dollar cap"
        )
    limit = Decimal(str(usage["limit"]))
    spent = Decimal(str(usage["usage"]))
    if not limit.is_finite() or not spent.is_finite() or not 0 < limit <= budget:
        raise ValueError("Provider key cap exceeds the approved job budget")
    if spent < 0 or spent > budget:
        raise ValueError("Provider key usage is outside the approved budget")
    return spent


def source_hash(package: Any) -> str:
    root = Path(package.__file__).parent
    files = {str(p.relative_to(root)): file_sha256(p) for p in root.rglob("*.py")}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


class TrackedPolicy:
    def __init__(
        self, policy: Any, folder: Path, budget: Decimal, tracked: Any
    ) -> None:
        self.policy, self.folder, self.budget, self.tracked = (
            policy,
            folder,
            budget,
            tracked,
        )
        self.stopped_for_budget = False

    def drain_interrupted_decisions(self) -> Any:
        return self.policy.drain_interrupted_decisions()

    async def step(self, graph: Any, status: Any) -> Any:
        usage = await asyncio.to_thread(key_usage)
        spent = check_key_budget(usage, self.budget)
        progress = {
            "step": status.step,
            "states": len(graph),
            "provider_spend_usd": str(spent),
        }
        atomic_json(self.folder / "progress.json", progress)
        self.tracked.log({**progress, "provider_spend_usd": float(spent)})
        # Leave headroom for the manager and explorer to finish their current turn.
        if min(self.budget, Decimal(str(usage["limit"]))) - spent < Decimal("1.25"):
            self.stopped_for_budget = True
            return [
                Decision(role="budget", output={"reason": "model_budget", **progress})
            ]
        return await self.policy.step(graph, status)


async def run_job(
    bundle: Path, index: int, repo: Path, budget: Decimal, resume: bool
) -> None:
    spec, job = load_job(bundle, index)
    continuation = job.get("continuation")
    execution_id = job.get("execution_id", job["run_id"])
    if not budget.is_finite() or budget <= Decimal("1.25"):
        raise ValueError(
            "Job budget must be finite and greater than the $1.25 turn reserve"
        )
    inputs = {name: checked_file(repo, ref) for name, ref in spec["inputs"].items()}
    start = json.loads((bundle / job["starting_genotype"]).read_text())
    initial = GenomeState(frozenset(start["deleted_gene_ids"]))
    genes = load_genes(inputs["genes"])
    problem = EcoliProblem(
        genes, max_genes_per_action=spec["agent"]["bundle_size"], initial_state=initial
    )
    modules = ModuleEvaluator.from_json(inputs["modules"], genes)
    scorers: list[Evaluator[GenomeState]] = [
        GenomeSizeScorer(genes),
        EssentialityScorer(genes=genes, artifact_hash=file_sha256(inputs["genes"])),
        modules,
        FBAScorer(genes=genes, model_path=inputs["fba_model"]),
        RBAScorer(genes=genes, artifact_dir=inputs["rba_manifest"].parent),
    ]
    identities = active_evaluator_ids(scorers)
    folder = (bundle / job["graph_path"]).parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = AgentSearchConfig.model_validate(
            {
                **job["agent_config"],
                "session_path": bundle / job["agent_config"]["session_path"],
                "edison_cache_path": bundle / job["agent_config"]["edison_cache_path"],
            }
        )
        if config.edison:
            raise ValueError("Live Edison is forbidden in this ablation")
        dossier = (
            json.loads((bundle / job["dossier"]).read_text())
            if job["dossier"]
            else None
        )
        metadata = {
            "job": job,
            "policy": job["arm"],
            "agent": config.metadata(genes),
            "evaluators": identities,
            "model_budget_usd": str(budget),
            "protocol_sha256": file_sha256(bundle / "protocol.json"),
            "sources": {
                "application": source_hash(yggdrisil_ecoli),
                "framework": source_hash(yggdrisil),
                "runner": file_sha256(Path(__file__)),
            },
            "versions": {
                name: version(name)
                for name in [
                    "google-adk",
                    "litellm",
                    "pandas",
                    "cobra",
                    "RBApy",
                    "RBAtools",
                ]
            },
            "credential_sha256": hashlib.sha256(
                os.environ["OPENROUTER_API_KEY"].encode()
            ).hexdigest(),
        }
        config_path = folder / "config.json"
        if config_path.exists():
            if not resume or json.loads(config_path.read_text()) != metadata:
                raise ValueError(
                    "Run exists; resume requires identical code, inputs, settings and key"
                )
        elif resume:
            raise ValueError("Cannot resume a run without its original config")
        else:
            usage = key_usage()
            if check_key_budget(usage, budget) != 0:
                raise ValueError(
                    "A new job requires an unused dedicated OpenRouter key"
                )
            if continuation:
                initialize_continuation(bundle, job, metadata)
            atomic_json(config_path, metadata)
            atomic_json(
                folder / "launch.json",
                {
                    "started_at_unix": time.time(),
                    "wandb_id": hashlib.sha256(execution_id.encode()).hexdigest()[:12],
                },
            )
        launch = json.loads((folder / "launch.json").read_text())
        remaining = job["limits"]["max_wall_time_s"] - (
            time.time() - launch["started_at_unix"]
        )
        if remaining <= 0:
            raise ValueError(
                "Original wall-time deadline expired; do not silently extend it on resume"
            )
        await seed_memory(config.session_path, job["run_id"], dossier)
        if continuation:
            await continuation_memory(config.session_path, job["run_id"], execution_id)
        goal = (
            f"Continue minimizing the fixed {start['name']} coding-gene projection on MG1655 (NC_000913.3), already lacking {len(initial.deleted_genes)} genes. "
            "Keep all starting deletions and propose additional deletions. Aerobic M9 glucose at 37 C; positive FBA and feasible RBA at 0.1/h are required. "
            "Seek useful additional targets; distinguish model-covered from unmodeled functions and do not claim laboratory viability or global novelty. "
            f"Use variable bundles of 1 to {config.bundle_size} genes. Increase bundle size when evaluated "
            "bundles succeed, and use smaller bundles to investigate failed combinations."
        )
        with wandb.init(
            project=spec["tracking"]["project"],
            entity=spec["tracking"]["entity"],
            id=launch["wandb_id"],
            resume="allow" if resume else "never",
            name=execution_id,
            dir=str(folder),
            config=metadata,
            tags=[*spec["tracking"]["tags"], job["arm"]],
            settings=wandb.Settings(
                disable_git=True, disable_code=True, save_code=False
            ),
        ) as tracked:
            atomic_json(
                folder / "tracking.json",
                {"wandb_url": tracked.url, "wandb_id": tracked.id},
            )
            try:
                with SQLiteStateGraph[GenomeState, DeleteGenes](
                    bundle / job["graph_path"]
                ) as graph:
                    policy = make_agent_policy(
                        genes=genes,
                        modules=modules,
                        config=config,
                        evaluator_ids=identities,
                        graph=graph.readonly(),
                        run_id=job["run_id"],
                        goal=goal,
                    )
                    bounded = TrackedPolicy(policy, folder, budget, tracked)
                    result = await Runner(
                        problem,
                        bounded,
                        graph,
                        RunLimits(**{**job["limits"], "max_wall_time_s": remaining}),
                        evaluators=EvaluatorSuite(scorers),
                        run_id=job["run_id"],
                        metadata=metadata,
                        resume=resume or bool(continuation),
                    ).run()
                summary = summarize_run(bundle / job["graph_path"])
                literature_searches = summary["scientific_tool_calls"].get(
                    "search_literature", 0
                )
                stage_searches = (
                    await continuation_memory(
                        config.session_path, job["run_id"], execution_id
                    )
                    if continuation
                    else literature_searches
                )
                if config.require_initial_research and not stage_searches:
                    raise ValueError(
                        "Adaptive run produced no recorded Europe PMC searches"
                    )
                candidate = cast(
                    dict[str, Any] | None, summary["deepest_viable_candidate"]
                )
                if (
                    candidate
                    and set(candidate["deleted_gene_ids"]) == initial.deleted_genes
                ):
                    candidate = None
                report = {
                    "status": "completed",
                    "result": asdict(result),
                    "summary": summary,
                    "finalist": candidate,
                    "additional_deletions": candidate["genes_deleted"]
                    - len(initial.deleted_genes)
                    if candidate
                    else 0,
                    "literature_searches": literature_searches,
                    "stage_successful_literature_searches": stage_searches,
                    "execution_id": execution_id,
                    "stop_reason": "model_budget"
                    if bounded.stopped_for_budget
                    else result.stop_reason,
                    "provider_spend_usd": str(check_key_budget(key_usage(), budget)),
                }
                if continuation:
                    report["continuation"] = {
                        "parent_run_id": job["run_id"],
                        "new_states": result.unique_states
                        - continuation["parent_states"],
                        "new_steps": result.step - continuation["parent_step"],
                        "additional_deletions_vs_parent_best": (
                            candidate["genes_deleted"]
                            - continuation["parent_best_deletions"]
                            if candidate
                            else 0
                        ),
                    }
                atomic_json(folder / "result.json", report)
                progress = {
                    "step": result.step,
                    "states": result.unique_states,
                    "status": report["status"],
                    "stop_reason": report["stop_reason"],
                    "provider_spend_usd": report["provider_spend_usd"],
                }
                atomic_json(folder / "progress.json", progress)
                tracked.log(
                    {
                        **progress,
                        "provider_spend_usd": float(report["provider_spend_usd"]),
                    }
                )
                tracked.summary.update(
                    {
                        key: report[key]
                        for key in [
                            "status",
                            "additional_deletions",
                            "stop_reason",
                            "provider_spend_usd",
                        ]
                    }
                )
            except Exception as exc:
                atomic_json(
                    folder / "failure.json",
                    {"type": type(exc).__name__, "message": str(exc)},
                )
                tracked.summary["status"] = "failed"
                raise
            finally:
                artifact = wandb.Artifact(
                    execution_id + "-data", type="experiment-data"
                )
                for name in [
                    "config.json",
                    "parent-checkpoint.json",
                    "launch.json",
                    "tracking.json",
                    "progress.json",
                    "result.json",
                    "failure.json",
                    "graph.sqlite",
                    "agents.sqlite",
                ]:
                    path = folder / name
                    if path.exists():
                        artifact.add_file(str(path), name=name)
                tracked.log_artifact(artifact).wait()
                atomic_json(folder / "artifact.json", {"name": artifact.qualified_name})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--job-index", type=int, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--model-budget-usd", type=Decimal, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if sys.platform == "darwin":
        parser.error(
            "Experiment execution is remote-only. Run this job on Linux compute."
        )
    load_dotenv(args.repo_root / ".env", override=False)
    asyncio.run(
        run_job(
            args.bundle.resolve(),
            args.job_index,
            args.repo_root.resolve(),
            args.model_budget_usd,
            args.resume,
        )
    )


if __name__ == "__main__":
    main()
