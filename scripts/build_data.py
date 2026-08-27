#!/usr/bin/env python3
"""Build every local scientific artifact in dependency order."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import atomic_bytes
from yggdrisil_ecoli.data.sources import (
    IML1515_PUBLICATION_ARCHIVE,
    IML1515_PUBLICATION_MEMBER,
    IML1515_PUBLICATION_MEMBER_SHA256,
    KEGG_KO_LINKS,
    acquire_source,
)
from yggdrisil_ecoli.data_build import build_registry
from yggdrisil_ecoli.essentiality_build import build_essentiality_data
from yggdrisil_ecoli.module_build import build_kegg_modules


def fetch_iml1515(data_dir: Path, *, refresh: bool) -> Path:
    """Extract and validate the exact publication-supplement model."""

    external = data_dir / "external"
    archive_path, source = acquire_source(
        IML1515_PUBLICATION_ARCHIVE, external, refresh=refresh
    )
    with zipfile.ZipFile(archive_path) as archive:
        try:
            model_bytes = archive.read(IML1515_PUBLICATION_MEMBER)
        except KeyError as exc:
            raise DataValidationError(
                f"publication archive lacks {IML1515_PUBLICATION_MEMBER!r}"
            ) from exc
    digest = hashlib.sha256(model_bytes).hexdigest()
    if digest != IML1515_PUBLICATION_MEMBER_SHA256:
        raise DataValidationError(
            "iML1515 publication member changed: expected "
            f"{IML1515_PUBLICATION_MEMBER_SHA256}, got {digest}"
        )
    model = json.loads(model_bytes)
    _validate_model(model)
    model_path = external / "iML1515.json"
    atomic_bytes(model_path, model_bytes)
    manifest = {
        "schema_version": 1,
        "fetched_at": datetime.now(UTC).isoformat(),
        "publication_doi": "10.1038/nbt.3956",
        "archive": source.as_dict(),
        "member": {
            "path": IML1515_PUBLICATION_MEMBER,
            "sha256": digest,
            "bytes": len(model_bytes),
        },
        "model": {
            "id": model["id"],
            "version": model["version"],
            **{key: len(model[key]) for key in ("genes", "reactions", "metabolites")},
        },
    }
    atomic_bytes(
        external / "iML1515.manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )
    return model_path


def _validate_model(payload: object) -> None:
    if not isinstance(payload, dict):
        raise DataValidationError("iML1515 publication member is not a JSON object")
    if payload.get("id") != "iML1515" or payload.get("version") != "1":
        raise DataValidationError("unexpected iML1515 model identity or version")
    for key, expected in {
        "genes": 1516,
        "reactions": 2712,
        "metabolites": 1877,
    }.items():
        value = payload.get(key)
        if not isinstance(value, list) or len(value) != expected:
            raise DataValidationError(
                f"unexpected iML1515 {key} count: expected {expected}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--accept-kegg-terms", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if not args.accept_kegg_terms:
        raise SystemExit("full build requires --accept-kegg-terms")
    try:
        model_path = fetch_iml1515(args.data_dir, refresh=args.refresh)
        registry_path = build_registry(
            args.data_dir,
            include_kegg=True,
            accept_kegg_terms=True,
            iml1515_json=model_path,
            refresh=args.refresh,
        )
        build_essentiality_data(
            registry_path=registry_path,
            data_dir=args.data_dir,
            refresh=args.refresh,
        )
        build_kegg_modules(
            registry_path=registry_path,
            ko_links_path=args.data_dir / "raw" / KEGG_KO_LINKS.filename,
            data_dir=args.data_dir,
            accept_kegg_terms=True,
            refresh=args.refresh,
        )
    except (DataValidationError, ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
