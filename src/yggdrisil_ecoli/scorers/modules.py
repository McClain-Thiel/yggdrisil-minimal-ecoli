"""Score retention of the modules that are complete in wild-type MG1655."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path

from pandas import DataFrame
from yggdrisil import EvaluationResult, stable_hash

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import file_sha256
from yggdrisil_ecoli.data.kegg_modules import (
    KeggModuleEntry,
    evaluate_module_expression,
    parse_module_expression,
    referenced_ids,
    registry_ko_mapping_hash,
)
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState


class ModuleEvaluator:
    name = "module_retention"
    version = "3"

    def __init__(
        self,
        *,
        registry: DataFrame,
        entries: dict[str, KeggModuleEntry],
        wt_complete_module_ids: tuple[str, ...],
        parser_semantics_version: str,
        background_kos: tuple[str, ...] = (),
        provenance: Mapping[str, str] | None = None,
    ) -> None:
        self.registry = registry.copy(deep=True)
        self.entries = {key: entry.copy() for key, entry in entries.items()}
        self.wt_complete_module_ids = tuple(sorted(set(wt_complete_module_ids)))
        self.background_kos = frozenset(background_kos)
        self._expressions = {
            key: parse_module_expression(entry["definition"])
            for key, entry in self.entries.items()
        }
        mapping_hash = registry_ko_mapping_hash(self.registry)
        self.provenance = {
            **(provenance or {}),
            "parser_semantics_version": parser_semantics_version,
            "lark_version": version("lark"),
            "boolean_py_version": version("boolean.py"),
        }
        if (
            self.provenance.get("reference_registry_ko_mapping_hash", mapping_hash)
            != mapping_hash
        ):
            raise DataValidationError(
                "module catalog and registry use different snapshots"
            )
        self.provenance["reference_registry_ko_mapping_hash"] = mapping_hash
        self.config = {
            **self.provenance,
            "catalog_sha256": stable_hash(
                {
                    "definitions": self.entries,
                    "wt_complete_module_ids": self.wt_complete_module_ids,
                    "background_kos": sorted(self.background_kos),
                }
            ),
        }
        self._module_kos = {
            key: self.ko_ids_for_module(key) for key in self.wt_complete_module_ids
        }

    @classmethod
    def from_json(cls, path: str | Path, registry: DataFrame) -> ModuleEvaluator:
        artifact = json.loads(Path(path).read_text())
        if artifact["schema_version"] != 1:
            raise DataValidationError("unsupported KEGG module catalog schema")
        return cls(
            registry=registry,
            entries=artifact["definitions"],
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

    def ko_ids_for_module(
        self, module_id: str, _stack: tuple[str, ...] = ()
    ) -> frozenset[str]:
        if module_id in _stack:
            raise DataValidationError(f"cyclic module reference: {module_id}")
        if module_id not in self._expressions:
            raise DataValidationError(f"unresolved module reference: {module_id}")
        identifiers = referenced_ids(self._expressions[module_id])
        kos = {key for key in identifiers if key.startswith("K")}
        for key in identifiers - kos:
            kos.update(self.ko_ids_for_module(key, (*_stack, module_id)))
        return frozenset(kos)

    def modules_for_kos(self, ko_ids: set[str] | frozenset[str]) -> tuple[str, ...]:
        return tuple(key for key, kos in self._module_kos.items() if kos & ko_ids)

    def evaluate_deleted(
        self, module_id: str, deleted_genes: set[str] | frozenset[str]
    ) -> bool:
        return evaluate_module_expression(
            self._expressions[module_id],
            self._remaining_kos(deleted_genes),
            module_definitions=self._expressions,
        )

    def score_deleted(
        self, deleted_genes: set[str] | frozenset[str]
    ) -> tuple[str, ...]:
        """Return broken module IDs; definitions remain available in entries."""
        remaining = self._remaining_kos(deleted_genes)
        return tuple(
            key
            for key in self.wt_complete_module_ids
            if not evaluate_module_expression(
                self._expressions[key], remaining, module_definitions=self._expressions
            )
        )

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        broken = self.score_deleted(state.deleted_genes)
        deleted = self.registry.loc[sorted(state.deleted_genes), "ko_ids"]
        without_ko = deleted.loc[deleted.map(len) == 0].index.tolist()
        return scientific_evaluation(
            metrics={
                "n_complete": len(self.wt_complete_module_ids) - len(broken),
                "n_broken": len(broken),
                "broken_modules": list(broken),
            },
            coverage={
                "deleted_genes_total": len(deleted),
                "deleted_genes_with_ko": len(deleted) - len(without_ko),
                "deleted_genes_without_ko": without_ko,
            },
            provenance=self.provenance,
        )

    def _remaining_kos(self, deleted_genes: set[str] | frozenset[str]) -> set[str]:
        remaining = self.registry.drop(
            index=list(deleted_genes)
        )  # Unknown IDs must fail.
        return set(self.background_kos).union(*remaining.ko_ids)
