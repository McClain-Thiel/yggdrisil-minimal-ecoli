from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from yggdrisil import (
    EvaluationResult,
    EvaluatorSuite,
    GraphError,
    RandomPolicy,
    RunLimits,
    Runner,
    RunStatus,
    SQLiteStateGraph,
)

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.essentiality import (
    EssentialityClass,
    EssentialityDataset,
    EssentialitySummary,
)
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.policies import RandomDeletionSampler, SimpleHeuristicPolicy
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import (
    active_evaluator_ids,
    scientific_evaluation,
)
from yggdrisil_ecoli.search import validate_search_resume
from yggdrisil_ecoli.state import GenomeState

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class _CountingScorer:
    name: str = "evidence"
    version: str = "1"
    config: str = "fixture-config"
    calls: int = 0

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        self.calls += 1
        return scientific_evaluation(
            {
                "genes_deleted": len(state.deleted_genes),
                "deleted_gene_ids": sorted(state.deleted_genes),
            },
            coverage={"deleted_genes_total": len(state.deleted_genes)},
            provenance={"fixture": True},
        )


@dataclass
class _StateEvidenceScorer:
    name: str
    metric: str
    config: str = "fixture-config"
    version: str = "1"

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        if self.name == "essentiality":
            value: object = int("b0001" in state.deleted_genes)
        else:
            value = "b0002" not in state.deleted_genes
        metrics = {self.metric: value}
        if self.name == "fba":
            metrics["growth_rate"] = 1.0 if value is True else 0.0
        return scientific_evaluation(metrics)


@dataclass
class _FixedScorer:
    name: str
    metrics: dict[str, object]
    config: str
    version: str = "1"

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        return scientific_evaluation(self.metrics)


@pytest.mark.asyncio
async def test_runner_persists_serializable_states_actions_and_evidence(
    tmp_path: Path,
) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    problem = EcoliProblem(registry, max_genes_per_action=1)
    scorer = _CountingScorer()
    graph_path = tmp_path / "search.sqlite"
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](graph_path)

    result = await Runner(
        problem,
        RandomPolicy(
            RandomDeletionSampler(registry),
            n_proposals=1,
            seed=7,
        ),
        graph,
        RunLimits(max_states=3, max_steps=3),
        evaluators=EvaluatorSuite([scorer], concurrent=True),
        run_id="integration",
    ).run()

    assert result.unique_states == 3
    assert scorer.calls == 3
    assert all(graph.evaluations(node.state_id) for node in graph.states())
    child = max(graph.states(), key=lambda node: len(node.state.deleted_genes))
    evidence = graph.evaluations(child.state_id)[0]
    assert evidence.metrics["genes_deleted"] == len(child.state.deleted_genes)
    assert evidence.metadata["details"]["deleted_gene_ids"] == sorted(
        child.state.deleted_genes
    )
    assert evidence.metadata["coverage"] == {
        "deleted_genes_total": len(child.state.deleted_genes)
    }
    graph.close()

    reopened = SQLiteStateGraph[GenomeState, DeleteGenes](graph_path)
    assert all(isinstance(node.state, GenomeState) for node in reopened.states())
    assert all(isinstance(edge.action, DeleteGenes) for edge in reopened.edges())
    reopened.close()


@pytest.mark.asyncio
async def test_framework_suite_uses_yggdrisil_cache(tmp_path: Path) -> None:
    scorer = _CountingScorer()
    suite = EvaluatorSuite([scorer], concurrent=True)
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "cache.sqlite")
    state = GenomeState(frozenset({"b0001"}))
    graph.add_state("state", state)

    first = await suite.evaluate_cached(graph, "state")
    second = await suite.evaluate_cached(graph, "state")

    assert scorer.calls == 1
    assert first[0].evaluation_id == second[0].evaluation_id
    assert first[0].metrics == {"genes_deleted": 1}
    graph.close()


