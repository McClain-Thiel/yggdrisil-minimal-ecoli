"""Compact, condition-aware experimental essentiality evidence."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

import pyarrow as pa
import pyarrow.parquet as pq

from yggdrisil_ecoli.constants import REFERENCE_ACCESSION, is_b_number
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry

EssentialityClass = Literal[
    "essential", "conditionally_essential", "nonessential", "ambiguous", "unknown"
]
Coverage = Literal["measured", "unknown"]
SourceCall = Literal["E", "NE"]

ESSENTIALITY_SCHEMA = pa.schema(
    [
        pa.field("b_number", pa.string(), nullable=False),
        pa.field("classification", pa.string(), nullable=False),
        pa.field("coverage", pa.string(), nullable=False),
        pa.field("lb_call_raw", pa.string()),
        pa.field("lb_ecipkm", pa.float64()),
        pa.field("m9_call_raw", pa.string()),
        pa.field("m9_ecipkm", pa.float64()),
    ]
)

_METADATA_KEY = b"yggdrisil_ecoli.essentiality"
_STUDY_METADATA: dict[str, object] = {
    "schema_version": 2,
    "study_id": "choe2023_tnseq",
    "doi": "10.1128/msystems.00896-22",
    "license_spdx": "CC-BY-4.0",
    "strain": "Escherichia coli K-12 MG1655",
    "reference_accession": REFERENCE_ACCESSION,
    "lb_medium": "Luria-Bertani rich medium",
    "m9_medium": "M9 minimal salts with 0.2% w/v D-glucose",
    "m9_glucose_g_l": 2.0,
    "oxygenation": "aerobic",
    "temperature_c": 37.0,
    "perturbation": "Tn5 transposon insertion library",
    "effect_metric": "ecIPKM",
    "threshold_rule": "ecIPKM <= 2.2",
    "classification_basis": "author_call_validated_against_threshold",
}
_EXPECTED_SOURCE_COUNTS = {
    "source_rows": 4498,
    "protein_coding_nonpseudo_rows": 4140,
    "lb_essential": 422,
    "lb_nonessential": 3718,
    "m9_essential": 545,
    "m9_nonessential": 3595,
}
_IDENTITY_HEADERS = (
    "Gene",
    "Start",
    "End",
    "Length (nt)",
    "Strand",
    "Locus Tag",
    "CDS",
    "Pseudo",
    "PEC",
    "Gerdes",
)
_ASSAY_HEADERS = ("Insertion", "IPKM", "ec Insertion", "ecIPKM", "Essentiality")


@dataclass(frozen=True, slots=True)
class EssentialityRecord:
    """One canonical gene's LB/M9 evidence, including explicit unknown coverage."""

    b_number: str
    classification: EssentialityClass
    coverage: Coverage
    lb_call_raw: SourceCall | None
    lb_ecipkm: float | None
    m9_call_raw: SourceCall | None
    m9_ecipkm: float | None

    def __post_init__(self) -> None:
        if not is_b_number(self.b_number):
            raise DataValidationError(f"malformed canonical ID: {self.b_number!r}")
        values = (self.lb_call_raw, self.lb_ecipkm, self.m9_call_raw, self.m9_ecipkm)
        if self.coverage == "unknown":
            if self.classification != "unknown" or any(
                value is not None for value in values
            ):
                raise DataValidationError(
                    f"{self.b_number}: unknown coverage must not contain measurements"
                )
            return
        if self.coverage != "measured":
            raise DataValidationError(
                f"{self.b_number}: invalid essentiality coverage {self.coverage!r}"
            )
        if self.lb_call_raw not in {"E", "NE"} or self.m9_call_raw not in {"E", "NE"}:
            raise DataValidationError(
                f"{self.b_number}: measured calls must be E or NE"
            )
        if self.lb_ecipkm is None or self.m9_ecipkm is None:
            raise DataValidationError(
                f"{self.b_number}: measured ecIPKM values are required"
            )
        _validate_call(self.lb_call_raw, self.lb_ecipkm, f"{self.b_number} LB")
        _validate_call(self.m9_call_raw, self.m9_ecipkm, f"{self.b_number} M9")
        expected = _classification(self.lb_call_raw, self.m9_call_raw)
        if self.classification != expected:
            raise DataValidationError(
                f"{self.b_number}: classification must be {expected!r}, "
                f"got {self.classification!r}"
            )

    @property
    def condition_disagreement(self) -> bool:
        return self.coverage == "measured" and self.lb_call_raw != self.m9_call_raw

    @property
    def evidence_conflict(self) -> bool:
        """Flag LB-essential/M9-nonessential evidence as unsafe ambiguity."""

        return self.lb_call_raw == "E" and self.m9_call_raw == "NE"

    @property
    def cross_condition_pattern(self) -> str | None:
        if self.coverage == "unknown":
            return None
        return f"LB_{self.lb_call_raw}/M9_{self.m9_call_raw}"

    @property
    def basis_observation_ids(self) -> tuple[str, ...]:
        if self.coverage == "unknown":
            return ()
        prefix = f"{_STUDY_METADATA['study_id']}:{self.b_number}"
        return (f"{prefix}:lb", f"{prefix}:m9_glucose")


