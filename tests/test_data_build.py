import hashlib
import json
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import (
    KEGG_GENE_LIST,
    KEGG_KO_LINKS,
    NCBI_GFF,
    extract_member,
)
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


def test_publication_member_hash_is_checked_before_replacing_output(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "supplement.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("model.json", b"new source")
    output = tmp_path / "model.json"
    output.write_bytes(b"previous frozen source")
    with pytest.raises(ValueError, match="expected"):
        extract_member(archive, "model.json", "0" * 64, output)
    assert output.read_bytes() == b"previous frozen source"
    digest = hashlib.sha256(b"new source").hexdigest()
    assert extract_member(archive, "model.json", digest, output) == output
    assert output.read_bytes() == b"new source"
