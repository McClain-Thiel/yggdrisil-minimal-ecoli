"""Validated deletion universes for controlled search comparisons."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import (
    WCM_1219_GENE_LIST_SHA256,
    WCM_1219_SOURCE_COMMIT,
)

WCM_1219_UNIVERSE_ID = "bristol-wcm-1219-canonical-intersection-v1"
WCM_1219_SOURCE_GENE_COUNT = 1_219
WCM_1219_UNMAPPED_SOURCE_IDS = frozenset({"EG10498", "EG11283", "G8221"})


def gene_set_sha256(genes: frozenset[str]) -> str:
    """Hash a canonical gene set independently of its container ordering."""

    return hashlib.sha256(("\n".join(sorted(genes)) + "\n").encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateUniverse:
    """One immutable, provenance-bearing set of genes eligible for deletion."""

    universe_id: str
    genes: frozenset[str]
    gene_set_hash: str
    artifact_hash: str | None = None
    source_hash: str | None = None
    source_commit: str | None = None
    source_gene_count: int | None = None
    unmapped_source_ids: tuple[str, ...] = ()

    @classmethod
    def full_registry(cls, registry: GeneRegistry) -> CandidateUniverse:
        """Return the complete canonical protein-coding search universe."""

        genes = registry.search_universe
        return cls(
            universe_id="canonical-protein-coding-v1",
            genes=genes,
            gene_set_hash=gene_set_sha256(genes),
        )

    @classmethod
    def from_json(
        cls,
        path: str | Path,
        *,
        registry: GeneRegistry,
        registry_path: str | Path,
    ) -> CandidateUniverse:
        """Load a pinned candidate universe and validate every boundary."""

        artifact_path = Path(path)
        try:
            payload = json.loads(artifact_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise DataValidationError(
                f"{artifact_path}: invalid candidate-universe artifact"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise DataValidationError(
                f"{artifact_path}: unsupported candidate-universe schema"
            )
        if payload.get("universe_id") != WCM_1219_UNIVERSE_ID:
            raise DataValidationError(
                f"{artifact_path}: unexpected candidate-universe identity"
            )
        raw_genes = payload.get("gene_ids")
        if (
            not isinstance(raw_genes, list)
            or not raw_genes
            or any(not isinstance(gene, str) for gene in raw_genes)
        ):
            raise DataValidationError(f"{artifact_path}: invalid gene_ids")
        if raw_genes != sorted(raw_genes) or len(raw_genes) != len(set(raw_genes)):
            raise DataValidationError(
                f"{artifact_path}: gene_ids must be sorted and unique"
            )
        genes = frozenset(raw_genes)
        outside = sorted(genes - registry.search_universe)
        if outside:
            raise DataValidationError(
                f"{artifact_path}: genes outside the canonical registry: {outside}"
            )
        expected_registry_hash = payload.get("registry_sha256")
        if expected_registry_hash != file_sha256(registry_path):
            raise DataValidationError(
                f"{artifact_path}: registry hash does not match the sibling artifact"
            )
        expected_gene_hash = payload.get("gene_set_sha256")
        actual_gene_hash = gene_set_sha256(genes)
        if expected_gene_hash != actual_gene_hash:
            raise DataValidationError(
                f"{artifact_path}: candidate gene-set hash does not match"
            )
        counts = payload.get("counts")
        source = payload.get("source")
        if not isinstance(counts, dict) or not isinstance(source, dict):
            raise DataValidationError(f"{artifact_path}: missing counts or source")
        if counts.get("canonical_candidate_genes") != len(genes):
            raise DataValidationError(
                f"{artifact_path}: canonical candidate count does not match"
            )
        unmapped = payload.get("source_ids_outside_registry")
        if (
            not isinstance(unmapped, list)
            or any(not isinstance(item, str) for item in unmapped)
            or unmapped != sorted(set(unmapped))
        ):
            raise DataValidationError(
                f"{artifact_path}: invalid source_ids_outside_registry"
            )
        source_hash = source.get("sha256")
        source_commit = source.get("commit")
        source_gene_count = counts.get("source_wcm_genes")
        if (
            not isinstance(source_hash, str)
            or not isinstance(source_commit, str)
            or not isinstance(source_gene_count, int)
        ):
            raise DataValidationError(f"{artifact_path}: incomplete source provenance")
        if (
            source_hash != WCM_1219_GENE_LIST_SHA256
            or source_commit != WCM_1219_SOURCE_COMMIT
            or source_gene_count != WCM_1219_SOURCE_GENE_COUNT
            or frozenset(unmapped) != WCM_1219_UNMAPPED_SOURCE_IDS
        ):
            raise DataValidationError(
                f"{artifact_path}: source provenance is not the pinned WCM-1219 set"
            )
        if source_gene_count != len(genes) + len(unmapped):
            raise DataValidationError(
                f"{artifact_path}: source and mapped counts do not reconcile"
            )
        return cls(
            universe_id=WCM_1219_UNIVERSE_ID,
            genes=genes,
            gene_set_hash=actual_gene_hash,
            artifact_hash=file_sha256(artifact_path),
            source_hash=source_hash,
            source_commit=source_commit,
            source_gene_count=source_gene_count,
            unmapped_source_ids=tuple(unmapped),
        )

    def metadata(self) -> dict[str, object]:
        """Return secret-free run metadata sufficient for resume validation."""

        return {
            "universe_id": self.universe_id,
            "canonical_candidate_genes": len(self.genes),
            "gene_set_sha256": self.gene_set_hash,
            "artifact_sha256": self.artifact_hash,
            "source_sha256": self.source_hash,
            "source_commit": self.source_commit,
            "source_wcm_genes": self.source_gene_count,
            "source_ids_outside_registry": list(self.unmapped_source_ids),
        }
