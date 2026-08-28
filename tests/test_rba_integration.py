from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from yggdrisil import evaluator_identity

import yggdrisil_ecoli.rba_build as rba_build
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.rba_build import (
    RBA_ARTIFACT_MANIFEST,
    RBA_EXPECTED_LP_DIMENSIONS,
    RBA_EXPECTED_REGISTRY_MAPPING,
    RBA_EXPECTED_STRUCTURE_DIMENSIONS,
    RBA_GROWTH_FLOOR_H,
    RBA_MODEL_FILES,
    RBA_MODELS_COMMIT,
    RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H,
    build_rba_artifact,
)
from yggdrisil_ecoli.scorers.rba import RBAScorer
from yggdrisil_ecoli.state import GenomeState

ROOT = Path(__file__).parents[1]
ARTIFACT_DIR = ROOT / "data" / "external" / "rba_ecoli_k12_wt"
REGISTRY_PATH = ROOT / "data" / "processed" / "gene_registry.parquet"
RBA_DEPENDENCIES_AVAILABLE = all(
    importlib.util.find_spec(package) is not None
    for package in ("rba", "rbatools", "swiglpk")
)

pytestmark = pytest.mark.skipif(
    not RBA_DEPENDENCIES_AVAILABLE
    or not (ARTIFACT_DIR / RBA_ARTIFACT_MANIFEST).exists()
    or not REGISTRY_PATH.exists(),
    reason="pinned RBA artifact, dependencies, and registry are not available",
)


@pytest.fixture(scope="module")
def registry() -> GeneRegistry:
    return GeneRegistry.from_parquet(REGISTRY_PATH)


@pytest.fixture(scope="module")
def scorer(registry: GeneRegistry) -> RBAScorer:
    return RBAScorer.from_artifact(ARTIFACT_DIR, registry=registry)


def test_builder_reuses_sources_without_changing_semantic_identity(
    scorer: RBAScorer,
    registry: GeneRegistry,
) -> None:
    before_identity = evaluator_identity(scorer)
    before_mapping_hash = scorer.registry_mapping_sha256
    before_bundle_hash = scorer.artifact_bundle_sha256

    manifest_path = build_rba_artifact(ARTIFACT_DIR)
    regenerated = RBAScorer(artifact_dir=ARTIFACT_DIR, registry=registry)

    assert manifest_path == ARTIFACT_DIR / RBA_ARTIFACT_MANIFEST
    assert evaluator_identity(regenerated) == before_identity
    assert regenerated.registry_mapping_sha256 == before_mapping_hash
    assert regenerated.artifact_bundle_sha256 == before_bundle_hash
    for source in RBA_MODEL_FILES:
        assert file_sha256(ARTIFACT_DIR / source.path) == source.sha256


async def test_wild_type_is_feasible_at_fixed_growth_floor(
    scorer: RBAScorer,
) -> None:
    result = await scorer.evaluate(GenomeState(frozenset()))

    assert result.metrics == {
        "feasible_at_growth_floor": True,
        "growth_rate_floor_h": RBA_GROWTH_FLOOR_H,
        "repository_wild_type_max_growth_rate_h": (RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H),
    }
    assert result.metadata["details"]["solver_status"]["status"] == "optimal"
    assert result.metadata["provenance"]["rba_models_commit"] == RBA_MODELS_COMMIT
    dependency_versions = result.metadata["provenance"]["dependency_versions"]
    assert {
        name: dependency_versions[name] for name in ("RBApy", "RBAtools", "swiglpk")
    } == {
        "RBApy": "3.0.3",
        "RBAtools": "2.0.1",
        "swiglpk": "5.0.13",
    }
    assert dependency_versions["setuptools"] == "80.10.2"
    assert set(dependency_versions) >= {
        "lxml",
        "numpy",
        "pandas",
        "python-libsbml",
        "scipy",
    }
    assert result.metadata["provenance"]["model_dimensions"] == (
        RBA_EXPECTED_LP_DIMENSIONS
    )


async def test_neutral_modeled_deletion_remains_feasible(scorer: RBAScorer) -> None:
    assert scorer.variables_for_gene("b0002") == (
        "R_ASPK_duplicate_2_enzyme",
        "R_HSDy_duplicate_2_enzyme",
    )

    result = await scorer.evaluate(GenomeState(frozenset({"b0002"})))

    assert result.metrics["feasible_at_growth_floor"] is True
    assert result.metadata["coverage"]["deleted_genes_modeled"] == 1


