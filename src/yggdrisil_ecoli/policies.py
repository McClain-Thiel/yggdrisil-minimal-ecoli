"""Small deterministic baselines over the shared biological problem."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from yggdrisil import Decision, Proposal, ReadOnlyStateGraph, RunStatus
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.state import GenomeState


class RandomDeletionSampler:
    """Sample one direct-child deletion bundle for Yggdrisil RandomPolicy."""

    def __init__(self, registry: GeneRegistry, *, bundle_size: int = 1) -> None:
        if bundle_size < 1:
            raise ValueError("bundle_size must be positive")
        self._universe = tuple(sorted(registry.search_universe))
        self.bundle_size = bundle_size

    def __call__(
        self,
        state: GenomeState,
        rng: random.Random,
    ) -> Sequence[DeleteGenes]:
        available = sorted(set(self._universe) - state.deleted_genes)
        if not available:
            return ()
        count = min(self.bundle_size, len(available))
        return (DeleteGenes(genes=tuple(rng.sample(available, count))),)


class SimpleHeuristicPolicy:
    """Frozen baseline that uses only declared essentiality and FBA evidence."""

    def __init__(
        self,
        *,
        registry: GeneRegistry,
        essentiality: EssentialityDataset,
        evaluator_ids: Mapping[str, str],
        bundle_size: int = 1,
        n_proposals: int = 1,
        seed: int = 0,
    ) -> None:
        if bundle_size < 1:
            raise ValueError("bundle_size must be positive")
        if n_proposals < 0:
            raise ValueError("n_proposals must be non-negative")
        self._universe = tuple(sorted(registry.search_universe))
        self._essentiality = essentiality
        missing = {"essentiality", "fba"} - set(evaluator_ids)
        if missing:
            raise ValueError(f"missing evaluator identities: {sorted(missing)}")
        self._evaluator_ids = dict(evaluator_ids)
        self.bundle_size = bundle_size
        self.n_proposals = n_proposals
        self.seed = seed

    async def step(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        status: RunStatus,
    ) -> list[Decision[DeleteGenes]]:
        if self.n_proposals == 0:
            return []
        eligible = [
            node
            for node in graph.states()
            if _evidence_passes_heuristic(
                graph.evaluations(node.state_id),
                self._evaluator_ids,
            )
        ]
        if not eligible:
            return []
        eligible.sort(
            key=lambda node: (len(node.state.deleted_genes), node.state_id),
            reverse=True,
        )
        most_deleted = len(eligible[0].state.deleted_genes)
        parents = [
            node for node in eligible if len(node.state.deleted_genes) == most_deleted
        ]
        rng = random.Random(f"{self.seed}:{status.step}")
        proposals: list[Proposal[DeleteGenes]] = []
        attempts = max(self.n_proposals * 4, 1)
        for _ in range(attempts):
            parent = parents[rng.randrange(len(parents))]
            action = self._sample_action(parent.state, rng)
            if action is None:
                continue
            proposal = Proposal(parent_id=parent.state_id, action=action)
            if proposal not in proposals:
                proposals.append(proposal)
            if len(proposals) == self.n_proposals:
                break
        if not proposals:
            return []
        selected = list(dict.fromkeys(proposal.parent_id for proposal in proposals))
        return [
            Decision(
                role="heuristic",
                proposals=proposals,
                selected_state_ids=selected,
                input_context={
                    "rule": "avoid essential genes and FBA-infeasible parents",
                    "bundle_size": self.bundle_size,
                },
                output={"proposal_count": len(proposals)},
                metadata={"seed": self.seed},
            )
        ]

    def _sample_action(
        self,
        state: GenomeState,
        rng: random.Random,
    ) -> DeleteGenes | None:
        available = [
            gene
            for gene in self._universe
            if gene not in state.deleted_genes
            and self._essentiality.summary(gene).classification != "essential"
        ]
        if not available:
            return None
        count = min(self.bundle_size, len(available))
        return DeleteGenes(genes=tuple(rng.sample(available, count)))


def _evidence_passes_heuristic(
    records: Sequence[EvaluationRecord],
    evaluator_ids: Mapping[str, str],
) -> bool:
    by_id = {record.evaluator_id: record for record in records}
    essentiality = by_id.get(evaluator_ids["essentiality"])
    fba = by_id.get(evaluator_ids["fba"])
    if essentiality is None or fba is None:
        return False
    essential_count = essentiality.metrics.get("n_essential_deleted")
    feasible = fba.metrics.get("feasible")
    growth = fba.metrics.get("growth_rate")
    return (
        essential_count == 0
        and feasible is True
        and isinstance(growth, (int, float))
        and not isinstance(growth, bool)
        and growth > 0
    )
