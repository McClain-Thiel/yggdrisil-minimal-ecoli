from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import Workbook

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.essentiality import (
    EssentialityDataset,
    parse_choe_workbook,
    write_essentiality_tables,
)
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.data.kegg_modules import KeggModuleEntry
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.essentiality import score_essentiality
from yggdrisil_ecoli.scorers.modules import ModuleCatalog
from yggdrisil_ecoli.tools.genes import GeneTools

FIXTURES = Path(__file__).parent / "fixtures"


def test_choe_parser_preserves_conditions_and_derives_target_summary(
    tmp_path: Path,
) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    workbook_path = tmp_path / "choe.xlsx"
    _write_choe_fixture(workbook_path)

    parsed = parse_choe_workbook(workbook_path, registry, expected_source_counts=None)
    summaries = {summary.b_number: summary for summary in parsed.summaries}

    assert summaries["b0001"].classification == "essential"
    assert summaries["b0002"].classification == "conditionally_essential"
    assert summaries["b0003"].classification == "ambiguous"
    assert summaries["b0003"].evidence_conflict is True
    assert len(parsed.observations) == 6
    assert parsed.report.unmapped_source_ids == ("b9999",)


def test_essentiality_parquet_and_scorer_keep_unknown_separate(tmp_path: Path) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    workbook_path = tmp_path / "choe.xlsx"
    _write_choe_fixture(workbook_path, omit_b0003=True)
    parsed = parse_choe_workbook(workbook_path, registry, expected_source_counts=None)
    observation_path = tmp_path / "observations.parquet"
    summary_path = tmp_path / "summary.parquet"
    write_essentiality_tables(parsed, observation_path, summary_path)
    dataset = EssentialityDataset.from_parquet(summary_path, observation_path)

    result = score_essentiality({"b0001", "b0002", "b0003"}, registry, dataset)

    assert result.essential_deleted == ("b0001",)
    assert result.conditional_essential_deleted == ("b0002",)
    assert result.unknown_deleted == ("b0003",)
    assert result.coverage() == {
        "deleted_genes_total": 3,
        "deleted_genes_classified": 2,
        "deleted_genes_unknown": 1,
    }


def test_gene_tools_expose_evidence_without_symbol_translation(tmp_path: Path) -> None:
    base = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    registry = GeneRegistry(
        replace(
            record,
            ko_ids=("K00001",) if record.b_number in {"b0001", "b0002"} else (),
            iml1515_gene_id="b0002" if record.b_number == "b0002" else None,
            in_iml1515=record.b_number == "b0002",
        )
        for record in base
    )
    workbook_path = tmp_path / "choe.xlsx"
    _write_choe_fixture(workbook_path)
    parsed = parse_choe_workbook(workbook_path, registry, expected_source_counts=None)
    observation_path = tmp_path / "observations.parquet"
    summary_path = tmp_path / "summary.parquet"
    write_essentiality_tables(parsed, observation_path, summary_path)
    dataset = EssentialityDataset.from_parquet(summary_path, observation_path)
    modules = ModuleCatalog(
        entries={
            "M00001": KeggModuleEntry(
                module_id="M00001",
                name="Shared KO fixture",
                definition="K00001",
                module_class="Pathway modules; Synthetic",
            )
        },
        wt_complete_module_ids=("M00001",),
        parser_semantics_version="test",
    )
    tools = GeneTools(registry=registry, essentiality=dataset, modules=modules)

    info = tools.get_gene_info("b0002")
    analysis = tools.analyze_gene_set(["b0001", "b0002", "b0002", "thrA"])
    intact_module = tools.get_module_info("M00001")
    broken_module = tools.get_module_info("M00001", deleted_genes=["b0001", "b0002"])

    assert info["essentiality_classification"] == "conditionally_essential"
    assert info["kegg_modules"] == ("M00001",)
    assert info["published_wcm_membership"] is None
    assert analysis["invalid_ids"] == ["thrA"]
    assert analysis["duplicate_ids"] == ["b0002"]
    assert analysis["shared_kegg_modules"] == {"M00001": ["b0001", "b0002"]}
    assert analysis["iml1515"] == {
        "modeled": ["b0002"],
        "unmodeled": ["b0001"],
    }
    assert len(analysis["modules_broken_if_deleted"]) == 1
    assert intact_module["complete_after_deletion"] is True
    assert intact_module["remaining_gene_support_by_ko"] == {
        "K00001": ["b0001", "b0002"]
    }
    assert broken_module["complete_after_deletion"] is False
    assert broken_module["missing_required_kos"] == ["K00001"]
    with pytest.raises(DataValidationError, match="canonical b-number"):
        tools.get_gene_info("thrA")
    with pytest.raises(KeyError, match="frozen catalog"):
        tools.get_module_info("M99999")


def _write_choe_fixture(path: Path, *, omit_b0003: bool = False) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Table S1"
    worksheet.append(
        [
            "Gene",
            "Start",
            "End",
            "Length (nt)",
            "Strand",
            "Locus Tag",
            "CDS",
            "Pseudo",
            "PEC",
            "Gerdes",
            "LB medium",
            None,
            None,
            None,
            None,
            "M9 glucose (0.2%) medium",
            None,
            None,
            None,
            None,
        ]
    )
    worksheet.append(
        [
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "Insertion",
            "IPKM",
            "ec Insertion",
            "ecIPKM",
            "Essentiality",
            "Insertion",
            "IPKM",
            "ec Insertion",
            "ecIPKM",
            "Essentiality",
        ]
    )
    worksheet.append(_source_row("thrL", "b0001", 190, 255, "E", "E"))
    worksheet.append(_source_row("thrA", "b0002", 337, 2799, "NE", "E"))
    if not omit_b0003:
        worksheet.append(_source_row("thrB", "b0003", 2801, 3733, "E", "NE"))
    worksheet.append(_source_row("outside", "b9999", 1, 3, "NE", "NE"))
    workbook.save(path)


def _source_row(
    symbol: str,
    b_number: str,
    start: int,
    end: int,
    lb_call: str,
    m9_call: str,
) -> list[object]:
    lb_ecipkm = 1.0 if lb_call == "E" else 10.0
    m9_ecipkm = 1.0 if m9_call == "E" else 10.0
    return [
        symbol,
        start,
        end,
        end - start + 1,
        "+",
        b_number,
        "Y",
        "N",
        "NE",
        "NE",
        1,
        1.0,
        1,
        lb_ecipkm,
        lb_call,
        1,
        1.0,
        1,
        m9_ecipkm,
        m9_call,
    ]
