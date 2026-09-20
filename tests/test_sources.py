import hashlib
import zipfile
from pathlib import Path

import pytest

from yggdrisil_ecoli.data.sources import SourceSpec, acquire_source, extract_member


def test_cached_sources_are_verified_without_network(tmp_path: Path) -> None:
    content = b"frozen reference data"
    path = tmp_path / "source.txt"
    path.write_bytes(content)
    source = SourceSpec(
        "https://invalid.example/source", path.name, hashlib.sha256(content).hexdigest()
    )
    assert acquire_source(source, tmp_path) == path
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum changed"):
        acquire_source(source, tmp_path)


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
