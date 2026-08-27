#!/usr/bin/env python3
"""Build held-out reduced-genome labels from primary source artifacts."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.registry import GeneRecord, GeneRegistry, file_sha256

MDS42_ACCESSION = "AP012306"
REFERENCE_ACCESSION = "NC_000913.3"
MS56_TABLE_TITLE = "Table S3 Descriptions of the deleted genes in MS56"
MS56_SOURCE_URL = (
    "https://media.springernature.com/original/springer-static/esm/"
    "art%3A10.1007%2Fs00253-014-5739-y/MediaObjects/"
    "253_2014_5739_MOESM1_ESM.pdf"
)
MIN_DELETION_BP = 1_000
_CIGAR_OPERATION = re.compile(r"(\d+)([MIDNSHP=X])")
_B_NUMBER_ROW = re.compile(r"^\s*(b\d{4})\b")


@dataclass(frozen=True, slots=True)
class Interval:
    """One 1-based, inclusive deletion interval on NC_000913.3."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def load_ncbi_sequence(path: Path, accession: str) -> str:
    """Load one nucleotide record emitted by the approved NCBI wrapper."""

    payload = json.loads(path.read_text())
    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError(f"{path}: expected exactly one NCBI sequence record")
    record = payload[0]
    if not isinstance(record, dict) or record.get("accession") != accession:
        raise ValueError(f"{path}: expected accession {accession}")
    sequence = record.get("sequence")
    if not isinstance(sequence, str) or not sequence:
        raise ValueError(f"{path}: missing nucleotide sequence")
    sequence = sequence.upper()
    if set(sequence) - {"A", "C", "G", "T", "N"}:
        raise ValueError(f"{path}: sequence contains non-nucleotide characters")
    if record.get("length") != len(sequence):
        raise ValueError(f"{path}: sequence length does not match wrapper metadata")
    return sequence


def extract_ms56_gene_ids(path: Path) -> frozenset[str]:
    """Extract the locus-tag column of Park et al. Supplementary Table S3."""

    return _ms56_ids_from_pages(
        page.extract_text() or "" for page in PdfReader(path).pages
    )


def _ms56_ids_from_pages(pages: Iterable[str]) -> frozenset[str]:
    gene_ids: set[str] = set()
    table_started = False
    for text in pages:
        if MS56_TABLE_TITLE in " ".join(text.split()):
            table_started = True
        if not table_started:
            continue
        for line in text.splitlines():
            match = _B_NUMBER_ROW.match(line)
            if match is not None:
                if match.group(1) in gene_ids:
                    raise ValueError(f"duplicate MS56 locus tag: {match.group(1)}")
                gene_ids.add(match.group(1))
    if not table_started or not gene_ids:
        raise ValueError("MS56 Supplementary Table S3 was not found")
    return frozenset(gene_ids)


def deletion_intervals_from_paf(
    paf: str, reference_length: int, *, minimum_length: int = MIN_DELETION_BP
) -> tuple[Interval, ...]:
    """Return large uncovered reference intervals from primary PAF alignments."""

    covered: list[tuple[int, int]] = []
    for line in paf.splitlines():
        fields = line.split("\t")
        if len(fields) < 12 or "tp:A:P" not in fields[12:]:
            continue
        cigar = next(
            (
                field.removeprefix("cg:Z:")
                for field in fields[12:]
                if field.startswith("cg:Z:")
            ),
            None,
        )
        if cigar is None:
            raise ValueError("primary minimap2 alignment is missing a CIGAR")
        target_position = int(fields[7])
        operations = _CIGAR_OPERATION.findall(cigar)
        if "".join(f"{length}{code}" for length, code in operations) != cigar:
            raise ValueError("unrecognized minimap2 CIGAR operation")
        for raw_length, code in operations:
            length = int(raw_length)
            if code in {"M", "=", "X"}:
                covered.append((target_position, target_position + length))
                target_position += length
            elif code in {"D", "N"}:
                target_position += length
            elif code not in {"I", "S", "H", "P"}:
                raise ValueError(f"unsupported CIGAR operation: {code}")
        if target_position != int(fields[8]):
            raise ValueError("CIGAR target span does not match PAF coordinates")
    if not covered:
        raise ValueError("minimap2 produced no primary aligned reference blocks")

    merged: list[list[int]] = []
    for start, end in sorted(covered):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    gaps: list[Interval] = []
    cursor = 0
    for start, end in merged:
        if start - cursor >= minimum_length:
            gaps.append(Interval(cursor + 1, start))
        cursor = max(cursor, end)
    if reference_length - cursor >= minimum_length:
        gaps.append(Interval(cursor + 1, reference_length))
    return tuple(gaps)


