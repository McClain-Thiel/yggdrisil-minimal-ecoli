from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest
from yggdrisil import (
    EvaluationResult,
    EvaluatorSuite,
    RandomPolicy,
    RunLimits,
    Runner,
    RunStatus,
    SQLiteStateGraph,
)

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.policies import deletion_sampler, make_heuristic_policy
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import (
    active_evaluator_ids,
    scientific_evaluation,
)
from yggdrisil_ecoli.state import GenomeState


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
    genes: pd.DataFrame,
) -> None:
    problem = EcoliProblem(genes, max_genes_per_action=1)
    scorer = _CountingScorer()
    graph_path = tmp_path / "search.sqlite"
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](graph_path)

    result = await Runner(
        problem,
        RandomPolicy(
            deletion_sampler(genes),
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
    genes: pd.DataFrame,
) -> None:
    problem = EcoliProblem(genes)
    genes.loc["b0001", "classification"] = "essential"
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
    policy = make_heuristic_policy(
        genes=genes,
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


@pytest.mark.asyncio
async def test_heuristic_selects_active_cached_identity_after_config_reversion(
    tmp_path: Path,
    genes: pd.DataFrame,
) -> None:
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
    policy = make_heuristic_policy(
        genes=genes,
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


@pytest.mark.asyncio
async def test_agent_prompts_select_active_cached_evaluations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, genes
) -> None:
    import json

    from pydantic_ai import models
    from yggdrisil.agents import ExplorationRequest

    from yggdrisil_ecoli.agent_policy import AgentSearchConfig, make_agent_policy
    from yggdrisil_ecoli.scorers.modules import ModuleEvaluator

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    modules = ModuleEvaluator(
        registry=genes,
        entries={},
        wt_complete_module_ids=(),
        parser_semantics_version="fixture",
    )
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "agent-cache.sqlite")
    graph.add_state("root", GenomeState(frozenset()))
    active = _FixedScorer("fba", {"feasible": True, "growth_rate": 1.0}, "config-a")
    inactive = _FixedScorer("fba", {"feasible": False, "growth_rate": 0.0}, "config-b")
    for scorer in (active, inactive, active):
        await EvaluatorSuite([scorer]).evaluate_cached(graph, "root")
    policy = make_agent_policy(
        genes=genes,
        modules=modules,
        config=AgentSearchConfig(model="vendor/model"),
        evaluator_ids=active_evaluator_ids([active]),
        evaluations=graph.evaluations,
    )
    status = RunStatus(
        step=0, unique_states=1, edges=0, elapsed_s=0, limits=RunLimits(max_states=2)
    )
    explorer_prompt = policy.explorer.format_prompt(
        policy._explorer_context(graph.readonly(), ExplorationRequest("root"))
    )
    navigator_prompt = policy.navigator.format_prompt(
        policy._navigator_context(graph.readonly(), status)
    )

    expected = {"fba": {"feasible": True, "growth_rate": 1.0}}
    assert json.loads(explorer_prompt)["evaluations"] == expected
    assert json.loads(navigator_prompt)["recent_states"][0]["evaluations"] == expected
    graph.close()
