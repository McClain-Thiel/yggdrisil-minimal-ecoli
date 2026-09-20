from dataclasses import asdict, replace
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from yggdrisil_ecoli.data.audit import audit_registry
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.data.registry import GeneRegistry

FIXTURES = Path(__file__).parent / "fixtures"


def test_parquet_round_trip_preserves_fields_and_list_values(tmp_path: Path) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    records = [
        replace(record, ko_ids=("K00002", "K00001", "K00002"))
        if record.b_number == "b0001"
        else record
        for record in registry
    ]
    expected = GeneRegistry(records)
    path = tmp_path / "gene_registry.parquet"

    expected.to_parquet(path)
    actual = GeneRegistry.from_parquet(path)

    assert list(actual) == list(expected)
    assert pq.read_schema(path).names == list(asdict(records[0]))
    assert actual.require("b0001").ko_ids == ("K00001", "K00002")


def test_audit_reports_coverage_and_mapping_gaps() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    report = audit_registry(registry)

    assert report["canonical_protein_coding_genes"] == 3
    assert report["coverage"]["ncbi_gene"] == 3
    assert report["coverage"]["ecocyc"] == 3
    assert report["coverage"]["ko"] == 0


def test_registry_and_audit_reject_duplicate_identifiers() -> None:
    records = list(parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry)
    ambiguous = replace(records[1], ncbi_gene_id=records[0].ncbi_gene_id)

    with pytest.raises(DataValidationError, match="duplicate canonical ID: b0001"):
        GeneRegistry([records[0], records[0]])
    with pytest.raises(DataValidationError, match="ambiguous ncbi_gene mappings"):
        audit_registry(GeneRegistry([records[0], ambiguous]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("b_number", "thrA"),
        ("start", 0),
        ("strand", "."),
        ("ko_ids", ("bad",)),
    ],
)
def test_gene_fields_use_library_validation(field: str, value: object) -> None:
    record = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry.require("b0001")
    with pytest.raises(ValidationError):
        replace(record, **{field: value})
