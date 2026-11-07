from __future__ import annotations

import json
import random

import pandas as pd
import pytest

from yggdrisil_ecoli.analysis import score_rediscovery
from yggdrisil_ecoli.data.reduced_genomes import (
    Interval,
    _ms56_ids_from_pages,
    align_genome,
    deletion_intervals_from_sam,
    deletion_reachability,
    genes_in_intervals,
    load_ncbi_sequence,
    uncovered_intervals,
)


def test_load_ncbi_sequence_checks_wrapper_metadata(tmp_path) -> None:
    path = tmp_path / "sequence.json"
    path.write_text(
        json.dumps(
            [
                {
                    "accession": "AP012306",
                    "header": ">AP012306.1 test",
                    "sequence": "ACGTN",
                    "length": 5,
                }
            ]
        )
    )

    assert load_ncbi_sequence(path, "AP012306") == "ACGTN"

    with pytest.raises(ValueError, match="expected accession"):
        load_ncbi_sequence(path, "NC_000913.3")


def test_sam_large_reference_gaps_become_one_based_intervals(tmp_path) -> None:
    path = tmp_path / "alignment.sam"
    path.write_text(
        "@HD\tVN:1.6\n@SQ\tSN:reference\tLN:10000\n"
        "query\t0\treference\t1\t60\t100M1500D200M2500D5700M\t*\t0\t0\t*\t*\n"
    )
    assert deletion_intervals_from_sam(path, 10_000) == (
        Interval(101, 1600),
        Interval(1801, 4300),
    )


@pytest.mark.parametrize("separator", ["\n", "\f"])
def test_ms56_parser_stops_at_next_table(separator) -> None:
    pages = [
        "Table S3 Descriptions of the deleted genes in MS56\nb0016 first\n"
        + separator
        + "Table S4 Other measurements\nb0257 other\n"
    ]
    assert _ms56_ids_from_pages(pages) == {"b0016"}


def test_gene_interval_mapping_includes_boundary_overlap() -> None:
    registry = pd.DataFrame(
        {"start": [50, 100, 151], "end": [100, 150, 200]},
        index=pd.Index(["b0001", "b0002", "b0003"], name="b_number"),
    )

    assert genes_in_intervals(registry, (Interval(100, 120),)) == {
        "b0001",
        "b0002",
    }


def test_ms56_parser_uses_only_first_locus_tag_column() -> None:
    pages = [
        "cover page",
        "Table S3 Descriptions of the deleted genes in MS56\n"
        "Locus_Tag gene function\n"
        "b0016 yi81_1 mobile element\n"
        "b0257 b0257 unknown\n",
        "b0301 ykgB unknown\n",
    ]

    assert _ms56_ids_from_pages(pages) == {"b0016", "b0257", "b0301"}


def test_posthoc_rediscovery_metrics_keep_truth_outside_search() -> None:
    validation = {
        "strains": {
            "MDS42": {
                "deleted_gene_ids": ["b0001", "b0002", "b0003"],
                "deletion_intervals": [
                    {"gene_ids": ["b0001", "b0002"]},
                    {"gene_ids": ["b0003"]},
                ],
            },
            "MS56": {
                "deleted_gene_ids": ["b0002", "b0004"],
                "deletion_intervals": [],
            },
        }
    }

    scores = score_rediscovery({"b0001", "b0002", "b0004"}, validation)

    assert scores["MDS42"] == {
        "published_deleted_genes": 3,
        "candidate_genes": 3,
        "overlap_genes": 2,
        "overlap_gene_ids": ["b0001", "b0002"],
        "published_deletion_gene_precision": 2 / 3,
        "published_deletion_gene_recall": 2 / 3,
        "published_deletion_gene_jaccard": 1 / 2,
        "published_intervals_with_search_genes": 2,
        "published_intervals_hit": 1,
        "published_deletion_interval_recall": 1 / 2,
        "published_intervals_fully_recovered": 1,
        "published_deletion_interval_complete_recall": 1 / 2,
    }
    assert scores["MS56"]["published_deletion_gene_precision"] == 2 / 3
    assert scores["MS56"]["published_deletion_interval_recall"] is None


def test_partial_gene_loss_is_not_silently_a_full_knockout() -> None:
    registry = pd.DataFrame(
        {"start": [50, 110, 151], "end": [105, 140, 200]},
        index=pd.Index(["b0001", "b0002", "b0003"], name="b_number"),
    )
    assert genes_in_intervals(
        registry, (Interval(100, 150),), fully_contained=True
    ) == {"b0002"}


def test_reference_gaps_union_overlapping_and_reversed_alignment_blocks() -> None:
    assert uncovered_intervals([(80, 100), (0, 20), (10, 30)], 110) == (
        Interval(31, 80),
        Interval(101, 110),
    )
    with pytest.raises(ValueError, match="outside reference"):
        uncovered_intervals([(0, 120)], 110)


def test_monotonic_reachability_requires_restoration_for_non_nested_strains() -> None:
    result = deletion_reachability({"b0001", "b0002"}, {"b0002", "b0003"})
    assert result["deletion_projection_reachable"] is False
    assert result["genes_requiring_restoration"] == ["b0001"]
    assert result["additional_gene_deletions"] == ["b0003"]
    assert (
        deletion_reachability(set(), {"b0001"})["deletion_projection_reachable"] is True
    )


def test_complete_region_recovery_requires_more_than_one_gene_per_region() -> None:
    validation = {
        "strains": {
            "test": {
                "deleted_gene_ids": ["b0001", "b0002"],
                "deletion_intervals": [{"gene_ids": ["b0001", "b0002"]}],
            }
        }
    }
    result = score_rediscovery({"b0001"}, validation)["test"]
    assert result["published_deletion_interval_recall"] == 1
    assert result["published_deletion_interval_complete_recall"] == 0


def test_assembly_projection_handles_an_inverted_reduced_chromosome() -> None:
    mappy = pytest.importorskip("mappy")
    rng = random.Random(17)
    reference = "".join(rng.choices("ACGT", k=30_000))
    query = mappy.revcomp(reference[:8000] + reference[13000:])
    result = align_genome(reference, query)
    assert result["query_aligned_span_fraction"] > 0.99
    assert any(a["strand"] == -1 for a in result["alignments"])
    assert any(
        gap["start"] <= 9000 < gap["end"] for gap in result["deletion_intervals"]
    )
    assert sum(gap["length_bp"] for gap in result["deletion_intervals"]) == 5000
