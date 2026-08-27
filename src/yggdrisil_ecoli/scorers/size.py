"""Exact gene-count evidence."""

from __future__ import annotations

import hashlib
import json

from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState


class GenomeSizeScorer:
    name = "genome_size"
    version = "1"

    def __init__(self, registry: GeneRegistry) -> None:
        self._universe = registry.search_universe
        payload = json.dumps(sorted(self._universe), separators=(",", ":")).encode()
        self.config = {"search_universe_sha256": hashlib.sha256(payload).hexdigest()}

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
