from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq

from yggdrisil_ecoli.data.audit import audit_registry
from yggdrisil_ecoli.data.crosswalks import CrosswalkDiagnostics
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.data.registry import REGISTRY_SCHEMA, GeneRegistry

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
    assert pq.read_schema(path) == REGISTRY_SCHEMA
    assert REGISTRY_SCHEMA.names == [
        "b_number",
        "symbol",
        "name",
        "description",
        "start",
        "end",
        "strand",
        "ncbi_gene_id",
        "ecocyc_id",
        "kegg_gene_id",
        "ko_ids",
        "iml1515_gene_id",
    ]


def test_audit_reports_coverage_and_mapping_gaps() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    diagnostics = CrosswalkDiagnostics(
        unresolved_identifiers={"kegg": ["eco:b9999"]},
        notes=["fixture note"],
    )

    report = audit_registry(registry, diagnostics)

    assert report.canonical_protein_coding_genes == 3
    assert report.coverage["ncbi_gene"] == 3
    assert report.coverage["ecocyc"] == 3
    assert report.coverage["ko"] == 0
    assert report.unresolved_count == 1
    assert "Missing" not in report.render_text()


def test_audit_reports_duplicate_and_ambiguous_identifiers() -> None:
    records = list(parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry)
    ambiguous = replace(records[1], ncbi_gene_id=records[0].ncbi_gene_id)

    report = audit_registry([records[0], records[0], ambiguous])

    assert report.duplicate_b_numbers == ["b0001"]
    assert report.ambiguous_mappings == {"ncbi_gene": {"944742": ["b0001", "b0002"]}}
    assert len(report.fatal_errors) == 2
