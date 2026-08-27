import json
import shutil
from dataclasses import replace
from pathlib import Path

from pytest import MonkeyPatch

from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import KEGG_GENE_LIST, KEGG_KO_LINKS, NCBI_GFF
from yggdrisil_ecoli.data_build import build_registry

FIXTURES = Path(__file__).parent / "fixtures"


def test_offline_cached_build_writes_registry_audit_and_manifest(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "mg1655_excerpt.gff3", raw_dir / NCBI_GFF.filename)
    shutil.copyfile(FIXTURES / "kegg_eco_genes.tsv", raw_dir / KEGG_GENE_LIST.filename)
    shutil.copyfile(
        FIXTURES / "kegg_eco_ko_links.tsv", raw_dir / KEGG_KO_LINKS.filename
    )
    fixture_source = replace(
        NCBI_GFF,
        expected_sha256=file_sha256(FIXTURES / "mg1655_excerpt.gff3"),
    )
    monkeypatch.setattr("yggdrisil_ecoli.data_build.NCBI_GFF", fixture_source)

    registry_path = build_registry(
        data_dir,
        include_kegg=True,
        accept_kegg_terms=True,
        iml1515_json=FIXTURES / "iml1515_excerpt.json",
        refresh=False,
    )

    registry = GeneRegistry.from_parquet(registry_path)
    manifest = json.loads((data_dir / "processed" / "source_manifest.json").read_text())
    assert len(registry) == 3
    assert manifest["schema_version"] == 2
    assert manifest["outputs"]["gene_registry"]["sha256"] == file_sha256(registry_path)
    assert manifest["outputs"]["gene_registry"]["rows"] == 3
    assert (data_dir / "processed" / "crosswalk_audit.txt").exists()
