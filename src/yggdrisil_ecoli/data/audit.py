"""Check one-to-one identifiers and count crosswalk coverage."""

import pandas as pd

from yggdrisil_ecoli.data.errors import DataValidationError


def audit_registry(genes: pd.DataFrame) -> dict[str, object]:
    coverage = {}
    for namespace, column in {
        "ncbi_gene": "ncbi_gene_id",
        "ecocyc": "ecocyc_id",
        "kegg_gene": "kegg_gene_id",
        "iml1515": "iml1515_gene_id",
    }.items():
        mapped = genes[column].dropna()
        conflicts = mapped[mapped.duplicated(keep=False)]
        if not conflicts.empty:
            raise DataValidationError(
                f"ambiguous {namespace} mappings: {conflicts.to_dict()}"
            )
        coverage[namespace] = len(mapped)
    coverage["ko"] = int(genes.ko_ids.map(len).gt(0).sum())
    return {"canonical_protein_coding_genes": len(genes), "coverage": coverage}
