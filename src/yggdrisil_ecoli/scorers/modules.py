"""Conservative KEGG Module retention evaluation over remaining genes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.kegg_modules import (
    Expression,
    KeggModuleEntry,
    ModuleEvaluation,
    evaluate_module_expression,
    referenced_kos,
    referenced_modules,
)
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState


@dataclass(frozen=True, slots=True)
class BrokenModule:
    module_id: str
    name: str
    missing_required_kos: tuple[str, ...]
    minimal_missing_ko_sets: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class ModuleRetentionResult:
    n_complete: int
    n_broken: int
    complete_modules: tuple[str, ...]
    broken_modules: tuple[BrokenModule, ...]
    deleted_genes_total: int
    deleted_genes_with_ko: int
    deleted_genes_without_ko: tuple[str, ...]

    def metrics(self) -> dict[str, object]:
        return {
            "n_complete": self.n_complete,
            "n_broken": self.n_broken,
            "complete_modules": list(self.complete_modules),
            "broken_modules": [asdict(module) for module in self.broken_modules],
        }

    def coverage(self) -> dict[str, object]:
        return {
            "deleted_genes_total": self.deleted_genes_total,
            "deleted_genes_with_ko": self.deleted_genes_with_ko,
            "deleted_genes_without_ko": list(self.deleted_genes_without_ko),
        }


class ModuleCatalog:
    """Frozen WT-complete module definitions and an exact local evaluator."""

    def __init__(
        self,
        *,
        entries: dict[str, KeggModuleEntry],
        wt_complete_module_ids: tuple[str, ...],
        parser_semantics_version: str,
        background_kos: tuple[str, ...] = (),
        reference_registry_ko_mapping_hash: str | None = None,
    ) -> None:
        missing = set(wt_complete_module_ids) - set(entries)
        if missing:
            raise DataValidationError(
                f"module catalog is missing WT definitions: {sorted(missing)}"
            )
        self.entries = dict(entries)
        self.wt_complete_module_ids = tuple(sorted(set(wt_complete_module_ids)))
        self.parser_semantics_version = parser_semantics_version
        self.background_kos = frozenset(background_kos)
        self.reference_registry_ko_mapping_hash = reference_registry_ko_mapping_hash
        self._expressions: dict[str, Expression] = {
            module_id: entry.expression for module_id, entry in self.entries.items()
        }
        self._module_kos = {
            module_id: self._resolved_kos(module_id, stack=())
            for module_id in self.wt_complete_module_ids
        }

    @classmethod
    def from_json(cls, path: str | Path) -> ModuleCatalog:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("schema_version") != 1:
            raise DataValidationError("unsupported KEGG module catalog schema")
        raw_definitions = payload.get("definitions")
        raw_wt = payload.get("wt_complete_module_ids")
        raw_background = payload.get("background_kos", [])
        parser_version = payload.get("parser_semantics_version")
        registry_mapping_hash = payload.get("reference_registry_ko_mapping_hash")
        background_source_hash = payload.get("background_ko_source_sha256")
        if (
            not isinstance(raw_definitions, dict)
            or not isinstance(raw_wt, list)
            or not isinstance(raw_background, list)
            or not isinstance(parser_version, str)
            or not isinstance(registry_mapping_hash, str)
            or len(registry_mapping_hash) != 64
            or not isinstance(background_source_hash, str)
            or len(background_source_hash) != 64
        ):
            raise DataValidationError("malformed KEGG module catalog")
        entries = {}
        for module_id, raw_entry in raw_definitions.items():
            if not isinstance(module_id, str) or not isinstance(raw_entry, dict):
                raise DataValidationError("malformed KEGG module definition")
            try:
                entries[module_id] = KeggModuleEntry(
                    module_id=module_id,
                    name=raw_entry["name"],
                    definition=raw_entry["definition"],
                    module_class=raw_entry.get("module_class"),
                )
            except (KeyError, TypeError) as exc:
                raise DataValidationError(
                    f"malformed KEGG module definition: {module_id}"
                ) from exc
        if any(not isinstance(item, str) for item in raw_wt):
            raise DataValidationError("malformed WT-complete module ID list")
        if any(not isinstance(item, str) for item in raw_background):
            raise DataValidationError("malformed background KO list")
        return cls(
            entries=entries,
            wt_complete_module_ids=tuple(raw_wt),
            parser_semantics_version=parser_version,
            background_kos=tuple(raw_background),
            reference_registry_ko_mapping_hash=registry_mapping_hash,
        )

    def validate_registry(self, registry: GeneRegistry) -> str:
        """Reject a catalog paired with a different gene-to-KO crosswalk."""

        mapping_hash = registry_ko_mapping_hash(registry)
        expected = self.reference_registry_ko_mapping_hash
        if expected is not None and mapping_hash != expected:
            raise DataValidationError(
                "KEGG module catalog and registry KO mappings were built from "
                "different snapshots"
            )
        return mapping_hash

    def require_entry(self, module_id: str) -> KeggModuleEntry:
        try:
            return self.entries[module_id]
        except KeyError as exc:
            raise KeyError(
                f"module is absent from the frozen catalog: {module_id}"
            ) from exc

    def ko_ids_for_module(self, module_id: str) -> frozenset[str]:
        self.require_entry(module_id)
        return self._resolved_kos(module_id, stack=())

    def evaluate_deleted(
        self,
        module_id: str,
        deleted_genes: set[str] | frozenset[str],
        registry: GeneRegistry,
    ) -> ModuleEvaluation:
        """Evaluate one catalog module after a canonical deletion set."""

        self.require_entry(module_id)
        remaining_kos = self._remaining_kos(deleted_genes, registry)
        return evaluate_module_expression(
            self._expressions[module_id],
            remaining_kos,
            module_definitions=self._expressions,
        )

    def score_deleted(
        self, deleted_genes: set[str] | frozenset[str], registry: GeneRegistry
    ) -> ModuleRetentionResult:
        """Evaluate WT-complete modules from KOs encoded by remaining genes."""

        deleted = frozenset(deleted_genes)
        remaining_kos = self._remaining_kos(deleted, registry)
        complete: list[str] = []
        broken: list[BrokenModule] = []
        for module_id in self.wt_complete_module_ids:
            result = evaluate_module_expression(
                self._expressions[module_id],
                remaining_kos,
                module_definitions=self._expressions,
            )
            if result.complete:
                complete.append(module_id)
            else:
                broken.append(
                    BrokenModule(
                        module_id=module_id,
                        name=self.entries[module_id].name,
                        missing_required_kos=result.missing_required_kos,
                        minimal_missing_ko_sets=result.minimal_missing_ko_sets,
                    )
                )
        without_ko = tuple(
            sorted(
                b_number
                for b_number in deleted
                if not registry.require(b_number).ko_ids
            )
        )
        return ModuleRetentionResult(
            n_complete=len(complete),
            n_broken=len(broken),
            complete_modules=tuple(complete),
            broken_modules=tuple(broken),
            deleted_genes_total=len(deleted),
            deleted_genes_with_ko=len(deleted) - len(without_ko),
            deleted_genes_without_ko=without_ko,
        )

    def modules_for_kos(self, ko_ids: set[str] | frozenset[str]) -> tuple[str, ...]:
        """Return WT-complete modules that reference at least one supplied KO."""

        return tuple(
            module_id
            for module_id in self.wt_complete_module_ids
            if self._module_kos[module_id].intersection(ko_ids)
        )

    def _remaining_kos(
        self,
        deleted_genes: set[str] | frozenset[str],
        registry: GeneRegistry,
    ) -> set[str]:
        for b_number in deleted_genes:
            registry.require(b_number)
        deleted = frozenset(deleted_genes)
        remaining_kos = set(self.background_kos)
        remaining_kos.update(
            ko_id
            for record in registry
            if record.b_number not in deleted
            for ko_id in record.ko_ids
        )
        return remaining_kos

    def _resolved_kos(
        self, module_id: str, *, stack: tuple[str, ...]
    ) -> frozenset[str]:
        if module_id in stack:
            raise DataValidationError(
                f"cyclic module reference: {' -> '.join((*stack, module_id))}"
            )
        expression = self._expressions[module_id]
        result = set(referenced_kos(expression))
        for reference in referenced_modules(expression):
            if reference not in self._expressions:
                raise DataValidationError(f"unresolved module reference: {reference}")
            result.update(self._resolved_kos(reference, stack=(*stack, module_id)))
        return frozenset(result)


class ModuleRetentionScorer:
    name = "module_retention"
    version = "1"

    def __init__(
        self,
        *,
        registry: GeneRegistry,
        catalog: ModuleCatalog,
        artifact_hash: str,
    ) -> None:
        self.registry = registry
        self.catalog = catalog
        self.artifact_hash = artifact_hash
        self.registry_ko_mapping_hash = catalog.validate_registry(registry)
        self.config = {
            "artifact_sha256": artifact_hash,
            "registry_ko_mapping_sha256": self.registry_ko_mapping_hash,
            "parser_semantics_version": catalog.parser_semantics_version,
        }

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        result = self.catalog.score_deleted(state.deleted_genes, self.registry)
        return scientific_evaluation(
            result.metrics(),
            coverage=result.coverage(),
            provenance={
                "artifact_hash": self.artifact_hash,
                "registry_ko_mapping_hash": self.registry_ko_mapping_hash,
                "parser_semantics_version": self.catalog.parser_semantics_version,
            },
        )


def registry_ko_mapping_hash(registry: GeneRegistry) -> str:
    """Fingerprint the canonical gene-to-KO mapping used by module evaluation."""

    payload = [(record.b_number, list(record.ko_ids)) for record in registry]
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
