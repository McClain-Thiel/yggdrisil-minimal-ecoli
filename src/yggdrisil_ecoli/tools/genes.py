"""Gene evidence from the shared table, with optional blinded display IDs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property

import pandas as pd
from yggdrisil.agents import ToolInputError

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
    max_gene_inspections: int | None = None
    _inspected_genes: set[str] = field(default_factory=set, init=False)

    @cached_property
    def _canonical_ids(self) -> dict[str, str]:
        return {self.public(gene): gene for gene in self.genes.index}

    def public(self, gene: str) -> str:
        return self.aliases[gene] if self.aliases is not None else gene

    def canonical(self, gene_id: str) -> str:
        if self.exposed_ids is not None and gene_id not in self.exposed_ids:
            raise ToolInputError("gene identifier was not exposed in this invocation")
        try:
            return self._canonical_ids[gene_id]
        except KeyError as exc:
            raise ToolInputError(f"unknown candidate gene id: {gene_id}") from exc

    def resolve_gene_ids(self, identifiers: list[str]) -> list[dict[str, object]]:
        """Resolve 1–100 IDs or names, reporting ambiguity and genes still present."""
        if self.aliases is not None:
            raise ToolInputError(
                "identifier resolution is unavailable in closed-book mode"
            )
        if not 1 <= len(identifiers) <= 100:
            raise ToolInputError("identifiers must contain 1 to 100 entries")
        results: list[dict[str, object]] = []
        columns = [
            "symbol",
            "name",
            "ncbi_gene_id",
            "ecocyc_id",
            "kegg_gene_id",
            "iml1515_gene_id",
        ]
        positions: dict[str, set[int]] = {}
        for column in columns:
            if column not in self.genes.columns:
                continue
            for position, value in enumerate(self.genes[column]):
                if isinstance(value, str):
                    positions.setdefault(value.casefold(), set()).add(position)
        for identifier in identifiers:
            if identifier in self.genes.index:
                candidates = [identifier]
            else:
                candidates = [
                    str(self.genes.index[position])
                    for position in sorted(positions.get(identifier.casefold(), ()))
                ]
            results.append(
                {
                    "input": identifier,
                    "status": "resolved"
                    if len(candidates) == 1
                    else ("ambiguous" if candidates else "unmatched"),
                    "gene_ids": candidates,
                    "remaining_gene_ids": [
                        gene for gene in candidates if gene not in self.deleted_genes
                    ],
                }
            )
        return results

    def get_deleted_genes(self, offset: int = 0, count: int = 50) -> dict[str, object]:
        """Read the exact deletion set without a truncated prompt summary."""
        if offset < 0 or not 1 <= count <= 100:
            raise ToolInputError(
                "offset must be non-negative and count must be 1 to 100"
            )
        genes = sorted(self.public(gene) for gene in self.deleted_genes)
        return {"total": len(genes), "gene_ids": genes[offset : offset + count]}

    def deletion_delta(
        self,
        gene_ids: list[str],
        offset: int = 0,
        count: int = 50,
    ) -> dict[str, object]:
        """Compare a validated target deletion set with the current genotype."""
        if offset < 0 or not 1 <= count <= 100:
            raise ToolInputError(
                "offset must be non-negative and count must be 1 to 100"
            )
        target = {self.canonical(gene) for gene in gene_ids}
        remaining = sorted(target - self.deleted_genes)
        return {
            "target_count": len(target),
            "matched_count": len(target & self.deleted_genes),
            "remaining_count": len(remaining),
            "additional_deletion_count": len(self.deleted_genes - target),
            "remaining_gene_ids": remaining[offset : offset + count],
        }

    def list_deletion_candidates(
        self, page: int = 0, count: int = 24
    ) -> dict[str, object]:
        """List a reproducible page of undeleted candidates and allowed evidence."""
        if page < 0 or not 1 <= count <= 100:
            raise ToolInputError("page must be non-negative and count must be 1 to 100")
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
            "candidates": self.inspect_genes([self.public(gene) for gene in candidates])
            if candidates
            else [],
            "remaining_gene_inspections": self.remaining_gene_inspections,
        }

    @property
    def remaining_gene_inspections(self) -> int | None:
        if self.max_gene_inspections is None:
            return None
        return max(0, self.max_gene_inspections - len(self._inspected_genes))

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

    def inspect_genes(self, gene_ids: list[str]) -> list[dict[str, object]]:
        """Inspect 1–100 IDs; skip deleted genes and evidence already supplied.

        Preview, candidate pages and inspections share one per-turn evidence limit.
        When exhausted, propose actions from existing evidence or explain abstention.
        """
        if not 1 <= len(gene_ids) <= 100:
            raise ToolInputError("gene_ids must contain 1 to 100 identifiers")
        results: list[dict[str, object]] = []
        for gene_id in gene_ids:
            try:
                gene = self.canonical(gene_id)
                if gene in self.deleted_genes:
                    results.append({"gene_id": gene_id, "status": "already_deleted"})
                    continue
                if self.max_gene_inspections is not None:
                    if gene in self._inspected_genes:
                        results.append(
                            {"gene_id": gene_id, "status": "already_inspected"}
                        )
                        continue
                    if self.remaining_gene_inspections == 0:
                        raise ToolInputError(
                            "Gene inspection limit reached. Use evidence already supplied "
                            "to return actions, or return no actions and explain why."
                        )
                results.append(self.inspect_gene_evidence(gene_id))
                self._inspected_genes.add(gene)
            except ToolInputError as exc:
                results.append({"gene_id": gene_id, "error": str(exc)})
        return results

    def analyze_deletion_bundle(self, gene_ids: list[str]) -> dict[str, object]:
        """Summarize the cumulative deletion set after adding this proposed bundle."""
        if self.max_bundle_size is not None:
            if not 1 <= len(gene_ids) <= self.max_bundle_size:
                raise ToolInputError(
                    f"bundle must contain 1 to {self.max_bundle_size} genes"
                )
            if len(set(gene_ids)) != len(gene_ids):
                raise ToolInputError("bundle contains duplicate genes")
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
            raise ToolInputError("module details are unavailable in closed-book mode")
        if module_id not in self.modules.entries:
            raise ToolInputError(f"Unknown KEGG module: {module_id}")
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
