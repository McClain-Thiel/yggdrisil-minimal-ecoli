from pathlib import Path

import pytest

from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.fba import FBAEvaluator

ROOT = Path(__file__).parents[1]
MODEL_PATH = ROOT / "data" / "external" / "iML1515.json"
REGISTRY_PATH = ROOT / "data" / "processed" / "gene_registry.parquet"

pytestmark = pytest.mark.skipif(
    not MODEL_PATH.exists() or not REGISTRY_PATH.exists(),
    reason="frozen iML1515 and generated registry are not available",
)


@pytest.fixture(scope="module")
def evaluator() -> FBAEvaluator:
    return FBAEvaluator(
        model_path=MODEL_PATH,
        registry=GeneRegistry.from_parquet(REGISTRY_PATH),
    )


def test_wild_type_grows_in_explicit_aerobic_m9_glucose(
    evaluator: FBAEvaluator,
) -> None:
    result = evaluator.score_deleted(set())

    assert result.feasible is True
    assert result.growth_rate == pytest.approx(0.87699721442698, rel=1e-6)
    assert len(result.registry_mapping_hash) == 64
    assert result.provenance()["registry_mapping_hash"] == (
        evaluator.registry_mapping_hash
    )
    assert result.provenance()["cobra_version"] == "0.32.1"
    assert result.provenance()["solver_package"] == "swiglpk"
    assert result.provenance()["solver_package_version"] == "5.0.13"


def test_non_model_gene_changes_coverage_not_solution(
    evaluator: FBAEvaluator,
) -> None:
    wild_type = evaluator.score_deleted(set())
    deleted = evaluator.score_deleted({"b3702"})

    assert deleted.growth_rate == pytest.approx(wild_type.growth_rate)
    assert deleted.deleted_genes_modeled == 0
    assert deleted.unmodeled_gene_ids == ("b3702",)


def test_or_gpr_requires_both_isozymes_to_disable_reaction(
    evaluator: FBAEvaluator,
) -> None:
    original = evaluator.base_reaction_bounds(("TALA",))
    one_deleted = evaluator.reaction_bounds_after_deletion({"b0008"}, ("TALA",))
    both_deleted = evaluator.reaction_bounds_after_deletion(
        {"b0008", "b2464"}, ("TALA",)
    )

    assert one_deleted == original
    assert both_deleted == {"TALA": (0, 0)}


def test_and_gpr_disables_reaction_and_growth(evaluator: FBAEvaluator) -> None:
    bounds = evaluator.reaction_bounds_after_deletion({"b1260"}, ("TRPS2",))
    result = evaluator.score_deleted({"b1260"})

    assert bounds == {"TRPS2": (0, 0)}
    assert result.feasible is True
    assert result.growth_rate == 0.0


def test_repeated_scoring_does_not_mutate_base_model(
    evaluator: FBAEvaluator,
) -> None:
    original_bounds = evaluator.base_reaction_bounds(("TALA", "TRPS2"))
    original_growth = evaluator.score_deleted(set()).growth_rate

    evaluator.score_deleted({"b0008", "b2464", "b1260"})

    assert evaluator.base_reaction_bounds(("TALA", "TRPS2")) == original_bounds
    assert evaluator.score_deleted(set()).growth_rate == pytest.approx(original_growth)
