from dataclasses import dataclass
from pathlib import Path

import pytest
from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.scorers.base import (
    active_evaluator_ids,
    scientific_evaluation,
)
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.state import GenomeState

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class _Evaluator:
    name: str = "duplicate"
    version: str = "1"
    config: str = "fixture"

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        return scientific_evaluation({"deleted": len(state.deleted_genes)})


@pytest.mark.asyncio
async def test_genome_size_evaluator_returns_only_exact_gene_counts() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    evaluator = GenomeSizeScorer(registry)

    result = await evaluator.evaluate(GenomeState(frozenset({"b0001"})))

    assert result.metrics == {"genes_deleted": 1, "genes_remaining": 2}
    assert result.metadata["coverage"] == {}


def test_active_evaluator_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        active_evaluator_ids((_Evaluator(), _Evaluator()))
