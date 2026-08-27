import json
from dataclasses import replace
from pathlib import Path

import pytest

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.data.kegg_modules import (
    KeggModuleEntry,
    ModuleExpressionError,
    evaluate_module_expression,
    parse_kegg_module_flat_file,
    parse_module_expression,
    referenced_kos,
)
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.module_build import _validated_ko_links_source
from yggdrisil_ecoli.scorers.modules import ModuleCatalog, ModuleRetentionScorer

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("raw", "present", "complete", "required", "options"),
    [
        ("K00001+K00002", {"K00001"}, False, ("K00002",), (("K00002",),)),
        ("K00001 K00002", {"K00001", "K00002"}, True, (), ()),
        (
            "K00001,K00002",
            set(),
            False,
            (),
            (("K00001",), ("K00002",)),
        ),
        (
            "K00001,K00002 K00003,K00004",
            {"K00001", "K00003"},
            True,
            (),
            (),
        ),
        ("K00001-K00002", {"K00001"}, True, (), ()),
        ("K00001-K00002", {"K00002"}, False, ("K00001",), (("K00001",),)),
        ("(K00001 K00002),K00003", {"K00003"}, True, (), ()),
        (
            "K00001+(K00002,K00003+K00004)",
            {"K00001", "K00003"},
            False,
            (),
            (("K00002",), ("K00004",)),
        ),
    ],
)
def test_module_completeness_semantics(
    raw: str,
    present: set[str],
    complete: bool,
    required: tuple[str, ...],
    options: tuple[tuple[str, ...], ...],
) -> None:
    result = evaluate_module_expression(parse_module_expression(raw), present)

    assert result.complete is complete
    assert result.missing_required_kos == required
    assert result.minimal_missing_ko_sets == options


def test_module_references_are_resolved_from_the_same_frozen_snapshot() -> None:
    definitions = {"M00001": parse_module_expression("K00001,K00002")}

    result = evaluate_module_expression(
        parse_module_expression("M00001+K00003"),
        {"K00002"},
        module_definitions=definitions,
    )

    assert result.minimal_missing_ko_sets == (("K00003",),)


def test_referenced_kos_includes_optional_components() -> None:
    expression = parse_module_expression("K00001+(K00002,K00003)-K00004")

    assert referenced_kos(expression) == frozenset(
        {"K00001", "K00002", "K00003", "K00004"}
    )


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
            parse_module_expression("M00001"),
            set(),
            module_definitions=definitions,
        )


def test_kegg_flat_file_continuations_are_parsed_without_changing_grammar() -> None:
    entries = parse_kegg_module_flat_file(FIXTURES / "kegg_modules_excerpt.txt")

    assert entries["M00001"].definition == "K00001 (K00002,K00003)"
    assert entries["M00001"].name == "Synthetic pathway"
    assert entries["M00002"].module_class == ("Pathway modules; Synthetic metabolism")


def test_module_retention_uses_remaining_ko_presence_and_reports_coverage() -> None:
    base = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    records = [
        replace(record, ko_ids=("K00001",))
        if record.b_number in {"b0001", "b0002"}
        else record
        for record in base
    ]
    registry = GeneRegistry(records)
    catalog = ModuleCatalog(
        entries={
            "M00001": KeggModuleEntry(
                module_id="M00001",
                name="Duplicate-gene KO fixture",
                definition="K00001",
                module_class="Pathway modules; Synthetic",
            )
        },
        wt_complete_module_ids=("M00001",),
        parser_semantics_version="test",
    )

    one_isozyme_deleted = catalog.score_deleted({"b0001", "b0003"}, registry)
    both_isozymes_deleted = catalog.score_deleted({"b0001", "b0002"}, registry)

    assert one_isozyme_deleted.n_complete == 1
    assert one_isozyme_deleted.deleted_genes_without_ko == ("b0003",)
    assert both_isozymes_deleted.n_broken == 1
    assert both_isozymes_deleted.broken_modules[0].missing_required_kos == ("K00001",)


def test_non_search_universe_kos_are_fixed_background() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    catalog = ModuleCatalog(
        entries={
            "M00001": KeggModuleEntry(
                module_id="M00001",
                name="Fixed ncRNA fixture",
                definition="K18513",
                module_class="Signature modules; Synthetic",
            )
        },
        wt_complete_module_ids=("M00001",),
        parser_semantics_version="test",
        background_kos=("K18513",),
    )

    result = catalog.score_deleted(registry.search_universe, registry)

    assert result.n_complete == 1


def test_module_scorer_rejects_registry_crosswalk_snapshot_mismatch() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    catalog = ModuleCatalog(
        entries={
            "M00001": KeggModuleEntry(
                module_id="M00001",
                name="Mismatch fixture",
                definition="K00001",
                module_class=None,
            )
        },
        wt_complete_module_ids=("M00001",),
        parser_semantics_version="test",
        reference_registry_ko_mapping_hash="0" * 64,
    )

    with pytest.raises(DataValidationError, match="different snapshots"):
        ModuleRetentionScorer(
            registry=registry,
            catalog=catalog,
            artifact_hash="catalog-hash",
        )


def test_background_ko_input_must_match_registry_source_manifest(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "gene_registry.parquet"
    ko_links_path = tmp_path / "kegg_eco_ko_links.tsv"
    registry_path.write_bytes(b"registry fixture")
    ko_links_path.write_text("eco:b0001\tko:K00001\n", encoding="utf-8")
    source_manifest = {
        "sources": [
            {
                "name": "kegg_eco_ko_links",
                "sha256": file_sha256(ko_links_path),
                "url": "https://rest.kegg.jp/link/ko/eco",
            }
        ],
        "outputs": {"gene_registry": {"sha256": file_sha256(registry_path)}},
    }
    registry_path.with_name("source_manifest.json").write_text(
        json.dumps(source_manifest), encoding="utf-8"
    )

    provenance = _validated_ko_links_source(registry_path, ko_links_path)

    assert provenance["sha256"] == file_sha256(ko_links_path)
    assert provenance["source"] == source_manifest["sources"][0]

    ko_links_path.write_text("eco:b0002\tko:K00002\n", encoding="utf-8")
    with pytest.raises(DataValidationError, match="snapshot used to build"):
        _validated_ko_links_source(registry_path, ko_links_path)
