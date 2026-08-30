"""MG1655 deletion problem semantics for the thin framework boundary."""

from __future__ import annotations

from collections.abc import Iterable

from yggdrisil import stable_hash

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.state import GenomeState, genome_state_key


class EcoliProblem:
    """Validate and apply direct-child deletion bundles."""

    def __init__(
        self,
        registry: GeneRegistry,
        *,
        max_genes_per_action: int | None = None,
        candidate_genes: Iterable[str] | None = None,
    ) -> None:
        if max_genes_per_action is not None and max_genes_per_action < 1:
            raise ValueError("max_genes_per_action must be positive")
        self.registry = registry
        self.max_genes_per_action = max_genes_per_action
        self.candidate_genes = frozenset(
            registry.search_universe if candidate_genes is None else candidate_genes
        )
        if not self.candidate_genes:
            raise ValueError("candidate_genes must not be empty")
        outside = sorted(self.candidate_genes - registry.search_universe)
        if outside:
            raise DataValidationError(
                f"candidate genes outside the canonical registry: {outside}"
            )
        self.initial_state = GenomeState(deleted_genes=frozenset())

    def state_key(self, state: GenomeState) -> str:
        self.validate_state(state)
        return genome_state_key(state)

    def validate_state(self, state: GenomeState) -> None:
        outside = sorted(state.deleted_genes - self.candidate_genes)
        if outside:
            raise DataValidationError(
                f"state contains genes outside the candidate universe: {outside}"
            )

    def validate_action(self, state: GenomeState, action: DeleteGenes) -> None:
        self.validate_state(state)
        requested = frozenset(action.genes)
        outside = sorted(requested - self.candidate_genes)
        if outside:
            raise DataValidationError(
                f"action contains genes outside the candidate universe: {outside}"
            )
        already_deleted = sorted(requested.intersection(state.deleted_genes))
        if already_deleted:
            raise DataValidationError(
                f"action requests genes already deleted: {already_deleted}"
            )
        if (
            self.max_genes_per_action is not None
            and len(action.genes) > self.max_genes_per_action
        ):
            raise DataValidationError(
                f"action exceeds max_genes_per_action={self.max_genes_per_action}"
            )

    def apply(self, state: GenomeState, action: DeleteGenes) -> GenomeState:
        self.validate_action(state, action)
        return GenomeState(deleted_genes=state.deleted_genes.union(action.genes))

    def problem_fingerprint(self) -> dict[str, object]:
        """Identify the deletion universe and action constraint for safe resume."""

        return {
            "search_universe": stable_hash(sorted(self.candidate_genes)),
            "max_genes_per_action": self.max_genes_per_action,
        }