@dataclass(frozen=True, slots=True)
class EssentialityImportReport:
    source_rows: int
    protein_coding_nonpseudo_rows: int
    source_call_counts: dict[str, int]
    mapped_source_genes: int
    unmapped_source_ids: tuple[str, ...]
    canonical_genes_without_measurement: tuple[str, ...]
    coordinate_mismatches: tuple[str, ...]
    summary_counts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class EssentialityDataset:
    """Validated one-row-per-gene evidence artifact and lookup layer."""

    def __init__(
        self,
        records: Iterable[EssentialityRecord],
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        by_id: dict[str, EssentialityRecord] = {}
        for record in records:
            if record.b_number in by_id:
                raise DataValidationError(
                    f"duplicate essentiality b_number: {record.b_number}"
                )
            by_id[record.b_number] = record
        if not by_id:
            raise DataValidationError("essentiality dataset is empty")
        self._by_id = by_id
        supplied_metadata = dict(metadata or {})
        changed_constants = {
            key
            for key, value in _STUDY_METADATA.items()
            if key in supplied_metadata and supplied_metadata[key] != value
        }
        if changed_constants:
            raise DataValidationError(
                f"essentiality metadata changes fixed fields: {sorted(changed_constants)}"
            )
        self.metadata = {**_STUDY_METADATA, **supplied_metadata}

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[EssentialityRecord]:
        for b_number in sorted(self._by_id):
            yield self._by_id[b_number]

    def record(self, b_number: str) -> EssentialityRecord:
        if not is_b_number(b_number):
            raise DataValidationError(
                f"expected a canonical b-number, got {b_number!r}"
            )
        try:
            return self._by_id[b_number]
        except KeyError as exc:
            raise KeyError(
                f"gene is absent from essentiality data: {b_number}"
            ) from exc

    # Keep the concise lookup name used by policies and tools.
    summary = record

    def detail(self, b_number: str) -> dict[str, object]:
        """Return compact agent-facing evidence without repeated artifact constants."""

        record = self.record(b_number)
        return {
            **asdict(record),
            "cross_condition_pattern": record.cross_condition_pattern,
            "condition_disagreement": record.condition_disagreement,
            "evidence_conflict": record.evidence_conflict,
            "basis_observation_ids": list(record.basis_observation_ids),
            "source": dict(self.metadata),
        }

    @classmethod
    def from_parquet(cls, path: str | Path) -> EssentialityDataset:
        table = pq.read_table(path)
        if not table.schema.remove_metadata().equals(ESSENTIALITY_SCHEMA):
            raise DataValidationError(
                f"unexpected essentiality schema: {table.schema.remove_metadata()}"
            )
        raw_metadata = (
            table.schema.metadata.get(_METADATA_KEY) if table.schema.metadata else None
        )
        if raw_metadata is None:
            raise DataValidationError("essentiality artifact lacks dataset metadata")
        try:
            metadata = json.loads(raw_metadata)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataValidationError(
                "essentiality artifact metadata is malformed"
            ) from exc
        if not isinstance(metadata, dict):
            raise DataValidationError("essentiality artifact metadata is malformed")
        return cls(
            (EssentialityRecord(**row) for row in table.to_pylist()),
            metadata=metadata,
        )

    def to_parquet(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            _METADATA_KEY: json.dumps(
                self.metadata, sort_keys=True, separators=(",", ":")
            ).encode()
        }
        table = pa.Table.from_pylist(
            [asdict(record) for record in self],
            schema=ESSENTIALITY_SCHEMA.with_metadata(metadata),
        )
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        try:
            pq.write_table(
                table,
                temporary,
                compression="zstd",
                use_dictionary=True,
                write_statistics=True,
            )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def parse_choe_workbook(
    path: str | Path,
    registry: GeneRegistry,
    *,
    expected_source_counts: dict[str, int] | None = _EXPECTED_SOURCE_COUNTS,
    metadata: Mapping[str, object] | None = None,
) -> tuple[EssentialityDataset, EssentialityImportReport]:
    """Parse checksum-pinned Choe 2023 Table S1 into one row per gene."""

    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if workbook.sheetnames != ["Table S1"]:
            raise DataValidationError(
                f"unexpected Choe workbook sheets: {workbook.sheetnames}"
            )
        worksheet = workbook["Table S1"]
        headers = tuple(
            tuple(
                cell.value
                for cell in next(worksheet.iter_rows(min_row=row, max_row=row))
            )
            for row in (1, 2)
        )
        _validate_headers(*headers)
        source_rows = list(worksheet.iter_rows(min_row=3, values_only=True))
    finally:
        workbook.close()

    protein_rows = [row for row in source_rows if row[6] == "Y" and row[7] == "N"]
    source_counts = {
        "lb_essential": sum(row[14] == "E" for row in protein_rows),
        "lb_nonessential": sum(row[14] == "NE" for row in protein_rows),
        "m9_essential": sum(row[19] == "E" for row in protein_rows),
        "m9_nonessential": sum(row[19] == "NE" for row in protein_rows),
    }
    observed_contract = {
        "source_rows": len(source_rows),
        "protein_coding_nonpseudo_rows": len(protein_rows),
        **source_counts,
    }
    if (
        expected_source_counts is not None
        and observed_contract != expected_source_counts
    ):
        raise DataValidationError(
            f"Choe source snapshot contract changed: {observed_contract}"
        )

    mapped: dict[str, EssentialityRecord] = {}
    unmapped: list[str] = []
    coordinate_mismatches: list[str] = []
    for row_number, row in enumerate(protein_rows, start=3):
        b_number = _text(row[5], f"row {row_number} locus tag")
        if not is_b_number(b_number):
            raise DataValidationError(
                f"row {row_number}: malformed source locus tag {b_number!r}"
            )
        lb_call, lb_ecipkm = _source_evidence(row, row_number, 13, 14)
        m9_call, m9_ecipkm = _source_evidence(row, row_number, 18, 19)
        if b_number not in registry.search_universe:
            unmapped.append(b_number)
            continue
        if b_number in mapped:
            raise DataValidationError(f"duplicate Choe source locus tag: {b_number}")
        reference = registry.require(b_number)
        if (
            _integer(row[1], f"{b_number} start"),
            _integer(row[2], f"{b_number} end"),
        ) != (
            reference.start,
            reference.end,
        ):
            coordinate_mismatches.append(b_number)
        mapped[b_number] = EssentialityRecord(
            b_number=b_number,
            classification=_classification(lb_call, m9_call),
            coverage="measured",
            lb_call_raw=lb_call,
            lb_ecipkm=lb_ecipkm,
            m9_call_raw=m9_call,
            m9_ecipkm=m9_ecipkm,
        )

    records = tuple(
        mapped.get(
            gene.b_number,
            EssentialityRecord(
                b_number=gene.b_number,
                classification="unknown",
                coverage="unknown",
                lb_call_raw=None,
                lb_ecipkm=None,
                m9_call_raw=None,
                m9_ecipkm=None,
            ),
        )
        for gene in registry
    )
    missing = tuple(sorted(registry.search_universe - mapped.keys()))
    summary_counts = {
        str(classification): count
        for classification, count in Counter(
            record.classification for record in records
        ).items()
    }
    return EssentialityDataset(records, metadata=metadata), EssentialityImportReport(
        source_rows=len(source_rows),
        protein_coding_nonpseudo_rows=len(protein_rows),
        source_call_counts=source_counts,
        mapped_source_genes=len(mapped),
        unmapped_source_ids=tuple(sorted(unmapped)),
        canonical_genes_without_measurement=missing,
        coordinate_mismatches=tuple(sorted(coordinate_mismatches)),
        summary_counts=summary_counts,
    )


def _classification(lb_call: SourceCall, m9_call: SourceCall) -> EssentialityClass:
    if m9_call == "E":
        return "essential" if lb_call == "E" else "conditionally_essential"
    return "ambiguous" if lb_call == "E" else "nonessential"


def _source_evidence(
    row: tuple[object, ...], row_number: int, ecipkm_index: int, call_index: int
) -> tuple[SourceCall, float]:
    call = _text(row[call_index], f"row {row_number} essentiality")
    if call not in {"E", "NE"}:
        raise DataValidationError(f"row {row_number}: unexpected call {call!r}")
    ecipkm = _number(row[ecipkm_index], f"row {row_number} ecIPKM")
    typed_call = cast(SourceCall, call)
    _validate_call(typed_call, ecipkm, f"row {row_number}")
    return typed_call, ecipkm


def _validate_call(call: SourceCall, ecipkm: float, label: str) -> None:
    if (ecipkm <= 2.2) != (call == "E"):
        raise DataValidationError(
            f"{label}: author call disagrees with ecIPKM threshold"
        )


def _validate_headers(first: tuple[object, ...], second: tuple[object, ...]) -> None:
    if (
        first[0:10] != _IDENTITY_HEADERS
        or first[10] != "LB medium"
        or first[15] != "M9 glucose (0.2%) medium"
        or second[10:15] != _ASSAY_HEADERS
        or second[15:20] != _ASSAY_HEADERS
    ):
        raise DataValidationError("unexpected Choe Table S1 column contract")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DataValidationError(f"{label} is not non-empty text")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataValidationError(f"{label} is not numeric")
    result = int(value)
    if result != value:
        raise DataValidationError(f"{label} is not an integer")
    return result


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataValidationError(f"{label} is not numeric")
    return float(value)
