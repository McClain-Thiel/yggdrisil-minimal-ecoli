"""Check one-to-one identifiers and count crosswalk coverage."""

from collections import defaultdict

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry


def audit_registry(registry: GeneRegistry) -> dict[str, object]:
    coverage = {}
    for namespace, field in {
        "ncbi_gene": "ncbi_gene_id",
        "ecocyc": "ecocyc_id",
        "kegg_gene": "kegg_gene_id",
        "iml1515": "iml1515_gene_id",
    }.items():
        inverse: dict[str, list[str]] = defaultdict(list)
        for gene in registry:
            if identifier := getattr(gene, field):
                inverse[identifier].append(gene.b_number)
        conflicts = {key: ids for key, ids in inverse.items() if len(ids) > 1}
        if conflicts:
            raise DataValidationError(f"ambiguous {namespace} mappings: {conflicts}")
        coverage[namespace] = len(inverse)
    coverage["ko"] = sum(bool(gene.ko_ids) for gene in registry)
    return {"canonical_protein_coding_genes": len(registry), "coverage": coverage}
