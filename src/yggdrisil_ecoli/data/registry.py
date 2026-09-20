"""Validated MG1655 gene records and a small Parquet-backed lookup."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field, field_validator
from pydantic.dataclasses import dataclass

from yggdrisil_ecoli.constants import is_b_number
from yggdrisil_ecoli.data.errors import DataValidationError

BNumber = Annotated[str, Field(pattern=r"^b[0-9]{4}$")]
KO = Annotated[str, Field(pattern=r"^K[0-9]{5}$")]


@dataclass(frozen=True)
class GeneRecord:
    """Coordinates are one-based and inclusive."""

    b_number: BNumber
    symbol: str | None
    name: str | None
    description: str | None
    start: Annotated[int, Field(ge=1, strict=True)]
    end: Annotated[int, Field(ge=1, strict=True)]
    strand: Literal["+", "-"]
    ncbi_gene_id: str | None
    ecocyc_id: str | None
    kegg_gene_id: str | None = None
    ko_ids: tuple[KO, ...] = ()
    iml1515_gene_id: str | None = None

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"{self.b_number}: end precedes start")

    @field_validator("ko_ids")
    @classmethod
    def unique_kos(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(values)))

    @property
    def in_iml1515(self) -> bool:
        return self.iml1515_gene_id is not None


class GeneRegistry:
    def __init__(self, records: Iterable[GeneRecord]) -> None:
        self._by_id: dict[str, GeneRecord] = {}
        for record in records:
            if record.b_number in self._by_id:
                raise DataValidationError(f"duplicate canonical ID: {record.b_number}")
            self._by_id[record.b_number] = record
        if not self._by_id:
            raise DataValidationError("canonical registry is empty")

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[GeneRecord]:
        return (self._by_id[tag] for tag in sorted(self._by_id))

    @property
    def search_universe(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def get(self, b_number: str) -> GeneRecord | None:
        return self._by_id.get(b_number)

    def require(self, b_number: str) -> GeneRecord:
        if not is_b_number(b_number):
            raise DataValidationError(
                f"expected a canonical b-number, got {b_number!r}"
            )
        return self._by_id[b_number]

    @classmethod
    def from_parquet(cls, path: str | Path) -> GeneRegistry:
        return cls(GeneRecord(**row) for row in pq.read_table(path).to_pylist())

    def to_parquet(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.Table.from_pylist([asdict(r) for r in self]), path, compression="zstd"
        )


def file_sha256(path: str | Path) -> str:
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
