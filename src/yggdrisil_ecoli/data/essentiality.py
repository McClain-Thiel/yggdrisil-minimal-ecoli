"""Compact, condition-aware experimental essentiality evidence."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from math import isfinite
from pathlib import Path
from typing import Literal

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
        if not isinstance(self.b_number, str) or not is_b_number(self.b_number):
            raise DataValidationError(f"malformed canonical ID: {self.b_number!r}")
        measurements = (
            self.lb_call_raw,
            self.lb_ecipkm,
            self.m9_call_raw,
            self.m9_ecipkm,
        )
        if self.coverage == "unknown":
            if self.classification != "unknown" or any(
                v is not None for v in measurements
            ):
                raise DataValidationError(
                    "unknown coverage must not contain measurements"
                )
            return
        if self.coverage != "measured":
            raise DataValidationError(
                f"invalid essentiality coverage: {self.coverage!r}"
            )
        for medium, call, value in (
            ("LB", self.lb_call_raw, self.lb_ecipkm),
            ("M9", self.m9_call_raw, self.m9_ecipkm),
        ):
            if call not in {"E", "NE"}:
                raise DataValidationError(
                    f"{self.b_number} {medium}: calls must be E or NE"
                )
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
            ):
                raise DataValidationError(
                    f"{self.b_number} {medium}: ecIPKM must be finite and nonnegative"
                )
            if (value <= 2.2) != (call == "E"):
                raise DataValidationError(
                    f"{self.b_number} {medium}: author call disagrees with ecIPKM threshold"
                )
        expected = _classification(self.lb_call_raw, self.m9_call_raw)
        if self.classification != expected:
            raise DataValidationError(
                f"{self.b_number}: classification must be {expected!r}"
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
        records = list(records)
        duplicates = [
            tag for tag, n in Counter(r.b_number for r in records).items() if n > 1
        ]
        if duplicates:
            raise DataValidationError(f"duplicate essentiality b_number: {duplicates}")
        if not records:
            raise DataValidationError("essentiality dataset is empty")
        self._by_id = {record.b_number: record for record in records}
        self.metadata = {**_STUDY_METADATA, **(metadata or {})}
        for key, value in _STUDY_METADATA.items():
            if self.metadata[key] != value:
                raise DataValidationError(
                    f"essentiality metadata changes fixed field: {key}"
                )

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[EssentialityRecord]:
        return (self._by_id[tag] for tag in sorted(self._by_id))

    def record(self, b_number: str) -> EssentialityRecord:
        if not is_b_number(b_number):
            raise DataValidationError(
                f"expected a canonical b-number, got {b_number!r}"
            )
        return self._by_id[b_number]

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
        raw_metadata = (table.schema.metadata or {}).get(_METADATA_KEY)
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
        pq.write_table(table, destination, compression="zstd")


def parse_choe_workbook(
    path: str | Path,
    registry: GeneRegistry,
    *,
    expected_source_counts: dict[str, int] | None = _EXPECTED_SOURCE_COUNTS,
    metadata: Mapping[str, object] | None = None,
) -> tuple[EssentialityDataset, EssentialityImportReport]:
    """Prepare Choe 2023 Table S1; experiments only need the resulting Parquet."""
    import pandas as pd

    with pd.ExcelFile(path) as workbook:
        if workbook.sheet_names != ["Table S1"]:
            raise DataValidationError(
                f"unexpected Choe workbook sheets: {workbook.sheet_names}"
            )
        sheet = workbook.parse(
            "Table S1", header=None, dtype=object, keep_default_na=False
        )
    first, second = sheet.iloc[0].tolist(), sheet.iloc[1].tolist()
    if (
        tuple(first[:10]) != _IDENTITY_HEADERS
        or first[10] != "LB medium"
        or first[15] != "M9 glucose (0.2%) medium"
        or tuple(second[10:15]) != _ASSAY_HEADERS
        or tuple(second[15:20]) != _ASSAY_HEADERS
    ):
        raise DataValidationError("unexpected Choe Table S1 column contract")
    columns = {
        1: "start",
        2: "end",
        5: "b_number",
        6: "cds",
        7: "pseudo",
        13: "lb_ecipkm",
        14: "lb_call",
        18: "m9_ecipkm",
        19: "m9_call",
    }
    source = sheet.iloc[2:][list(columns)].rename(columns=columns)
    source.index += 1  # DataFrame index 2 is worksheet row 3; filtering keeps it.
    protein = source.loc[(source.cds == "Y") & (source.pseudo == "N")]
    source_counts = {
        f"{medium}_{label}": int((protein[f"{medium}_call"] == call).sum())
        for medium in ("lb", "m9")
        for label, call in (("essential", "E"), ("nonessential", "NE"))
    }
    observed = {
        "source_rows": len(source),
        "protein_coding_nonpseudo_rows": len(protein),
        **source_counts,
    }
    if expected_source_counts is not None and observed != expected_source_counts:
        raise DataValidationError(f"Choe source snapshot contract changed: {observed}")

    mapped: dict[str, EssentialityRecord] = {}
    unmapped: list[str] = []
    coordinate_mismatches: list[str] = []
    for row in protein.itertuples():
        try:
            record = EssentialityRecord(
                b_number=row.b_number,
                classification=_classification(row.lb_call, row.m9_call),
                coverage="measured",
                lb_call_raw=row.lb_call,
                lb_ecipkm=row.lb_ecipkm,
                m9_call_raw=row.m9_call,
                m9_ecipkm=row.m9_ecipkm,
            )
        except DataValidationError as exc:
            raise DataValidationError(f"row {row.Index}: {exc}") from exc
        reference = registry.get(record.b_number)
        if reference is None:
            unmapped.append(record.b_number)
            continue
        if record.b_number in mapped:
            raise DataValidationError(
                f"row {row.Index}: duplicate Choe source locus tag: {record.b_number}"
            )
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not isfinite(v)
            or v != int(v)
            for v in (row.start, row.end)
        ):
            raise DataValidationError(f"row {row.Index}: coordinates must be integers")
        if (row.start, row.end) != (reference.start, reference.end):
            coordinate_mismatches.append(record.b_number)
        mapped[record.b_number] = record

    missing = tuple(sorted(registry.search_universe - mapped.keys()))
    records = [
        *mapped.values(),
        *(
            EssentialityRecord(tag, "unknown", "unknown", None, None, None, None)
            for tag in missing
        ),
    ]
    return EssentialityDataset(records, metadata=metadata), EssentialityImportReport(
        source_rows=len(source),
        protein_coding_nonpseudo_rows=len(protein),
        source_call_counts=source_counts,
        mapped_source_genes=len(mapped),
        unmapped_source_ids=tuple(sorted(unmapped)),
        canonical_genes_without_measurement=missing,
        coordinate_mismatches=tuple(sorted(coordinate_mismatches)),
        summary_counts=dict(Counter(record.classification for record in records)),
    )


def _classification(
    lb_call: SourceCall | None, m9_call: SourceCall | None
) -> EssentialityClass:
    if m9_call == "E":
        return "essential" if lb_call == "E" else "conditionally_essential"
    return "ambiguous" if lb_call == "E" else "nonessential"
