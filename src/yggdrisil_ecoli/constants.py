"""Frozen biological scope for the v1 benchmark."""

from __future__ import annotations

import re

ORGANISM_NAME = "Escherichia coli K-12 MG1655"
ASSEMBLY_ACCESSION = "GCF_000005845.2"
ASSEMBLY_NAME = "ASM584v2"
REFERENCE_ACCESSION = "NC_000913.3"
TAXONOMY_ID = "511145"
STRAIN = "K-12"
SUBSTRAIN = "MG1655"
GENE_TYPE = "protein_coding"
KEGG_ORGANISM = "eco"

B_NUMBER_PATTERN = re.compile(r"^b[0-9]{4}$")


def is_b_number(value: str) -> bool:
    """Return whether *value* is a canonical MG1655 locus tag."""

    return B_NUMBER_PATTERN.fullmatch(value) is not None
