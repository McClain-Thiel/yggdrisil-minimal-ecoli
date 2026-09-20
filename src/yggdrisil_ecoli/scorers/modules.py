"""Score retention of the modules that are complete in wild-type MG1655."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path

from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.kegg_modules import (
    KeggModuleEntry,
    ModuleEvaluation,
    evaluate_module_expression,
    referenced_ids,
    registry_ko_mapping_hash,
)
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.state import GenomeState


@dataclass(frozen=True)
class BrokenModule:
    module_id: str
    name: str
    missing_required_kos: tuple[str, ...]
    minimal_missing_ko_sets: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ModuleRetentionResult:
    n_complete: int
    broken_modules: tuple[BrokenModule, ...]
    deleted_genes_total: int
    deleted_genes_with_ko: int
    deleted_genes_without_ko: tuple[str, ...]

    @property
    def n_broken(self) -> int:
        return len(self.broken_modules)


class ModuleEvaluator:
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
        self.registry = registry
        self.entries = entries
        self.wt_complete_module_ids = tuple(sorted(set(wt_complete_module_ids)))
        self.background_kos = frozenset(background_kos)
        self._expressions = {key: entry.expression for key, entry in entries.items()}
        mapping_hash = registry_ko_mapping_hash(registry)
        self.provenance = {
            **(provenance or {}),
            "parser_semantics_version": parser_semantics_version,
            "lark_version": version("lark"),
            "boolean_py_version": version("boolean.py"),
        }
        expected = self.provenance.get(
            "reference_registry_ko_mapping_hash", mapping_hash
        )
        if expected != mapping_hash:
            raise DataValidationError(
                "module catalog and registry use different snapshots"
            )
        self.provenance["reference_registry_ko_mapping_hash"] = mapping_hash
        self.config = self.provenance
        self._module_kos = {
            key: self._resolved_kos(key) for key in self.wt_complete_module_ids
        }

    @classmethod
    def from_json(cls, path: str | Path, registry: GeneRegistry) -> ModuleEvaluator:
        artifact = json.loads(Path(path).read_text())
        if artifact["schema_version"] != 1:
            raise DataValidationError("unsupported KEGG module catalog schema")
        return cls(
            registry=registry,
            entries={
                key: KeggModuleEntry(key, **entry)
                for key, entry in artifact["definitions"].items()
            },
            wt_complete_module_ids=tuple(artifact["wt_complete_module_ids"]),
            parser_semantics_version=artifact["parser_semantics_version"],
            background_kos=tuple(artifact["background_kos"]),
            provenance={
                "artifact_sha256": file_sha256(path),
                **{
                    key: artifact[key]
                    for key in (
                        "reference_registry_sha256",
                        "reference_registry_ko_mapping_hash",
                        "background_ko_source_sha256",
                    )
                },
            },
        )

    def require_entry(self, module_id: str) -> KeggModuleEntry:
        return self.entries[module_id]

    def ko_ids_for_module(self, module_id: str) -> frozenset[str]:
        return self._resolved_kos(module_id)

    def modules_for_kos(self, ko_ids: set[str] | frozenset[str]) -> tuple[str, ...]:
        return tuple(key for key, kos in self._module_kos.items() if kos & ko_ids)

    def evaluate_deleted(
        self, module_id: str, deleted_genes: set[str] | frozenset[str]
    ) -> ModuleEvaluation:
        return evaluate_module_expression(
            self._expressions[module_id],
            self._remaining_kos(deleted_genes),
            module_definitions=self._expressions,
        )

    def score_deleted(
        self, deleted_genes: set[str] | frozenset[str]
    ) -> ModuleRetentionResult:
        remaining_kos = self._remaining_kos(deleted_genes)
        broken = []
        for key in self.wt_complete_module_ids:
            result = evaluate_module_expression(
                self._expressions[key],
                remaining_kos,
                module_definitions=self._expressions,
            )
            if not result.complete:
                broken.append(
                    BrokenModule(
                        key,
                        self.entries[key].name,
                        result.missing_required_kos,
                        result.minimal_missing_ko_sets,
                    )
                )
        without_ko = tuple(
            sorted(
                gene for gene in deleted_genes if not self.registry.require(gene).ko_ids
            )
        )
        return ModuleRetentionResult(
            len(self.wt_complete_module_ids) - len(broken),
            tuple(broken),
            len(deleted_genes),
            len(deleted_genes) - len(without_ko),
            without_ko,
        )

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        result = self.score_deleted(state.deleted_genes)
        return EvaluationResult(
            metrics={"n_complete": result.n_complete, "n_broken": result.n_broken},
            metadata={
                "details": {
                    "broken_modules": [asdict(item) for item in result.broken_modules]
                },
                "coverage": {
                    "deleted_genes_total": result.deleted_genes_total,
                    "deleted_genes_with_ko": result.deleted_genes_with_ko,
                    "deleted_genes_without_ko": list(result.deleted_genes_without_ko),
                },
                "provenance": self.provenance,
            },
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

    def _resolved_kos(
        self, module_id: str, stack: tuple[str, ...] = ()
    ) -> frozenset[str]:
        if module_id in stack:
            raise DataValidationError(f"cyclic module reference: {module_id}")
        if module_id not in self._expressions:
            raise DataValidationError(f"unresolved module reference: {module_id}")
        identifiers = referenced_ids(self._expressions[module_id])
        kos = {key for key in identifiers if key.startswith("K")}
        for key in identifiers - kos:
            kos.update(self._resolved_kos(key, (*stack, module_id)))
        return frozenset(kos)
