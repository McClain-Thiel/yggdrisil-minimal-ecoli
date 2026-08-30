from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from yggdrisil import (
    EvaluationResult,
    NavigatorExplorerPolicy,
    RunLimits,
    Runner,
    RunStatus,
    SQLiteStateGraph,
)
from yggdrisil.agents import ExplorerContext, ExplorerResult

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.open_set import OpenSetConfig, RecoverableOpenSetSelector
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.state import GenomeState

FIXTURES = Path(__file__).parent / "fixtures"

EVALUATOR_IDS = {
    "essentiality": "essentiality-fixture",
    "fba": "fba-fixture",
    "module_retention": "modules-fixture",
    "resource_allocation": "resource-fixture",
}


def test_viable_nonleaf_recovers_after_lethal_siblings_and_resume(
    tmp_path: Path,
) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "recovery.sqlite")
    graph.save_run("run_a", step=1, status="running", config={}, metadata={})
    graph.save_run("other_run", step=1, status="running", config={}, metadata={})
    root = _add_state(graph, "root", ())
    parent = _add_state(graph, "parent", ("b0001",), essential=55, broken=6)
    lethal_a = _add_state(graph, "lethal_a", ("b0001", "b0002"), growth=0.0)
    lethal_b = _add_state(graph, "lethal_b", ("b0001", "b0003"), growth=0.0)
    graph.add_edge(root, parent, DeleteGenes(genes=("b0001",)))
    edge_a = graph.add_edge(parent, lethal_a, DeleteGenes(genes=("b0002",)))
    edge_b = graph.add_edge(parent, lethal_b, DeleteGenes(genes=("b0003",)))
    _add_attempt(
        graph,
        run_id="run_a",
        decision_id="attempt-a",
        parent_id=parent,
        actions=(
            ("b0002", lethal_a, edge_a.edge_id),
            ("b0003", lethal_b, edge_b.edge_id),
        ),
    )
    for index in range(4):
        _add_attempt(
            graph,
            run_id="run_a",
            decision_id=f"root-{index}",
            parent_id=root,
            actions=(),
            step=index + 1,
        )
    # A different run must not exhaust this run's parent or contaminate its history.
    for index in range(4):
        _add_attempt(
            graph,
            run_id="other_run",
            decision_id=f"other-{index}",
            parent_id=parent,
            actions=(),
            step=index + 1,
        )

    selector = _selector()
    request = selector.select(graph.readonly(), _status("run_a", step=2))[0]

    assert request.state_id == parent
    assert request.guidance is not None
    assert "RECOVERY_ATTEMPT: 2" in request.guidance
    assert "SUGGESTED_FALLBACK_CEILING: 10" in request.guidance
    assert request.guidance.count('"child_viability": "nonviable"') == 2
    assert "b0002" in request.guidance
    assert selector.attempted_actions(parent) == frozenset({("b0002",), ("b0003",)})

    resumed = _selector()
    resumed_request = resumed.select(graph.readonly(), _status("run_a", step=3))[0]
    assert resumed_request.state_id == parent
    assert resumed.attempted_actions(parent) == selector.attempted_actions(parent)
    assert "CANDIDATE_PREVIEW_PAGE: 3" in resumed_request.guidance
    graph.close()


def test_closed_book_recovery_guidance_blinds_canonical_gene_ids(
    tmp_path: Path,
) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "blind.sqlite")
    graph.save_run("run_a", step=1, status="running", config={}, metadata={})
    parent = _add_state(graph, "parent", ("b0001",))
    child = _add_state(graph, "child", ("b0001", "b0002"), growth=0.0)
    edge = graph.add_edge(parent, child, DeleteGenes(genes=("b0002",)))
    _add_attempt(
        graph,
        run_id="run_a",
        decision_id="attempt",
        parent_id=parent,
        actions=(("b0002", child, edge.edge_id),),
    )
    blinded = {"b0001": "g0001", "b0002": "g0002"}
    selector = _selector(public_gene_id=blinded.__getitem__)

    request = selector.select(graph.readonly(), _status("run_a", step=2))[0]

    assert request.guidance is not None
    assert "g0002" in request.guidance
    assert "b0001" not in request.guidance
    assert "b0002" not in request.guidance
    graph.close()


