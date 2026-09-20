import pandas as pd
import pytest
from pydantic import ValidationError

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.state import GenomeState


def test_state_identity_is_order_independent_and_action_is_monotonic(
    genes: pd.DataFrame,
) -> None:
    problem = EcoliProblem(genes, max_genes_per_action=2)
    left = GenomeState(frozenset({"b0001", "b0002"}))
    right = GenomeState(frozenset({"b0002", "b0001"}))

    assert problem.state_key(left) == problem.state_key(right)
    child = problem.apply(
        GenomeState(frozenset({"b0001"})), DeleteGenes(genes=("b0003", "b0002"))
    )
    assert child.deleted_genes == frozenset({"b0001", "b0002", "b0003"})


def test_action_rejects_empty_malformed_duplicate_and_redeleted_genes(
    genes: pd.DataFrame,
) -> None:
    problem = EcoliProblem(genes, max_genes_per_action=1)

    with pytest.raises(ValidationError):
        DeleteGenes(genes=())
    with pytest.raises(ValidationError):
        DeleteGenes(genes=("thrA",))
    with pytest.raises(ValidationError):
        DeleteGenes(genes=("b0001", "b0001"))
    with pytest.raises(DataValidationError, match="outside the search universe"):
        problem.apply(problem.initial_state, DeleteGenes(genes=("b9999",)))
    with pytest.raises(DataValidationError, match="already deleted"):
        problem.apply(GenomeState(frozenset({"b0001"})), DeleteGenes(genes=("b0001",)))
    with pytest.raises(DataValidationError, match="max_genes_per_action"):
        problem.apply(problem.initial_state, DeleteGenes(genes=("b0001", "b0002")))
