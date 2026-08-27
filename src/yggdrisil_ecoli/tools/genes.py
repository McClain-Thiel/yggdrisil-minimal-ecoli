"""Compact canonical-gene and deletion-bundle inspection tools."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from yggdrisil_ecoli.constants import is_b_number
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRecord, GeneRegistry
from yggdrisil_ecoli.scorers.modules import ModuleCatalog


@dataclass(frozen=True, slots=True)
class GeneInfo:
    b_number: str
    symbol: str | None
    name: str | None
    description: str | None
    essentiality_classification: str
    m9_ecipkm: float | None
    lb_ecipkm: float | None
    ko_ids: tuple[str, ...]
    kegg_modules: tuple[str, ...]
    iml1515_membership: bool
    published_wcm_membership: bool | None
    current_vecoli_membership: bool | None


class GeneTools:
    """Evidence tools that accept only canonical `b` identifiers."""

    def __init__(
        self,
        *,
        registry: GeneRegistry,
        essentiality: EssentialityDataset,
        modules: ModuleCatalog,
    ) -> None:
        self.registry = registry
        self.essentiality = essentiality
        self.modules = modules
        self.modules.validate_registry(registry)
        self._wcm_loaded = any(record.in_published_wcm for record in registry)
        self._vecoli_loaded = any(record.in_current_vecoli for record in registry)

    def get_essentiality(self, gene: str) -> dict[str, object]:
        """Return target summary plus the complete source-observation detail."""

        record = self.registry.require(gene)
        summary = self.essentiality.summary(record.b_number)
        observations = self.essentiality.observations(record.b_number)
        return {
            "canonical_gene": record.b_number,
            "symbol": record.symbol,
            "classification": summary.classification,
            "cross_condition_pattern": summary.cross_condition_pattern,
            "evidence_conflict": summary.evidence_conflict,
            "basis_observation_ids": list(summary.basis_observation_ids),
            "observations": [asdict(observation) for observation in observations],
            "coverage": "measured" if observations else "unknown",
        }

    def get_gene_info(self, gene: str) -> dict[str, object]:
        """Return a concise evidence summary without translating symbols."""

        record = self.registry.require(gene)
        summary = self.essentiality.summary(record.b_number)
        info = GeneInfo(
            b_number=record.b_number,
            symbol=record.symbol,
            name=record.name,
            description=record.description,
            essentiality_classification=summary.classification,
            m9_ecipkm=summary.m9_ecipkm,
            lb_ecipkm=summary.lb_ecipkm,
            ko_ids=record.ko_ids,
            kegg_modules=self.modules.modules_for_kos(set(record.ko_ids)),
            iml1515_membership=record.in_iml1515,
            published_wcm_membership=(
                record.in_published_wcm if self._wcm_loaded else None
            ),
            current_vecoli_membership=(
                record.in_current_vecoli if self._vecoli_loaded else None
            ),
        )
        return asdict(info)

    def analyze_gene_set(self, genes: list[str]) -> dict[str, object]:
        """Expose batch evidence for a proposed direct deletion bundle."""

        counts = Counter(genes)
        duplicate_ids = sorted(
            identifier for identifier, count in counts.items() if count > 1
        )
        invalid_ids = sorted(
            identifier
            for identifier in counts
            if not is_b_number(identifier) or self.registry.get(identifier) is None
        )
        valid_ids = tuple(sorted(set(genes) - set(invalid_ids)))
        records = [self.registry.require(b_number) for b_number in valid_ids]
        essentiality_counts = Counter(
            self.essentiality.summary(record.b_number).classification
            for record in records
        )
        module_to_genes: dict[str, list[str]] = defaultdict(list)
        for record in records:
            for module_id in self.modules.modules_for_kos(set(record.ko_ids)):
                module_to_genes[module_id].append(record.b_number)
        shared_modules = {
            module_id: sorted(module_genes)
            for module_id, module_genes in sorted(module_to_genes.items())
            if len(module_genes) > 1
        }
        module_result = self.modules.score_deleted(set(valid_ids), self.registry)
        return {
            "valid_genes": list(valid_ids),
            "invalid_ids": invalid_ids,
            "duplicate_ids": duplicate_ids,
            "essentiality_summary": {
                classification: essentiality_counts.get(classification, 0)
                for classification in (
                    "essential",
                    "conditionally_essential",
                    "nonessential",
                    "ambiguous",
                    "unknown",
                )
            },
            "shared_kegg_modules": shared_modules,
            "modules_likely_affected": sorted(module_to_genes),
            "modules_broken_if_deleted": [
                asdict(module) for module in module_result.broken_modules
            ],
            "iml1515": {
                "modeled": [record.b_number for record in records if record.in_iml1515],
                "unmodeled": [
                    record.b_number for record in records if not record.in_iml1515
                ],
            },
            "published_wcm": _membership_summary(
                records,
                source_loaded=self._wcm_loaded,
                field="in_published_wcm",
            ),
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
        """Inspect one frozen module in wild type or a candidate deletion state."""

        entry = self.modules.require_entry(module_id)
        deleted = frozenset(deleted_genes or ())
        for b_number in deleted:
            self.registry.require(b_number)
        evaluation = self.modules.evaluate_deleted(module_id, deleted, self.registry)
        referenced = self.modules.ko_ids_for_module(module_id)
        remaining_support: dict[str, list[str]] = {
            ko_id: [
                record.b_number
                for record in self.registry
                if record.b_number not in deleted and ko_id in record.ko_ids
            ]
            for ko_id in sorted(referenced)
        }
        fixed_background = sorted(referenced.intersection(self.modules.background_kos))
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
            "remaining_gene_support_by_ko": remaining_support,
            "fixed_background_kos": fixed_background,
        }


def _membership_summary(
    records: list[GeneRecord], *, source_loaded: bool, field: str
) -> dict[str, object]:
    if not source_loaded:
        return {"coverage": "not_integrated", "present": [], "absent": []}
    return {
        "coverage": "available",
        "present": [
            record.b_number for record in records if bool(getattr(record, field))
        ],
        "absent": [
            record.b_number for record in records if not bool(getattr(record, field))
        ],
    }
