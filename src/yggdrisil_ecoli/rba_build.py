"""Build the pinned E. coli K-12 resource-balance model artifact."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import atomic_json, file_sha256

RBA_MODELS_COMMIT = "973f00e0618493e6df6af52bdde55686168fda62"
RBA_MODEL_NAME = "Escherichia-coli-K12-WT"
RBA_MODEL_BASE_URL = (
    "https://raw.githubusercontent.com/RBAgroup/RBA-models/"
    f"{RBA_MODELS_COMMIT}/{RBA_MODEL_NAME}/"
)
RBA_ARTIFACT_MANIFEST = "rba_artifact_manifest.json"
MODEL_STRUCTURE_PATH = "other/ModelStructure.json"
RBA_GROWTH_FLOOR_H = 0.1
RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H = 0.5986785888671875
RBA_REPOSITORY_WT_GROWTH_PATH = "outputs/growth_rate.out"
RBA_EXPECTED_STRUCTURE_DIMENSIONS = {
    "mathematical_constraints": 8_378,
    "mathematical_variables": 7_378,
    "proteins": 1_447,
    "processes": 7,
    "enzymes": 3_445,
    "reactions": 3_926,
}
RBA_EXPECTED_LP_DIMENSIONS = {"rows": 8_386, "columns": 7_384}
RBA_EXPECTED_REGISTRY_MAPPING = {"genes": 1_441, "variables": 3_407}
RBA_NUMERICAL_DEPENDENCIES = (
    "lxml",
    "numpy",
    "pandas",
    "python-libsbml",
    "scipy",
)


RBA_MODEL_FILES = {
    "model/compartments.xml": "1192a9f14e9d8f852b89aed8e035ba1e24fa16527f92a466a4a1348b21df4333",
    "model/metabolism.xml": "40636e85e6d627ac5ebd6a88e9e303920ea89fec464c28ae88038e0a1376ec5e",
    "model/enzymes.xml": "32ee3a070b80ce2e1c7dcf153573aced3decf7811b22e5d7317d2899902d6bcb",
    "model/proteins.xml": "fb3379ff7270fddfa53e36fb4dab21f337461ba0aa9ee7724e824a1718f1c161",
    "model/rnas.xml": "7eb9baf580614813fdb565c80a0f0f2bbb101242e1fc9ae8e9809d15ea0c00d0",
    "model/dna.xml": "009f0a77ad3ca55c5a1edeb8aca17bce40615d31a1b37bb6cec17bc44dd36619",
    "model/other_macromolecules.xml": "5cdbff6608680f5ac52c03119d1238a17c542ac217f2bb487d69fd04870f1a4d",
    "model/processes.xml": "bc87f31dbee2e702359ff846bd4b3aa0e9db0918c83eb400a18836c8aa307ba5",
    "model/targets.xml": "858fa43d8dbbb9ba0e0249673a26e851700b619eb36cdd457ca4f5cdfc288590",
    "model/parameters.xml": "ba2bf6380183b5e86acf165dc24edf97af606e8a15f4761895f1b45fcd5f8b13",
    "model/custom_constraints.xml": "e5f9d70463c94448b6db9b7e61d4e67e58a44bf2db937e94c1be575563730993",
    "model/medium.tsv": "4154761305fcef541ba38d0fe7f2025a532111fc482e463628e41d7e31441342",
    "model_file_index.in": "b78d17ce5d61dd66311e4bfe4e7e61d056258315a4eae15220d32bccc1b911b7",
    "metadata.tsv": "c1c41fff95be61747312089a207bcdda6b5249d87f400c8039896624f7fe2756",
    "README.rst": "18ac95423921ca560f0cf53d27d7d16f46c53412bffc9ec88ee47a8fb21d43b4",
    RBA_REPOSITORY_WT_GROWTH_PATH: "007906fff17975251ecb85f89a57d2e7d44268165ffc0e85c01d34a22f05d0dc",
}


def build_rba_artifact(
    output_dir: str | Path,
    *,
    refresh: bool = False,
) -> Path:
    """Download, verify, and derive the deterministic RBA model structure."""

    from yggdrisil_ecoli.data.sources import SourceSpec, acquire_source

    artifact_dir = Path(output_dir)
    dependencies = _dependency_versions()
    source_records: list[dict[str, object]] = []
    for source_path, sha256 in RBA_MODEL_FILES.items():
        relative_path = Path(source_path)
        spec = SourceSpec(
            url=f"{RBA_MODEL_BASE_URL}{source_path}",
            filename=relative_path.name,
            expected_sha256=sha256,
        )
        local_path = acquire_source(
            spec,
            artifact_dir / relative_path.parent,
            refresh=refresh,
        )
        source_records.append(
            {
                "path": local_path.relative_to(artifact_dir).as_posix(),
                "url": spec.url,
                "sha256": file_sha256(local_path),
                "bytes": local_path.stat().st_size,
            }
        )

    generated_path = artifact_dir / MODEL_STRUCTURE_PATH
    model_dimensions = _generate_model_structure(artifact_dir, generated_path)
    provenance = {
        "repository": "https://github.com/RBAgroup/RBA-models",
        "commit": RBA_MODELS_COMMIT,
        "model": RBA_MODEL_NAME,
        "source_files": sorted(source_records, key=lambda item: str(item["path"])),
        "generated_files": [
            {
                "path": MODEL_STRUCTURE_PATH,
                "sha256": file_sha256(generated_path),
                "bytes": generated_path.stat().st_size,
            }
        ],
        "dependencies": dependencies,
        "model_dimensions": model_dimensions,
        "growth_floor_h": RBA_GROWTH_FLOOR_H,
        "repository_wild_type_max_growth_rate_h": RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H,
    }
    manifest = {
        "schema_version": 1,
        "artifact": "ecoli_k12_wt_rba",
        "built_at": datetime.now(UTC).isoformat(),
        "provenance": provenance,
        "provenance_sha256": _sha256_json(provenance),
        "artifact_bundle_sha256": _sha256_json(sorted(RBA_MODEL_FILES.items())),
    }
    manifest_path = artifact_dir / RBA_ARTIFACT_MANIFEST
    atomic_json(manifest_path, manifest)
    return manifest_path


def _generate_model_structure(artifact_dir: Path, destination: Path) -> dict[str, int]:
    # RBAtools traverses sets while deriving the structure. Fix its hash seed in
    # a subprocess so repeated builds have the same content and artifact identity.
    generate = """
