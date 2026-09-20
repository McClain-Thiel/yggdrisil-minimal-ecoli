"""Canonical gene and deletion-bundle inspection tools."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict

from yggdrisil_ecoli.constants import is_b_number
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator


class GeneTools:
    """Evidence lookups that accept canonical `b` identifiers only."""

    def __init__(
        self,
        *,
        registry: GeneRegistry,
        essentiality: EssentialityDataset,
        modules: ModuleEvaluator,
    ) -> None:
        self.registry = registry
        self.essentiality = essentiality
        self.modules = modules

    def get_essentiality(self, gene: str) -> dict[str, object]:
        record = self.registry.require(gene)
        return {"symbol": record.symbol, **self.essentiality.detail(record.b_number)}

    def get_gene_info(self, gene: str) -> dict[str, object]:
        record = self.registry.require(gene)
        essentiality = self.essentiality.record(record.b_number)
        return {
            "b_number": record.b_number,
            "symbol": record.symbol,
            "name": record.name,
            "description": record.description,
            "essentiality_classification": essentiality.classification,
            "m9_ecipkm": essentiality.m9_ecipkm,
            "lb_ecipkm": essentiality.lb_ecipkm,
            "ko_ids": record.ko_ids,
            "kegg_modules": self.modules.modules_for_kos(set(record.ko_ids)),
            "iml1515_membership": record.in_iml1515,
        }

    def analyze_gene_set(self, genes: list[str]) -> dict[str, object]:
        counts = Counter(genes)
        duplicates = sorted(gene for gene, count in counts.items() if count > 1)
        invalid = sorted(
            gene
            for gene in counts
            if not is_b_number(gene) or self.registry.get(gene) is None
        )
        valid = tuple(sorted(set(genes) - set(invalid)))
        records = [self.registry.require(gene) for gene in valid]
        classes = Counter(
            self.essentiality.record(record.b_number).classification
            for record in records
        )
        module_genes: dict[str, list[str]] = defaultdict(list)
        for record in records:
            for module in self.modules.modules_for_kos(set(record.ko_ids)):
                module_genes[module].append(record.b_number)
        result = self.modules.score_deleted(set(valid))
        return {
            "valid_genes": list(valid),
            "invalid_ids": invalid,
            "duplicate_ids": duplicates,
            "essentiality_summary": {
                classification: classes.get(classification, 0)
                for classification in (
                    "essential",
                    "conditionally_essential",
                    "nonessential",
                    "ambiguous",
                    "unknown",
                )
            },
            "shared_kegg_modules": {
                module: sorted(support)
                for module, support in sorted(module_genes.items())
                if len(support) > 1
            },
            "modules_likely_affected": sorted(module_genes),
            "modules_broken_if_deleted": [
                asdict(module) for module in result.broken_modules
            ],
            "iml1515": {
                "modeled": [record.b_number for record in records if record.in_iml1515],
                "unmodeled": [
                    record.b_number for record in records if not record.in_iml1515
                ],
            },
            "annotations": [
                {
                    "b_number": record.b_number,
                    "symbol": record.symbol,
                    "description": record.description,
                }
                for record in records
            ],
        }

    def get_module_info(
        self, module_id: str, *, deleted_genes: list[str] | None = None
    ) -> dict[str, object]:
        entry = self.modules.require_entry(module_id)
        deleted = frozenset(deleted_genes or ())
        for gene in deleted:
            self.registry.require(gene)
        evaluation = self.modules.evaluate_deleted(module_id, deleted)
        referenced = self.modules.ko_ids_for_module(module_id)
        return {
            "module_id": module_id,
            "name": entry.name,
            "definition": entry.definition,
            "module_class": entry.module_class,
            "wild_type_complete_catalog_member": (
                module_id in self.modules.wt_complete_module_ids
            ),
            "complete_after_deletion": evaluation.complete,
            "missing_required_kos": list(evaluation.missing_required_kos),
            "minimal_missing_ko_sets": [
                list(option) for option in evaluation.minimal_missing_ko_sets
            ],
            "referenced_kos": sorted(referenced),
            "remaining_gene_support_by_ko": {
                ko: [
                    record.b_number
                    for record in self.registry
                    if record.b_number not in deleted and ko in record.ko_ids
                ]
                for ko in sorted(referenced)
            },
            "fixed_background_kos": sorted(
                referenced.intersection(self.modules.background_kos)
            ),
        }
