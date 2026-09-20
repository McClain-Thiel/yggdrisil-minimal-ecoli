from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from pydantic import ValidationError

from yggdrisil_ecoli.data.audit import audit_registry
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.evidence import load_genes, validate_genes, write_genes


def test_one_table_round_trip_preserves_fields_metadata_and_kos(
    tmp_path: Path, genes: pd.DataFrame
) -> None:
    genes.at["b0001", "ko_ids"] = ("K00002", "K00001", "K00002")
    genes.attrs["fixture"] = {"provenance": ["synthetic annotations"]}
    expected = validate_genes(genes)
    path = tmp_path / "genes.parquet"

    write_genes(genes, path)
    actual = load_genes(path)

    assert_frame_equal(actual, expected)
    assert actual.attrs == expected.attrs
    assert actual.at["b0001", "ko_ids"] == ("K00001", "K00002")


def test_audit_reports_coverage_and_mapping_gaps(genes: pd.DataFrame) -> None:
    report = audit_registry(genes)

    assert report["canonical_protein_coding_genes"] == 3
    assert report["coverage"]["ncbi_gene"] == 3
    assert report["coverage"]["ecocyc"] == 3
    assert report["coverage"]["ko"] == 0


def test_table_and_audit_reject_duplicate_identifiers(genes: pd.DataFrame) -> None:
    with pytest.raises(DataValidationError, match="unique b_number"):
        validate_genes(pd.concat([genes, genes.iloc[:1]]))
    genes.loc["b0002", "ncbi_gene_id"] = genes.loc["b0001", "ncbi_gene_id"]
    with pytest.raises(DataValidationError, match="ambiguous ncbi_gene mappings"):
        audit_registry(genes)


@pytest.mark.parametrize(
    ("field", "value"),
    [("start", 0), ("strand", "."), ("ko_ids", ("bad",))],
)
def test_gene_fields_use_library_validation(
    genes: pd.DataFrame, field: str, value: object
) -> None:
    genes.at["b0001", field] = value
    with pytest.raises(ValidationError):
        validate_genes(genes)


def test_gene_symbols_cannot_replace_canonical_index(genes: pd.DataFrame) -> None:
    with pytest.raises(ValidationError):
        validate_genes(genes.rename(index={"b0001": "thrL"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("classification", "essential"),
        ("coverage", "unknown"),
        ("condition_disagreement", True),
        ("evidence_conflict", True),
    ],
)
def test_loaded_labels_must_agree_with_raw_evidence(
    tmp_path: Path, genes: pd.DataFrame, field: str, value: object
) -> None:
    path = tmp_path / "genes.parquet"
    genes.at["b0001", field] = value
    genes.to_parquet(path)

    with pytest.raises(DataValidationError, match="stored labels disagree"):
        load_genes(path)


def test_boundary_rejects_changed_study_metadata(genes: pd.DataFrame) -> None:
    from yggdrisil_ecoli.data.essentiality import STUDY_METADATA

    genes.attrs["essentiality"] = {**STUDY_METADATA, "m9_glucose_g_l": 20.0}
    with pytest.raises(DataValidationError, match="fixed study fields"):
        validate_genes(genes)
