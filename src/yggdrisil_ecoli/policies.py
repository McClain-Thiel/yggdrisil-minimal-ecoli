"""Small application callbacks for Yggdrisil's baseline policies."""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Literal

from yggdrisil import BestFirstPolicy
from yggdrisil.types import EvaluationRecord, StateNode

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.state import GenomeState

DeletionSampler = Callable[[GenomeState, random.Random], Sequence[DeleteGenes]]
Eligibility = Callable[[StateNode[GenomeState], Sequence[EvaluationRecord]], bool]
ActionSizeMode = Literal["fixed-max", "uniform-1-max"]
ViabilityGate = Literal["fba-rba", "fba-only"]


def deletion_sampler(
    registry: GeneRegistry,
    *,
    bundle_size: int = 1,
    essentiality: EssentialityDataset | None = None,
    exclude_known_essential: bool = False,
    candidate_genes: Iterable[str] | None = None,
    action_size_mode: ActionSizeMode = "fixed-max",
) -> DeletionSampler:
    """Build a direct-child sampler over the canonical search universe.

    Experimental essentiality is soft evidence by default. The explicit filter is
    retained only for ablation baselines that intentionally narrow the action space.
    """

    if bundle_size < 1:
        raise ValueError("bundle_size must be positive")
    if action_size_mode not in {"fixed-max", "uniform-1-max"}:
        raise ValueError(f"unknown action size mode: {action_size_mode!r}")
    if exclude_known_essential and essentiality is None:
        raise ValueError(
            "essentiality is required when exclude_known_essential is enabled"
        )
    selected = frozenset(
        registry.search_universe if candidate_genes is None else candidate_genes
    )
    if not selected:
        raise ValueError("candidate_genes must not be empty")
    outside = sorted(selected - registry.search_universe)
    if outside:
        raise ValueError(f"candidate genes outside the canonical registry: {outside}")
    universe = tuple(sorted(selected))

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
        maximum = min(bundle_size, len(available))
        count = maximum if action_size_mode == "fixed-max" else rng.randint(1, maximum)
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
    candidate_genes: Iterable[str] | None = None,
    action_size_mode: ActionSizeMode = "fixed-max",
    viability_gate: ViabilityGate = "fba-rba",
) -> BestFirstPolicy[GenomeState, DeleteGenes]:
    """Build the framework best-first baseline over active scientific evidence."""

    eligible = viability_eligibility(evaluator_ids, gate=viability_gate)

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
            candidate_genes=candidate_genes,
            action_size_mode=action_size_mode,
        ),
        priority,
        n_proposals=n_proposals,
        seed=seed,
        eligible=eligible,
    )


def evaluations_are_viable(
    records: Sequence[EvaluationRecord],
    evaluator_ids: Mapping[str, str],
    *,
    gate: ViabilityGate = "fba-rba",
) -> bool:
    """Apply one explicit mechanistic parent-eligibility gate."""

    if gate not in {"fba-rba", "fba-only"}:
        raise ValueError(f"unknown viability gate: {gate!r}")
    required = {"fba"}
    if gate == "fba-rba":
        required.add("resource_allocation")
    missing = required - set(evaluator_ids)
    if missing:
        raise ValueError(f"missing evaluator identities: {sorted(missing)}")
    by_id = {record.evaluator_id: record for record in records}
    fba = by_id.get(evaluator_ids["fba"])
    if fba is None:
        return False
    growth = fba.metrics.get("growth_rate")
    fba_positive = (
        fba.metrics.get("feasible") is True
        and isinstance(growth, (int, float))
        and not isinstance(growth, bool)
        and growth > 0
    )
    if not fba_positive or gate == "fba-only":
        return fba_positive
    resource = by_id.get(evaluator_ids["resource_allocation"])
    return (
        resource is not None
        and resource.metrics.get("feasible_at_growth_floor") is True
    )


def viability_eligibility(
    evaluator_ids: Mapping[str, str],
    *,
    gate: ViabilityGate = "fba-rba",
) -> Eligibility:
    """Build a framework eligibility callback for the configured hard gate."""

    evaluations_are_viable((), evaluator_ids, gate=gate)

    def eligible(
        node: StateNode[GenomeState], records: Sequence[EvaluationRecord]
    ) -> bool:
        del node
        return evaluations_are_viable(records, evaluator_ids, gate=gate)

    return eligible
