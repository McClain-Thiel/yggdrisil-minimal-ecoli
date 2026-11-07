"""Exact gene-count evidence."""

from __future__ import annotations

import pandas as pd
from yggdrisil import EvaluationResult, stable_hash

from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState


class GenomeSizeScorer:
    name = "genome_size"
    version = "1"

    def __init__(self, genes: pd.DataFrame) -> None:
        self._universe = frozenset(genes.index)
        self.config = {"search_universe_sha256": stable_hash(sorted(self._universe))}

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        outside = state.deleted_genes - self._universe
        if outside:
            raise ValueError(f"state contains genes outside search universe: {outside}")
        deleted = len(state.deleted_genes)
        return scientific_evaluation(
            {
                "genes_deleted": deleted,
                "genes_remaining": len(self._universe) - deleted,
            }
        )
