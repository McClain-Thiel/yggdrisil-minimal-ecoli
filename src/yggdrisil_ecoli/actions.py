"""Monotonic gene-deletion action."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator
from yggdrisil import serializable

from yggdrisil_ecoli.constants import is_b_number


@serializable
class DeleteGenes(BaseModel):
    """Delete one non-empty bundle of canonical genes."""

    model_config = ConfigDict(frozen=True)

    genes: tuple[str, ...]

    @field_validator("genes")
    @classmethod
    def validate_genes(cls, genes: tuple[str, ...]) -> tuple[str, ...]:
        if not genes:
            raise ValueError("deletion action must contain at least one gene")
        invalid = sorted(gene for gene in genes if not is_b_number(gene))
        if invalid:
            raise ValueError(f"expected canonical b-numbers, got {invalid}")
        duplicates = sorted(gene for gene in set(genes) if genes.count(gene) > 1)
        if duplicates:
            raise ValueError(f"deletion action contains duplicate genes: {duplicates}")
        return tuple(sorted(genes))
