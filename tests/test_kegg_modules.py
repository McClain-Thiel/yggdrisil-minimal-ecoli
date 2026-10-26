import json
from importlib.metadata import version
from itertools import product
from pathlib import Path

import pandas as pd
import pytest
from yggdrisil import EvaluatorSuite, SQLiteStateGraph

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import file_sha256
from yggdrisil_ecoli.data.kegg_modules import (
    ModuleExpressionError,
    evaluate_module_expression,
    parse_kegg_module_flat_file,
    parse_module_expression,
    referenced_ids,
    registry_ko_mapping_hash,
)
from yggdrisil_ecoli.module_build import (
    _background_kos,
    _parse_wt_module_ids,
    _validated_ko_links_source,
)
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def registry() -> pd.DataFrame:
    return pd.DataFrame(
        {"ko_ids": [("K00001",), ("K00001",), ()]},
        index=pd.Index(["b0001", "b0002", "b0003"], name="b_number"),
    )


@pytest.mark.parametrize(
    ("raw", "present", "complete"),
    [
        ("K00001+K00002", {"K00001"}, False),
        ("K00001 K00002", {"K00001", "K00002"}, True),
        ("K00001,K00002", set(), False),
        ("K00001,K00002 K00003,K00004", {"K00001", "K00003"}, True),
        ("K00001-K00002", {"K00001"}, True),
        ("K00001-K00002", {"K00002"}, False),
        ("K00001-(K00002+K00003)", {"K00001"}, True),
        ("K00001-K00002+K00003", {"K00001"}, False),
        ("K00001,(K00001+K00002)", set(), False),
        ("(K00001 K00002),K00003", {"K00003"}, True),
        ("K00001+(K00002,K00003+K00004)", {"K00001", "K00003"}, False),
    ],
)
def test_module_completeness_semantics(raw, present, complete) -> None:
    assert evaluate_module_expression(parse_module_expression(raw), present) is complete


def test_module_references_use_the_same_frozen_snapshot() -> None:
    definitions = {"M00001": parse_module_expression("K00001,K00002")}
    expression = parse_module_expression("M00001+K00003")
    assert not evaluate_module_expression(
        expression, {"K00002"}, module_definitions=definitions
    )
    assert evaluate_module_expression(
        expression, {"K00002", "K00003"}, module_definitions=definitions
    )


def test_pathway_spaces_join_complete_alternative_blocks() -> None:
    expression = parse_module_expression("K00001,K00002 K00003+K00004")
    for a, b, c, d in product((False, True), repeat=4):
        present = {
            f"K{index:05}"
            for index, included in enumerate((a, b, c, d), start=1)
            if included
        }
        assert evaluate_module_expression(expression, present) == ((a or b) and c and d)


def test_unresolved_required_module_reference_is_rejected() -> None:
    with pytest.raises(ModuleExpressionError, match="unresolved module reference"):
        evaluate_module_expression(parse_module_expression("M00001"), set())


def test_referenced_ids_include_optional_components_and_module_refs() -> None:
    expression = parse_module_expression("M00001+K00001+(K00002,K00003)-K00004")
    assert referenced_ids(expression) == {
        "M00001",
        "K00001",
        "K00002",
        "K00003",
        "K00004",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "K00001,,K00002",
        "-K00001",
        "K001",
        "(K00001+K00002",
        "K00001 +K00002",
        "K00001\tK00002",
    ],
)
def test_invalid_or_ambiguous_syntax_is_rejected(raw: str) -> None:
    with pytest.raises(ModuleExpressionError):
        parse_module_expression(raw)


def test_cyclic_module_reference_is_rejected() -> None:
    definitions = {
        "M00001": parse_module_expression("M00002"),
        "M00002": parse_module_expression("M00001"),
    }
    with pytest.raises(ModuleExpressionError, match="cyclic"):
        evaluate_module_expression(
            parse_module_expression("M00001"), set(), module_definitions=definitions
        )


def test_large_module_evaluates_without_enumerating_repairs() -> None:
    # Forty independent pairs have over a trillion repair combinations.
    expression = parse_module_expression(
        " ".join(f"K{index:05},K{index + 1:05}" for index in range(1, 81, 2))
    )
    assert not evaluate_module_expression(expression, set())
    assert evaluate_module_expression(
        expression, {f"K{index:05}" for index in range(1, 81, 2)}
    )


