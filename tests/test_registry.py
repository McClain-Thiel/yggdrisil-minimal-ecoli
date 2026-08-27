from dataclasses import replace
from pathlib import Path

from yggdrisil_ecoli.data.audit import audit_registry
from yggdrisil_ecoli.data.crosswalks import CrosswalkDiagnostics
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.data.registry import GeneRegistry

FIXTURES = Path(__file__).parent / "fixtures"


def test_parquet_round_trip_preserves_schema_and_list_values(tmp_path: Path) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    records = [
        replace(record, ko_ids=("K00001", "K00002"))
        if record.b_number == "b0001"
        else record
        for record in registry
    ]
    expected = GeneRegistry(records)
    path = tmp_path / "gene_registry.parquet"

    expected.to_parquet(path)
    actual = GeneRegistry.from_parquet(path)

    assert list(actual) == list(expected)


def test_audit_reports_coverage_and_mapping_gaps() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    diagnostics = CrosswalkDiagnostics(
        unresolved_identifiers={"kegg": ["eco:b9999"]},
        notes=["fixture note"],
    )

    report = audit_registry(registry, diagnostics)

    assert report.canonical_protein_coding_genes == 3
    assert report.coverage["ncbi_gene"].mapped == 3
    assert report.coverage["ecocyc"].mapped == 3
    assert report.coverage["ko"].mapped == 0
    assert report.unresolved_count == 1
    assert "Missing" not in report.render_text()
