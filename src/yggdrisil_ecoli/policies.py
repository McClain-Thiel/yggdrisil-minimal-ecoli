"""Small application callbacks for Yggdrisil's baseline policies."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence

from yggdrisil import BestFirstPolicy
from yggdrisil.types import EvaluationRecord, StateNode

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.state import GenomeState

DeletionSampler = Callable[[GenomeState, random.Random], Sequence[DeleteGenes]]
Eligibility = Callable[[StateNode[GenomeState], Sequence[EvaluationRecord]], bool]


def deletion_sampler(
    registry: GeneRegistry,
    *,
    bundle_size: int = 1,
    essentiality: EssentialityDataset | None = None,
    exclude_known_essential: bool = False,
) -> DeletionSampler:
    """Build a direct-child sampler over the canonical search universe.

    Experimental essentiality is soft evidence by default. The explicit filter is
    retained only for ablation baselines that intentionally narrow the action space.
    """

    if bundle_size < 1:
        raise ValueError("bundle_size must be positive")
    if exclude_known_essential and essentiality is None:
        raise ValueError(
            "essentiality is required when exclude_known_essential is enabled"
        )
    universe = tuple(sorted(registry.search_universe))

    def sample(state: GenomeState, rng: random.Random) -> Sequence[DeleteGenes]:
        available = [
            gene
            for gene in universe
            if gene not in state.deleted_genes
            and not (
                exclude_known_essential
                and essentiality is not None
                and essentiality.record(gene).classification == "essential"
            )
        ]
        if not available:
            return ()
        count = min(bundle_size, len(available))
        return (DeleteGenes(genes=tuple(rng.sample(available, count))),)

    return sample


def make_heuristic_policy(
    *,
    registry: GeneRegistry,
    essentiality: EssentialityDataset | None = None,
    evaluator_ids: Mapping[str, str],
    bundle_size: int = 1,
    n_proposals: int = 1,
    seed: int = 0,
    exclude_known_essential: bool = False,
) -> BestFirstPolicy[GenomeState, DeleteGenes]:
    """Build the framework best-first baseline over active scientific evidence."""

    eligible = viability_eligibility(evaluator_ids)

    def priority(
        node: StateNode[GenomeState], records: Sequence[EvaluationRecord]
    ) -> float:
        return float(len(node.state.deleted_genes))

    return BestFirstPolicy(
        deletion_sampler(
            registry,
            bundle_size=bundle_size,
            essentiality=essentiality,
            exclude_known_essential=exclude_known_essential,
        ),
        priority,
        n_proposals=n_proposals,
        seed=seed,
        eligible=eligible,
    )


def viability_eligibility(evaluator_ids: Mapping[str, str]) -> Eligibility:
    """Require positive FBA growth and fixed-floor resource feasibility."""

    missing = {"fba", "resource_allocation"} - set(evaluator_ids)
    if missing:
        raise ValueError(f"missing evaluator identities: {sorted(missing)}")

    def eligible(
        node: StateNode[GenomeState], records: Sequence[EvaluationRecord]
    ) -> bool:
        by_id = {record.evaluator_id: record for record in records}
        fba = by_id.get(evaluator_ids["fba"])
        resource = by_id.get(evaluator_ids["resource_allocation"])
        if fba is None or resource is None:
            return False
        growth = fba.metrics.get("growth_rate")
        return (
            fba.metrics.get("feasible") is True
            and isinstance(growth, (int, float))
            and not isinstance(growth, bool)
            and growth > 0
            and resource.metrics.get("feasible_at_growth_floor") is True
        )

    return eligible
