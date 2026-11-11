"""Build held-out reduced-genome labels from primary source artifacts."""

from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pandas as pd
import pysam
from pypdf import PdfReader

from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.data.io import file_sha256

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
    registry: pd.DataFrame,
    intervals: tuple[Interval, ...],
    *,
    fully_contained: bool = False,
) -> frozenset[str]:
    """Map touched genes, or only genes wholly removed by an interval."""

    touched = pd.Series(False, index=registry.index)
    for interval in intervals:
        if fully_contained:
            touched |= registry.start.ge(interval.start) & registry.end.le(interval.end)
        else:
            touched |= registry.start.le(interval.end) & registry.end.ge(interval.start)
    return frozenset(registry.index[touched])


def uncovered_intervals(
    blocks: list[tuple[int, int]], length: int
) -> tuple[Interval, ...]:
    """Complement zero-based aligned blocks, including both sequence ends."""
    if not blocks or length < 1:
        raise ValueError("Expected aligned blocks and a positive reference length")
    gaps = []
    cursor = 0
    for start, end in sorted(blocks):
        if not 0 <= start < end <= length:
            raise ValueError(f"Aligned block outside reference: {(start, end)}")
        if start > cursor:
            gaps.append(Interval(cursor + 1, start))
        cursor = max(cursor, end)
    if cursor < length:
        gaps.append(Interval(cursor + 1, length))
    return tuple(gaps)


def align_genome(reference: str, query: str) -> dict[str, Any]:
    """Project large reference losses without treating inversions as deletions."""
    import mappy

    if not reference or not query or (set(reference + query) - set("ACGTN")):
        raise ValueError("Expected nonempty uppercase DNA sequences")
    aligner = mappy.Aligner(seq=reference, preset="asm5", best_n=100, n_threads=1)
    if not aligner:
        raise RuntimeError("minimap2 could not index the reference")
    hits = [hit for hit in aligner.map(query) if hit.is_primary]
    if not hits or any(hit.mapq < 20 for hit in hits):
        raise ValueError("Missing or ambiguous primary assembly alignments")
    blocks, query_blocks, records = [], [], []
    for hit in hits:
        cursor = hit.r_st
        for size, operation in hit.cigar:
            if operation in {0, 7, 8}:
                blocks.append((cursor, cursor + size))
                cursor += size
            elif operation == 2:
                cursor += size
            elif operation != 1:
                raise ValueError(f"Unexpected DNA CIGAR operation: {operation}")
        if cursor != hit.r_en:
            raise ValueError("CIGAR and reference coordinates disagree")
        query_blocks.append((hit.q_st, hit.q_en))
        records.append(
            {
                "reference_start": hit.r_st,
                "reference_end": hit.r_en,
                "query_start": hit.q_st,
                "query_end": hit.q_en,
                "strand": hit.strand,
                "mapq": hit.mapq,
                "edit_distance": hit.NM,
                "cigar": hit.cigar_str,
            }
        )
    unaligned_query = uncovered_intervals(query_blocks, len(query))
    query_coverage = 1 - sum(x.length for x in unaligned_query) / len(query)
    if query_coverage < 0.98:
        raise ValueError(f"Insufficient assembly alignment coverage: {query_coverage}")
    gaps = uncovered_intervals(blocks, len(reference))
    return {
        "method": "whole_genome_alignment",
        "aligner": "minimap2/mappy",
        "aligner_version": version("mappy"),
        "preset": "asm5",
        "minimum_mapq": 20,
        "minimum_deletion_bp": MIN_DELETION_BP,
        "query_aligned_span_fraction": query_coverage,
        "unmapped_query_bp": sum(x.length for x in unaligned_query),
        "alignments": records,
        "deletion_intervals": [
            {"start": x.start, "end": x.end, "length_bp": x.length}
            for x in gaps
            if x.length >= MIN_DELETION_BP
        ],
        "smaller_reference_gaps": [
            {"start": x.start, "end": x.end, "length_bp": x.length}
            for x in gaps
            if x.length < MIN_DELETION_BP
        ],
    }


def read_annotated_genome(path: Path, accession: str) -> tuple[str, dict[str, Any]]:
    """Read one pinned annotated record, checking its accession and actual size."""
    from Bio import SeqIO

    opener = gzip.open if path.suffix == ".gz" else open
    format_name = "genbank" if ".gbff" in path.name else "embl"
    with opener(path, "rt") as handle:
        record = SeqIO.read(handle, format_name)  # type: ignore[no-untyped-call]
    if record.id != accession:
        raise ValueError(f"{path}: expected {accession}, found {record.id}")
    return str(record.seq).upper(), {
        "accession": record.id,
        "sequence_length_bp": len(record.seq),
        "annotated_cds": sum(f.type == "CDS" for f in record.features),
        "annotated_pseudo_cds": sum(
            f.type == "CDS"
            and ("pseudo" in f.qualifiers or "pseudogene" in f.qualifiers)
            for f in record.features
        ),
        "source_sha256": file_sha256(path),
    }


def deletion_reachability(starting: set[str], target: set[str]) -> dict[str, object]:
    """Only compare deletion projections; strain alleles remain a separate issue."""
    restore = sorted(starting - target)
    return {
        "deletion_projection_reachable": not restore,
        "genes_requiring_restoration": restore,
        "restoration_count": len(restore),
        "additional_gene_deletions": sorted(target - starting),
        "additional_deletion_count": len(target - starting),
    }


