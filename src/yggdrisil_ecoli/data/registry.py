"""Validate source gene coordinates and identifiers before tabular analysis."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BNumber = Annotated[str, Field(pattern=r"^b[0-9]{4}$")]
KO = Annotated[str, Field(pattern=r"^K[0-9]{5}$")]


class GeneRecord(BaseModel):
    """Coordinates are one-based and inclusive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    b_number: BNumber
    symbol: str | None = None
    name: str | None = None
    description: str | None = None
    start: Annotated[int, Field(ge=1, strict=True)]
    end: Annotated[int, Field(ge=1, strict=True)]
    strand: Literal["+", "-"]
    ncbi_gene_id: str | None = None
    ecocyc_id: str | None = None
    kegg_gene_id: str | None = None
    ko_ids: tuple[KO, ...] = ()
    iml1515_gene_id: str | None = None

    @model_validator(mode="after")
    def ordered_coordinates(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"{self.b_number}: end precedes start")
        return self

    @field_validator("ko_ids")
    @classmethod
    def unique_kos(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(values)))
