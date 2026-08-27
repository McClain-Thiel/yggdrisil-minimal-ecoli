"""Build the frozen, one-row-per-gene Choe 2023 essentiality artifact."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from yggdrisil_ecoli import __version__
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.essentiality import (
    parse_choe_workbook,
)
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import (
    CHOE_2023_MEMBER,
    CHOE_2023_MEMBER_SHA256,
    CHOE_2023_SUPPLEMENT_BUNDLE,
    acquire_source,
)


def build_essentiality_data(
    *, registry_path: Path, data_dir: Path, refresh: bool
) -> Path:
    registry = GeneRegistry.from_parquet(registry_path)
    registry_sha256 = file_sha256(registry_path)
    external_dir = data_dir / "external" / "choe2023"
    raw_dir = data_dir / "raw" / "essentiality"
    processed_dir = data_dir / "processed"
    bundle_path, source_record = acquire_source(
        CHOE_2023_SUPPLEMENT_BUNDLE, external_dir, refresh=refresh
    )
    with zipfile.ZipFile(bundle_path) as archive:
        try:
            workbook_bytes = archive.read(CHOE_2023_MEMBER)
        except KeyError as exc:
            raise DataValidationError(
                f"Choe supplement bundle lacks {CHOE_2023_MEMBER!r}"
            ) from exc
    member_sha256 = hashlib.sha256(workbook_bytes).hexdigest()
    if member_sha256 != CHOE_2023_MEMBER_SHA256:
        raise DataValidationError(
            f"Choe Table S1 changed: expected {CHOE_2023_MEMBER_SHA256}, "
            f"got {member_sha256}"
        )
    workbook_path = raw_dir / CHOE_2023_MEMBER
    _atomic_bytes(workbook_path, workbook_bytes)

    dataset, report = parse_choe_workbook(
        workbook_path,
        registry,
        metadata={
            "provenance": {
                "source_bundle_sha256": source_record.sha256,
                "workbook_sha256": member_sha256,
                "reference_registry_sha256": registry_sha256,
            }
        },
    )
    output_path = processed_dir / "essentiality.parquet"
    dataset.to_parquet(output_path)
    manifest = {
        "schema_version": 2,
        "built_at": datetime.now(UTC).isoformat(),
        "script_version": __version__,
        "reference_registry": {
            "path": str(registry_path),
            "sha256": registry_sha256,
        },
        "source": source_record.as_dict(),
        "archive_member": {
            "path": CHOE_2023_MEMBER,
            "sha256": member_sha256,
            "bytes": len(workbook_bytes),
            "license": "CC-BY-4.0",
            "doi": "10.1128/msystems.00896-22",
        },
        "import_audit": report.as_dict(),
        "output": {
            "path": str(output_path),
            "sha256": file_sha256(output_path),
            "rows": len(dataset),
        },
    }
    _atomic_json(processed_dir / "essentiality_manifest.json", manifest)
    print(
        f"Mapped {report.mapped_source_genes} Choe genes; "
        f"{len(report.canonical_genes_without_measurement)} canonical genes "
        "remain unknown."
    )
    print(json.dumps(report.summary_counts, sort_keys=True))
    print(f"Wrote {output_path}")
    return output_path


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    _atomic_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
