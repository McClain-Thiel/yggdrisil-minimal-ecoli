import json
from pathlib import Path

import pytest

from yggdrisil_ecoli.data.crosswalks import add_crosswalks
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.gff import parse_ncbi_gff

FIXTURES = Path(__file__).parent / "fixtures"


def test_crosswalks_preserve_the_gene_universe_and_report_mapping_gaps() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    mapped, audit = add_crosswalks(
        registry,
        FIXTURES / "kegg_eco_genes.tsv",
        FIXTURES / "kegg_eco_ko_links.tsv",
        FIXTURES / "iml1515_excerpt.json",
    )
    assert mapped.search_universe == registry.search_universe
    assert mapped.require("b0001").kegg_gene_id == "eco:b0001"
    assert mapped.require("b0002").ko_ids == ("K12524", "K99999")
    assert mapped.require("b0003").kegg_gene_id is None
    assert mapped.require("b0002").iml1515_gene_id == "b0002"
    assert not mapped.require("b0001").in_iml1515
    assert audit == {
        "unresolved_identifiers": {"kegg": ["eco:b9999"], "iml1515": ["b9999"]},
        "excluded_model_ids": ["s0001"],
    }


def test_malformed_kegg_mapping_is_rejected(tmp_path: Path) -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    malformed = tmp_path / "malformed.tsv"
    malformed.write_text("eco:b0001\tko:not-a-ko\n")
    with pytest.raises(DataValidationError, match="malformed KEGG KO link"):
        add_crosswalks(
            registry,
            FIXTURES / "kegg_eco_genes.tsv",
            malformed,
            FIXTURES / "iml1515_excerpt.json",
        )


def test_duplicate_model_gene_is_rejected(tmp_path: Path) -> None:
    model = tmp_path / "model.json"
    model.write_text(json.dumps({"genes": [{"id": "b0001"}, {"id": "b0001"}]}))
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    with pytest.raises(DataValidationError, match="duplicate iML1515"):
        add_crosswalks(
            registry,
            FIXTURES / "kegg_eco_genes.tsv",
            FIXTURES / "kegg_eco_ko_links.tsv",
            model,
        )