def test_active_window_favors_diverse_deletion_sets(tmp_path: Path) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "diverse.sqlite")
    graph.save_run("run_a", step=0, status="running", config={}, metadata={})
    states = {
        _add_state(graph, "near-a", ("b0001", "b0002")),
        _add_state(graph, "near-b", ("b0001", "b0003")),
        _add_state(graph, "far", ("b0004", "b0005")),
    }
    selector = RecoverableOpenSetSelector(
        evaluator_ids=EVALUATOR_IDS,
        max_action_size=20,
        config=OpenSetConfig(
            active_width=2,
            parents_per_step=2,
        ),
        seed=4,
        candidate_count=400,
        candidate_page_size=100,
    )

    requests = selector.select(graph.readonly(), _status("run_a", step=0))
    chosen = [
        graph.get_state(request.state_id).state.deleted_genes for request in requests
    ]

    assert len(requests) == 2
    assert {request.state_id for request in requests} <= states
    assert _distance(chosen[0], chosen[1]) == 1.0
    graph.close()


def test_fba_positive_resource_infeasible_state_is_not_reopened(
    tmp_path: Path,
) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "resource.sqlite")
    graph.save_run("run_a", step=0, status="running", config={}, metadata={})
    viable = _add_state(graph, "viable", ("b0001",))
    _add_state(
        graph,
        "resource-lethal",
        ("b0001", "b0002"),
        growth=1.0,
        resource_feasible=False,
    )

    requests = _selector().select(graph.readonly(), _status("run_a", step=0))

    assert [request.state_id for request in requests] == [viable]
    graph.close()


def test_fba_only_ablation_reopens_resource_infeasible_state(tmp_path: Path) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "fba-only.sqlite")
    graph.save_run("run_a", step=0, status="running", config={}, metadata={})
    state_id = _add_state(
        graph,
        "resource-infeasible",
        ("b0001",),
        growth=1.0,
        resource_feasible=False,
    )
    selector = RecoverableOpenSetSelector(
        evaluator_ids=EVALUATOR_IDS,
        max_action_size=20,
        config=OpenSetConfig(viability_gate="fba-only"),
        seed=3,
        candidate_count=400,
        candidate_page_size=100,
    )

    requests = selector.select(graph.readonly(), _status("run_a", step=0))

    assert [request.state_id for request in requests] == [state_id]
    graph.close()


def test_frontier_only_ablation_cannot_reopen_viable_nonleaf(tmp_path: Path) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "frontier.sqlite")
    graph.save_run("run_a", step=0, status="running", config={}, metadata={})
    parent = _add_state(graph, "parent", ("b0001",))
    lethal = _add_state(graph, "lethal", ("b0001", "b0002"), growth=0.0)
    graph.add_edge(parent, lethal, DeleteGenes(genes=("b0002",)))
    selector = RecoverableOpenSetSelector(
        evaluator_ids=EVALUATOR_IDS,
        max_action_size=20,
        config=OpenSetConfig(recover_nonleaf=False),
        seed=3,
        candidate_count=400,
        candidate_page_size=100,
    )

    assert selector.select(graph.readonly(), _status("run_a", step=0)) == []
    assert (
        _selector().select(graph.readonly(), _status("run_a", step=0))[0].state_id
        == parent
    )
    graph.close()


def test_failed_model_call_does_not_consume_attempt_but_valid_empty_does(
    tmp_path: Path,
) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "attempts.sqlite")
    graph.save_run("run_a", step=1, status="running", config={}, metadata={})
    parent = _add_state(graph, "parent", ("b0001",))
    _add_empty_decision(
        graph,
        "failed",
        parent,
        metadata={"attempt_status": "failed"},
    )
    selector = _selector()

    after_failure = selector.select(graph.readonly(), _status("run_a", step=1))[0]

    assert "RECOVERY_ATTEMPT: 1" in (after_failure.guidance or "")
    _add_empty_decision(graph, "valid-empty", parent, metadata={})
    after_empty = selector.select(graph.readonly(), _status("run_a", step=2))[0]
    assert "RECOVERY_ATTEMPT: 2" in (after_empty.guidance or "")
    graph.close()


