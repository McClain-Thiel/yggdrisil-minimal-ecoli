"""Run reproducible baseline searches on the Yggdrisil DAG."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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
    AgentSearchConfig,
    make_agent_policy,
)
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.policies import deletion_sampler, make_heuristic_policy
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import active_evaluator_ids
from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.state import GenomeState

SEARCH_CONTRACT_VERSION = 4


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


async def run_search(
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
) -> RunResult:
    """Run a baseline or bounded agent policy over identical evidence."""

    if policy_name not in {"random", "heuristic", "agent"}:
        raise ValueError(f"unknown policy: {policy_name!r}")
    if policy_name == "agent" and agent_config is None:
        raise ValueError("agent_config is required for the agent policy")
    if policy_name != "agent" and agent_config is not None:
        raise ValueError("agent_config is only valid for the agent policy")
    if agent_config is not None and (
        agent_config.seed != seed
        or agent_config.bundle_size != bundle_size
        or agent_config.max_actions != n_proposals
    ):
        raise ValueError(
            "agent seed, bundle_size and max_actions must match the search"
        )
    registry, essentiality, evaluators = load_standard_evaluators(artifacts)
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
    }
    if agent_config is not None:
        metadata["agent"] = agent_config.metadata(registry)
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](graph_path)
    try:
        validate_search_resume(
            graph,
            run_id=run_id,
            resume=resume,
            expected_metadata=metadata,
        )
        problem = EcoliProblem(registry, max_genes_per_action=bundle_size)
        policy: Policy[DeleteGenes]
        if policy_name == "random":
            policy = RandomPolicy(
                deletion_sampler(registry, bundle_size=bundle_size),
                n_proposals=n_proposals,
                seed=seed,
            )
        elif policy_name == "heuristic":
            policy = make_heuristic_policy(
                registry=registry,
                essentiality=essentiality,
                evaluator_ids=evaluator_ids,
                bundle_size=bundle_size,
                n_proposals=n_proposals,
                seed=seed,
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
                config=agent_config,
                evaluator_ids=evaluator_ids,
                evaluations=graph.evaluations,
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
            "experiment, or resume=False to reuse the existing DAG"
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
