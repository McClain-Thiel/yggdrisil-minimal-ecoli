"""One-time source preparation for the frozen MG1655 experiment dataset."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from yggdrisil_ecoli import __version__
from yggdrisil_ecoli.constants import (
    ASSEMBLY_ACCESSION,
    ORGANISM_NAME,
    REFERENCE_ACCESSION,
)
from yggdrisil_ecoli.data.audit import audit_registry, write_audit
from yggdrisil_ecoli.data.crosswalks import (
    CrosswalkDiagnostics,
    add_iml1515_membership,
    add_kegg_crosswalk,
)
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.registry import file_sha256
from yggdrisil_ecoli.data.sources import (
    IML1515_PUBLICATION_ARCHIVE,
    IML1515_PUBLICATION_MEMBER,
    IML1515_PUBLICATION_MEMBER_SHA256,
    KEGG_GENE_LIST,
    KEGG_KO_LINKS,
    NCBI_GFF,
    SourceRecord,
    acquire_source,
    extract_member,
    record_local_source,
)


def build_registry(
    data_dir: Path,
    *,
    include_kegg: bool = False,
    accept_kegg_terms: bool = False,
    iml1515_json: Path | None = None,
    refresh: bool = False,
) -> Path:
    """Run the reproducible Milestone 1 build and return the registry path."""

    if include_kegg and not accept_kegg_terms:
        raise ValueError(
            "KEGG access requires --accept-kegg-terms; the REST API is limited "
            "to academic use and its snapshots must not be redistributed"
        )
    raw_dir = data_dir / "raw"
    processed_dir = data_dir / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    sources: list[SourceRecord] = []
    diagnostics = CrosswalkDiagnostics()

    gff_path, gff_source = acquire_source(NCBI_GFF, raw_dir, refresh=refresh)
    sources.append(gff_source)
    parsed = parse_ncbi_gff(gff_path)
    registry = parsed.registry

    if include_kegg:
        gene_path, gene_source = acquire_source(
            KEGG_GENE_LIST, raw_dir, refresh=refresh
        )
        # KEGG asks clients to remain below three requests per second.
        time.sleep(0.35)
        ko_path, ko_source = acquire_source(KEGG_KO_LINKS, raw_dir, refresh=refresh)
        sources.extend((gene_source, ko_source))
        registry = add_kegg_crosswalk(registry, gene_path, ko_path, diagnostics)
    else:
        diagnostics.notes.append(
            "KEGG crosswalk was not requested; KEGG/KO coverage is unknown, not safe."
        )

    if iml1515_json is not None:
        registry = add_iml1515_membership(registry, iml1515_json, diagnostics)
        sources.append(
            record_local_source(
                name="monk_2017_iml1515_json",
                path=iml1515_json,
                source_version=(
                    "Monk et al. 2017 Supplementary Data Set 1; model version 1"
                ),
                source_url="https://doi.org/10.1038/nbt.3956",
                redistribution="publication supplement; local artifact not vendored",
            )
        )
    else:
        diagnostics.notes.append(
            "iML1515 model was not supplied; model coverage is unknown, not safe."
        )
    diagnostics.normalize()

    audit = audit_registry(registry, diagnostics)
    if audit.fatal_errors:
        raise ValueError("crosswalk audit failed: " + "; ".join(audit.fatal_errors))

    registry_path = processed_dir / "gene_registry.parquet"
    registry.to_parquet(registry_path)
    write_audit(
        audit,
        processed_dir / "crosswalk_audit.json",
        processed_dir / "crosswalk_audit.txt",
    )

    manifest = {
        "schema_version": 2,
        "built_at": datetime.now(UTC).isoformat(),
        "script_version": __version__,
        "reference": {
            "organism": ORGANISM_NAME,
            "assembly": ASSEMBLY_ACCESSION,
            "reference_accession": REFERENCE_ACCESSION,
            "annotation_date": parsed.metadata.annotation_date,
            "annotation_source": parsed.metadata.annotation_source,
        },
        "sources": [source.as_dict() for source in sources],
        "outputs": {
            "gene_registry": {
                "path": str(registry_path),
                "sha256": file_sha256(registry_path),
                "rows": len(registry),
            }
        },
        "crosswalk_audit": audit.as_dict(),
    }
    atomic_json(processed_dir / "source_manifest.json", manifest)
    return registry_path


def fetch_iml1515(data_dir: Path, *, refresh: bool = False) -> Path:
    """Extract the exact publication model; its checksum pins identity and contents."""

    external = data_dir / "external"
    archive, source = acquire_source(
        IML1515_PUBLICATION_ARCHIVE, external, refresh=refresh
    )
    output = extract_member(
        archive,
        IML1515_PUBLICATION_MEMBER,
        IML1515_PUBLICATION_MEMBER_SHA256,
        external / "iML1515.json",
    )
    atomic_json(
        external / "iML1515.manifest.json",
        {
            "schema_version": 2,
            "publication_doi": "10.1038/nbt.3956",
            "archive": source.as_dict(),
            "member": IML1515_PUBLICATION_MEMBER,
            "sha256": IML1515_PUBLICATION_MEMBER_SHA256,
        },
    )
    return output


def build_data(
    data_dir: Path, *, accept_kegg_terms: bool = False, refresh: bool = False
) -> Path:
    """Prepare sources in dependency order; return the dataset root for a notebook.

    KEGG snapshots remain local unless redistribution permission is obtained.
    """

    from yggdrisil_ecoli.essentiality_build import build_essentiality_data
    from yggdrisil_ecoli.module_build import build_kegg_modules

    if not accept_kegg_terms:
        raise ValueError("full source preparation requires accept_kegg_terms=True")
    model = fetch_iml1515(data_dir, refresh=refresh)
    registry = build_registry(
        data_dir,
        include_kegg=True,
        accept_kegg_terms=True,
        iml1515_json=model,
        refresh=refresh,
    )
    build_essentiality_data(registry_path=registry, data_dir=data_dir, refresh=refresh)
    build_kegg_modules(
        registry_path=registry,
        ko_links_path=data_dir / "raw" / KEGG_KO_LINKS.filename,
        data_dir=data_dir,
        accept_kegg_terms=True,
        refresh=refresh,
    )
    return data_dir
