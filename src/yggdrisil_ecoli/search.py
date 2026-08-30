"""Run reproducible baseline searches on the Yggdrisil DAG."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from importlib.metadata import distribution
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

from yggdrisil_ecoli import __version__
from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.agent_policy import (
    AgentPolicyError,
    AgentSearchConfig,
    make_agent_policy,
)
from yggdrisil_ecoli.data.candidate_universe import CandidateUniverse
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.policies import (
    deletion_sampler,
    make_heuristic_policy,
    viability_eligibility,
)
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import active_evaluator_ids
from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.state import GenomeState

SEARCH_CONTRACT_VERSION = 7


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

    @property
    def rba(self) -> Path:
        return self.data_dir / "external" / "rba_ecoli_k12_wt"

    @property
    def wcm_candidate_universe(self) -> Path:
        return self.data_dir / "processed" / "wcm_1219_candidate_universe.json"


DEFAULT_SEARCH_ARTIFACTS = SearchArtifacts()


def load_standard_evaluators(
    artifacts: SearchArtifacts,
) -> tuple[
    GeneRegistry,
    EssentialityDataset,
    tuple[Evaluator[GenomeState], ...],
]:
    """Load the five automatic scorers from frozen local artifacts."""

    from yggdrisil_ecoli.scorers.fba import FBAScorer
    from yggdrisil_ecoli.scorers.rba import RBAScorer

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
        RBAScorer.from_artifact(artifacts.rba, registry=registry),
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
    agent_config: AgentSearchConfig | None = None,
    candidate_universe_name: str = "all",
) -> RunResult:
    """Run a baseline or bounded agent policy over identical evidence."""

    if policy_name not in {"random", "heuristic", "agent"}:
        raise ValueError(f"unknown policy: {policy_name!r}")
    if policy_name == "agent" and agent_config is None:
        raise ValueError("agent_config is required for the agent policy")
    if policy_name != "agent" and agent_config is not None:
        raise ValueError("agent_config is only valid for the agent policy")
    registry, essentiality, evaluators = load_standard_evaluators(artifacts)
    candidate_universe = load_candidate_universe(
        artifacts,
        registry=registry,
        name=candidate_universe_name,
    )
    evaluator_ids = active_evaluator_ids(evaluators)
    metadata = {
        "application": {
            "distribution": f"yggdrisil-ecoli=={__version__}",
            "source_sha256": _application_source_hash(),
        },
        "search_contract": SEARCH_CONTRACT_VERSION,
        "framework": _installed_revision("yggdrisil"),
        "evaluators": evaluator_ids,
        "policy": policy_name,
        "seed": seed,
        "bundle_size": bundle_size,
        "n_proposals": n_proposals,
        "candidate_universe": candidate_universe.metadata(),
    }
    if agent_config is not None:
        if agent_config.bundle_size != bundle_size:
            raise ValueError("agent bundle_size must match the search bundle_size")
        if agent_config.max_actions != n_proposals:
            raise ValueError("agent max_actions must match n_proposals")
        metadata["agent"] = agent_config.metadata(
            registry,
            candidate_genes=candidate_universe.genes,
        )
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](graph_path)
    try:
        validate_search_resume(
            graph,
            run_id=run_id,
            resume=resume,
            expected_metadata=metadata,
        )
        problem = EcoliProblem(
            registry,
            max_genes_per_action=bundle_size,
            candidate_genes=candidate_universe.genes,
        )
        policy: Policy[DeleteGenes]
        if policy_name == "random":
            policy = RandomPolicy(
                deletion_sampler(
                    registry,
                    bundle_size=bundle_size,
                    candidate_genes=candidate_universe.genes,
                ),
                n_proposals=n_proposals,
                seed=seed,
                eligible=viability_eligibility(evaluator_ids),
            )
        elif policy_name == "heuristic":
            policy = make_heuristic_policy(
                registry=registry,
                essentiality=essentiality,
                evaluator_ids=evaluator_ids,
                bundle_size=bundle_size,
                n_proposals=n_proposals,
                seed=seed,
                candidate_genes=candidate_universe.genes,
            )
        else:
            assert agent_config is not None
            modules = next(
                evaluator
                for evaluator in evaluators
                if isinstance(evaluator, ModuleEvaluator)
            )
            policy = make_agent_policy(
                registry=registry,
                essentiality=essentiality,
                modules=modules,
                evaluator_ids=evaluator_ids,
                config=agent_config,
                candidate_genes=candidate_universe.genes,
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


def load_candidate_universe(
    artifacts: SearchArtifacts,
    *,
    registry: GeneRegistry,
    name: str,
) -> CandidateUniverse:
    """Resolve one explicit deletion universe for every policy arm."""

    if name == "all":
        return CandidateUniverse.full_registry(registry)
    if name == "wcm-1219":
        return CandidateUniverse.from_json(
            artifacts.wcm_candidate_universe,
            registry=registry,
            registry_path=artifacts.registry,
        )
    raise ValueError(f"unknown candidate universe: {name!r}")


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


def _installed_revision(name: str) -> str:
    """Return the exact VCS revision when PEP 610 metadata provides one."""

    package = distribution(name)
    identity = f"{name}=={package.version}"
    raw = package.read_text("direct_url.json")
    if raw is None:
        return identity
    try:
        direct_url = json.loads(raw)
    except json.JSONDecodeError:
        return identity
    if isinstance(direct_url, dict):
        vcs = direct_url.get("vcs_info")
        if isinstance(vcs, dict) and isinstance(vcs.get("commit_id"), str):
            return f"{identity}@{vcs['commit_id']}"
    return identity


def _application_source_hash() -> str:
    """Fingerprint the exact local package source used by this search."""

    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        digest.update(str(path.relative_to(package_root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, default=Path("runs/search.sqlite"))
    parser.add_argument(
        "--policy", choices=("random", "heuristic", "agent"), default="random"
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--bundle-size", type=int, default=1)
    parser.add_argument("--n-proposals", type=int, default=2)
    parser.add_argument("--max-states", type=int, default=10)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-wall-time-s", type=float)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--candidate-universe",
        choices=("all", "wcm-1219"),
        default="all",
        help="genes eligible for deletion; wcm-1219 uses the pinned EMine universe",
    )
    parser.add_argument(
        "--model",
        help="fixed OpenRouter model id for --policy agent, for example vendor/model",
    )
    parser.add_argument(
        "--agent-mode",
        choices=("closed-book", "tool-rich"),
        default="closed-book",
    )
    parser.add_argument("--open-set-width", type=int, default=16)
    parser.add_argument(
        "--parents-per-step",
        "--max-agent-requests",
        dest="parents_per_step",
        type=int,
        default=4,
        help="viable parent states expanded per search step",
    )
    parser.add_argument("--max-model-requests", type=int, default=6)
    parser.add_argument("--max-agent-tool-calls", type=int, default=16)
    parser.add_argument("--max-agent-output-tokens", type=int, default=800)
    parser.add_argument(
        "--max-agent-cost-usd",
        type=Decimal,
        default=Decimal("0.02"),
        help="maximum estimated cost per explorer invocation",
    )
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="start a new run over the existing shared DAG",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    if args.policy == "agent" and args.model is None:
        parser.error("--model is required with --policy agent")
    agent_config = None
    if args.policy == "agent":
        agent_config = AgentSearchConfig(
            model=args.model,
            mode=args.agent_mode,
            seed=args.seed,
            bundle_size=args.bundle_size,
            max_actions=args.n_proposals,
            open_set_width=args.open_set_width,
            parents_per_step=args.parents_per_step,
            max_model_requests=args.max_model_requests,
            max_tool_calls=args.max_agent_tool_calls,
            max_output_tokens=args.max_agent_output_tokens,
            max_cost_per_call_usd=args.max_agent_cost_usd,
        )
    try:
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
                agent_config=agent_config,
                candidate_universe_name=args.candidate_universe,
            )
        )
    except (
        AgentPolicyError,
        DataValidationError,
        GraphError,
        OSError,
        ValueError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(asdict(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
