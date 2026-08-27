"""Experimental essentiality evidence for candidate deletion sets."""

from __future__ import annotations

from dataclasses import dataclass

from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState


@dataclass(frozen=True, slots=True)
class EssentialityResult:
    essential_deleted: tuple[str, ...]
    conditional_essential_deleted: tuple[str, ...]
    ambiguous_deleted: tuple[str, ...]
    unknown_deleted: tuple[str, ...]
    n_essential_deleted: int
    n_conditional_essential_deleted: int
    n_ambiguous_deleted: int
    n_unknown_deleted: int
    deleted_genes_total: int
    deleted_genes_classified: int

    def metrics(self) -> dict[str, object]:
        return {
            "essential_deleted": list(self.essential_deleted),
            "conditional_essential_deleted": list(self.conditional_essential_deleted),
            "ambiguous_deleted": list(self.ambiguous_deleted),
            "unknown_deleted": list(self.unknown_deleted),
            "n_essential_deleted": self.n_essential_deleted,
            "n_conditional_essential_deleted": self.n_conditional_essential_deleted,
            "n_ambiguous_deleted": self.n_ambiguous_deleted,
            "n_unknown_deleted": self.n_unknown_deleted,
        }

    def coverage(self) -> dict[str, object]:
        return {
            "deleted_genes_total": self.deleted_genes_total,
            "deleted_genes_classified": self.deleted_genes_classified,
            "deleted_genes_unknown": self.n_unknown_deleted,
        }


def score_essentiality(
    deleted_genes: set[str] | frozenset[str],
    registry: GeneRegistry,
    dataset: EssentialityDataset,
) -> EssentialityResult:
    """Classify deleted canonical genes without treating unknown as safe."""

    categories: dict[str, list[str]] = {
        "essential": [],
        "conditionally_essential": [],
        "ambiguous": [],
        "unknown": [],
    }
    for b_number in sorted(deleted_genes):
        registry.require(b_number)
        classification = dataset.summary(b_number).classification
        if classification != "nonessential":
            categories[classification].append(b_number)
    return EssentialityResult(
        essential_deleted=tuple(categories["essential"]),
        conditional_essential_deleted=tuple(categories["conditionally_essential"]),
        ambiguous_deleted=tuple(categories["ambiguous"]),
        unknown_deleted=tuple(categories["unknown"]),
        n_essential_deleted=len(categories["essential"]),
        n_conditional_essential_deleted=len(categories["conditionally_essential"]),
        n_ambiguous_deleted=len(categories["ambiguous"]),
        n_unknown_deleted=len(categories["unknown"]),
        deleted_genes_total=len(deleted_genes),
        deleted_genes_classified=len(deleted_genes) - len(categories["unknown"]),
    )


class EssentialityScorer:
    name = "essentiality"
    version = "1"

    def __init__(
        self,
        *,
        registry: GeneRegistry,
        dataset: EssentialityDataset,
        artifact_hash: str,
    ) -> None:
        self.registry = registry
        self.dataset = dataset
        self.artifact_hash = artifact_hash
        self.config = {"artifact_sha256": artifact_hash}

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        result = score_essentiality(state.deleted_genes, self.registry, self.dataset)
        return scientific_evaluation(
            result.metrics(),
            coverage=result.coverage(),
            provenance={"artifact_hash": self.artifact_hash},
        )
