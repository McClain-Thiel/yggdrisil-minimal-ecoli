"""Join KEGG and iML1515 identifiers to the fixed MG1655 gene universe."""

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

from yggdrisil_ecoli.data.errors import DataValidationError


def add_crosswalks(
    registry: pd.DataFrame,
    gene_list: Path,
    ko_links: Path,
    model: Path,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Annotate existing genes; report unmatched IDs without adding search genes."""

    with gene_list.open() as stream:
        listed = [row[0] for row in csv.reader(stream, delimiter="\t") if row]
    if len(listed) != len(set(listed)) or any(
        not re.fullmatch(r"eco:b\d{4}", identifier) for identifier in listed
    ):
        raise DataValidationError("duplicate or malformed KEGG gene ID")
    listed_genes = {identifier.removeprefix("eco:") for identifier in listed}
    kos: dict[str, set[str]] = defaultdict(set)
    for number, line in enumerate(ko_links.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        match = re.fullmatch(r"eco:(b\d{4})\tko:(K\d{5})", line)
        if match is None:
            raise DataValidationError(f"{ko_links}:{number}: malformed KEGG KO link")
        gene_id, ko_id = match.groups()
        kos[gene_id].add(ko_id)

    model_ids = [gene["id"] for gene in json.loads(model.read_text())["genes"]]
    if any(not isinstance(identifier, str) for identifier in model_ids):
        raise DataValidationError("iML1515 gene IDs must be strings")
    if len(model_ids) != len(set(model_ids)):
        raise DataValidationError("duplicate iML1515 gene ID")
    model_genes = set(model_ids) - {"s0001"}  # COBRA's non-biological placeholder.
    mapped = registry.assign(
        kegg_gene_id=[
            f"eco:{tag}" if tag in listed_genes else None for tag in registry.index
        ],
        ko_ids=[tuple(sorted(kos[tag])) for tag in registry.index],
        iml1515_gene_id=[tag if tag in model_genes else None for tag in registry.index],
    )
    return mapped, {
        "unresolved_identifiers": {
            "kegg": sorted(
                f"eco:{gene}"
                for gene in (listed_genes | kos.keys()) - set(registry.index)
            ),
            "iml1515": sorted(model_genes - set(registry.index)),
        },
        "excluded_model_ids": sorted(set(model_ids) & {"s0001"}),
    }
