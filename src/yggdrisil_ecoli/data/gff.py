"""Prepare the MG1655 registry from an NCBI GFF3 using gffutils."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

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
from yggdrisil_ecoli.data.registry import GeneRecord

if TYPE_CHECKING:
    from gffutils import Feature


def parse_ncbi_gff(path: str | Path) -> tuple[pd.DataFrame, dict[str, str | None]]:
    """Read protein-coding genes and CDS products, rejecting reference drift."""
    from gffutils.feature import feature_from_line

    directives: dict[str, str] = {}
    records = []
    products: dict[str, str] = {}
    region = None
    raw = Path(path).read_bytes()
    text = (gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw).decode()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("##FASTA"):
            break
        if line.startswith("#!"):
            key, _, value = line[2:].partition(" ")
            directives[key] = value.strip()
        if not line.strip() or line.startswith("#"):
            continue
        try:
            feature = feature_from_line(line.rstrip("\n"))
        except (ValueError, IndexError) as exc:
            raise DataValidationError(f"line {line_number}: invalid GFF3") from exc
        if feature.featuretype == "region" and feature.seqid == REFERENCE_ACCESSION:
            region = feature
        tag = _attribute(feature, "locus_tag")
        if tag and feature.featuretype == "CDS":
            product = _attribute(feature, "product")
            if product:
                products.setdefault(tag, product)
        if not (tag and feature.featuretype == "gene"):
            continue
        if _attribute(feature, "gene_biotype") != GENE_TYPE:
            continue
        if feature.start is None or feature.end is None:
            raise DataValidationError(f"{tag}: missing gene coordinates")
        if feature.seqid != REFERENCE_ACCESSION:
            raise DataValidationError(
                f"{tag}: expected reference {REFERENCE_ACCESSION}"
            )
        name = _attribute(feature, "Name")
        symbol = _attribute(feature, "gene") or name
        records.append(
            GeneRecord(
                b_number=tag,
                symbol=symbol,
                name=name if name != symbol else None,
                start=feature.start,
                end=feature.end,
                strand=feature.strand,
                ncbi_gene_id=_attribute(feature, "Dbxref", "GeneID:"),
                ecocyc_id=_attribute(feature, "Dbxref", "ECOCYC:"),
            ).model_dump()
        )

    metadata = _validate_reference(directives, region)
    if not records:
        raise DataValidationError("canonical gene table is empty")
    genes = pd.DataFrame(records).set_index("b_number")
    genes["description"] = [products.get(tag) for tag in genes.index]
    if not genes.index.is_unique:
        raise DataValidationError("duplicate canonical IDs in GFF3")
    genes = genes.sort_index()
    genes.attrs["reference"] = metadata
    return genes, metadata


def _validate_reference(
    directives: dict[str, str], region: Feature | None
) -> dict[str, str | None]:
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
    return {
        "assembly_accession": accession,
        "assembly_name": name,
        "reference_accession": REFERENCE_ACCESSION,
        "taxonomy_id": TAXONOMY_ID,
        "annotation_date": directives.get("annotation-date"),
        "annotation_source": directives.get("annotation-source"),
    }


def _attribute(feature: Feature, key: str, prefix: str = "") -> str | None:
    values = {
        value.removeprefix(prefix)
        for value in feature.attributes.get(key, [])
        if value.startswith(prefix)
    }
    if len(values) > 1:
        raise DataValidationError(
            f"conflicting GFF3 attribute {key}/{prefix}: {sorted(values)}"
        )
    return next(iter(values), None)