def test_kegg_flat_file_continuations_preserve_definition() -> None:
    entries = parse_kegg_module_flat_file(FIXTURES / "kegg_modules_excerpt.txt")
    assert entries["M00001"]["definition"] == "K00001 (K00002,K00003)"
    assert entries["M00001"]["name"] == "Synthetic pathway"
    assert entries["M00002"]["module_class"] == "Pathway modules; Synthetic metabolism"


def test_remaining_isozymes_preserve_module(registry) -> None:
    evaluator = ModuleEvaluator(
        registry=registry,
        entries={
            "M00001": {
                "name": "Isozyme fixture",
                "definition": "K00001",
                "module_class": None,
            }
        },
        wt_complete_module_ids=("M00001",),
        parser_semantics_version="test",
    )
    assert evaluator.score_deleted({"b0001", "b0003"}) == ()
    assert evaluator.score_deleted({"b0001", "b0002"}) == ("M00001",)
    with pytest.raises(KeyError):
        evaluator.score_deleted({"thrA"})


def test_non_search_universe_kos_are_fixed_background(registry) -> None:
    evaluator = ModuleEvaluator(
        registry=registry,
        entries={
            "M00001": {
                "name": "Fixed ncRNA",
                "definition": "K18513",
                "module_class": None,
            }
        },
        wt_complete_module_ids=("M00001",),
        parser_semantics_version="test",
        background_kos=("K18513",),
    )
    assert evaluator.score_deleted(set(registry.index)) == ()


def test_module_evaluator_rejects_registry_crosswalk_snapshot_mismatch(
    registry,
) -> None:
    with pytest.raises(DataValidationError, match="different snapshots"):
        ModuleEvaluator(
            registry=registry,
            entries={
                "M00001": {
                    "name": "Mismatch",
                    "definition": "K00001",
                    "module_class": None,
                }
            },
            wt_complete_module_ids=("M00001",),
            parser_semantics_version="test",
            provenance={"reference_registry_ko_mapping_hash": "0" * 64},
        )


@pytest.mark.asyncio
async def test_historical_catalog_keeps_status_and_provenance(
    tmp_path, registry
) -> None:
    artifact = tmp_path / "kegg_modules.json"
    provenance = {
        "reference_registry_sha256": "1" * 64,
        "reference_registry_ko_mapping_hash": registry_ko_mapping_hash(registry),
        "background_ko_source_sha256": "2" * 64,
    }
    artifact.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parser_semantics_version": "test",
                **provenance,
                "background_kos": ["K00001"],
                "wt_complete_module_ids": ["M00001"],
                "definitions": {
                    "M00001": {
                        "name": "Provenance fixture",
                        "definition": "K00001",
                        "module_class": None,
                    }
                },
            }
        )
    )
    evaluator = ModuleEvaluator.from_json(artifact, registry)
    result = await evaluator.evaluate(GenomeState(frozenset()))
    assert result.metrics == {"n_complete": 1, "n_broken": 0}
    assert result.metadata == {
        "coverage": {
            "deleted_genes_total": 0,
            "deleted_genes_with_ko": 0,
            "deleted_genes_without_ko": [],
        },
        "details": {"broken_modules": []},
        "provenance": {
            **provenance,
            "artifact_sha256": file_sha256(artifact),
            "parser_semantics_version": "test",
            "lark_version": version("lark"),
            "boolean_py_version": version("boolean.py"),
        },
    }
    assert registry_ko_mapping_hash(registry.iloc[::-1]) == registry_ko_mapping_hash(
        registry
    )


