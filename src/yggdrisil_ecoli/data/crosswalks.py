"""Source-specific crosswalks that annotate the canonical registry."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from yggdrisil_ecoli.constants import KEGG_ORGANISM, is_b_number
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry

_IML1515_NONBIOLOGICAL_IDS = frozenset({"s0001"})


@dataclass(slots=True)
class CrosswalkDiagnostics:
    """Non-fatal mapping gaps that must remain visible in build output."""

    unresolved_identifiers: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add_unresolved(self, source: str, identifier: str) -> None:
        self.unresolved_identifiers.setdefault(source, []).append(identifier)

    def normalize(self) -> None:
        self.unresolved_identifiers = {
            source: sorted(set(values))
            for source, values in sorted(self.unresolved_identifiers.items())
        }
        self.notes = sorted(set(self.notes))

    @property
    def unresolved_count(self) -> int:
        return sum(len(values) for values in self.unresolved_identifiers.values())


def add_kegg_crosswalk(
    registry: GeneRegistry,
    gene_list_path: str | Path,
    ko_link_path: str | Path,
    diagnostics: CrosswalkDiagnostics,
) -> GeneRegistry:
    """Attach a frozen KEGG `eco` gene/KO snapshot by canonical locus tag."""

    listed_genes = _parse_kegg_gene_list(Path(gene_list_path))
    ko_by_gene = _parse_kegg_ko_links(Path(ko_link_path))
    universe = registry.search_universe
    for b_number in sorted(listed_genes | set(ko_by_gene)):
        if b_number not in universe:
            diagnostics.add_unresolved("kegg", f"{KEGG_ORGANISM}:{b_number}")

    records = []
    for record in registry:
        kegg_id = (
            f"{KEGG_ORGANISM}:{record.b_number}"
            if record.b_number in listed_genes
            else None
        )
        records.append(
            replace(
                record,
                kegg_gene_id=kegg_id,
                ko_ids=tuple(sorted(ko_by_gene.get(record.b_number, set()))),
            )
        )
    diagnostics.normalize()
    return GeneRegistry(records)


def add_iml1515_membership(
    registry: GeneRegistry,
    model_path: str | Path,
    diagnostics: CrosswalkDiagnostics,
) -> GeneRegistry:
    """Attach membership from the frozen publication-supplement iML1515 JSON."""

    with Path(model_path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_genes = payload.get("genes")
    if not isinstance(raw_genes, list):
        raise DataValidationError("iML1515 JSON does not contain a genes list")
    model_ids: set[str] = set()
    for item in raw_genes:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise DataValidationError("iML1515 contains a gene without a string ID")
        identifier = item["id"]
        if identifier in model_ids:
            raise DataValidationError(f"duplicate iML1515 gene ID: {identifier}")
        model_ids.add(identifier)

    universe = registry.search_universe
    for identifier in sorted(model_ids):
        if identifier in _IML1515_NONBIOLOGICAL_IDS:
            diagnostics.notes.append(
                f"Excluded iML1515 non-biological placeholder gene {identifier}."
            )
            continue
        if not is_b_number(identifier) or identifier not in universe:
            diagnostics.add_unresolved("iml1515", identifier)

    records = [
        replace(
            record,
            iml1515_gene_id=(record.b_number if record.b_number in model_ids else None),
            in_iml1515=record.b_number in model_ids,
        )
        for record in registry
    ]
    diagnostics.normalize()
    return GeneRegistry(records)


def _parse_kegg_gene_list(path: Path) -> set[str]:
    result: set[str] = set()
    for line_number, fields in _tab_rows(path):
        identifier = fields[0]
        prefix, separator, b_number = identifier.partition(":")
        if not separator or prefix != KEGG_ORGANISM or not is_b_number(b_number):
            raise DataValidationError(
                f"{path}:{line_number}: malformed KEGG gene ID {identifier!r}"
            )
        if b_number in result:
            raise DataValidationError(
                f"{path}:{line_number}: duplicate KEGG gene ID {identifier}"
            )
        result.add(b_number)
    return result


def _parse_kegg_ko_links(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for line_number, fields in _tab_rows(path, expected_fields=2):
        gene_id, ko_id = fields
        prefix, separator, b_number = gene_id.partition(":")
        ko_prefix, ko_separator, ko_value = ko_id.partition(":")
        if (
            not separator
            or prefix != KEGG_ORGANISM
            or not is_b_number(b_number)
            or not ko_separator
            or ko_prefix != "ko"
            or len(ko_value) != 6
            or not ko_value.startswith("K")
            or not ko_value[1:].isdigit()
        ):
            raise DataValidationError(f"{path}:{line_number}: malformed KEGG KO link")
        result.setdefault(b_number, set()).add(ko_value)
    return result


def _tab_rows(
    path: Path, expected_fields: int | None = None
) -> Iterable[tuple[int, list[str]]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            fields = line.rstrip("\n").split("\t")
            if expected_fields is not None and len(fields) != expected_fields:
                raise DataValidationError(
                    f"{path}:{line_number}: expected {expected_fields} tab fields"
                )
            if not fields or not fields[0]:
                raise DataValidationError(f"{path}:{line_number}: empty record")
            yield line_number, fields
