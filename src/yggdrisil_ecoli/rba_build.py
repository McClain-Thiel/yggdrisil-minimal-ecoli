"""Build the pinned E. coli K-12 resource-balance model artifact."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.registry import file_sha256
from yggdrisil_ecoli.data.sources import SourceSpec, acquire_source

RBA_MODELS_COMMIT = "973f00e0618493e6df6af52bdde55686168fda62"
RBA_MODEL_NAME = "Escherichia-coli-K12-WT"
RBA_MODEL_BASE_URL = (
    "https://raw.githubusercontent.com/RBAgroup/RBA-models/"
    f"{RBA_MODELS_COMMIT}/{RBA_MODEL_NAME}/"
)
RBA_ARTIFACT_MANIFEST = "rba_artifact_manifest.json"
MODEL_STRUCTURE_PATH = "other/ModelStructure.json"
RBA_GROWTH_FLOOR_H = 0.1
PUBLISHED_WILD_TYPE_MAX_GROWTH_RATE_H = 0.5986785888671875


@dataclass(frozen=True, slots=True)
class _PinnedFile:
    path: str
    sha256: str


RBA_MODEL_FILES = (
    _PinnedFile(
        "model/compartments.xml",
        "1192a9f14e9d8f852b89aed8e035ba1e24fa16527f92a466a4a1348b21df4333",
    ),
    _PinnedFile(
        "model/metabolism.xml",
        "40636e85e6d627ac5ebd6a88e9e303920ea89fec464c28ae88038e0a1376ec5e",
    ),
    _PinnedFile(
        "model/enzymes.xml",
        "32ee3a070b80ce2e1c7dcf153573aced3decf7811b22e5d7317d2899902d6bcb",
    ),
    _PinnedFile(
        "model/proteins.xml",
        "fb3379ff7270fddfa53e36fb4dab21f337461ba0aa9ee7724e824a1718f1c161",
    ),
    _PinnedFile(
        "model/rnas.xml",
        "7eb9baf580614813fdb565c80a0f0f2bbb101242e1fc9ae8e9809d15ea0c00d0",
    ),
    _PinnedFile(
        "model/dna.xml",
        "009f0a77ad3ca55c5a1edeb8aca17bce40615d31a1b37bb6cec17bc44dd36619",
    ),
    _PinnedFile(
        "model/other_macromolecules.xml",
        "5cdbff6608680f5ac52c03119d1238a17c542ac217f2bb487d69fd04870f1a4d",
    ),
    _PinnedFile(
        "model/processes.xml",
        "bc87f31dbee2e702359ff846bd4b3aa0e9db0918c83eb400a18836c8aa307ba5",
    ),
    _PinnedFile(
        "model/targets.xml",
        "858fa43d8dbbb9ba0e0249673a26e851700b619eb36cdd457ca4f5cdfc288590",
    ),
    _PinnedFile(
        "model/parameters.xml",
        "ba2bf6380183b5e86acf165dc24edf97af606e8a15f4761895f1b45fcd5f8b13",
    ),
    _PinnedFile(
        "model/custom_constraints.xml",
        "e5f9d70463c94448b6db9b7e61d4e67e58a44bf2db937e94c1be575563730993",
    ),
    _PinnedFile(
        "model/medium.tsv",
        "4154761305fcef541ba38d0fe7f2025a532111fc482e463628e41d7e31441342",
    ),
    _PinnedFile(
        "model_file_index.in",
        "b78d17ce5d61dd66311e4bfe4e7e61d056258315a4eae15220d32bccc1b911b7",
    ),
    _PinnedFile(
        "metadata.tsv",
        "c1c41fff95be61747312089a207bcdda6b5249d87f400c8039896624f7fe2756",
    ),
    _PinnedFile(
        "README.rst",
        "18ac95423921ca560f0cf53d27d7d16f46c53412bffc9ec88ee47a8fb21d43b4",
    ),
)


def build_rba_artifact(
    output_dir: str | Path,
    *,
    refresh: bool = False,
) -> Path:
    """Download, verify, and derive the deterministic RBA model structure."""

    artifact_dir = Path(output_dir)
    source_records: list[dict[str, object]] = []
    for pinned in RBA_MODEL_FILES:
        relative_path = Path(pinned.path)
        spec = SourceSpec(
            name=f"rba_models_{pinned.path.replace('/', '_')}",
            url=f"{RBA_MODEL_BASE_URL}{pinned.path}",
            filename=relative_path.name,
            source_version=RBA_MODELS_COMMIT,
            redistribution="RBA-models CC-BY-NC-4.0; local artifact is not vendored",
            expected_sha256=pinned.sha256,
        )
        local_path, record = acquire_source(
            spec,
            artifact_dir / relative_path.parent,
            refresh=refresh,
        )
        source_records.append(
            {
                "path": local_path.relative_to(artifact_dir).as_posix(),
                "url": record.url,
                "sha256": record.sha256,
                "bytes": record.bytes,
            }
        )

    generated_path = artifact_dir / MODEL_STRUCTURE_PATH
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    _generate_model_structure(artifact_dir, generated_path)
    generated_sha256 = file_sha256(generated_path)
    dependency_versions = _dependency_versions()
    provenance = {
        "repository": "https://github.com/RBAgroup/RBA-models",
        "commit": RBA_MODELS_COMMIT,
        "model": RBA_MODEL_NAME,
        "source_files": sorted(source_records, key=lambda item: str(item["path"])),
        "generated_files": [
            {
                "path": MODEL_STRUCTURE_PATH,
                "sha256": generated_sha256,
                "bytes": generated_path.stat().st_size,
            }
        ],
        "dependencies": dependency_versions,
        "growth_floor_h": RBA_GROWTH_FLOOR_H,
        "published_wild_type_max_growth_rate_h": (
            PUBLISHED_WILD_TYPE_MAX_GROWTH_RATE_H
        ),
    }
    provenance_sha256 = _sha256_json(provenance)
    manifest = {
        "schema_version": 1,
        "artifact": "ecoli_k12_wt_rba",
        "built_at": datetime.now(UTC).isoformat(),
        "provenance": provenance,
        "provenance_sha256": provenance_sha256,
        "artifact_bundle_sha256": _bundle_sha256(artifact_dir, provenance),
    }
    manifest_path = artifact_dir / RBA_ARTIFACT_MANIFEST
    atomic_json(manifest_path, manifest)
    return manifest_path


def _generate_model_structure(artifact_dir: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".generated",
        dir=destination.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import sys; "
            "from yggdrisil_ecoli.rba_build import "
            "_generate_model_structure_in_process as generate; "
            "generate(Path(sys.argv[1]), Path(sys.argv[2]))"
        ),
        str(artifact_dir.resolve()),
        str(temporary.resolve()),
    ]
    try:
        subprocess.run(command, check=True, env=environment)  # noqa: S603
        payload = json.loads(temporary.read_text())
        atomic_json(destination, payload)
    except subprocess.CalledProcessError as exc:
        raise DataValidationError("RBA ModelStructure generation failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _generate_model_structure_in_process(artifact_dir: Path, destination: Path) -> None:
    try:
        import rba
        from rbatools.rba_model_structure import ModelStructureRBA
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise DataValidationError(
            "RBA artifact building requires the project's pinned 'rba' extra"
        ) from exc

    model = rba.RbaModel.from_xml(input_dir=str(artifact_dir))
    structure = ModelStructureRBA()
    structure.from_files(xml_dir=str(artifact_dir), rba_model=model, verbose=False)
    structure.export_json(path=str(destination))


def _dependency_versions() -> dict[str, str]:
    expected = {
        "RBApy": "3.0.3",
        "RBAtools": "2.0.1",
        "setuptools": "80.10.2",
        "swiglpk": "5.0.13",
    }
    try:
        installed = {package: version(package) for package in expected}
    except PackageNotFoundError as exc:
        raise DataValidationError(
            "RBA artifact building requires the project's pinned 'rba' extra"
        ) from exc
    mismatched = {
        package: (expected[package], installed[package])
        for package in expected
        if installed[package] != expected[package]
    }
    if mismatched:
        raise DataValidationError(
            "RBA dependency versions differ from the pinned build environment: "
            f"mismatched={mismatched}"
        )
    return installed


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _bundle_sha256(artifact_dir: Path, provenance: dict[str, object]) -> str:
    entries = []
    files = provenance["source_files"]
    if not isinstance(files, list):
        raise DataValidationError("invalid RBA provenance source_files")
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise DataValidationError("invalid RBA provenance source entry")
        path = artifact_dir / item["path"]
        entries.append((item["path"], file_sha256(path)))
    return _sha256_json(sorted(entries))


__all__ = [
    "MODEL_STRUCTURE_PATH",
    "PUBLISHED_WILD_TYPE_MAX_GROWTH_RATE_H",
    "RBA_ARTIFACT_MANIFEST",
    "RBA_GROWTH_FLOOR_H",
    "RBA_MODEL_FILES",
    "RBA_MODELS_COMMIT",
    "build_rba_artifact",
]
