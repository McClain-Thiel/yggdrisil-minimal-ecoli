"""Small application callbacks for Yggdrisil's baseline policies."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable, Mapping, Sequence

import pandas as pd
from yggdrisil import BestFirstPolicy, Decision, Proposal, RunStatus
from yggdrisil.graph.base import ReadOnlyStateGraph
from yggdrisil.types import EvaluationRecord, StateNode

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.scorers.base import passes_growth_gates
from yggdrisil_ecoli.state import GenomeState, genome_state_key

DeletionSampler = Callable[[GenomeState, random.Random], Sequence[DeleteGenes]]


def deletion_sampler(
    genes: pd.DataFrame,
    *,
    bundle_size: int = 1,
    exclude_essential: bool = False,
) -> DeletionSampler:
    """Build a direct-child sampler, optionally excluding known essential genes."""

    if bundle_size < 1:
        raise ValueError("bundle_size must be positive")
    eligible = (
        genes.loc[genes.classification != "essential"] if exclude_essential else genes
    )
    universe = tuple(sorted(eligible.index))

    def sample(state: GenomeState, rng: random.Random) -> Sequence[DeleteGenes]:
        available = [gene for gene in universe if gene not in state.deleted_genes]
        if not available:
            return ()
        count = min(bundle_size, len(available))
        return (DeleteGenes(genes=tuple(rng.sample(available, count))),)

    return sample


def make_heuristic_policy(
    *,
    genes: pd.DataFrame,
    evaluator_ids: Mapping[str, str],
    bundle_size: int = 1,
    n_proposals: int = 1,
    seed: int = 0,
    exclude_essential: bool = False,
) -> BestFirstPolicy[GenomeState, DeleteGenes]:
    """Build the framework best-first baseline over active scientific evidence."""

    def priority(
        node: StateNode[GenomeState], records: Sequence[EvaluationRecord]
    ) -> float:
        return float(len(node.state.deleted_genes))

    return BestFirstPolicy(
        deletion_sampler(
            genes,
            bundle_size=bundle_size,
            exclude_essential=exclude_essential,
        ),
        priority,
        n_proposals=n_proposals,
        seed=seed,
        eligible=viability_eligibility(evaluator_ids),
    )


def viability_eligibility(
    evaluator_ids: Mapping[str, str],
) -> Callable[[StateNode[GenomeState], Sequence[EvaluationRecord]], bool]:
    """Use the same active growth gates for random and heuristic policies."""
    missing = {"fba", "resource_allocation"} - evaluator_ids.keys()
    if missing:
        raise ValueError(f"missing evaluator identities: {sorted(missing)}")

    def eligible(
        node: StateNode[GenomeState], records: Sequence[EvaluationRecord]
    ) -> bool:
        by_id = {record.evaluator_id: record for record in records}
        return passes_growth_gates(
            {name: by_id[key] for name, key in evaluator_ids.items() if key in by_id}
        )

    return eligible


class BlockDeletionPolicy:
    """Seeded first/best improvement over a shared coarse-to-fine neighborhood.

    Search state lives entirely in the graph. Reopening a run does not reset a
    random generator, repeat rejected candidates, or change the next proposal.
    Blocks are gene sets, not necessarily contiguous chromosome intervals.
    """

    def __init__(
        self,
        *,
        gene_ids: Sequence[str],
        evaluator_ids: Mapping[str, str],
        initial_state: GenomeState,
        kind: str,
        seed: int,
        block_sizes: Sequence[int] = (100, 50, 25, 10, 5, 1),
        neighborhood_size: int = 8,
    ) -> None:
        if kind not in {"randomized_block", "greedy_block"}:
            raise ValueError("Unknown block baseline")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        sizes = tuple(block_sizes)
        if (
            not sizes
            or sizes[-1] != 1
            or any(type(n) is not int or not 1 <= n <= 100 for n in sizes)
            or tuple(sorted(set(sizes), reverse=True)) != sizes
        ):
            raise ValueError("block_sizes must descend uniquely from <=100 to 1")
        if type(neighborhood_size) is not int or neighborhood_size < 1:
            raise ValueError("neighborhood_size must be positive")
        self.universe = frozenset(gene_ids)
        if len(self.universe) != len(gene_ids):
            raise ValueError("Duplicate genes in baseline universe")
        GenomeState(self.universe)
        if not initial_state.deleted_genes <= self.universe:
            raise ValueError("Starting deletions are outside the universe")
        if {"fba", "resource_allocation"} - evaluator_ids.keys():
            raise ValueError("Both growth evaluator identities are required")
        self.identities = dict(evaluator_ids)
        self.initial = initial_state
        self.kind, self.seed = kind, seed
        self.block_sizes = sizes
        self.width = 1 if kind == "randomized_block" else neighborhood_size

    def _rank(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        node: StateNode[GenomeState],
    ) -> tuple[int, float, str] | None:
        if not self.initial.deleted_genes <= node.state.deleted_genes:
            raise ValueError("Graph contains a state outside this starting genotype")
        records = {r.evaluator_id: r for r in graph.evaluations(node.state_id)}
        missing = set(self.identities.values()) - records.keys()
        if missing:
            raise ValueError(f"State lacks active evaluations: {node.state_id}")
        evidence = {name: records[key] for name, key in self.identities.items()}
        if not passes_growth_gates(evidence):
            return None
        growth = float(evidence["fba"].metrics["growth_rate"])
        if not math.isfinite(growth):
            raise ValueError("Nonfinite feasible FBA growth")
        return (-len(node.state.deleted_genes), -growth, node.state_id)

    async def step(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        status: RunStatus,
    ) -> list[Decision[DeleteGenes]]:
        nodes = graph.states()
        if not graph.has_state(genome_state_key(self.initial)):
            raise ValueError("Graph lacks the frozen starting genotype")
        root = graph.get_state(genome_state_key(self.initial))
        if self._rank(graph, root) is None:
            raise ValueError("The starting genotype fails the frozen growth gates")
        ranked = [(self._rank(graph, node), node) for node in nodes]
        feasible = sorted(
            [(rank, node) for rank, node in ranked if rank is not None],
            key=lambda item: item[0],
        )
        if not feasible:
            raise ValueError("The starting genotype fails the frozen growth gates")
        parent = feasible[0][1]
        # Once finer blocks are needed, keep that resolution on descendants;
        # restarting the largest failed scale after every success wastes budget.
        ceiling = min(
            [
                self.block_sizes[0],
                *(
                    edge.metadata["block_size"]
                    for edge in graph.edges()
                    if edge.child_id == parent.state_id
                ),
            ]
        )
        available = sorted(
            self.universe - parent.state.deleted_genes,
            key=lambda gene: hashlib.sha256(
                f"block-v1:{self.seed}:{parent.state_id}:{gene}".encode()
            ).digest(),
        )
        proposals: list[Proposal[DeleteGenes]] = []
        seen: set[str] = set()
        remaining = (
            status.limits.max_states - len(graph)
            if status.limits.max_states is not None
            else self.width
        )
        width = min(self.width, remaining)
        if width <= 0:
            return [Decision(role="baseline", output={"reason": "evaluation_budget"})]
        for size in self.block_sizes:
            if size > ceiling:
                continue
            for offset in range(0, len(available), size):
                action = DeleteGenes(genes=tuple(available[offset : offset + size]))
                child_id = genome_state_key(
                    GenomeState(parent.state.deleted_genes.union(action.genes))
                )
                if child_id in seen or graph.has_state(child_id):
                    continue
                seen.add(child_id)
                if len(proposals) < width:
                    proposals.append(
                        Proposal(
                            parent_id=parent.state_id,
                            action=action,
                            metadata={"block_size": size, "offset": offset},
                        )
                    )
                if len(proposals) >= width:
                    break
            if len(proposals) >= width:
                break
        if not proposals:
            return [
                Decision(
                    role="baseline",
                    output={"reason": "neighborhood_exhausted"},
                    metadata={"kind": self.kind, "seed": self.seed},
                )
            ]
        return [
            Decision(
                role="baseline",
                proposals=proposals,
                selected_state_ids=[parent.state_id],
                metadata={
                    "kind": self.kind,
                    "seed": self.seed,
                    "block_sizes": list(self.block_sizes),
                    "width": self.width,
                    "selection": "deletions_desc_growth_desc_state_id_asc",
                },
            )
        ]