async def test_fba_unmodeled_translation_gene_is_rba_infeasible(
    scorer: RBAScorer,
    registry: GeneRegistry,
) -> None:
    assert registry.require("b0023").iml1515_gene_id is None
    assert scorer.variables_for_gene("b0023") == ("P_TA_machinery",)

    result = await scorer.evaluate(GenomeState(frozenset({"b0023"})))

    assert result.metrics["feasible_at_growth_floor"] is False
    assert result.metadata["coverage"] == {
        "deleted_genes_total": 1,
        "deleted_genes_modeled": 1,
        "deleted_genes_unmodeled": 0,
    }


async def test_b1260_is_lethal_via_exact_enzyme_columns(scorer: RBAScorer) -> None:
    assert scorer.variables_for_gene("b1260") == (
        "R_TRPS1_enzyme",
        "R_TRPS2_enzyme",
        "R_TRPS3_enzyme",
    )

    result = await scorer.evaluate(GenomeState(frozenset({"b1260"})))

    assert result.metrics["feasible_at_growth_floor"] is False
    assert result.metadata["details"]["knocked_out_variable_ids"] == [
        "R_TRPS1_enzyme",
        "R_TRPS2_enzyme",
        "R_TRPS3_enzyme",
    ]


async def test_unmodeled_gene_has_explicit_coverage_and_no_lp_columns(
    scorer: RBAScorer,
) -> None:
    wild_type = await scorer.evaluate(GenomeState(frozenset()))
    result = await scorer.evaluate(GenomeState(frozenset({"b3702"})))

    assert scorer.variables_for_gene("b3702") == ()
    assert result.metrics == wild_type.metrics
    assert result.metadata["coverage"] == {
        "deleted_genes_total": 1,
        "deleted_genes_modeled": 0,
        "deleted_genes_unmodeled": 1,
    }
    assert result.metadata["details"]["unmodeled_gene_ids"] == ["b3702"]


async def test_infeasible_sibling_does_not_contaminate_repeated_solves(
    scorer: RBAScorer,
) -> None:
    states = [
        GenomeState(frozenset({"b0023"})),
        GenomeState(frozenset()),
        GenomeState(frozenset({"b1260"})),
        GenomeState(frozenset()),
        GenomeState(frozenset({"b1260"})),
    ]

    feasibility = [
        (await scorer.evaluate(state)).metrics["feasible_at_growth_floor"]
        for state in states
    ]

    assert feasibility == [False, True, False, True, False]


async def test_concurrent_evaluations_are_isolated_and_deterministic(
    scorer: RBAScorer,
) -> None:
    variables = list(scorer._base_lower_bounds)
    lower_before = dict(scorer._session.Problem.get_lb(variables))
    upper_before = dict(scorer._session.Problem.get_ub(variables))
    states = [
        GenomeState(frozenset()),
        GenomeState(frozenset({"b0023"})),
        GenomeState(frozenset({"b0002"})),
        GenomeState(frozenset({"b1260"})),
    ] * 3

    results = await asyncio.gather(*(scorer.evaluate(state) for state in states))

    assert [result.metrics["feasible_at_growth_floor"] for result in results] == (
        [True, False, True, False] * 3
    )
    assert dict(scorer._session.Problem.get_lb(variables)) == lower_before
    assert dict(scorer._session.Problem.get_ub(variables)) == upper_before


def test_artifact_and_mapping_participate_in_evaluator_identity(
    scorer: RBAScorer,
) -> None:
    evaluator_id, config_hash = evaluator_identity(scorer)

    assert evaluator_id
    assert config_hash
    assert len(scorer.artifact_bundle_sha256) == 64
    assert len(scorer.registry_mapping_sha256) == 64
    assert scorer.model_dimensions == RBA_EXPECTED_LP_DIMENSIONS
    assert (
        len([gene for gene in scorer._variables_by_gene.values() if gene])
        == (RBA_EXPECTED_REGISTRY_MAPPING["genes"])
    )


def test_artifact_records_expected_structure_dimensions() -> None:
    manifest = json.loads((ARTIFACT_DIR / RBA_ARTIFACT_MANIFEST).read_text())

    assert manifest["provenance"]["model_dimensions"] == (
        RBA_EXPECTED_STRUCTURE_DIMENSIONS
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ModelStatistics": {"Proteins Total": 1}},
    ],
)
def test_builder_rejects_malformed_or_changed_dimensions(payload: object) -> None:
    with pytest.raises(DataValidationError, match="ModelStructure|dimensions"):
        rba_build._validated_model_dimensions(payload)