@pytest.mark.asyncio
async def test_simple_heuristic_avoids_infeasible_parent_and_essential_gene(
    tmp_path: Path,
) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    problem = EcoliProblem(registry)
    essentiality = EssentialityDataset(
        (
            _essentiality_summary("b0001", "essential"),
            _essentiality_summary("b0002", "nonessential"),
            _essentiality_summary("b0003", "nonessential"),
        ),
        (),
    )
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "policy.sqlite")
    root = graph.add_state(
        problem.state_key(problem.initial_state),
        problem.initial_state,
    )
    children = []
    for gene in ("b0002", "b0003"):
        action = DeleteGenes(genes=(gene,))
        state = problem.apply(problem.initial_state, action)
        node, _edge, _node_created, _edge_created = graph.add_transition(
            parent_id=root.state_id,
            child_id=problem.state_key(state),
            child=state,
            action=action,
        )
        children.append(node)
    scorers = (
        _StateEvidenceScorer("essentiality", "n_essential_deleted"),
        _StateEvidenceScorer("fba", "feasible"),
    )
    suite = EvaluatorSuite(list(scorers), concurrent=True)
    for node in graph.states():
        await suite.evaluate_cached(graph, node.state_id)
    policy = SimpleHeuristicPolicy(
        registry=registry,
        essentiality=essentiality,
        evaluator_ids=active_evaluator_ids(scorers),
        seed=3,
    )

    decisions = await policy.step(
        graph.readonly(),
        RunStatus(
            step=0,
            unique_states=len(graph),
            edges=graph.edge_count(),
            elapsed_s=0,
            limits=RunLimits(max_states=10),
        ),
    )

    proposal = decisions[0].proposals[0]
    viable_child = next(
        node for node in children if node.state.deleted_genes == frozenset({"b0003"})
    )
    assert proposal.parent_id == viable_child.state_id
    assert proposal.action.genes == ("b0002",)


def _essentiality_summary(
    gene: str,
    classification: EssentialityClass,
) -> EssentialitySummary:
    return EssentialitySummary(
        b_number=gene,
        classification=classification,
        m9_call_raw=None,
        lb_call_raw=None,
        m9_ecipkm=None,
        lb_ecipkm=None,
        cross_condition_pattern=None,
        evidence_conflict=False,
        basis_observation_ids=(),
        study_id="fixture",
    )


def test_resume_rejects_changed_policy_configuration(tmp_path: Path) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "resume.sqlite")
    graph.save_run(
        "run_a",
        step=0,
        status="completed",
        config={},
        metadata={"policy": "random", "seed": 7},
    )

    with pytest.raises(GraphError, match="policy"):
        validate_search_resume(
            graph,
            run_id="run_a",
            resume=True,
            expected_metadata={"policy": "heuristic", "seed": 7},
        )

    graph.close()


@pytest.mark.asyncio
async def test_heuristic_selects_active_cached_identity_after_config_reversion(
    tmp_path: Path,
) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    essentiality = EssentialityDataset(
        (
            _essentiality_summary("b0001", "nonessential"),
            _essentiality_summary("b0002", "nonessential"),
            _essentiality_summary("b0003", "nonessential"),
        ),
        (),
    )
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "identity.sqlite")
    graph.add_state("root", GenomeState(frozenset()))
    active = (
        _FixedScorer(
            "essentiality",
            {"n_essential_deleted": 0},
            "config-a",
        ),
        _FixedScorer(
            "fba",
            {"feasible": True, "growth_rate": 1.0},
            "config-a",
        ),
    )
    inactive = (
        _FixedScorer(
            "essentiality",
            {"n_essential_deleted": 1},
            "config-b",
        ),
        _FixedScorer(
            "fba",
            {"feasible": False, "growth_rate": 0.0},
            "config-b",
        ),
    )
    await EvaluatorSuite(list(active), concurrent=True).evaluate_cached(graph, "root")
    await EvaluatorSuite(list(inactive), concurrent=True).evaluate_cached(graph, "root")
    # Reverting to A is a cache hit, so A remains older than B.
    await EvaluatorSuite(list(active), concurrent=True).evaluate_cached(graph, "root")
    policy = SimpleHeuristicPolicy(
        registry=registry,
        essentiality=essentiality,
        evaluator_ids=active_evaluator_ids(active),
        seed=0,
    )

    decisions = await policy.step(
        graph.readonly(),
        RunStatus(
            step=0,
            unique_states=1,
            edges=0,
            elapsed_s=0,
            limits=RunLimits(max_states=2),
        ),
    )

    assert decisions
    graph.close()
