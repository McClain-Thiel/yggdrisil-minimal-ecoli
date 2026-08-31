"""Reproducible, non-LLM genome-minimization policies."""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence

from yggdrisil import (
    Decision,
    EvaluationRecord,
    Proposal,
    ProposalEvent,
    ReadOnlyStateGraph,
    RunStatus,
    StateNode,
    stable_hash,
)

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.policies import (
    ActionSizeMode,
    ViabilityGate,
    evaluations_are_viable,
)
from yggdrisil_ecoli.state import GenomeState

_MATERIALIZED_OUTCOMES = frozenset({"created", "reused"})


class EvolutionaryPolicy:
    """Steady-state constrained evolution with mutation and union crossover.

    The graph is the population store. Reconstructing the population from viable
    states on every step makes interruption and resume deterministic without a
    second checkpoint format.
    """

    version = "1"

    def __init__(
        self,
        *,
        candidate_genes: Iterable[str],
        evaluator_ids: Mapping[str, str],
        max_action_size: int,
        n_proposals: int,
        seed: int,
        population_size: int = 32,
        action_size_mode: ActionSizeMode = "uniform-1-max",
        viability_gate: ViabilityGate = "fba-rba",
    ) -> None:
        if max_action_size < 1 or n_proposals < 1:
            raise ValueError("action and proposal sizes must be positive")
        if population_size < 2:
            raise ValueError("population_size must be at least two")
        if action_size_mode not in {"fixed-max", "uniform-1-max"}:
            raise ValueError(f"unknown action size mode: {action_size_mode!r}")
        self.candidate_genes = tuple(sorted(frozenset(candidate_genes)))
        if not self.candidate_genes:
            raise ValueError("candidate_genes must not be empty")
        self.evaluator_ids = dict(evaluator_ids)
        self.max_action_size = max_action_size
        self.n_proposals = n_proposals
        self.seed = seed
        self.population_size = population_size
        self.action_size_mode = action_size_mode
        self.viability_gate = viability_gate
        evaluations_are_viable((), self.evaluator_ids, gate=viability_gate)

    def metadata(self) -> dict[str, object]:
        return {
            "implementation": "steady-state-union-crossover",
            "version": self.version,
            "population_size": self.population_size,
            "action_size_mode": self.action_size_mode,
            "crossover_probability": 0.7,
        }

    async def step(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        status: RunStatus,
    ) -> list[Decision[DeleteGenes]]:
        population = _ranked_viable(graph, self.evaluator_ids, self.viability_gate)[
            : self.population_size
        ]
        if not population:
            return []
        rng = random.Random(f"evolutionary:{self.seed}:{status.step}")
        existing = {node.state.deleted_genes for node in graph.states()}
        reserved = set(existing)
        proposals: list[Proposal[DeleteGenes]] = []
        sources: list[str] = []

        for _ in range(self.n_proposals * 80):
            if len(proposals) == self.n_proposals:
                break
            parent = _tournament(population, rng)
            available = set(self.candidate_genes) - parent.state.deleted_genes
            if not available:
                continue
            genes: list[str] = []
            source = "mutation"
            if len(population) > 1 and rng.random() < 0.7:
                mate = _tournament(
                    [node for node in population if node.state_id != parent.state_id],
                    rng,
                )
                genes = sorted(mate.state.deleted_genes - parent.state.deleted_genes)
                source = "union_crossover"
            if not genes:
                genes = sorted(available)
                source = "mutation"
            maximum = min(self.max_action_size, len(genes))
            count = (
                maximum
                if self.action_size_mode == "fixed-max"
                else rng.randint(1, maximum)
            )
            selected = tuple(rng.sample(genes, count))
            target = parent.state.deleted_genes.union(selected)
            if target in reserved:
                continue
            reserved.add(target)
            proposals.append(
                Proposal(
                    parent_id=parent.state_id,
                    action=DeleteGenes(genes=selected),
                    metadata={"operator": source},
                )
            )
            sources.append(source)

        if not proposals:
            return []
        return [
            Decision(
                role="evolutionary",
                proposals=proposals,
                selected_state_ids=[proposal.parent_id for proposal in proposals],
                output={"operators": sources},
                metadata=self.metadata(),
            )
        ]