import sys
from rba import RbaModel
from rbatools.rba_model_structure import ModelStructureRBA
model = RbaModel.from_xml(input_dir=sys.argv[1])
structure = ModelStructureRBA()
structure.from_files(xml_dir=sys.argv[1], rba_model=model, verbose=False)
structure.export_json(path=sys.argv[2])
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        temporary = Path(staging) / destination.name
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    generate,
                    str(artifact_dir.resolve()),
                    str(temporary),
                ],
                check=True,
                env={**os.environ, "PYTHONHASHSEED": "0"},
            )
        except subprocess.CalledProcessError as exc:
            raise DataValidationError("RBA ModelStructure generation failed") from exc
        payload = json.loads(temporary.read_text())
        dimensions = _validated_model_dimensions(payload)
        atomic_json(destination, payload)
    return dimensions


def _dependency_versions() -> dict[str, str]:
    expected = {
        "RBApy": "3.0.3",
        "RBAtools": "2.0.1",
        "setuptools": "80.10.2",
        "swiglpk": "5.0.13",
    }
    try:
        installed = {
            package: version(package)
            for package in (*expected, *RBA_NUMERICAL_DEPENDENCIES)
        }
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


def _validated_model_dimensions(payload: object) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise DataValidationError("RBA ModelStructure must be a JSON object")
    statistics = payload.get("ModelStatistics")
    if not isinstance(statistics, dict):
        raise DataValidationError("RBA ModelStructure lacks ModelStatistics")
    dimensions = {
        "mathematical_constraints": statistics.get(
            "Mathematical constraints constraints"
        ),
        "mathematical_variables": statistics.get("Mathematical constraints variables"),
        "proteins": statistics.get("Proteins Total"),
        "processes": statistics.get("Processes Total"),
        "enzymes": statistics.get("Enzymes Total"),
        "reactions": statistics.get("Reactions Total"),
    }
    if dimensions != RBA_EXPECTED_STRUCTURE_DIMENSIONS:
        raise DataValidationError(
            "RBA model dimensions differ from the pinned snapshot: "
            f"expected={RBA_EXPECTED_STRUCTURE_DIMENSIONS}, actual={dimensions}"
        )
    return {name: int(value) for name, value in dimensions.items()}


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
