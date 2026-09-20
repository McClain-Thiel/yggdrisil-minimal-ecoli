from __future__ import annotations

import json

import pytest

from yggdrisil_ecoli.analysis import score_rediscovery
from yggdrisil_ecoli.data.reduced_genomes import (
    Interval,
    _ms56_ids_from_pages,
    deletion_intervals_from_sam,
    genes_in_intervals,
    load_ncbi_sequence,
)
from yggdrisil_ecoli.data.registry import GeneRecord, GeneRegistry


def _gene(b_number: str, start: int, end: int) -> GeneRecord:
    return GeneRecord(
        b_number=b_number,
        symbol=None,
        name=None,
        description=None,
        start=start,
        end=end,
        strand="+",
        ncbi_gene_id=None,
        ecocyc_id=None,
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
    registry = GeneRegistry(
        [_gene("b0001", 50, 100), _gene("b0002", 100, 150), _gene("b0003", 151, 200)]
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
    }
    assert scores["MS56"]["published_deletion_gene_precision"] == 2 / 3
    assert scores["MS56"]["published_deletion_interval_recall"] is None
