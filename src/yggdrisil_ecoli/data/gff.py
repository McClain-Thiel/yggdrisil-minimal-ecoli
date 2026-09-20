"""Prepare the MG1655 registry from an NCBI GFF3 using gffutils."""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from yggdrisil_ecoli.constants import (
    ASSEMBLY_ACCESSION,
    ASSEMBLY_NAME,
    GENE_TYPE,
    REFERENCE_ACCESSION,
    STRAIN,
    SUBSTRAIN,
    TAXONOMY_ID,
)
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRecord, GeneRegistry

if TYPE_CHECKING:
    from gffutils import Feature


@dataclass(frozen=True, slots=True)
class GffMetadata:
    assembly_accession: str
    assembly_name: str
    reference_accession: str
    taxonomy_id: str
    annotation_date: str | None
    annotation_source: str | None


@dataclass(frozen=True, slots=True)
class ParsedGff:
    registry: GeneRegistry
    metadata: GffMetadata


def parse_ncbi_gff(path: str | Path) -> ParsedGff:
    """Read protein-coding genes and CDS products, rejecting reference drift."""
    from gffutils.feature import feature_from_line

    directives: dict[str, str] = {}
    genes: list[Feature] = []
    products: dict[str, str] = {}
    region = None
    for line_number, line in enumerate(_text_lines(Path(path)), start=1):
        if line.startswith("##FASTA"):
            break
        if line.startswith("#!"):
            key, _, value = line[2:].partition(" ")
            directives[key] = value.strip()
        elif line.strip() and not line.startswith("#"):
            try:
                feature = feature_from_line(line.rstrip("\n"))
            except (ValueError, IndexError) as exc:
                raise DataValidationError(f"line {line_number}: invalid GFF3") from exc
            if feature.featuretype == "region" and feature.seqid == REFERENCE_ACCESSION:
                region = feature
            tag = _attribute(feature, "locus_tag")
            if tag and feature.featuretype == "gene":
                if _attribute(feature, "gene_biotype") == GENE_TYPE:
                    genes.append(feature)
            elif tag and feature.featuretype == "CDS":
                product = _attribute(feature, "product")
                if product:
                    products.setdefault(tag, product)

    metadata = _validate_reference(directives, region)
    records = []
    for feature in genes:
        tag = _attribute(feature, "locus_tag") or ""
        if feature.start is None or feature.end is None:
            raise DataValidationError(f"{tag}: missing gene coordinates")
        if feature.seqid != REFERENCE_ACCESSION:
            raise DataValidationError(
                f"{tag}: expected reference {REFERENCE_ACCESSION}"
            )
        symbol = _attribute(feature, "gene") or _attribute(feature, "Name")
        name = _attribute(feature, "Name")
        records.append(
            GeneRecord(
                b_number=tag,
                symbol=symbol,
                name=name if name != symbol else None,
                description=products.get(tag),
                start=feature.start,
                end=feature.end,
                strand=feature.strand,
                ncbi_gene_id=_crossref(feature, "GeneID"),
                ecocyc_id=_crossref(feature, "ECOCYC"),
            )
        )
    return ParsedGff(GeneRegistry(records), metadata)


def _validate_reference(
    directives: dict[str, str], region: Feature | None
) -> GffMetadata:
    accession = directives.get("genome-build-accession", "").removeprefix(
        "NCBI_Assembly:"
    )
    name = directives.get("genome-build", "")
    if accession != ASSEMBLY_ACCESSION or name != ASSEMBLY_NAME:
        raise DataValidationError(
            f"expected assembly {ASSEMBLY_ACCESSION} ({ASSEMBLY_NAME})"
        )
    if region is None:
        raise DataValidationError(
            f"expected reference chromosome {REFERENCE_ACCESSION}"
        )
    for key, expected in {"strain": STRAIN, "substrain": SUBSTRAIN}.items():
        if _attribute(region, key) != expected:
            raise DataValidationError(f"reference region must have {key}={expected!r}")
    if f"taxon:{TAXONOMY_ID}" not in region.attributes.get("Dbxref", []):
        raise DataValidationError(f"reference region must have taxon:{TAXONOMY_ID}")
    return GffMetadata(
        assembly_accession=accession,
        assembly_name=name,
        reference_accession=REFERENCE_ACCESSION,
        taxonomy_id=TAXONOMY_ID,
        annotation_date=directives.get("annotation-date"),
        annotation_source=directives.get("annotation-source"),
    )


def _attribute(feature: Feature, key: str) -> str | None:
    values: list[str] = feature.attributes.get(key, [])
    if len(set(values)) > 1:
        raise DataValidationError(f"conflicting GFF3 attribute {key!r}: {values}")
    return values[0] if values else None


def _crossref(feature: Feature, namespace: str) -> str | None:
    prefix = f"{namespace}:"
    values = {
        v.removeprefix(prefix)
        for v in feature.attributes.get("Dbxref", [])
        if v.startswith(prefix)
    }
    if len(values) > 1:
        raise DataValidationError(f"multiple {namespace} identifiers: {sorted(values)}")
    return next(iter(values), None)


def _text_lines(path: Path) -> Iterator[str]:
    with path.open("rb") as handle:
        compressed = handle.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    with opener(path, "rt", encoding="utf-8") as handle:
        yield from handle