def genes_in_intervals(
    registry: GeneRegistry, intervals: tuple[Interval, ...]
) -> frozenset[str]:
    """Map any protein-coding reference gene touched by a deletion interval."""

    return frozenset(
        gene.b_number
        for gene in registry
        if any(_overlaps(gene, interval) for interval in intervals)
    )


def _overlaps(gene: GeneRecord, interval: Interval) -> bool:
    return gene.start <= interval.end and interval.start <= gene.end


def _run_minimap2(reference: str, query: str) -> tuple[str, str]:
    executable = shutil.which("minimap2")
    if executable is None:
        raise RuntimeError("minimap2 is required to derive MDS42 deletion intervals")
    version = subprocess.run(
        [executable, "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="yggdrisil-ecoli-validation-") as raw:
        temporary = Path(raw)
        reference_path = temporary / "reference.fasta"
        query_path = temporary / "query.fasta"
        reference_path.write_text(f">{REFERENCE_ACCESSION}\n{reference}\n")
        query_path.write_text(f">{MDS42_ACCESSION}\n{query}\n")
        result = subprocess.run(
            [executable, "-x", "asm5", "-c", str(reference_path), str(query_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    return result.stdout, version


def _interval_payload(interval: Interval, registry: GeneRegistry) -> dict[str, object]:
    return {
        "start": interval.start,
        "end": interval.end,
        "length_bp": interval.length,
        "gene_ids": sorted(
            gene.b_number for gene in registry if _overlaps(gene, interval)
        ),
    }


def build_validation(
    *,
    registry_path: Path,
    reference_path: Path,
    mds42_path: Path,
    ms56_pdf_path: Path,
) -> dict[str, object]:
    """Build the complete held-out validation payload."""

    registry = GeneRegistry.from_parquet(registry_path)
    reference = load_ncbi_sequence(reference_path, REFERENCE_ACCESSION)
    mds42 = load_ncbi_sequence(mds42_path, MDS42_ACCESSION)
    paf, minimap2_version = _run_minimap2(reference, mds42)
    intervals = deletion_intervals_from_paf(paf, len(reference))
    mds42_genes = genes_in_intervals(registry, intervals)
    ms56_published = extract_ms56_gene_ids(ms56_pdf_path)
    ms56_genes = ms56_published & registry.search_universe
    return {
        "schema_version": 1,
        "agent_visible": False,
        "reference": {
            "accession": REFERENCE_ACCESSION,
            "registry_sha256": file_sha256(registry_path),
            "sequence_artifact_sha256": file_sha256(reference_path),
        },
        "strains": {
            "MDS42": {
                "source": {
                    "method": "whole_genome_alignment",
                    "accession": MDS42_ACCESSION,
                    "sequence_artifact_sha256": file_sha256(mds42_path),
                    "aligner": "minimap2",
                    "aligner_version": minimap2_version,
                    "aligner_parameters": ["-x", "asm5", "-c"],
                    "minimum_deletion_bp": MIN_DELETION_BP,
                },
                "deleted_gene_ids": sorted(mds42_genes),
                "deletion_intervals": [
                    _interval_payload(interval, registry) for interval in intervals
                ],
                "counts": {
                    "deleted_genes_in_search_universe": len(mds42_genes),
                    "deletion_intervals": len(intervals),
                    "deleted_bp": sum(interval.length for interval in intervals),
                },
            },
            "MS56": {
                "source": {
                    "method": "published_locus_tag_table",
                    "url": MS56_SOURCE_URL,
                    "doi": "10.1007/s00253-014-5739-y",
                    "table": "Supplementary Table S3",
                    "pdf_sha256": file_sha256(ms56_pdf_path),
                },
                "deleted_gene_ids": sorted(ms56_genes),
                "published_ids_outside_search_universe": sorted(
                    ms56_published - registry.search_universe
                ),
                "deletion_intervals": [],
                "counts": {
                    "published_locus_tags": len(ms56_published),
                    "deleted_genes_in_search_universe": len(ms56_genes),
                    "published_ids_outside_search_universe": len(
                        ms56_published - registry.search_universe
                    ),
                },
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry", type=Path, default=Path("data/processed/gene_registry.parquet")
    )
    parser.add_argument(
        "--reference", type=Path, default=Path("data/validation/NC_000913.3.ncbi.json")
    )
    parser.add_argument(
        "--mds42", type=Path, default=Path("data/validation/AP012306.ncbi.json")
    )
    parser.add_argument(
        "--ms56-pdf",
        type=Path,
        default=Path("data/validation/MS56_Park_2014_supplement.pdf"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/validation/reduced_genomes.json"),
    )
    args = parser.parse_args()
    payload = build_validation(
        registry_path=args.registry,
        reference_path=args.reference,
        mds42_path=args.mds42,
        ms56_pdf_path=args.ms56_pdf,
    )
    atomic_json(args.output, payload)
    print(f"Wrote held-out validation labels to {args.output}")


if __name__ == "__main__":
    main()
