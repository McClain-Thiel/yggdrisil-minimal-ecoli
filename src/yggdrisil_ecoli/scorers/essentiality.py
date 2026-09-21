"""Experimental essentiality evidence for candidate deletion sets."""

from __future__ import annotations

import pandas as pd
from yggdrisil import EvaluationResult, stable_hash

from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState


class EssentialityScorer:
    name = "essentiality"
    version = "2"

    def __init__(
        self,
        *,
        genes: pd.DataFrame,
        artifact_hash: str,
    ) -> None:
        self.classification = genes.classification.copy()
        self.artifact_hash = artifact_hash
        self.config = {
            "artifact_sha256": artifact_hash,
            "classification_sha256": stable_hash(self.classification.to_dict()),
        }

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        deleted = self.classification.loc[sorted(state.deleted_genes)]
        categories = {
            name: deleted.loc[deleted == name].index.tolist()
            for name in ("essential", "conditionally_essential", "ambiguous", "unknown")
        }

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
