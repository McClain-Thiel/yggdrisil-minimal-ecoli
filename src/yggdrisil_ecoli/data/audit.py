"""Crosswalk invariants and human-readable coverage reporting."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from yggdrisil_ecoli.data.crosswalks import CrosswalkDiagnostics
from yggdrisil_ecoli.data.registry import GeneRecord, GeneRegistry

_CROSSWALK_FIELDS = {
    "ncbi_gene": "ncbi_gene_id",
    "ecocyc": "ecocyc_id",
    "kegg_gene": "kegg_gene_id",
    "iml1515": "iml1515_gene_id",
}


@dataclass(slots=True)
class AuditReport:
    canonical_protein_coding_genes: int
    coverage: dict[str, int]
    duplicate_b_numbers: list[str] = field(default_factory=list)
    ambiguous_mappings: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    unresolved_identifiers: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def unresolved_count(self) -> int:
        return sum(len(values) for values in self.unresolved_identifiers.values())

    @property
    def ambiguous_count(self) -> int:
        return sum(len(identifiers) for identifiers in self.ambiguous_mappings.values())

    @property
    def fatal_errors(self) -> list[str]:
        errors = []
        if self.duplicate_b_numbers:
            errors.append(
                f"duplicate canonical IDs: {', '.join(self.duplicate_b_numbers)}"
            )
        for namespace, identifiers in self.ambiguous_mappings.items():
            errors.append(
                f"{namespace} identifiers map to multiple canonical genes: "
                f"{', '.join(sorted(identifiers))}"
            )
        return errors

    def as_dict(self) -> dict[str, object]:
        total = self.canonical_protein_coding_genes
        return {
            "canonical_protein_coding_genes": total,
            "coverage": {
                name: {
                    "mapped": mapped,
                    "total": total,
                    "percent": round(_percent(mapped, total), 4),
                }
                for name, mapped in sorted(self.coverage.items())
            },
            "duplicate_b_numbers": self.duplicate_b_numbers,
            "ambiguous_mappings": self.ambiguous_mappings,
            "unresolved_identifiers": self.unresolved_identifiers,
            "unresolved_count": self.unresolved_count,
            "ambiguous_count": self.ambiguous_count,
            "notes": self.notes,
            "fatal_errors": self.fatal_errors,
        }

    def render_text(self) -> str:
        total = self.canonical_protein_coding_genes

        def line(label: str, key: str) -> str:
            mapped = self.coverage[key]
            return f"{label:<30}{mapped:>5} / {total} ({_percent(mapped, total):6.2f}%)"

        output = [
            f"Canonical protein-coding MG1655 genes: {total}",
            "",
            line("NCBI Gene mapped:", "ncbi_gene"),
            line("EcoCyc mapped:", "ecocyc"),
            line("KEGG gene mapped:", "kegg_gene"),
            line("KO mapped:", "ko"),
            "",
            f"Present in iML1515:          {self.coverage['iml1515']}",
            "",
            f"Duplicate b-numbers:         {len(self.duplicate_b_numbers)}",
            f"Ambiguous mappings:          {self.ambiguous_count}",
            f"Unresolved identifiers:      {self.unresolved_count}",
        ]
        if self.notes:
            output.extend(["", "Notes:", *(f"- {note}" for note in self.notes)])
        if self.fatal_errors:
            output.extend(
                [
                    "",
                    "Fatal audit errors:",
                    *(f"- {item}" for item in self.fatal_errors),
                ]
            )
        return "\n".join(output) + "\n"


def audit_registry(
    records: GeneRegistry | list[GeneRecord],
    diagnostics: CrosswalkDiagnostics | None = None,
) -> AuditReport:
    """Audit canonical uniqueness, crosswalk uniqueness, and mapping coverage."""

    rows = list(records)
    total = len(rows)
    b_counts = Counter(record.b_number for record in rows)

    ambiguous: dict[str, dict[str, list[str]]] = {}
    for namespace, field_name in _CROSSWALK_FIELDS.items():
        inverse: dict[str, set[str]] = defaultdict(set)
        for record in rows:
            value = getattr(record, field_name)
            if value is not None:
                inverse[value].add(record.b_number)
        conflicts = {
            value: sorted(b_numbers)
            for value, b_numbers in inverse.items()
            if len(b_numbers) > 1
        }
        if conflicts:
            ambiguous[namespace] = conflicts

    coverage = {
        name: sum(getattr(record, field_name) is not None for record in rows)
        for name, field_name in _CROSSWALK_FIELDS.items()
    }
    coverage["ko"] = sum(bool(record.ko_ids) for record in rows)
    unresolved = diagnostics.unresolved_identifiers if diagnostics else {}
    notes = diagnostics.notes if diagnostics else []
    return AuditReport(
        canonical_protein_coding_genes=total,
        coverage=coverage,
        duplicate_b_numbers=sorted(
            b_number for b_number, count in b_counts.items() if count > 1
        ),
        ambiguous_mappings=ambiguous,
        unresolved_identifiers=unresolved,
        notes=notes,
    )


def write_audit(report: AuditReport, json_path: Path, text_path: Path) -> None:
    """Atomically persist machine- and human-readable audit artifacts."""

    write_json(json_path, report.as_dict())
    _atomic_text(text_path, report.render_text())


def write_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically write a deterministic JSON object."""

    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content.rstrip("\n") + "\n")
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _percent(mapped: int, total: int) -> float:
    return 100.0 * mapped / total if total else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "registry",
        nargs="?",
        type=Path,
        default=Path("data/processed/gene_registry.parquet"),
    )
    args = parser.parse_args()
    report = audit_registry(GeneRegistry.from_parquet(args.registry))
    print(report.render_text(), end="")
    if report.fatal_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