def test_skipped_proposal_remains_retryable_and_does_not_consume_attempt(
    tmp_path: Path,
) -> None:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "skipped.sqlite")
    graph.save_run("run_a", step=1, status="running", config={}, metadata={})
    parent = _add_state(graph, "parent", ("b0001",))
    graph.add_decision(
        "decision",
        run_id="run_a",
        policy="fixture",
        role="explorer",
        model="fixture",
        selected_state_ids=[parent],
        input_context={},
        tool_calls=[],
        output={"actions": [{"genes": ["b0002"]}]},
        metadata={},
        created_step=1,
    )
    graph.add_proposal_event(
        "event",
        decision_id="decision",
        run_id="run_a",
        parent_id=parent,
        action=DeleteGenes(genes=("b0002",)),
        metadata={},
        created_step=1,
        proposal_index=0,
        sequence_index=0,
    )
    graph.finish_proposal_event("event", outcome="skipped_failure")
    selector = _selector()

    request = selector.select(graph.readonly(), _status("run_a", step=1))[0]

    assert "RECOVERY_ATTEMPT: 1" in (request.guidance or "")
    assert selector.attempted_actions(parent) == frozenset()
    assert "b0002" not in (request.guidance or "")
    graph.close()


@pytest.mark.asyncio
async def test_runner_retries_valid_empty_exploration_until_global_limit(
    tmp_path: Path,
) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    problem = EcoliProblem(registry, max_genes_per_action=20)
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "empty.sqlite")
    root_id = problem.state_key(problem.initial_state)
    _add_state(graph, root_id, ())
    selector = RecoverableOpenSetSelector(
        evaluator_ids=EVALUATOR_IDS,
        max_action_size=20,
        config=OpenSetConfig(
            active_width=1,
            parents_per_step=1,
        ),
        seed=1,
        candidate_count=len(registry.search_universe),
        candidate_page_size=2,
    )

    class EmptyExplorer:
        model = "fixture"
        calls = 0

        async def explore(
            self, context: ExplorerContext[GenomeState]
        ) -> ExplorerResult[DeleteGenes]:
            self.calls += 1
            return ExplorerResult(actions=[])

    explorer = EmptyExplorer()
    policy = NavigatorExplorerPolicy(
        None,
        explorer,
        max_requests=1,
        request_selector=selector,
    )

    result = await Runner(
        problem,
        policy,
        graph,
        RunLimits(max_steps=6),
        run_id="run_a",
        resume=False,
    ).run()

    assert result.stop_reason == "max_steps"
    assert explorer.calls == 6
    explorer_decisions = [
        decision
        for decision in graph.decisions(run_id="run_a")
        if decision.role == "explorer"
    ]
    assert len(explorer_decisions) == 6
    assert all(
        decision.output == {"actions": [], "note": None}
        for decision in explorer_decisions
    )
    graph.close()


@pytest.mark.asyncio
async def test_mixed_explorer_failure_still_materializes_successful_sibling(
    tmp_path: Path,
) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    problem = EcoliProblem(registry, max_genes_per_action=20)
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "mixed.sqlite")
    root_id = problem.state_key(problem.initial_state)
    _add_state(graph, root_id, ())
    branch = GenomeState(frozenset({"b0001"}))
    branch_id = problem.state_key(branch)
    _add_state(graph, branch_id, ("b0001",))
    graph.add_edge(root_id, branch_id, DeleteGenes(genes=("b0001",)))
    selector = RecoverableOpenSetSelector(
        evaluator_ids=EVALUATOR_IDS,
        max_action_size=20,
        config=OpenSetConfig(active_width=2, parents_per_step=2),
        seed=1,
        candidate_count=len(registry.search_universe),
        candidate_page_size=2,
    )

    class MixedExplorer:
        model = "fixture"

        async def explore(
            self, context: ExplorerContext[GenomeState]
        ) -> ExplorerResult[DeleteGenes]:
            if context.state.deleted_genes:
                raise RuntimeError("transient fixture failure")
            return ExplorerResult(actions=[DeleteGenes(genes=("b0002",))])

    policy = NavigatorExplorerPolicy(
        None,
        MixedExplorer(),
        max_requests=2,
        request_selector=selector,
        tolerate_explorer_failures=True,
    )

    result = await Runner(
        problem,
        policy,
        graph,
        RunLimits(max_steps=1),
        run_id="run_a",
        resume=False,
    ).run()

    events = graph.proposal_events(run_id="run_a")
    failed = [
        decision
        for decision in graph.decisions(run_id="run_a")
        if decision.metadata.get("attempt_status") == "failed"
    ]
    assert result.stop_reason == "max_steps"
    assert [event.outcome for event in events] == ["created"]
    assert graph.has_state(problem.state_key(GenomeState(frozenset({"b0002"}))))
    assert len(failed) == 1
    assert failed[0].selected_state_ids == [branch_id]
    graph.close()


