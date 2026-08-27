from pathlib import Path

import pyarrow.parquet as pq
import pytest
from openpyxl import Workbook

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.essentiality import (
    EssentialityDataset,
    EssentialityRecord,
    parse_choe_workbook,
)
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
from yggdrisil_ecoli.state import GenomeState

FIXTURES = Path(__file__).parent / "fixtures"


def test_dataset_rejects_duplicate_canonical_genes() -> None:
    record = _unknown("b0001")

    with pytest.raises(DataValidationError, match="duplicate.*b0001"):
        EssentialityDataset([record, record])


def test_parser_preserves_calls_conflicts_and_coordinate_audit(tmp_path: Path) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    workbook_path = tmp_path / "choe.xlsx"
    _write_choe_fixture(workbook_path, mismatch_b0002=True)

    dataset, report = parse_choe_workbook(
        workbook_path, registry, expected_source_counts=None
    )

    assert dataset.record("b0001").classification == "essential"
    conditional = dataset.record("b0002")
    assert conditional.classification == "conditionally_essential"
    assert (conditional.lb_call_raw, conditional.m9_call_raw) == ("NE", "E")
    assert (conditional.lb_ecipkm, conditional.m9_ecipkm) == (10.0, 1.0)
    assert conditional.condition_disagreement is True
    assert conditional.evidence_conflict is False
    ambiguous = dataset.record("b0003")
    assert ambiguous.classification == "ambiguous"
    assert ambiguous.evidence_conflict is True
    assert report.unmapped_source_ids == ("b9999",)
    assert report.coordinate_mismatches == ("b0002",)


@pytest.mark.asyncio
async def test_one_table_round_trip_and_scorer_keep_unknown_separate(
    tmp_path: Path,
) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    workbook_path = tmp_path / "choe.xlsx"
    _write_choe_fixture(workbook_path, omit_b0003=True)
    dataset, report = parse_choe_workbook(
        workbook_path,
        registry,
        expected_source_counts=None,
        metadata={"provenance": {"workbook_sha256": "fixture"}},
    )
    artifact = tmp_path / "essentiality.parquet"
    dataset.to_parquet(artifact)
    loaded = EssentialityDataset.from_parquet(artifact)

    assert pq.read_table(artifact).num_rows == len(registry)
    assert report.canonical_genes_without_measurement == ("b0003",)
    assert loaded.record("b0003") == _unknown("b0003")
    detail = loaded.detail("b0002")
    assert detail["classification"] == "conditionally_essential"
    assert (detail["lb_call_raw"], detail["lb_ecipkm"]) == ("NE", 10.0)
    assert (detail["m9_call_raw"], detail["m9_ecipkm"]) == ("E", 1.0)
    assert detail["source"]["study_id"] == "choe2023_tnseq"
    assert detail["source"]["provenance"] == {"workbook_sha256": "fixture"}

    scorer = EssentialityScorer(
        registry=registry, dataset=loaded, artifact_hash="artifact"
    )
    result = await scorer.evaluate(GenomeState(frozenset({"b0001", "b0002", "b0003"})))

    assert result.metrics == {
        "n_essential_deleted": 1,
        "n_conditional_essential_deleted": 1,
        "n_ambiguous_deleted": 0,
        "n_unknown_deleted": 1,
    }
    assert result.metadata["details"] == {
        "essential_deleted": ["b0001"],
        "conditional_essential_deleted": ["b0002"],
        "ambiguous_deleted": [],
        "unknown_deleted": ["b0003"],
    }
    assert result.metadata["coverage"] == {
        "deleted_genes_total": 3,
        "deleted_genes_classified": 2,
        "deleted_genes_unknown": 1,
    }


def test_record_rejects_author_call_threshold_disagreement() -> None:
    with pytest.raises(DataValidationError, match="disagrees with ecIPKM"):
        EssentialityRecord(
            b_number="b0001",
            classification="essential",
            coverage="measured",
            lb_call_raw="E",
            lb_ecipkm=10.0,
            m9_call_raw="E",
            m9_ecipkm=1.0,
        )


def _unknown(b_number: str) -> EssentialityRecord:
    return EssentialityRecord(
        b_number=b_number,
        classification="unknown",
        coverage="unknown",
        lb_call_raw=None,
        lb_ecipkm=None,
        m9_call_raw=None,
        m9_ecipkm=None,
    )


def _write_choe_fixture(
    path: Path, *, omit_b0003: bool = False, mismatch_b0002: bool = False
) -> None:
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
    worksheet.append(
        _source_row(
            "thrA",
            "b0002",
            338 if mismatch_b0002 else 337,
            2799,
            "NE",
            "E",
        )
    )
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
        1.0 if lb_call == "E" else 10.0,
        lb_call,
        1,
        1.0,
        1,
        1.0 if m9_call == "E" else 10.0,
        m9_call,
    ]
