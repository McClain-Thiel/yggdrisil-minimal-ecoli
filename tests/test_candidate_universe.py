import json
from pathlib import Path

import pytest

from yggdrisil_ecoli.data.candidate_universe import (
    WCM_1219_SOURCE_GENE_COUNT,
    WCM_1219_UNIVERSE_ID,
    WCM_1219_UNMAPPED_SOURCE_IDS,
    CandidateUniverse,
    gene_set_sha256,
)
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRecord, GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import (
    WCM_1219_GENE_LIST_SHA256,
    WCM_1219_SOURCE_COMMIT,
)
from yggdrisil_ecoli.wcm_universe_build import parse_wcm_ecocyc_ids


def test_parse_wcm_gene_list_requires_unique_rna_ids() -> None:
    content = (
        ",Gene,RNA,ID,Monomer,ID.1,Process,KO index\n"
        "1,thrL,EG11277_RNA,MONOMER,Metabolism,,,1\n"
        "2,thrA,EG10998_RNA,MONOMER,Metabolism,,,2\n"
    ).encode()

    assert parse_wcm_ecocyc_ids(content) == (
        ("EG11277", "thrL"),
        ("EG10998", "thrA"),
    )
    with pytest.raises(DataValidationError, match="duplicate WCM EcoCyc"):
        parse_wcm_ecocyc_ids(content + content.splitlines(keepends=True)[1])


def test_candidate_universe_validates_registry_and_content_hashes(
    tmp_path: Path,
) -> None:
    registry = GeneRegistry(
        GeneRecord(
            b_number=f"b{index:04d}",
            symbol=f"gene{index}",
            name=None,
            description=None,
            start=index,
            end=index,
            strand="+",
            ncbi_gene_id=str(index),
            ecocyc_id=f"EG{index:05d}",
        )
        for index in range(1, 1_217)
    )
    registry_path = tmp_path / "registry.parquet"
    registry.to_parquet(registry_path)
    genes = registry.search_universe
    artifact = tmp_path / "universe.json"
    payload = {
        "schema_version": 1,
        "universe_id": WCM_1219_UNIVERSE_ID,
        "gene_ids": sorted(genes),
        "gene_set_sha256": gene_set_sha256(genes),
        "registry_sha256": file_sha256(registry_path),
        "source_ids_outside_registry": sorted(WCM_1219_UNMAPPED_SOURCE_IDS),
        "counts": {
            "source_wcm_genes": WCM_1219_SOURCE_GENE_COUNT,
            "canonical_candidate_genes": 1_216,
            "source_ids_outside_registry": 3,
        },
        "source": {
            "sha256": WCM_1219_GENE_LIST_SHA256,
            "commit": WCM_1219_SOURCE_COMMIT,
        },
    }
    artifact.write_text(json.dumps(payload))

    universe = CandidateUniverse.from_json(
        artifact,
        registry=registry,
        registry_path=registry_path,
    )

    assert universe.genes == genes
    assert universe.metadata()["canonical_candidate_genes"] == 1_216
    assert universe.metadata()["artifact_sha256"] == file_sha256(artifact)

    payload["gene_set_sha256"] = "incorrect"
    artifact.write_text(json.dumps(payload))
    with pytest.raises(DataValidationError, match="gene-set hash"):
        CandidateUniverse.from_json(
            artifact,
            registry=registry,
            registry_path=registry_path,
        )