def build_benchmark_catalog(
    *, genes_path: Path, source_manifest: Path, source_dir: Path
) -> dict[str, Any]:
    """Build explicitly limited MG1655 projections from frozen public records."""
    specifications = json.loads(source_manifest.read_text())
    registry = load_genes(genes_path)
    universe = set(registry.index)
    reference_spec = specifications["reference"]

    def sequence(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        path = source_dir / spec["filename"]
        if file_sha256(path) != spec["sha256"]:
            raise ValueError(f"Source checksum changed: {path}")
        dna, annotation = read_annotated_genome(path, spec["accession"])
        if len(dna) != spec["sequence_length_bp"]:
            raise ValueError(f"Published sequence length differs: {path}")
        return dna, annotation

    reference, annotation = sequence(reference_spec)
    strains: dict[str, Any] = {
        "MG1655": {
            **annotation,
            "source": reference_spec,
            "deleted_gene_ids": [],
            "deletion_intervals": [],
            "partially_deleted_gene_ids": [],
            "representation": "reference control",
        }
    }
    ms56_spec = specifications["ms56"]
    ms56_path = source_dir / ms56_spec["filename"]
    if file_sha256(ms56_path) != ms56_spec["sha256"]:
        raise ValueError("MS56 source checksum changed")
    published = extract_ms56_gene_ids(ms56_path)
    if len(published) != ms56_spec["published_locus_tag_count"]:
        raise ValueError("MS56 deleted-locus table count changed")
    strains["MS56"] = {
        "source": ms56_spec,
        "deleted_gene_ids": sorted(published & universe),
        "published_ids_outside_search_universe": sorted(published - universe),
        "published_locus_tag_count": len(published),
        "deletion_intervals": [],
        "partially_deleted_gene_ids": [],
        "sequence_length_bp": None,
        "representation": "published locus-tag projection; sequence not reconstructed",
        "native_genotype_fully_represented": False,
    }
    for name, spec in specifications["genomes"].items():
        dna, annotation = sequence(spec)
        alignment = align_genome(reference, dna)
        intervals = tuple(
            Interval(item["start"], item["end"])
            for item in alignment["deletion_intervals"]
        )
        complete = genes_in_intervals(registry, intervals, fully_contained=True)
        touched = genes_in_intervals(registry, intervals)
        region_labels = []
        for interval in intervals:
            region = _interval_payload(interval, registry)
            region_genes = genes_in_intervals(registry, (interval,))
            region_labels.append(
                {
                    **region,
                    "gene_ids": sorted(region_genes & complete),
                    "partially_deleted_gene_ids": sorted(region_genes - complete),
                }
            )
        strains[name] = {
            **annotation,
            "source": spec,
            "deleted_gene_ids": sorted(complete),
            "partially_deleted_gene_ids": sorted(touched - complete),
            "deletion_intervals": region_labels,
            "alignment": alignment,
            "representation": "MG1655 protein-coding deletion projection",
            "native_genotype_fully_represented": False,
        }
    for strain in strains.values():
        strain["protein_coding_targets"] = len(strain["deleted_gene_ids"])
        strain["protein_coding_genes_untargeted"] = len(registry) - len(
            strain["deleted_gene_ids"]
        )
    parent_absences = set(strains["W3110"]["deleted_gene_ids"])
    strains["DGF298"]["absences_shared_with_parent_projection"] = sorted(
        set(strains["DGF298"]["deleted_gene_ids"]) & parent_absences
    )
    return {
        "schema_version": 2,
        "agent_visible": False,
        "purpose": "published-genome reconstruction and evaluator diagnostics",
        "reference": {
            "accession": reference_spec["accession"],
            "sequence_length_bp": len(reference),
            "genes_sha256": file_sha256(genes_path),
            "sequence_artifact_sha256": strains["MG1655"]["source_sha256"],
            "protein_coding_search_universe": len(registry),
        },
        "source_manifest_sha256": file_sha256(source_manifest),
        "projection_rule": (
            "Use only coding genes fully contained in reference gaps of at least "
            "1000 bp. Keep partial genes separate. MS56 uses the published locus table."
        ),
        "limitations": [
            "Reference-relative absences can include parental strain differences.",
            "Substitutions, small indels, partial gene disruptions and regulatory changes are not encoded as deletion actions.",
            "Deposited sequence lengths describe the published strains, not physically shortened simulator chromosomes.",
            "Assembly alignment, including inversions and junction repeats, does not identify every engineered deletion boundary exactly.",
        ],
        "strains": strains,
    }


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


def _interval_payload(interval: Interval, registry: pd.DataFrame) -> dict[str, object]:
    return {
        "start": interval.start,
        "end": interval.end,
        "length_bp": interval.length,
        "gene_ids": sorted(genes_in_intervals(registry, (interval,))),
    }


def build_validation(
    *,
    genes_path: Path,
    reference_path: Path,
    mds42_path: Path,
    ms56_pdf_path: Path,
) -> dict[str, object]:
    """Build the complete held-out validation payload."""

    registry = load_genes(genes_path)
    reference = load_ncbi_sequence(reference_path, REFERENCE_ACCESSION)
    mds42 = load_ncbi_sequence(mds42_path, MDS42_ACCESSION)
    intervals, minimap2_version = _run_minimap2(reference, mds42)
    mds42_genes = genes_in_intervals(registry, intervals)
    ms56_published = extract_ms56_gene_ids(ms56_pdf_path)
    ms56_genes = ms56_published & set(registry.index)
    return {
        "schema_version": 1,
        "agent_visible": False,
        "reference": {
            "accession": REFERENCE_ACCESSION,
            "genes_sha256": file_sha256(genes_path),
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
                    ms56_published - set(registry.index)
                ),
                "deletion_intervals": [],
            },
        },
    }
