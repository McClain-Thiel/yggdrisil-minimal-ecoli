#!/usr/bin/env python3
"""Fetch the exact iML1515 JSON from the 2017 publication supplement."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.sources import (
    IML1515_PUBLICATION_ARCHIVE,
    IML1515_PUBLICATION_MEMBER,
    IML1515_PUBLICATION_MEMBER_SHA256,
    acquire_source,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    external_dir = args.data_dir / "external"
    archive_path, record = acquire_source(
        IML1515_PUBLICATION_ARCHIVE,
        external_dir,
        refresh=args.refresh,
    )
    with zipfile.ZipFile(archive_path) as archive:
        try:
            model_bytes = archive.read(IML1515_PUBLICATION_MEMBER)
        except KeyError as exc:
            raise DataValidationError(
                f"publication archive lacks {IML1515_PUBLICATION_MEMBER!r}"
            ) from exc
    model_sha256 = hashlib.sha256(model_bytes).hexdigest()
    if model_sha256 != IML1515_PUBLICATION_MEMBER_SHA256:
        raise DataValidationError(
            "iML1515 publication member changed: expected "
            f"{IML1515_PUBLICATION_MEMBER_SHA256}, got {model_sha256}"
        )
    model_payload = json.loads(model_bytes)
    _validate_model(model_payload)
    model_path = external_dir / "iML1515.json"
    _atomic_bytes(model_path, model_bytes)
    manifest = {
        "schema_version": 1,
        "fetched_at": datetime.now(UTC).isoformat(),
        "publication_doi": "10.1038/nbt.3956",
        "archive": record.as_dict(),
        "member": {
            "path": IML1515_PUBLICATION_MEMBER,
            "sha256": model_sha256,
            "bytes": len(model_bytes),
        },
        "model": {
            "id": model_payload["id"],
            "version": model_payload["version"],
            "genes": len(model_payload["genes"]),
            "reactions": len(model_payload["reactions"]),
            "metabolites": len(model_payload["metabolites"]),
        },
    }
    _atomic_bytes(
        external_dir / "iML1515.manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )
    print(f"Wrote {model_path}")
    print(f"sha256: {model_sha256}")


def _validate_model(payload: object) -> None:
    if not isinstance(payload, dict):
        raise DataValidationError("iML1515 publication member is not a JSON object")
    expected_lengths = {"genes": 1516, "reactions": 2712, "metabolites": 1877}
    if payload.get("id") != "iML1515" or payload.get("version") != "1":
        raise DataValidationError("unexpected iML1515 model identity or version")
    for key, expected in expected_lengths.items():
        value = payload.get(key)
        if not isinstance(value, list) or len(value) != expected:
            raise DataValidationError(
                f"unexpected iML1515 {key} count: expected {expected}"
            )


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


if __name__ == "__main__":
    main()
