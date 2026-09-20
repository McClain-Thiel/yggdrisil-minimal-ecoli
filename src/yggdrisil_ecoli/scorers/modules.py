"""Exact KEGG Module retention evaluation over remaining-gene KOs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints, ValidationError
from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.kegg_modules import (
    Expression,
    KeggModuleEntry,
    ModuleEvaluation,
    evaluate_module_expression,
    referenced_ids,
    registry_ko_mapping_hash,
)
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ModuleId = Annotated[str, StringConstraints(pattern=r"^M[0-9]{5}$")]
KoId = Annotated[str, StringConstraints(pattern=r"^K[0-9]{5}$")]


class _FrozenEntry(BaseModel):
    name: str
    definition: str
    module_class: str | None = None


class _Artifact(BaseModel):
    schema_version: Literal[1]
    parser_semantics_version: str
    reference_registry_sha256: Sha256
    reference_registry_ko_mapping_hash: Sha256
    background_ko_source_sha256: Sha256
    background_kos: list[KoId]
    wt_complete_module_ids: list[ModuleId]
    definitions: dict[ModuleId, _FrozenEntry]


@dataclass(frozen=True, slots=True)
class BrokenModule:
    module_id: str
    name: str
    missing_required_kos: tuple[str, ...]
    minimal_missing_ko_sets: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class ModuleRetentionResult:
    n_complete: int
    broken_modules: tuple[BrokenModule, ...]
    deleted_genes_total: int
    deleted_genes_with_ko: int
    deleted_genes_without_ko: tuple[str, ...]

    @property
    def n_broken(self) -> int:
        return len(self.broken_modules)

    def metrics(self) -> dict[str, object]:
        return {
            "n_complete": self.n_complete,
            "n_broken": self.n_broken,
            "broken_modules": [asdict(module) for module in self.broken_modules],
        }

    def coverage(self) -> dict[str, object]:
        return {
            "deleted_genes_total": self.deleted_genes_total,
            "deleted_genes_with_ko": self.deleted_genes_with_ko,
            "deleted_genes_without_ko": list(self.deleted_genes_without_ko),
        }


class ModuleEvaluator:
    """Frozen WT-complete definitions plus their native Yggdrisil evaluator."""

    name = "module_retention"
    version = "2"

    def __init__(
        self,
        *,
        registry: GeneRegistry,
        entries: dict[str, KeggModuleEntry],
        wt_complete_module_ids: tuple[str, ...],
        parser_semantics_version: str,
        background_kos: tuple[str, ...] = (),
        provenance: Mapping[str, str] | None = None,
    ) -> None:
        missing = set(wt_complete_module_ids) - set(entries)
        if missing:
            raise DataValidationError(
                f"module catalog is missing WT definitions: {sorted(missing)}"
            )
        self.registry = registry
        self.entries = dict(entries)
        self.wt_complete_module_ids = tuple(sorted(set(wt_complete_module_ids)))
        self.background_kos = frozenset(background_kos)
        self._expressions: dict[str, Expression] = {
            module_id: entry.expression for module_id, entry in entries.items()
        }
        mapping_hash = registry_ko_mapping_hash(registry)
        source_provenance = dict(provenance or {})
        self.reference_registry_sha256 = source_provenance.get(
            "reference_registry_sha256"
        )
        self.background_ko_source_sha256 = source_provenance.get(
            "background_ko_source_sha256"
        )
        expected_hash = source_provenance.get("reference_registry_ko_mapping_hash")
        if expected_hash is not None and mapping_hash != expected_hash:
            raise DataValidationError(
                "KEGG module catalog and registry KO mappings were built from "
                "different snapshots"
            )
        self.provenance: dict[str, object] = {
            **source_provenance,
            "registry_ko_mapping_sha256": mapping_hash,
            "parser_semantics_version": parser_semantics_version,
        }
        self.config = self.provenance
        self._module_kos = {
            module_id: self._resolved_kos(module_id, ())
            for module_id in self.wt_complete_module_ids
        }

    @classmethod
    def from_json(cls, path: str | Path, registry: GeneRegistry) -> ModuleEvaluator:
        artifact_path = Path(path)
        try:
            artifact = _Artifact.model_validate_json(
                artifact_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise DataValidationError(f"invalid KEGG module catalog: {path}") from exc
        entries = {
            module_id: KeggModuleEntry(
                module_id,
                entry.name,
                entry.definition,
                entry.module_class,
            )
            for module_id, entry in artifact.definitions.items()
        }
        return cls(
            registry=registry,
            entries=entries,
            wt_complete_module_ids=tuple(artifact.wt_complete_module_ids),
            parser_semantics_version=artifact.parser_semantics_version,
            background_kos=tuple(artifact.background_kos),
            provenance={
                "artifact_sha256": file_sha256(artifact_path),
                "reference_registry_sha256": artifact.reference_registry_sha256,
                "reference_registry_ko_mapping_hash": (
                    artifact.reference_registry_ko_mapping_hash
                ),
                "background_ko_source_sha256": (artifact.background_ko_source_sha256),
            },
        )

    def require_entry(self, module_id: str) -> KeggModuleEntry:
        try:
            return self.entries[module_id]
        except KeyError as exc:
            raise KeyError(
                f"module is absent from the frozen catalog: {module_id}"
            ) from exc

    def ko_ids_for_module(self, module_id: str) -> frozenset[str]:
        self.require_entry(module_id)
        return self._resolved_kos(module_id, ())

    def modules_for_kos(self, ko_ids: set[str] | frozenset[str]) -> tuple[str, ...]:
        return tuple(
            module_id
            for module_id in self.wt_complete_module_ids
            if self._module_kos[module_id] & ko_ids
        )

    def evaluate_deleted(
        self, module_id: str, deleted_genes: set[str] | frozenset[str]
    ) -> ModuleEvaluation:
        self.require_entry(module_id)
        return evaluate_module_expression(
            self._expressions[module_id],
            self._remaining_kos(deleted_genes),
            module_definitions=self._expressions,
        )

    def score_deleted(
        self, deleted_genes: set[str] | frozenset[str]
    ) -> ModuleRetentionResult:
        deleted = frozenset(deleted_genes)
        remaining_kos = self._remaining_kos(deleted)
        broken: list[BrokenModule] = []
        for module_id in self.wt_complete_module_ids:
            result = evaluate_module_expression(
                self._expressions[module_id],
                remaining_kos,
                module_definitions=self._expressions,
            )
            if not result.complete:
                broken.append(
                    BrokenModule(
                        module_id,
                        self.entries[module_id].name,
                        result.missing_required_kos,
                        result.minimal_missing_ko_sets,
                    )
                )
        without_ko = tuple(
            sorted(gene for gene in deleted if not self.registry.require(gene).ko_ids)
        )
        return ModuleRetentionResult(
            len(self.wt_complete_module_ids) - len(broken),
            tuple(broken),
            len(deleted),
            len(deleted) - len(without_ko),
            without_ko,
        )

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        result = self.score_deleted(state.deleted_genes)
        return scientific_evaluation(
            result.metrics(), coverage=result.coverage(), provenance=self.provenance
        )

    def _remaining_kos(self, deleted_genes: set[str] | frozenset[str]) -> set[str]:
        for gene in deleted_genes:
            self.registry.require(gene)
        return set(self.background_kos).union(
            ko
            for record in self.registry
            if record.b_number not in deleted_genes
            for ko in record.ko_ids
        )

    def _resolved_kos(self, module_id: str, stack: tuple[str, ...]) -> frozenset[str]:
        if module_id in stack:
            raise DataValidationError(
                f"cyclic module reference: {' -> '.join((*stack, module_id))}"
            )
        try:
            identifiers = referenced_ids(self._expressions[module_id])
        except KeyError as exc:
            raise DataValidationError(
                f"unresolved module reference: {module_id}"
            ) from exc
        kos = {identifier for identifier in identifiers if identifier.startswith("K")}
        for reference in identifiers - kos:
            kos.update(self._resolved_kos(reference, (*stack, module_id)))
        return frozenset(kos)
