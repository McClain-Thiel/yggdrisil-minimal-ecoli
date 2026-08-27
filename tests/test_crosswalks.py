from pathlib import Path

import pytest

from yggdrisil_ecoli.data.crosswalks import (
    CrosswalkDiagnostics,
    add_iml1515_membership,
    add_kegg_crosswalk,
)
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.gff import parse_ncbi_gff

FIXTURES = Path(__file__).parent / "fixtures"


def test_kegg_crosswalk_preserves_many_kos_and_reports_unknown_genes() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    diagnostics = CrosswalkDiagnostics()

    mapped = add_kegg_crosswalk(
        registry,
        FIXTURES / "kegg_eco_genes.tsv",
        FIXTURES / "kegg_eco_ko_links.tsv",
        diagnostics,
    )

    assert mapped.require("b0001").kegg_gene_id == "eco:b0001"
    assert mapped.require("b0002").ko_ids == ("K12524", "K99999")
    assert mapped.require("b0003").kegg_gene_id is None
    assert diagnostics.unresolved_identifiers == {"kegg": ["eco:b9999"]}


def test_iml1515_membership_does_not_expand_the_search_universe() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    diagnostics = CrosswalkDiagnostics()

    mapped = add_iml1515_membership(
        registry,
        FIXTURES / "iml1515_excerpt.json",
        diagnostics,
    )

    assert mapped.search_universe == registry.search_universe
    assert mapped.require("b0002").in_iml1515 is True
    assert mapped.require("b0001").in_iml1515 is False
    assert mapped.require("b0002").iml1515_gene_id == "b0002"
    assert diagnostics.unresolved_identifiers == {"iml1515": ["b9999"]}
    assert diagnostics.notes == [
        "Excluded iML1515 non-biological placeholder gene s0001."
    ]


def test_malformed_kegg_mapping_is_rejected(tmp_path: Path) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    malformed = tmp_path / "malformed.tsv"
    malformed.write_text("eco:b0001\tko:not-a-ko\n")

    with pytest.raises(DataValidationError, match="malformed KEGG KO link"):
        add_kegg_crosswalk(
            registry,
            FIXTURES / "kegg_eco_genes.tsv",
            malformed,
            CrosswalkDiagnostics(),
        )