class MinesweeperPolicy:
    """Matched-budget segment screen, combination, and lethal-bundle bisection.

    This is a clean-room, action-capped adaptation of the published Minesweeper
    strategy. It uses the same experimental-essentiality labels exposed to the
    closed-book agent to omit only genes classified essential in both media.
    """

    version = "1"

    def __init__(
        self,
        *,
        candidate_genes: Iterable[str],
        essentiality: EssentialityDataset,
        evaluator_ids: Mapping[str, str],
        max_action_size: int,
        n_proposals: int,
        seed: int,
        viability_gate: ViabilityGate = "fba-rba",
    ) -> None:
        if max_action_size < 1 or n_proposals < 1:
            raise ValueError("action and proposal sizes must be positive")
        candidates = tuple(sorted(frozenset(candidate_genes)))
        if not candidates:
            raise ValueError("candidate_genes must not be empty")
        excluded = {
            gene
            for gene in candidates
            if essentiality.record(gene).classification == "essential"
        }
        ordered = [gene for gene in candidates if gene not in excluded]
        random.Random(f"minesweeper-order:{seed}").shuffle(ordered)
        self.candidate_genes = candidates
        self.ordered_genes = tuple(ordered)
        self.excluded_known_essential = frozenset(excluded)
        self.segments = tuple(
            tuple(ordered[index : index + max_action_size])
            for index in range(0, len(ordered), max_action_size)
        )
        self.evaluator_ids = dict(evaluator_ids)
        self.max_action_size = max_action_size
        self.n_proposals = n_proposals
        self.seed = seed
        self.viability_gate = viability_gate
        evaluations_are_viable((), self.evaluator_ids, gate=viability_gate)

    def metadata(self) -> dict[str, object]:
        return {
            "implementation": "matched-cap-segment-bisect",
            "version": self.version,
            "segment_size": self.max_action_size,
            "candidate_order_hash": stable_hash(self.ordered_genes),
            "excluded_known_essential": len(self.excluded_known_essential),
        }

    async def step(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        status: RunStatus,
    ) -> list[Decision[DeleteGenes]]:
        ranked = _ranked_viable(graph, self.evaluator_ids, self.viability_gate)
        if not ranked:
            return []
        root = min(graph.states(), key=lambda node: len(node.state.deleted_genes))
        events = graph.proposal_events(run_id=status.run_id)
        materialized = [
            event for event in events if event.outcome in _MATERIALIZED_OUTCOMES
        ]
        existing = {node.state.deleted_genes for node in graph.states()}
        attempted = {
            _event_target(graph, event)
            for event in materialized
            if graph.has_state(event.parent_id)
        }
        reserved = set(existing).union(attempted)

        screening = self._screening_proposals(root, reserved)
        if screening:
            return [self._decision("segment_screen", screening[: self.n_proposals])]

        viable_segments = self._viable_root_segments(graph, root, materialized)
        combinations = self._combination_proposals(ranked, viable_segments, reserved)
        splits = self._split_proposals(graph, materialized, reserved)
        selected = _interleave(combinations, splits, self.n_proposals)
        phase = "combine_and_bisect"
        if not selected:
            selected = self._singleton_proposals(ranked, reserved)
            phase = "singleton_cleanup"
        if not selected:
            return []
        return [self._decision(phase, selected[: self.n_proposals])]

    def _screening_proposals(
        self,
        root: StateNode[GenomeState],
        reserved: set[frozenset[str]],
    ) -> list[Proposal[DeleteGenes]]:
        proposals: list[Proposal[DeleteGenes]] = []
        for index, segment in enumerate(self.segments):
            target = root.state.deleted_genes.union(segment)
            if target in reserved:
                continue
            reserved.add(target)
            proposals.append(
                Proposal(
                    parent_id=root.state_id,
                    action=DeleteGenes(genes=segment),
                    metadata={"operator": "segment_screen", "segment": index},
                )
            )
        return proposals

    def _viable_root_segments(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        root: StateNode[GenomeState],
        events: Sequence[ProposalEvent[DeleteGenes]],
    ) -> list[tuple[str, ...]]:
        segments: set[tuple[str, ...]] = set()
        for event in events:
            if event.parent_id != root.state_id or event.child_id is None:
                continue
            child = graph.get_state(event.child_id)
            if _node_is_viable(graph, child, self.evaluator_ids, self.viability_gate):
                segments.add(event.action.genes)
        return sorted(segments, key=lambda genes: (-len(genes), genes))

    def _combination_proposals(
        self,
        ranked: Sequence[StateNode[GenomeState]],
        segments: Sequence[tuple[str, ...]],
        reserved: set[frozenset[str]],
    ) -> list[Proposal[DeleteGenes]]:
        proposals: list[Proposal[DeleteGenes]] = []
        for parent in ranked:
            for segment in segments:
                genes = tuple(
                    gene for gene in segment if gene not in parent.state.deleted_genes
                )
                if not genes:
                    continue
                target = parent.state.deleted_genes.union(genes)
                if target in reserved:
                    continue
                reserved.add(target)
                proposals.append(
                    Proposal(
                        parent_id=parent.state_id,
                        action=DeleteGenes(genes=genes),
                        metadata={"operator": "segment_combine"},
                    )
                )
        return proposals

    def _split_proposals(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        events: Sequence[ProposalEvent[DeleteGenes]],
        reserved: set[frozenset[str]],
    ) -> list[Proposal[DeleteGenes]]:
        proposals: list[Proposal[DeleteGenes]] = []
        blocked = sorted(events, key=lambda event: event.sequence_index, reverse=True)
        for event in blocked:
            if event.child_id is None or len(event.action.genes) < 2:
                continue
            parent = graph.get_state(event.parent_id)
            child = graph.get_state(event.child_id)
            if not _node_is_viable(
                graph, parent, self.evaluator_ids, self.viability_gate
            ) or _node_is_viable(graph, child, self.evaluator_ids, self.viability_gate):
                continue
            midpoint = len(event.action.genes) // 2
            for genes in (event.action.genes[:midpoint], event.action.genes[midpoint:]):
                target = parent.state.deleted_genes.union(genes)
                if target in reserved:
                    continue
                reserved.add(target)
                proposals.append(
                    Proposal(
                        parent_id=parent.state_id,
                        action=DeleteGenes(genes=genes),
                        metadata={
                            "operator": "lethal_bisection",
                            "source_event_id": event.event_id,
                        },
                    )
                )
        return proposals

    def _singleton_proposals(
        self,
        ranked: Sequence[StateNode[GenomeState]],
        reserved: set[frozenset[str]],
    ) -> list[Proposal[DeleteGenes]]:
        proposals: list[Proposal[DeleteGenes]] = []
        for parent in ranked:
            for gene in self.ordered_genes:
                if gene in parent.state.deleted_genes:
                    continue
                target = parent.state.deleted_genes.union((gene,))
                if target in reserved:
                    continue
                reserved.add(target)
                proposals.append(
                    Proposal(
                        parent_id=parent.state_id,
                        action=DeleteGenes(genes=(gene,)),
                        metadata={"operator": "singleton_cleanup"},
                    )
                )
        return proposals

    def _decision(
        self, phase: str, proposals: Sequence[Proposal[DeleteGenes]]
    ) -> Decision[DeleteGenes]:
        return Decision(
            role="minesweeper",
            proposals=list(proposals),
            selected_state_ids=[proposal.parent_id for proposal in proposals],
            output={"phase": phase},
            metadata=self.metadata(),
        )


