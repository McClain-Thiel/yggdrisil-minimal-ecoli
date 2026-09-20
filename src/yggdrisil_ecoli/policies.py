"""Small application callbacks for Yggdrisil's baseline policies."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence

import pandas as pd
from yggdrisil import BestFirstPolicy
from yggdrisil.types import EvaluationRecord, StateNode

from yggdrisil_ecoli.actions import DeleteGenes
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
) -> BestFirstPolicy[GenomeState, DeleteGenes]:
    """Build the framework best-first baseline over active scientific evidence."""

    missing = {"essentiality", "fba"} - set(evaluator_ids)
    if missing:
        raise ValueError(f"missing evaluator identities: {sorted(missing)}")

    def eligible(
        node: StateNode[GenomeState], records: Sequence[EvaluationRecord]
    ) -> bool:
        by_id = {record.evaluator_id: record for record in records}
        essential = by_id.get(evaluator_ids["essentiality"])
        fba = by_id.get(evaluator_ids["fba"])
        if essential is None or fba is None:
            return False
        growth = fba.metrics.get("growth_rate")
        return (
            essential.metrics.get("n_essential_deleted") == 0
            and fba.metrics.get("feasible") is True
            and isinstance(growth, (int, float))
            and not isinstance(growth, bool)
            and growth > 0
        )

    def priority(
        node: StateNode[GenomeState], records: Sequence[EvaluationRecord]
    ) -> float:
        return float(len(node.state.deleted_genes))

    return BestFirstPolicy(
        deletion_sampler(
            genes,
            bundle_size=bundle_size,
            exclude_essential=True,
        ),
        priority,
        n_proposals=n_proposals,
        seed=seed,
        eligible=eligible,
    )
