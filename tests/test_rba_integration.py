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
from yggdrisil_ecoli.scorers.rba import (
    RBA_FEASIBILITY_TOLERANCE,
    RBA_MATRIX_BACKEND,
    RBA_SOLVER,
    RBA_SOLVER_FALLBACKS,
    RBA_SOLVER_METHOD,
    RBA_SOLVER_PRESOLVE,
    RBAScorer,
)
from yggdrisil_ecoli.state import GenomeState

ROOT = Path(__file__).parents[1]
ARTIFACT_DIR = ROOT / "data" / "external" / "rba_ecoli_k12_wt"
REGISTRY_PATH = ROOT / "data" / "processed" / "gene_registry.parquet"
STATUS_UNKNOWN_FIXTURE = ROOT / "tests" / "fixtures" / "rba_status_unknown_seed505.json"
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


def test_only_pinned_highs_solver_is_accepted(registry: GeneRegistry) -> None:
    with pytest.raises(DataValidationError, match="only the pinned scipy-highs"):
        RBAScorer(artifact_dir=ARTIFACT_DIR, registry=registry, solver="swiglpk")


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
    assert scorer.config["solver"] == RBA_SOLVER
    assert scorer.config["solver_method"] == RBA_SOLVER_METHOD
    assert scorer.config["solver_fallbacks"] == [
        {"method": method, "presolve": presolve}
        for method, presolve in RBA_SOLVER_FALLBACKS
    ]
    assert scorer.config["matrix_backend"] == RBA_MATRIX_BACKEND
    assert scorer.config["solver_presolve"] is RBA_SOLVER_PRESOLVE
    assert scorer.config["feasibility_tolerance"] == RBA_FEASIBILITY_TOLERANCE
    assert (
        len([gene for gene in scorer._variables_by_gene.values() if gene])
        == (RBA_EXPECTED_REGISTRY_MAPPING["genes"])
    )


async def test_highs_resolves_recorded_random_baseline_stress_states(
    scorer: RBAScorer,
) -> None:
    stress_states = [
        frozenset(
            "b0019 b0109 b0331 b0480 b0524 b0596 b0639 b0752 b0874 b0885 "
            "b1124 b1190 b1474 b1516 b1623 b2061 b2095 b2235 b2253 b2373 "
            "b2670 b2683 b2810 b2890 b2957 b3117 b3528 b3736 b3744 b4015 "
            "b4088 b4105".split()
        ),
        frozenset(
            "b0064 b0661 b0885 b0978 b1326 b1385 b1415 b1488 b1854 b2039 "
            "b2341 b2379 b2889 b3449 b3453 b3617 b3648 b3709 b3779 b3789 "
            "b3833 b3849 b3883 b3903 b3988 b4474".split()
        ),
        frozenset("b0451 b1004 b1884 b2315 b2917 b3650 b3736 b4020".split()),
    ]

    results = await asyncio.gather(
        *(scorer.evaluate(GenomeState(deletions)) for deletions in stress_states)
    )

    assert [result.metrics["feasible_at_growth_floor"] for result in results] == [
        False,
        False,
        False,
    ]
    assert results[0].metadata["coverage"] == {
        "deleted_genes_total": 32,
        "deleted_genes_modeled": 26,
        "deleted_genes_unmodeled": 6,
    }


async def test_highs_ipm_fallback_resolves_recorded_status_not_set_state(
    scorer: RBAScorer,
) -> None:
    deletions = frozenset(
        "b0241 b0325 b0351 b0394 b0443 b0451 b0494 b0529 b0555 b0583 "
        "b0654 b0809 b0887 b1019 b1125 b1126 b1198 b1297 b1298 b1325 "
        "b1393 b1622 b1704 b1745 b1771 b1814 b1832 b1849 b1884 b2047 "
        "b2065 b2096 b2154 b2175 b2243 b2251 b2260 b2306 b2371 b2378 "
        "b2498 b2508 b2563 b2676 b2708 b2712 b2719 b2784 b2800 b2866 "
        "b2889 b2927 b3032 b3093 b3132 b3146 b3349 b3458 b3460 b3475 "
        "b3477 b3544 b3654 b3662 b3704 b3726 b3732 b3748 b3826 b3918 "
        "b3927 b3934 b3940 b4084 b4153 b4154 b4171 b4207 b4208 b4401".split()
    )

    result = await scorer.evaluate(GenomeState(deletions))

    assert result.metrics["feasible_at_growth_floor"] is False
    assert result.metadata["details"]["solver_status"] == {
        "status": "infeasible",
        "solution_type": "HiGHS",
        "method": "highs-ipm",
        "presolve": True,
        "attempts": [
            {"method": "highs", "presolve": True, "status_code": 4},
            {"method": "highs-ipm", "presolve": True, "status_code": 2},
        ],
    }


async def test_no_presolve_ipm_resolves_recorded_scaling_state(
    scorer: RBAScorer,
) -> None:
    fixture = json.loads(STATUS_UNKNOWN_FIXTURE.read_text())
    result = await scorer.evaluate(
        GenomeState(frozenset(map(str, fixture["deleted_genes"])))
    )

    assert fixture["state_id"] == (
        "88bb59f9cb220f16d4c7a0c00fec2d1002920ed743b01819fd008b5f6d5a5bc1"
    )
    assert result.metrics["feasible_at_growth_floor"] is True
    assert result.metadata["details"]["solver_status"] == {
        "status": "optimal",
        "solution_type": "HiGHS",
        "method": "highs-ipm",
        "presolve": False,
        "attempts": [
            {"method": "highs", "presolve": True, "status_code": 4},
            {"method": "highs-ipm", "presolve": True, "status_code": 4},
            {"method": "highs", "presolve": False, "status_code": 4},
            {"method": "highs-ipm", "presolve": False, "status_code": 0},
        ],
    }


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
