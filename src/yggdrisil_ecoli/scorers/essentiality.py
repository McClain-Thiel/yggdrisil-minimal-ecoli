"""Experimental essentiality evidence for candidate deletion sets."""

from __future__ import annotations

from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState


class EssentialityScorer:
    name = "essentiality"
    version = "2"

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
        categories: dict[str, list[str]] = {
            "essential": [],
            "conditionally_essential": [],
            "ambiguous": [],
            "unknown": [],
        }
        for b_number in sorted(state.deleted_genes):
            self.registry.require(b_number)
            classification = self.dataset.record(b_number).classification
            if classification != "nonessential":
                categories[classification].append(b_number)

        unknown = categories["unknown"]
        metrics: dict[str, object] = {}
        for category, genes in categories.items():
            label = (
                "conditional_essential"
                if category == "conditionally_essential"
                else category
            )
            metrics[f"{label}_deleted"] = genes
            metrics[f"n_{label}_deleted"] = len(genes)
        return scientific_evaluation(
            metrics,
            coverage={
                "deleted_genes_total": len(state.deleted_genes),
                "deleted_genes_classified": len(state.deleted_genes) - len(unknown),
                "deleted_genes_unknown": len(unknown),
            },
            provenance={"artifact_hash": self.artifact_hash},
        )
