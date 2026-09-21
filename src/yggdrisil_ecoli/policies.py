"""Small application callbacks for Yggdrisil's baseline policies."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence

import pandas as pd
from yggdrisil import BestFirstPolicy
from yggdrisil.types import EvaluationRecord, StateNode

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.scorers.base import passes_growth_gates
from yggdrisil_ecoli.state import GenomeState

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
