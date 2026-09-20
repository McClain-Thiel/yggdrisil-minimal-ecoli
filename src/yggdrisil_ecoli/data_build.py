"""Prepare the reference dataset once; experiments load its frozen artifacts."""

from datetime import UTC, datetime
from pathlib import Path

from yggdrisil_ecoli.data.audit import audit_registry
from yggdrisil_ecoli.data.crosswalks import add_crosswalks
from yggdrisil_ecoli.data.essentiality import parse_choe_workbook
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.registry import file_sha256
from yggdrisil_ecoli.data.sources import (
    CHOE_2023_MEMBER,
    CHOE_2023_MEMBER_SHA256,
    CHOE_2023_SUPPLEMENT_BUNDLE,
    IML1515_PUBLICATION_ARCHIVE,
    IML1515_PUBLICATION_MEMBER,
    IML1515_PUBLICATION_MEMBER_SHA256,
    KEGG_GENE_LIST,
    KEGG_KO_LINKS,
    NCBI_GFF,
    acquire_source,
    extract_member,
)
from yggdrisil_ecoli.module_build import build_kegg_modules


def build_data(
    data_dir: Path, *, accept_kegg_terms: bool = False, refresh: bool = False
) -> Path:
    """Download, map and validate MG1655 sources; write one provenance manifest.

    KEGG snapshots remain local unless redistribution permission is obtained.
    """
    if not accept_kegg_terms:
        raise ValueError("source preparation requires accept_kegg_terms=True")
    raw, processed = data_dir / "raw", data_dir / "processed"
    specs = (
        NCBI_GFF,
        KEGG_GENE_LIST,
        KEGG_KO_LINKS,
        IML1515_PUBLICATION_ARCHIVE,
        CHOE_2023_SUPPLEMENT_BUNDLE,
    )
    inputs = {
        spec.filename: acquire_source(spec, raw, refresh=refresh) for spec in specs
    }
    model = extract_member(
        inputs[IML1515_PUBLICATION_ARCHIVE.filename],
        IML1515_PUBLICATION_MEMBER,
        IML1515_PUBLICATION_MEMBER_SHA256,
        data_dir / "external" / "iML1515.json",
    )
    parsed = parse_ncbi_gff(inputs[NCBI_GFF.filename])
    registry, mappings = add_crosswalks(
        parsed.registry,
        inputs[KEGG_GENE_LIST.filename],
        inputs[KEGG_KO_LINKS.filename],
        model,
    )
    audit = {**audit_registry(registry), **mappings}
    registry_path = processed / "gene_registry.parquet"
    registry.to_parquet(registry_path)
    workbook = extract_member(
        inputs[CHOE_2023_SUPPLEMENT_BUNDLE.filename],
        CHOE_2023_MEMBER,
        CHOE_2023_MEMBER_SHA256,
        raw / CHOE_2023_MEMBER,
    )
    essentiality, essentiality_audit = parse_choe_workbook(
        workbook,
        registry,
        metadata={
            "provenance": {
                "source_bundle_sha256": file_sha256(
                    inputs[CHOE_2023_SUPPLEMENT_BUNDLE.filename]
                ),
                "workbook_sha256": CHOE_2023_MEMBER_SHA256,
                "reference_registry_sha256": file_sha256(registry_path),
            }
        },
    )
    essentiality_path = processed / "essentiality.parquet"
    essentiality.to_parquet(essentiality_path)
    atomic_json(
        processed / "source_manifest.json",
        {
            "built_at": datetime.now(UTC).isoformat(),
            "reference": parsed.metadata,
            "inputs": {
                spec.filename: {
                    "url": spec.url,
                    "sha256": file_sha256(inputs[spec.filename]),
                }
                for spec in specs
            },
            "outputs": {
                path.name: file_sha256(path)
                for path in (registry_path, essentiality_path, model)
            },
            "audit": {"crosswalks": audit, "essentiality": essentiality_audit},
        },
    )
    build_kegg_modules(
        registry_path=registry_path,
        ko_links_path=inputs[KEGG_KO_LINKS.filename],
        data_dir=data_dir,
        accept_kegg_terms=True,
        refresh=refresh,
    )
    return data_dir
