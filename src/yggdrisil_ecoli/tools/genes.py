"""Gene evidence from the shared table, with optional blinded display IDs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property

import pandas as pd

from yggdrisil_ecoli.scorers.modules import ModuleEvaluator


@dataclass
class GeneTools:
    genes: pd.DataFrame
    modules: ModuleEvaluator
    aliases: Mapping[str, str] | None = None
    order: Sequence[str] = ()
    deleted_genes: frozenset[str] = frozenset()
    exposed_ids: set[str] | None = None
    max_bundle_size: int | None = None

    @cached_property
    def _canonical_ids(self) -> dict[str, str]:
        return {self.public(gene): gene for gene in self.genes.index}

    def public(self, gene: str) -> str:
        return self.aliases[gene] if self.aliases is not None else gene

    def canonical(self, gene_id: str) -> str:
        if self.exposed_ids is not None and gene_id not in self.exposed_ids:
            raise ValueError("gene identifier was not exposed in this invocation")
        try:
            return self._canonical_ids[gene_id]
        except KeyError as exc:
            raise ValueError(f"unknown candidate gene id: {gene_id}") from exc

    def list_deletion_candidates(
        self, page: int = 0, count: int = 24
    ) -> dict[str, object]:
        """List a reproducible page of undeleted candidates and allowed evidence."""
        if page < 0 or not 1 <= count <= 50:
            raise ValueError("page must be non-negative and count must be 1 to 50")
        available = [
            gene
            for gene in self.order or sorted(self.genes.index)
            if gene not in self.deleted_genes
        ]
        candidates = available[page * count : (page + 1) * count]
        if self.exposed_ids is not None:
            self.exposed_ids.update(self.public(gene) for gene in candidates)
        return {
            "remaining_candidates": len(available),
            "candidates": [
                self.inspect_gene_evidence(self.public(gene)) for gene in candidates
            ],
        }

    def inspect_gene_evidence(self, gene_id: str) -> dict[str, object]:
        """Inspect a table row, exposing only aggregate evidence for blinded IDs."""
        row = self.genes.loc[self.canonical(gene_id)]
        columns = [
            "classification",
            "coverage",
            "condition_disagreement",
            "evidence_conflict",
        ]
        if self.aliases is None:
            columns += [
                "symbol",
                "name",
                "description",
                "ko_ids",
                "lb_call_raw",
                "lb_ecipkm",
                "m9_call_raw",
                "m9_ecipkm",
            ]
        info = row.loc[columns].copy()
        info["gene_id"] = gene_id
        info["in_iml1515"] = pd.notna(row.iml1515_gene_id)
        info["has_ko_mapping"] = bool(len(row.ko_ids))
        info["module_membership_count"] = len(
            self.modules.modules_for_kos(set(row.ko_ids))
        )
        result: dict[str, object] = json.loads(info.to_json())
        return result

    def analyze_deletion_bundle(self, gene_ids: list[str]) -> dict[str, object]:
        """Summarize the cumulative deletion set after adding this proposed bundle."""
        if self.max_bundle_size is not None:
            if not 1 <= len(gene_ids) <= self.max_bundle_size:
                raise ValueError(
                    f"bundle must contain 1 to {self.max_bundle_size} genes"
                )
            if len(set(gene_ids)) != len(gene_ids):
                raise ValueError("bundle contains duplicate genes")
        deleted = self.deleted_genes.union(self.canonical(gene) for gene in gene_ids)
        rows = self.genes.loc[sorted(deleted)]
        broken = self.modules.score_deleted(deleted)
        modeled = int(rows.iml1515_gene_id.notna().sum())
        mapped = int(rows.ko_ids.map(len).gt(0).sum())
        result: dict[str, object] = {
            "proposed_gene_ids": gene_ids,
            "deleted_genes_total": len(deleted),
            "essentiality": json.loads(rows.classification.value_counts().to_json()),
            "model_coverage": {"modeled": modeled, "unmodeled": len(deleted) - modeled},
            "ko_coverage": {"mapped": mapped, "unmapped": len(deleted) - mapped},
            "modules_complete": len(self.modules.wt_complete_module_ids) - len(broken),
            "modules_broken": len(broken),
        }
        if self.aliases is None:
            result.update(deleted_gene_ids=sorted(deleted), broken_module_ids=broken)
        return result

    def inspect_kegg_module(
        self, module_id: str, deleted_gene_ids: list[str] | None = None
    ) -> dict[str, object]:
        """Inspect a module's definition and completeness after a proposed deletion."""
        if self.aliases is not None:
            raise ValueError("module details are unavailable in closed-book mode")
        deleted = self.deleted_genes.union(
            self.canonical(gene) for gene in deleted_gene_ids or ()
        )
        return {
            "module_id": module_id,
            **self.modules.entries[module_id],
            "complete_after_deletion": self.modules.evaluate_deleted(
                module_id, deleted
            ),
        }
