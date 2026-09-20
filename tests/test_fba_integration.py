from pathlib import Path

import pytest
from yggdrisil import evaluator_identity

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.fba import FBAScorer
from yggdrisil_ecoli.state import GenomeState

ROOT = Path(__file__).parents[1]
MODEL_PATH = ROOT / "data" / "external" / "iML1515.json"
REGISTRY_PATH = ROOT / "data" / "processed" / "gene_registry.parquet"

pytestmark = pytest.mark.skipif(
    not MODEL_PATH.exists() or not REGISTRY_PATH.exists(),
    reason="frozen iML1515 and generated registry are not available",
)


@pytest.fixture(scope="module")
def scorer() -> FBAScorer:
    return FBAScorer(
        model_path=MODEL_PATH,
        registry=GeneRegistry.from_parquet(REGISTRY_PATH),
    )


async def test_wild_type_grows_in_explicit_aerobic_m9_glucose(
    scorer: FBAScorer,
) -> None:
    result = await scorer.evaluate(GenomeState(frozenset()))
    provenance = result.metadata["provenance"]

    assert result.metrics["feasible"] is True
    assert result.metrics["growth_rate"] == pytest.approx(0.87699721442698, rel=1e-6)
    assert len(scorer.registry_mapping_hash) == 64
    assert provenance["registry_mapping_hash"] == scorer.registry_mapping_hash
    assert provenance["cobra_version"] == "0.32.1"
    assert provenance["solver_package"] == "swiglpk"
    assert provenance["solver_package_version"] == "5.0.13"


def test_configuration_participates_in_evaluator_identity(scorer: FBAScorer) -> None:
    _evaluator_id, config_hash = evaluator_identity(scorer)

    assert config_hash
    assert scorer.config["model_sha256"] == scorer.model_sha256
    assert scorer.config["registry_mapping_sha256"] == (scorer.registry_mapping_hash)
    assert scorer.config["environment_config_sha256"] == (
        scorer.environment_config_hash
    )


def test_only_pinned_solver_is_accepted(scorer: FBAScorer) -> None:
    with pytest.raises(DataValidationError, match="only the pinned GLPK solver"):
        FBAScorer(
            model_path=MODEL_PATH,
            registry=scorer.registry,
            solver="not-glpk",
        )


async def test_non_model_gene_changes_coverage_not_solution(
    scorer: FBAScorer,
) -> None:
    wild_type = await scorer.evaluate(GenomeState(frozenset()))
    deleted = await scorer.evaluate(GenomeState(frozenset({"b3702"})))
    coverage = deleted.metadata["coverage"]

    assert deleted.metrics["growth_rate"] == pytest.approx(
        wild_type.metrics["growth_rate"]
    )
    assert coverage["deleted_genes_modeled"] == 0
    assert coverage["unmodeled_gene_ids"] == ["b3702"]


def test_or_gpr_requires_both_isozymes_to_disable_reaction(
    scorer: FBAScorer,
) -> None:
    original = scorer.base_reaction_bounds(("TALA",))
    one_deleted = scorer.reaction_bounds_after_deletion({"b0008"}, ("TALA",))
    both_deleted = scorer.reaction_bounds_after_deletion({"b0008", "b2464"}, ("TALA",))

    assert one_deleted == original
    assert both_deleted == {"TALA": (0, 0)}


async def test_and_gpr_disables_reaction_and_growth(scorer: FBAScorer) -> None:
    bounds = scorer.reaction_bounds_after_deletion({"b1260"}, ("TRPS2",))
    result = await scorer.evaluate(GenomeState(frozenset({"b1260"})))

    assert bounds == {"TRPS2": (0, 0)}
    assert result.metrics["feasible"] is True
    assert result.metrics["growth_rate"] == 0.0


async def test_repeated_scoring_does_not_mutate_base_model(
    scorer: FBAScorer,
) -> None:
    original_bounds = scorer.base_reaction_bounds(("TALA", "TRPS2"))
    original = await scorer.evaluate(GenomeState(frozenset()))

    await scorer.evaluate(GenomeState(frozenset({"b0008", "b2464", "b1260"})))

    repeated = await scorer.evaluate(GenomeState(frozenset()))
    assert scorer.base_reaction_bounds(("TALA", "TRPS2")) == original_bounds
    assert repeated.metrics["growth_rate"] == pytest.approx(
        original.metrics["growth_rate"]
    )
