"""Run reproducible baseline searches on the Yggdrisil DAG."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from yggdrisil import (
    Evaluator,
    EvaluatorSuite,
    GraphError,
    Policy,
    RandomPolicy,
    RunLimits,
    Runner,
    RunResult,
    SQLiteStateGraph,
)

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.policies import RandomDeletionSampler, SimpleHeuristicPolicy
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import active_evaluator_ids
from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.state import GenomeState

@dataclass(frozen=True, slots=True)
class SearchArtifacts:
    """Conventional artifact paths rooted at one local data directory."""

    data_dir: Path = Path("data")

    @property
    def registry(self) -> Path:
        return self.data_dir / "processed" / "gene_registry.parquet"

    @property
    def essentiality(self) -> Path:
        return self.data_dir / "processed" / "essentiality.parquet"

    @property
    def kegg_modules(self) -> Path:
        return self.data_dir / "processed" / "kegg_modules.json"

    @property
    def iml1515(self) -> Path:
        return self.data_dir / "external" / "iML1515.json"


DEFAULT_SEARCH_ARTIFACTS = SearchArtifacts()


def load_standard_evaluators(
    artifacts: SearchArtifacts,
) -> tuple[
    GeneRegistry,
    EssentialityDataset,
    tuple[Evaluator[GenomeState], ...],
]:
    """Load the four automatic scorers from frozen local artifacts."""

    from yggdrisil_ecoli.scorers.fba import FBAScorer

    registry = GeneRegistry.from_parquet(artifacts.registry)
    essentiality = EssentialityDataset.from_parquet(artifacts.essentiality)
    modules = ModuleEvaluator.from_json(artifacts.kegg_modules, registry)
    evaluators: tuple[Evaluator[GenomeState], ...] = (
        GenomeSizeScorer(registry),
        EssentialityScorer(
            registry=registry,
            dataset=essentiality,
            artifact_hash=file_sha256(artifacts.essentiality),
        ),
        modules,
        FBAScorer(model_path=artifacts.iml1515, registry=registry),
    )
    return registry, essentiality, evaluators


async def run_baseline_search(
    *,
    artifacts: SearchArtifacts,
    graph_path: str | Path,
    policy_name: str = "random",
    seed: int = 0,
    bundle_size: int = 1,
    n_proposals: int = 2,
    max_states: int = 10,
    max_steps: int = 10,
    max_wall_time_s: float | None = None,
    run_id: str | None = None,
    resume: bool = True,
) -> RunResult:
    """Run RandomPolicy or the frozen heuristic with identical evidence."""

    if policy_name not in {"random", "heuristic"}:
        raise ValueError(f"unknown policy: {policy_name!r}")
    metadata = {
        "application": "yggdrisil-ecoli",
        "policy": policy_name,
        "seed": seed,
        "bundle_size": bundle_size,
        "n_proposals": n_proposals,
    }
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](graph_path)
    try:
        validate_search_resume(
            graph,
            run_id=run_id,
            resume=resume,
            expected_metadata=metadata,
        )
        registry, essentiality, evaluators = load_standard_evaluators(artifacts)
        problem = EcoliProblem(registry, max_genes_per_action=bundle_size)
        policy: Policy[DeleteGenes]
        if policy_name == "random":
            policy = RandomPolicy(
                RandomDeletionSampler(registry, bundle_size=bundle_size),
                n_proposals=n_proposals,
                seed=seed,
            )
        else:
            policy = SimpleHeuristicPolicy(
                registry=registry,
                essentiality=essentiality,
                evaluator_ids=active_evaluator_ids(evaluators),
                bundle_size=bundle_size,
                n_proposals=n_proposals,
                seed=seed,
            )
        return await Runner(
            problem,
            policy,
            graph,
            RunLimits(
                max_states=max_states,
                max_steps=max_steps,
                max_wall_time_s=max_wall_time_s,
            ),
            evaluators=EvaluatorSuite(list(evaluators), concurrent=True),
            run_id=run_id,
            resume=resume,
            metadata=metadata,
        ).run()
    finally:
        graph.close()


def validate_search_resume(
    graph: SQLiteStateGraph[GenomeState, DeleteGenes],
    *,
    run_id: str | None,
    resume: bool,
    expected_metadata: dict[str, object],
) -> None:
    """Refuse to resume a trajectory with a different policy configuration."""

    if not resume:
        return
    if run_id is None:
        record = graph.latest_run()
    else:
        try:
            record = graph.get_run(run_id)
        except KeyError:
            return
    if record is None:
        return
    changed = [
        key
        for key, expected in expected_metadata.items()
        if record.metadata.get(key) != expected
    ]
    if changed:
        raise GraphError(
            "refusing to resume with changed search configuration: "
            f"{', '.join(sorted(changed))}; use a new graph for an independent "
            "experiment, or --new-run to reuse the existing DAG"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, default=Path("runs/search.sqlite"))
    parser.add_argument("--policy", choices=("random", "heuristic"), default="random")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--bundle-size", type=int, default=1)
    parser.add_argument("--n-proposals", type=int, default=2)
    parser.add_argument("--max-states", type=int, default=10)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-wall-time-s", type=float)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="start a new run over the existing shared DAG",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    result = asyncio.run(
        run_baseline_search(
            artifacts=SearchArtifacts(args.data_dir),
            graph_path=args.graph,
            policy_name=args.policy,
            seed=args.seed,
            bundle_size=args.bundle_size,
            n_proposals=args.n_proposals,
            max_states=args.max_states,
            max_steps=args.max_steps,
            max_wall_time_s=args.max_wall_time_s,
            run_id=args.run_id,
            resume=not args.new_run,
        )
    )
    print(json.dumps(asdict(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
