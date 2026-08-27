"""Strict NCBI GFF3 adapter for the canonical MG1655 gene universe."""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

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


@dataclass(frozen=True, slots=True)
class GffMetadata:
    """Reference identity and annotation version recorded in an NCBI GFF3."""

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


@dataclass(frozen=True, slots=True)
class _Feature:
    seqid: str
    feature_type: str
    start: int
    end: int
    strand: str
    attributes: dict[str, str]


def parse_ncbi_gff(path: str | Path) -> ParsedGff:
    """Parse a frozen NCBI MG1655 GFF3 and reject reference drift."""

    directives: dict[str, str] = {}
    genes: dict[str, _Feature] = {}
    products: dict[str, list[tuple[str | None, str | None]]] = {}
    region: _Feature | None = None

    for line_number, line in enumerate(_text_lines(Path(path)), start=1):
        line = line.rstrip("\n")
        if not line:
            continue
        if line.startswith("#!"):
            key, separator, value = line[2:].partition(" ")
            if separator:
                directives[key] = value.strip()
            continue
        if line.startswith("#"):
            continue
        feature = _parse_feature(line, line_number)
        if feature.feature_type == "region":
            if feature.seqid == REFERENCE_ACCESSION:
                region = feature
            continue
        locus_tag = feature.attributes.get("locus_tag")
        if locus_tag is None:
            continue
        if feature.feature_type == "gene":
            if feature.attributes.get("gene_biotype") != GENE_TYPE:
                continue
            if locus_tag in genes:
                raise DataValidationError(
                    f"line {line_number}: duplicate canonical ID {locus_tag}"
                )
            genes[locus_tag] = feature
        elif feature.feature_type == "CDS":
            candidate = (
                feature.attributes.get("product"),
                feature.attributes.get("protein_id"),
            )
            candidates = products.setdefault(locus_tag, [])
            if candidate not in candidates:
                candidates.append(candidate)

    metadata = _validate_reference(directives, region)
    records = [
        _gene_record(tag, feature, products.get(tag)) for tag, feature in genes.items()
    ]
    return ParsedGff(registry=GeneRegistry(records), metadata=metadata)


def _parse_feature(line: str, line_number: int) -> _Feature:
    fields = line.split("\t")
    if len(fields) != 9:
        raise DataValidationError(
            f"line {line_number}: expected 9 GFF3 fields, got {len(fields)}"
        )
    try:
        start = int(fields[3])
        end = int(fields[4])
    except ValueError as exc:
        raise DataValidationError(
            f"line {line_number}: non-integer coordinates"
        ) from exc
    return _Feature(
        seqid=fields[0],
        feature_type=fields[2],
        start=start,
        end=end,
        strand=fields[6],
        attributes=_parse_attributes(fields[8], line_number),
    )


def _parse_attributes(raw: str, line_number: int) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for item in raw.split(";"):
        if not item:
            continue
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise DataValidationError(
                f"line {line_number}: malformed GFF3 attribute {item!r}"
            )
        if key in attributes and attributes[key] != value:
            raise DataValidationError(
                f"line {line_number}: conflicting attribute {key!r}"
            )
        attributes[key] = unquote(value)
    return attributes


def _validate_reference(
    directives: dict[str, str], region: _Feature | None
) -> GffMetadata:
    raw_accession = directives.get("genome-build-accession", "")
    assembly_accession = raw_accession.removeprefix("NCBI_Assembly:")
    if assembly_accession != ASSEMBLY_ACCESSION:
        raise DataValidationError(
            f"expected assembly {ASSEMBLY_ACCESSION}, got {assembly_accession!r}"
        )
    assembly_name = directives.get("genome-build", "")
    if assembly_name != ASSEMBLY_NAME:
        raise DataValidationError(
            f"expected assembly name {ASSEMBLY_NAME}, got {assembly_name!r}"
        )
    if region is None or region.seqid != REFERENCE_ACCESSION:
        raise DataValidationError(
            f"expected reference chromosome {REFERENCE_ACCESSION}"
        )
    expected_region = {
        "strain": STRAIN,
        "substrain": SUBSTRAIN,
        "Dbxref": f"taxon:{TAXONOMY_ID}",
    }
    for key, expected in expected_region.items():
        actual = region.attributes.get(key, "")
        if key == "Dbxref":
            matches = expected in actual.split(",")
        else:
            matches = actual == expected
        if not matches:
            raise DataValidationError(
                f"reference region has {key}={actual!r}; expected {expected!r}"
            )
    return GffMetadata(
        assembly_accession=assembly_accession,
        assembly_name=assembly_name,
        reference_accession=region.seqid,
        taxonomy_id=TAXONOMY_ID,
        annotation_date=directives.get("annotation-date"),
        annotation_source=directives.get("annotation-source"),
    )


def _gene_record(
    b_number: str,
    feature: _Feature,
    products: list[tuple[str | None, str | None]] | None,
) -> GeneRecord:
    crossrefs = _crossrefs(feature.attributes.get("Dbxref", ""))
    products = products or []
    descriptions = tuple(
        description for description, _protein_id in products if description is not None
    )
    protein_ids = tuple(
        protein_id for _description, protein_id in products if protein_id is not None
    )
    description = descriptions[0] if descriptions else None
    protein_id = protein_ids[0] if protein_ids else None
    symbol = feature.attributes.get("gene") or feature.attributes.get("Name")
    display_name = feature.attributes.get("Name")
    if display_name == symbol:
        display_name = None
    return GeneRecord(
        b_number=b_number,
        symbol=symbol,
        name=display_name,
        description=description,
        product_descriptions=descriptions,
        gene_type=feature.attributes["gene_biotype"],
        reference_accession=feature.seqid,
        start=feature.start,
        end=feature.end,
        strand=feature.strand,
        protein_id=protein_id,
        protein_ids=protein_ids,
        ncbi_gene_id=_one_crossref(b_number, crossrefs, "GeneID"),
        ecocyc_id=_one_crossref(b_number, crossrefs, "ECOCYC"),
    )


def _crossrefs(raw: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in raw.split(","):
        if not item:
            continue
        namespace, separator, identifier = item.partition(":")
        if not separator:
            continue
        result.setdefault(namespace, []).append(identifier)
    return result


def _one_crossref(
    b_number: str, crossrefs: dict[str, list[str]], namespace: str
) -> str | None:
    values = sorted(set(crossrefs.get(namespace, ())))
    if len(values) > 1:
        raise DataValidationError(
            f"{b_number}: multiple {namespace} identifiers: {values}"
        )
    return values[0] if values else None


def _text_lines(path: Path) -> Iterator[str]:
    with path.open("rb") as raw_handle:
        is_gzip = raw_handle.read(2) == b"\x1f\x8b"
    if is_gzip:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            yield from handle
    else:
        with path.open(encoding="utf-8") as handle:
            yield from handle