@pytest.mark.asyncio
async def test_cached_module_evidence_snapshots_mutable_inputs(
    tmp_path, registry
) -> None:
    entries = {
        "M00001": {
            "name": "Isozyme fixture",
            "definition": "K00001",
            "module_class": None,
        }
    }
    evaluator = ModuleEvaluator(
        registry=registry,
        entries=entries,
        wt_complete_module_ids=("M00001",),
        parser_semantics_version="test",
    )
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "modules.sqlite")
    state = GenomeState(frozenset({"b0001", "b0003"}))
    graph.add_state("state", state)
    suite = EvaluatorSuite([evaluator])
    first = (await suite.evaluate_cached(graph, "state"))[0]

    registry.at["b0002", "ko_ids"] = ()
    entries["M00001"]["definition"] = "K99999"
    direct = await evaluator.evaluate(state)
    cached = (await suite.evaluate_cached(graph, "state"))[0]
    assert (
        first.metrics
        == direct.metrics
        == cached.metrics
        == {
            "n_complete": 1,
            "n_broken": 0,
        }
    )
    assert first.evaluation_id == cached.evaluation_id
    assert evaluator.entries["M00001"]["definition"] == "K00001"
    assert direct.metadata["coverage"] == {
        "deleted_genes_total": 2,
        "deleted_genes_with_ko": 1,
        "deleted_genes_without_ko": ["b0003"],
    }
    assert direct.metadata["details"] == {"broken_modules": []}
    graph.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "expected"),
    [("definition", (0, 2)), ("wild_type", (1, 0)), ("background", (2, 0))],
)
async def test_module_catalog_changes_get_distinct_cached_evidence(
    tmp_path, registry, change, expected
) -> None:
    entries = {
        "M00001": {"name": "First", "definition": "K00001", "module_class": None},
        "M00002": {"name": "Second", "definition": "K00002", "module_class": None},
    }
    options = {
        "registry": registry,
        "entries": entries,
        "wt_complete_module_ids": ("M00001", "M00002"),
        "parser_semantics_version": "test",
    }
    original = ModuleEvaluator(**options)
    if change == "definition":
        entries["M00001"]["definition"] = "K00002"
    elif change == "wild_type":
        options["wt_complete_module_ids"] = ("M00001",)
    else:
        options["background_kos"] = ("K00002",)
    changed = ModuleEvaluator(**options)
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](tmp_path / "catalog.sqlite")
    state = GenomeState(frozenset())
    graph.add_state("state", state)
    first = (await EvaluatorSuite([original]).evaluate_cached(graph, "state"))[0]
    second = (await EvaluatorSuite([changed]).evaluate_cached(graph, "state"))[0]
    assert first.metrics == {"n_complete": 1, "n_broken": 1}
    assert second.metrics == (await changed.evaluate(state)).metrics
    assert second.metrics == {"n_complete": expected[0], "n_broken": expected[1]}
    assert first.evaluator_id != second.evaluator_id
    assert len(graph.evaluations("state")) == 2
    graph.close()


def test_module_link_inputs_are_strict_and_keep_out_of_scope_kos(
    tmp_path, registry
) -> None:
    module_links, ko_links = tmp_path / "modules.tsv", tmp_path / "kos.tsv"
    module_links.write_text("eco:b0001\tmd:eco_M00001\neco:b0002\tmd:eco_M00002\n")
    ko_links.write_text("eco:b0001\tko:K00001\neco:b9999\tko:K00002\n")
    assert _parse_wt_module_ids(module_links) == {"M00001", "M00002"}
    assert _background_kos(ko_links, registry) == {"K00002"}
    ko_links.write_text("eco:b9999\textra\tko:K00002\n")
    with pytest.raises(DataValidationError, match="malformed KEGG gene-KO link"):
        _background_kos(ko_links, registry)


def test_background_ko_input_must_match_registry_source_manifest(tmp_path) -> None:
    genes_path, ko_links_path = (
        tmp_path / "genes.parquet",
        tmp_path / "kegg_eco_ko_links.tsv",
    )
    genes_path.write_bytes(b"registry fixture")
    ko_links_path.write_text("eco:b0001\tko:K00001\n")
    source_manifest = {
        "inputs": {
            "kegg_eco_ko_links.tsv": {
                "sha256": file_sha256(ko_links_path),
                "url": "https://rest.kegg.jp/link/ko/eco",
            }
        },
        "outputs": {"genes.parquet": file_sha256(genes_path)},
    }
    genes_path.with_name("source_manifest.json").write_text(json.dumps(source_manifest))
    assert (
        _validated_ko_links_source(genes_path, ko_links_path)
        == source_manifest["inputs"]["kegg_eco_ko_links.tsv"]
    )
    ko_links_path.write_text("eco:b0002\tko:K00002\n")
    with pytest.raises(DataValidationError, match="snapshot used to build"):
        _validated_ko_links_source(genes_path, ko_links_path)
