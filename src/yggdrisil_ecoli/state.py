"""Identity-only state for the monotonic deletion search."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from yggdrisil import serializable

from yggdrisil_ecoli.constants import is_b_number
from yggdrisil_ecoli.data.errors import DataValidationError


@serializable
@dataclass(frozen=True, slots=True)
class GenomeState:
    """A candidate is identified only by its deleted canonical genes."""

    deleted_genes: frozenset[str]

    def __post_init__(self) -> None:
        normalized = frozenset(self.deleted_genes)
        invalid = sorted(gene for gene in normalized if not is_b_number(gene))
        if invalid:
            raise DataValidationError(
                f"state contains malformed canonical IDs: {invalid}"
            )
        object.__setattr__(self, "deleted_genes", normalized)


def genome_state_key(state: GenomeState) -> str:
    """Return the sole canonical cache/graph identity for a deletion state."""

    payload = json.dumps(
        {"deleted_genes": sorted(state.deleted_genes)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()
