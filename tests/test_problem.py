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


def test_reduced_start_retains_seed_deletions_and_validates_registry(genes):
    initial = GenomeState(frozenset({"b0001"}))
    problem = EcoliProblem(genes, initial_state=initial, max_genes_per_action=1)
    assert problem.initial_state == initial
    assert problem.apply(initial, DeleteGenes(genes=("b0002",))).deleted_genes == {
        "b0001",
        "b0002",
    }
    with pytest.raises(DataValidationError, match="already deleted"):
        problem.apply(initial, DeleteGenes(genes=("b0001",)))
    with pytest.raises(DataValidationError, match="outside the search universe"):
        EcoliProblem(genes, initial_state=GenomeState(frozenset({"b9999"})))


async def test_resume_rejects_different_starting_genotype(genes, tmp_path):
    from yggdrisil import RunLimits, Runner, SQLiteStateGraph
    from yggdrisil.exceptions import GraphError

    class NoWork:
        async def step(self, graph, status):
            return []

    initial = GenomeState(frozenset({"b0001"}))
    with SQLiteStateGraph(tmp_path / "seeded.sqlite") as graph:
        await Runner(
            EcoliProblem(genes, initial_state=initial),
            NoWork(),
            graph,
            RunLimits(max_steps=0),
            run_id="seeded",
        ).run()
        assert graph.get_state(EcoliProblem(genes).state_key(initial)).state == initial
        with pytest.raises(GraphError, match="problem"):
            await Runner(
                EcoliProblem(genes),
                NoWork(),
                graph,
                RunLimits(max_steps=0),
                run_id="seeded",
            ).run()
