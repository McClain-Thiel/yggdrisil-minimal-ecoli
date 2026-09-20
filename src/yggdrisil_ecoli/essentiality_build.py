"""Prepare the Choe 2023 table once; experiments load the resulting Parquet file."""

from pathlib import Path

from yggdrisil_ecoli.data.essentiality import parse_choe_workbook
from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import (
    CHOE_2023_MEMBER,
    CHOE_2023_MEMBER_SHA256,
    CHOE_2023_SUPPLEMENT_BUNDLE,
    acquire_source,
    extract_member,
)


def build_essentiality_data(
    *, registry_path: Path, data_dir: Path, refresh: bool = False
) -> Path:
    """Join the pinned CC-BY-4.0 supplement to the reference gene registry."""

    bundle, source = acquire_source(
        CHOE_2023_SUPPLEMENT_BUNDLE,
        data_dir / "external" / "choe2023",
        refresh=refresh,
    )
    workbook = extract_member(
        bundle,
        CHOE_2023_MEMBER,
        CHOE_2023_MEMBER_SHA256,
        data_dir / "raw" / "essentiality" / CHOE_2023_MEMBER,
    )
    provenance = {
        "source_bundle_sha256": source.sha256,
        "workbook_sha256": CHOE_2023_MEMBER_SHA256,
        "reference_registry_sha256": file_sha256(registry_path),
    }
    dataset, report = parse_choe_workbook(
        workbook,
        GeneRegistry.from_parquet(registry_path),
        metadata={"provenance": provenance},
    )
    output = data_dir / "processed" / "essentiality.parquet"
    dataset.to_parquet(output)
    atomic_json(
        output.with_name("essentiality_manifest.json"),
        {
            "schema_version": 3,
            "source": source.as_dict(),
            "provenance": provenance,
            "archive_member": CHOE_2023_MEMBER,
            "doi": "10.1128/msystems.00896-22",
            "license": "CC-BY-4.0",
            "import_audit": report.as_dict(),
            "output_sha256": file_sha256(output),
        },
    )
    return output
