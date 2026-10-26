from dataclasses import dataclass

import pandas as pd
import pytest
from yggdrisil import EvaluationResult

from yggdrisil_ecoli.scorers.base import (
    active_evaluator_ids,
    scientific_evaluation,
)
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.state import GenomeState


@dataclass
class _Evaluator:
    name: str = "duplicate"
    version: str = "1"
    config: str = "fixture"

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        return scientific_evaluation({"deleted": len(state.deleted_genes)})


@pytest.mark.asyncio
async def test_genome_size_evaluator_returns_only_exact_gene_counts(
    genes: pd.DataFrame,
) -> None:
    evaluator = GenomeSizeScorer(genes)

    result = await evaluator.evaluate(GenomeState(frozenset({"b0001"})))

    assert result.metrics == {"genes_deleted": 1, "genes_remaining": 2}
    assert result.metadata["coverage"] == {}


def test_active_evaluator_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        active_evaluator_ids((_Evaluator(), _Evaluator()))


@pytest.mark.parametrize("status", ["unbounded", "undefined", "time_limit"])
async def test_fba_unresolved_solver_status_raises(status, monkeypatch):
    from types import SimpleNamespace

    pytest.importorskip("cobra")
    from yggdrisil_ecoli.data.errors import DataValidationError
    from yggdrisil_ecoli.scorers.fba import FBAScorer

    scorer = object.__new__(FBAScorer)
    model = SimpleNamespace(optimize=lambda: SimpleNamespace(status=status))
    monkeypatch.setattr(scorer, "model_for", lambda deleted: model)
    with pytest.raises(DataValidationError, match=f"status={status}"):
        await scorer.evaluate(GenomeState(frozenset()))
