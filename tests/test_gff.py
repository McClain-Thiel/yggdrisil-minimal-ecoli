from pathlib import Path

import pytest

from yggdrisil_ecoli.constants import ASSEMBLY_ACCESSION, REFERENCE_ACCESSION
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.gff import parse_ncbi_gff

FIXTURES = Path(__file__).parent / "fixtures"


def test_ncbi_gff_defines_only_canonical_protein_coding_genes() -> None:
    parsed = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3")

    assert parsed.metadata.assembly_accession == ASSEMBLY_ACCESSION
    assert parsed.metadata.reference_accession == REFERENCE_ACCESSION
    assert parsed.registry.search_universe == frozenset({"b0001", "b0002", "b0003"})

    thr_a = parsed.registry.require("b0002")
    assert thr_a.symbol == "thrA"
    assert thr_a.description == "fused aspartate kinase/homoserine dehydrogenase 1"
    assert thr_a.ncbi_gene_id == "945803"
    assert thr_a.ecocyc_id == "EG10998"
    assert (thr_a.start, thr_a.end, thr_a.strand) == (337, 2799, "+")


def test_wrong_reference_assembly_fails_before_registry_creation(
    tmp_path: Path,
) -> None:
    source = (FIXTURES / "mg1655_excerpt.gff3").read_text()
    wrong = tmp_path / "wrong.gff3"
    wrong.write_text(source.replace("GCF_000005845.2", "GCF_000005845.3"))

    with pytest.raises(DataValidationError, match="expected assembly"):
        parse_ncbi_gff(wrong)


def test_gene_on_wrong_reference_fails_at_gff_boundary(tmp_path: Path) -> None:
    source = (FIXTURES / "mg1655_excerpt.gff3").read_text()
    wrong = tmp_path / "wrong_gene_reference.gff3"
    wrong.write_text(
        source.replace("NC_000913.3\tRefSeq\tgene", "other\tRefSeq\tgene", 1)
    )

    with pytest.raises(DataValidationError, match="expected reference"):
        parse_ncbi_gff(wrong)


def test_symbols_are_not_translated_as_identifiers() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry

    with pytest.raises(DataValidationError, match="canonical b-number"):
        registry.require("thrA")
