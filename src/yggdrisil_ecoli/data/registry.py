"""Canonical MG1655 gene registry types and Parquet persistence."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from yggdrisil_ecoli.constants import is_b_number
from yggdrisil_ecoli.data.errors import DataValidationError

REGISTRY_SCHEMA = pa.schema(
    [
        pa.field("b_number", pa.string(), nullable=False),
        pa.field("symbol", pa.string()),
        pa.field("name", pa.string()),
        pa.field("description", pa.string()),
        pa.field("start", pa.int64(), nullable=False),
        pa.field("end", pa.int64(), nullable=False),
        pa.field("strand", pa.string(), nullable=False),
        pa.field("ncbi_gene_id", pa.string()),
        pa.field("ecocyc_id", pa.string()),
        pa.field("kegg_gene_id", pa.string()),
        pa.field("ko_ids", pa.list_(pa.string()), nullable=False),
        pa.field("iml1515_gene_id", pa.string()),
    ]
)


@dataclass(frozen=True, slots=True)
class GeneRecord:
    """One canonical protein-coding MG1655 gene and its crosswalks."""

    b_number: str
    symbol: str | None
    name: str | None
    description: str | None
    start: int
    end: int
    strand: str
    ncbi_gene_id: str | None
    ecocyc_id: str | None
    kegg_gene_id: str | None = None
    ko_ids: tuple[str, ...] = ()
    iml1515_gene_id: str | None = None

    def __post_init__(self) -> None:
        if not is_b_number(self.b_number):
            raise DataValidationError(f"malformed canonical ID: {self.b_number!r}")
        if self.start < 1 or self.end < self.start:
            raise DataValidationError(
                f"{self.b_number}: invalid coordinates {self.start}..{self.end}"
            )
        if self.strand not in {"+", "-"}:
            raise DataValidationError(
                f"{self.b_number}: invalid strand {self.strand!r}"
            )
        normalized_kos = tuple(sorted(set(self.ko_ids)))
        if any(not _is_ko_id(ko_id) for ko_id in normalized_kos):
            raise DataValidationError(f"{self.b_number}: malformed KO identifiers")
        object.__setattr__(self, "ko_ids", normalized_kos)

    @property
    def in_iml1515(self) -> bool:
        """Whether the canonical ID has an iML1515 crosswalk."""

        return self.iml1515_gene_id is not None

    def as_arrow_row(self) -> dict[str, object]:
        """Return a PyArrow-compatible row with deterministic list values."""

        return {**asdict(self), "ko_ids": list(self.ko_ids)}


class GeneRegistry:
    """Validated, immutable lookup over canonical gene records."""

    def __init__(self, records: Iterable[GeneRecord]) -> None:
        by_id: dict[str, GeneRecord] = {}
        for record in records:
            if record.b_number in by_id:
                raise DataValidationError(f"duplicate canonical ID: {record.b_number}")
            by_id[record.b_number] = record
        if not by_id:
            raise DataValidationError("canonical registry is empty")
        self._by_id = by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[GeneRecord]:
        for b_number in sorted(self._by_id):
            yield self._by_id[b_number]

    @property
    def search_universe(self) -> frozenset[str]:
        """Canonical IDs eligible for the v1 search."""

        return frozenset(self._by_id)

    def get(self, b_number: str) -> GeneRecord | None:
        """Return a record only for an exact canonical ID."""

        return self._by_id.get(b_number)

    def require(self, b_number: str) -> GeneRecord:
        """Return a record or fail without attempting symbol translation."""

        if not is_b_number(b_number):
            raise DataValidationError(
                f"expected a canonical b-number, got {b_number!r}"
            )
        try:
            return self._by_id[b_number]
        except KeyError as exc:
            raise KeyError(f"gene is outside the search universe: {b_number}") from exc

    @classmethod
    def from_parquet(cls, path: str | Path) -> GeneRegistry:
        """Load and validate a registry from the canonical Parquet schema."""

        table = pq.read_table(path, schema=REGISTRY_SCHEMA)
        records = []
        for row in table.to_pylist():
            row["ko_ids"] = tuple(row["ko_ids"] or ())
            records.append(GeneRecord(**row))
        return cls(records)

    def to_parquet(self, path: str | Path) -> None:
        """Atomically write a deterministic, compressed registry."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rows = [record.as_arrow_row() for record in self]
        table = pa.Table.from_pylist(rows, schema=REGISTRY_SCHEMA)
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        try:
            pq.write_table(
                table,
                temporary,
                compression="zstd",
                use_dictionary=True,
                write_statistics=True,
            )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def file_sha256(path: str | Path) -> str:
    """Hash a file without loading the whole artifact into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_ko_id(value: str) -> bool:
    return len(value) == 6 and value.startswith("K") and value[1:].isdigit()
