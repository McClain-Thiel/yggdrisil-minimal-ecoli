"""Build held-out reduced-genome labels from primary source artifacts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import pysam
from pypdf import PdfReader

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
    # Bound the extraction by table headings, including a following table on the
    # same page. Only the first (locus-tag) column is a deletion label.
    text = "\n".join(pages)
    heading = re.search(r"\s+".join(MS56_TABLE_TITLE.split()), text)
    if heading is None:
        raise ValueError("MS56 Supplementary Table S3 was not found")
    table = re.split(r"\bTable\s+S(?!3\b)\d+\b", text[heading.end() :], maxsplit=1)[0]
    ids = re.findall(r"^\s*(b\d{4})\b", table, flags=re.MULTILINE)
    if not ids:
        raise ValueError("MS56 Supplementary Table S3 was not found")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate MS56 locus tag")
    return frozenset(ids)


def deletion_intervals_from_sam(
    path: Path, reference_length: int, *, minimum_length: int = MIN_DELETION_BP
) -> tuple[Interval, ...]:
    """Map primary aligned blocks to 1-based, inclusive reference deletions."""

    with pysam.AlignmentFile(str(path), "r") as alignment:
        covered = sorted(
            block
            for record in alignment
            if not record.is_unmapped and not record.is_secondary
            for block in record.get_blocks()
        )
    if not covered:
        raise ValueError("minimap2 produced no primary aligned reference blocks")
    # Union coverage while taking its complement; overlapping blocks and
    # supplementary alignments must not create additional deletions.
    gaps = []
    cursor = 0
    for start, end in covered:
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


def _run_minimap2(reference: str, query: str) -> tuple[tuple[Interval, ...], str]:
    executable = shutil.which("minimap2")
    if executable is None:
        raise RuntimeError("minimap2 is required to derive MDS42 deletion intervals")
    aligner_version = subprocess.check_output(
        [executable, "--version"], text=True
    ).strip()
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        reference_path, query_path = directory / "reference.fa", directory / "query.fa"
        reference_path.write_text(f">{REFERENCE_ACCESSION}\n{reference}\n")
        query_path.write_text(f">{MDS42_ACCESSION}\n{query}\n")
        sam_path = directory / "alignment.sam"
        with sam_path.open("w") as output:
            subprocess.run(
                [executable, "-a", "-x", "asm5", str(reference_path), str(query_path)],
                stdout=output,
                check=True,
                capture_output=False,
            )
        intervals = deletion_intervals_from_sam(sam_path, len(reference))
    return intervals, aligner_version


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
    intervals, minimap2_version = _run_minimap2(reference, mds42)
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
                    "aligner_parameters": ["-a", "-x", "asm5"],
                    "alignment_reader": f"pysam {version('pysam')}",
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
