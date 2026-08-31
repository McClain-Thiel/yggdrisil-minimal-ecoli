from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from yggdrisil import (
    EvaluationResult,
    EvaluatorSuite,
    RunLimits,
    Runner,
    RunStatus,
    SQLiteStateGraph,
)

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.essentiality import (
    EssentialityDataset,
    EssentialityRecord,
)
from yggdrisil_ecoli.scorers.base import active_evaluator_ids, scientific_evaluation
from yggdrisil_ecoli.state import GenomeState, genome_state_key
from yggdrisil_ecoli.strong_baselines import EvolutionaryPolicy, MinesweeperPolicy


@dataclass
class _GateScorer:
    name: str
    lethal_gene: str | None = None
    version: str = "1"
    config: str = "strong-baseline-fixture"

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        feasible = self.lethal_gene not in state.deleted_genes
        if self.name == "fba":
            return scientific_evaluation(
                {"feasible": feasible, "growth_rate": 1.0 if feasible else 0.0}
            )
        return scientific_evaluation({"feasible_at_growth_floor": feasible})


@pytest.mark.asyncio
async def test_evolutionary_policy_uses_viable_population_and_unique_targets(
    tmp_path: Path,
) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "evolution.sqlite")
    root = graph.add_state(
        genome_state_key(GenomeState(frozenset())), GenomeState(frozenset())
    )
    for gene in ("b0001", "b0002"):
        state = GenomeState(frozenset({gene}))
        graph.add_transition(
            parent_id=root.state_id,
            child_id=genome_state_key(state),
            child=state,
            action=DeleteGenes(genes=(gene,)),
        )
    scorers = (_GateScorer("fba"), _GateScorer("resource_allocation"))
    suite = EvaluatorSuite(list(scorers), concurrent=True)
    for node in graph.states():
        await suite.evaluate_cached(graph, node.state_id)
    policy = EvolutionaryPolicy(
        candidate_genes={"b0001", "b0002", "b0003", "b0004"},
        evaluator_ids=active_evaluator_ids(scorers),
        max_action_size=2,
        n_proposals=4,
        seed=101,
        population_size=4,
    )

    decisions = await policy.step(graph.readonly(), _status(graph))

    proposals = decisions[0].proposals
    targets = {
        graph.get_state(proposal.parent_id).state.deleted_genes.union(
            proposal.action.genes
        )
        for proposal in proposals
    }
    assert len(proposals) == len(targets) == 4
    assert all(1 <= len(proposal.action.genes) <= 2 for proposal in proposals)
    assert {proposal.metadata["operator"] for proposal in proposals} <= {
        "mutation",
        "union_crossover",
    }
    graph.close()


@pytest.mark.asyncio
async def test_minesweeper_screens_combines_and_bisects_without_known_essentials(
    tmp_path: Path,
) -> None:
    genes = {f"b{index:04d}" for index in range(1, 10)}
    essentiality = _essentiality(genes, essential={"b0009"})
    scorers = (
        _GateScorer("fba"),
        _GateScorer("resource_allocation", lethal_gene="b0002"),
    )
    problem = _TestProblem(frozenset(genes), max_action_size=2)
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "minesweeper.sqlite")
    policy = MinesweeperPolicy(
        candidate_genes=genes,
        essentiality=essentiality,
        evaluator_ids=active_evaluator_ids(scorers),
        max_action_size=2,
        n_proposals=2,
        seed=101,
    )

    result = await Runner(
        problem,
        policy,
        graph,
        RunLimits(max_states=18, max_steps=12),
        evaluators=EvaluatorSuite(list(scorers), concurrent=True),
        run_id="minesweeper-test",
    ).run()

    events = graph.proposal_events(run_id=result.run_id)
    operators = {event.metadata["operator"] for event in events}
    assert "segment_screen" in operators
    assert "lethal_bisection" in operators
    assert "segment_combine" in operators
    assert all(1 <= len(event.action.genes) <= 2 for event in events)
    assert all("b0009" not in event.action.genes for event in events)
    assert policy.metadata()["excluded_known_essential"] == 1
    graph.close()


@pytest.mark.asyncio
async def test_minesweeper_fills_partial_final_screening_batch(tmp_path: Path) -> None:
    genes = {f"b{index:04d}" for index in range(1, 9)}
    essentiality = _essentiality(genes, essential=set())
    scorers = (_GateScorer("fba"), _GateScorer("resource_allocation"))
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "partial.sqlite")
    result = await Runner(
        _TestProblem(frozenset(genes), max_action_size=2),
        MinesweeperPolicy(
            candidate_genes=genes,
            essentiality=essentiality,
            evaluator_ids=active_evaluator_ids(scorers),
            max_action_size=2,
            n_proposals=3,
            seed=101,
        ),
        graph,
        RunLimits(max_states=7, max_steps=2),
        evaluators=EvaluatorSuite(list(scorers), concurrent=True),
        run_id="partial-screen",
    ).run()

    assert result.unique_states == 7
    final_decision = graph.decisions(run_id=result.run_id, newest=True)[0]
    assert final_decision.output == {"phase": "segment_screen_and_combine"}
    graph.close()


def test_minesweeper_seeded_order_is_reproducible() -> None:
    genes = {f"b{index:04d}" for index in range(1, 10)}
    essentiality = _essentiality(genes, essential={"b0009"})

    def make_policy(seed: int) -> MinesweeperPolicy:
        return MinesweeperPolicy(
            candidate_genes=genes,
            essentiality=essentiality,
            evaluator_ids={"fba": "fba", "resource_allocation": "rba"},
            max_action_size=2,
            n_proposals=2,
            seed=seed,
        )

    first = make_policy(101)
    repeated = make_policy(101)
    changed = make_policy(202)

    assert first.metadata() == repeated.metadata()
    assert (
        first.metadata()["candidate_order_hash"]
        != changed.metadata()["candidate_order_hash"]
    )


@dataclass(frozen=True)
class _TestProblem:
    candidate_genes: frozenset[str]
    max_action_size: int
    initial_state: GenomeState = GenomeState(frozenset())

    def state_key(self, state: GenomeState) -> str:
        return genome_state_key(state)

    def apply(self, state: GenomeState, action: DeleteGenes) -> GenomeState:
        assert set(action.genes) <= self.candidate_genes
        assert not state.deleted_genes.intersection(action.genes)
        assert len(action.genes) <= self.max_action_size
        return GenomeState(state.deleted_genes.union(action.genes))

    def problem_fingerprint(self) -> dict[str, object]:
        return {
            "candidate_genes": sorted(self.candidate_genes),
            "max_action_size": self.max_action_size,
        }


def _status(
    graph: SQLiteStateGraph[GenomeState, DeleteGenes],
) -> RunStatus:
    return RunStatus(
        step=0,
        unique_states=len(graph),
        edges=graph.edge_count(),
        elapsed_s=0,
        limits=RunLimits(max_states=20),
        run_id="test",
    )


def _essentiality(genes: set[str], *, essential: set[str]) -> EssentialityDataset:
    records = []
    for gene in genes:
        is_essential = gene in essential
        records.append(
            EssentialityRecord(
                b_number=gene,
                classification="essential" if is_essential else "nonessential",
                coverage="measured",
                lb_call_raw="E" if is_essential else "NE",
                lb_ecipkm=1.0 if is_essential else 3.0,
                m9_call_raw="E" if is_essential else "NE",
                m9_ecipkm=1.0 if is_essential else 3.0,
            )
        )
    return EssentialityDataset(records)
