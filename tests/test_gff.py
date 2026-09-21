import gzip
from pathlib import Path

import pytest

from yggdrisil_ecoli.constants import ASSEMBLY_ACCESSION, REFERENCE_ACCESSION
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.gff import parse_ncbi_gff

FIXTURES = Path(__file__).parent / "fixtures"


def test_ncbi_gff_defines_only_canonical_protein_coding_genes() -> None:
    genes, metadata = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3")

    assert metadata["assembly_accession"] == ASSEMBLY_ACCESSION
    assert metadata["reference_accession"] == REFERENCE_ACCESSION
    assert frozenset(genes.index) == frozenset({"b0001", "b0002", "b0003"})

    thr_a = genes.loc["b0002"]
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
    registry, _ = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3")

    with pytest.raises(KeyError):
        registry.loc["thrA"]


@pytest.mark.parametrize("compressed", [False, True])
def test_library_parser_handles_escaping_and_embedded_fasta(
    tmp_path: Path, compressed: bool
) -> None:
    source = (
        (FIXTURES / "mg1655_excerpt.gff3")
        .read_text()
        .replace(
            "product=thr operon leader peptide", "product=leader%3B peptide%2C 100%25"
        )
    )
    source += "##FASTA\n>NC_000913.3\nACGT\n"
    path = tmp_path / "download"  # Detect compressed downloads without a .gz suffix.
    path.write_bytes(gzip.compress(source.encode()) if compressed else source.encode())

    registry, _ = parse_ncbi_gff(path)

    assert len(registry) == 3
    assert registry.loc["b0001"].description == "leader; peptide, 100%"


def test_duplicate_genes_are_rejected_after_library_parsing(tmp_path: Path) -> None:
    source = (FIXTURES / "mg1655_excerpt.gff3").read_text()
    gene = next(line for line in source.splitlines() if "ID=gene-b0001;" in line)
    path = tmp_path / "duplicate.gff3"
    path.write_text(source + gene + "\n")

    with pytest.raises(DataValidationError, match="duplicate canonical IDs"):
        parse_ncbi_gff(path)