def _ranked_viable(
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    evaluator_ids: Mapping[str, str],
    gate: ViabilityGate,
) -> list[StateNode[GenomeState]]:
    nodes = [
        node
        for node in graph.states()
        if _node_is_viable(graph, node, evaluator_ids, gate)
    ]
    return sorted(
        nodes,
        key=lambda node: (
            -len(node.state.deleted_genes),
            -_growth(graph.evaluations(node.state_id), evaluator_ids),
            node.state_id,
        ),
    )


def _node_is_viable(
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    node: StateNode[GenomeState],
    evaluator_ids: Mapping[str, str],
    gate: ViabilityGate,
) -> bool:
    return evaluations_are_viable(
        graph.evaluations(node.state_id), evaluator_ids, gate=gate
    )


def _growth(
    records: Sequence[EvaluationRecord], evaluator_ids: Mapping[str, str]
) -> float:
    fba_id = evaluator_ids.get("fba")
    for record in records:
        if record.evaluator_id == fba_id:
            value = record.metrics.get("growth_rate")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return float("-inf")


def _tournament(
    population: Sequence[StateNode[GenomeState]], rng: random.Random
) -> StateNode[GenomeState]:
    indices = rng.sample(range(len(population)), min(3, len(population)))
    return population[min(indices)]


def _event_target(
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    event: ProposalEvent[DeleteGenes],
) -> frozenset[str]:
    return graph.get_state(event.parent_id).state.deleted_genes.union(
        event.action.genes
    )


def _interleave(
    first: Sequence[Proposal[DeleteGenes]],
    second: Sequence[Proposal[DeleteGenes]],
    limit: int,
) -> list[Proposal[DeleteGenes]]:
    selected: list[Proposal[DeleteGenes]] = []
    for index in range(max(len(first), len(second))):
        for candidates in (first, second):
            if index < len(candidates):
                selected.append(candidates[index])
                if len(selected) == limit:
                    return selected
    return selected