def _selector(
    *, public_gene_id: Callable[[str], str] = str
) -> RecoverableOpenSetSelector:
    return RecoverableOpenSetSelector(
        evaluator_ids=EVALUATOR_IDS,
        max_action_size=20,
        config=OpenSetConfig(),
        seed=3,
        candidate_count=400,
        candidate_page_size=100,
        public_gene_id=public_gene_id,
    )


def _status(run_id: str, *, step: int) -> RunStatus:
    return RunStatus(
        step=step,
        unique_states=0,
        edges=0,
        elapsed_s=0.0,
        limits=RunLimits(max_steps=20),
        run_id=run_id,
    )


def _add_state(
    graph: SQLiteStateGraph[GenomeState, DeleteGenes],
    state_id: str,
    genes: tuple[str, ...],
    *,
    growth: float = 1.0,
    resource_feasible: bool = True,
    essential: int = 0,
    broken: int = 0,
) -> str:
    graph.add_state(state_id, GenomeState(frozenset(genes)))
    evaluations = {
        "essentiality": {
            "n_essential_deleted": essential,
            "n_conditional_essential_deleted": 0,
            "n_ambiguous_deleted": 0,
            "n_unknown_deleted": 0,
        },
        "fba": {"feasible": True, "growth_rate": growth},
        "module_retention": {"n_broken": broken},
        "resource_allocation": {
            "feasible_at_growth_floor": resource_feasible,
            "growth_rate_floor_h": 0.1,
        },
    }
    for name, metrics in evaluations.items():
        graph.add_evaluation(
            state_id,
            evaluator_id=EVALUATOR_IDS[name],
            evaluator=name,
            version="fixture",
            config_hash="fixture",
            result=EvaluationResult(metrics=metrics),
        )
    return state_id


def _add_attempt(
    graph: SQLiteStateGraph[GenomeState, DeleteGenes],
    *,
    run_id: str,
    decision_id: str,
    parent_id: str,
    actions: tuple[tuple[str, str, str], ...],
    step: int = 1,
) -> None:
    graph.add_decision(
        decision_id,
        run_id=run_id,
        policy="fixture",
        role="explorer",
        model="fixture",
        selected_state_ids=[parent_id],
        input_context={},
        tool_calls=[],
        output={"actions": [gene for gene, _child, _edge in actions]},
        metadata={},
        created_step=step,
    )
    for index, (gene, child_id, edge_id) in enumerate(actions):
        event_id = f"{decision_id}-{index}"
        graph.add_proposal_event(
            event_id,
            decision_id=decision_id,
            run_id=run_id,
            parent_id=parent_id,
            action=DeleteGenes(genes=(gene,)),
            metadata={},
            created_step=step,
            proposal_index=index,
            sequence_index=index,
        )
        graph.finish_proposal_event(
            event_id,
            outcome="created",
            child_id=child_id,
            edge_id=edge_id,
        )


def _add_empty_decision(
    graph: SQLiteStateGraph[GenomeState, DeleteGenes],
    decision_id: str,
    parent_id: str,
    *,
    metadata: dict[str, object],
) -> None:
    graph.add_decision(
        decision_id,
        run_id="run_a",
        policy="fixture",
        role="explorer",
        model="fixture",
        selected_state_ids=[parent_id],
        input_context={},
        tool_calls=[],
        output={"actions": []},
        metadata=metadata,
        created_step=1,
    )


def _distance(left: frozenset[str], right: frozenset[str]) -> float:
    return 1.0 - len(left & right) / len(left | right)
