"""One gene-indexed table for reference annotations and LB/M9 evidence."""

from pathlib import Path

import pandas as pd
from pydantic import TypeAdapter

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.essentiality import STUDY_METADATA, EssentialityRecord
from yggdrisil_ecoli.data.registry import GeneRecord


class _GeneEvidence(GeneRecord, EssentialityRecord):
    """Validate input rows; analysis uses the resulting plain DataFrame."""


_ROWS = TypeAdapter(list[_GeneEvidence])
_DERIVED = tuple(EssentialityRecord.model_computed_fields)
_COLUMNS = list(GeneRecord.model_fields) + [
    column
    for column in (*EssentialityRecord.model_fields, *_DERIVED)
    if column != "b_number"
]


def validate_genes(genes: pd.DataFrame) -> pd.DataFrame:
    """Validate identifiers and evidence, deriving labels without inventing provenance."""
    if genes.index.name != "b_number" or not genes.index.is_unique or genes.empty:
        raise DataValidationError("genes must have a nonempty, unique b_number index")
    source = genes.drop(columns=list(_DERIVED), errors="ignore")
    rows = (
        source.astype(object)
        .where(source.notna(), None)
        .reset_index()
        .to_dict("records")
    )
    validated = _ROWS.validate_python(rows)
    result = pd.DataFrame(row.model_dump() for row in validated)[_COLUMNS]
    result = result.set_index("b_number")
    for column in _DERIVED:
        if column in genes and not genes[column].eq(result[column]).fillna(False).all():
            raise DataValidationError(
                f"stored labels disagree with measurements: {column}"
            )
    if "essentiality" in genes.attrs and any(
        genes.attrs["essentiality"].get(key) != value
        for key, value in STUDY_METADATA.items()
    ):
        raise DataValidationError("essentiality metadata changes fixed study fields")
    result.attrs = genes.attrs.copy()
    return result.sort_index()


def load_genes(path: str | Path) -> pd.DataFrame:
    """Read the prepared genes.parquet table and its source metadata."""
    return validate_genes(pd.read_parquet(path))


def write_genes(genes: pd.DataFrame, path: str | Path) -> None:
    """Write one validated table; pandas preserves its metadata in Parquet."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_genes(genes).to_parquet(path, compression="zstd")
