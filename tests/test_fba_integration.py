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
    assert len(provenance["registry_mapping_hash"]) == 64
    assert provenance == scorer.config
    assert provenance["cobra_version"] == "0.32.1"
    assert provenance["solver_package"] == "swiglpk"
    assert provenance["solver_package_version"] == "5.0.13"


def test_configuration_participates_in_evaluator_identity(scorer: FBAScorer) -> None:
    _evaluator_id, config_hash = evaluator_identity(scorer)

    assert config_hash
    assert len(scorer.config["model_sha256"]) == 64
    assert len(scorer.config["registry_mapping_hash"]) == 64
    assert len(scorer.config["environment_config_hash"]) == 64
    assert scorer.model.solver.interface.__name__ == "optlang.glpk_interface"


@pytest.mark.parametrize("change", ["negative_biomass", "extra_reaction"])
def test_rejects_changed_biomass_objective(
    tmp_path: Path, scorer: FBAScorer, change: str
) -> None:
    from cobra.io import save_json_model

    model = scorer.model.copy()
    if change == "negative_biomass":
        from yggdrisil_ecoli.scorers.fba import IML1515_OBJECTIVE_REACTION

        model.reactions.get_by_id(
            IML1515_OBJECTIVE_REACTION
        ).objective_coefficient = -1.0
    else:
        model.reactions.get_by_id("ATPM").objective_coefficient = 1.0
    path = tmp_path / "changed-model.json"
    save_json_model(model, str(path))
    with pytest.raises(DataValidationError, match="biomass maximization"):
        FBAScorer(model_path=path, registry=scorer.registry)


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
    original = scorer.model.reactions.get_by_id("TALA").bounds
    one_deleted = scorer.model_for({"b0008"})
    both_deleted = scorer.model_for({"b0008", "b2464"})

    assert one_deleted.reactions.get_by_id("TALA").bounds == original
    assert both_deleted.reactions.get_by_id("TALA").bounds == (0, 0)


async def test_and_gpr_disables_reaction_and_growth(scorer: FBAScorer) -> None:
    model = scorer.model_for({"b1260"})
    result = await scorer.evaluate(GenomeState(frozenset({"b1260"})))

    assert model.reactions.get_by_id("TRPS2").bounds == (0, 0)
    assert result.metrics["feasible"] is True
    assert result.metrics["growth_rate"] == 0.0


async def test_repeated_scoring_does_not_mutate_base_model(
    scorer: FBAScorer,
) -> None:
    original_bounds = [r.bounds for r in scorer.model.reactions]
    original = await scorer.evaluate(GenomeState(frozenset()))

    await scorer.evaluate(GenomeState(frozenset({"b0008", "b2464", "b1260"})))

    repeated = await scorer.evaluate(GenomeState(frozenset()))
    assert [r.bounds for r in scorer.model.reactions] == original_bounds
    assert repeated.metrics["growth_rate"] == pytest.approx(
        original.metrics["growth_rate"]
    )
