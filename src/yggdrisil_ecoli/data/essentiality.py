"""Choe 2023 LB/M9 measurements; essentiality labels are derived from the calls."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Annotated, Literal, Self

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    TypeAdapter,
    computed_field,
    model_validator,
)

from yggdrisil_ecoli.constants import REFERENCE_ACCESSION
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import BNumber, GeneRegistry

EssentialityClass = Literal[
    "essential", "conditionally_essential", "nonessential", "ambiguous", "unknown"
]
Coverage = Literal["measured", "unknown"]
SourceCall = Literal["E", "NE"]
Ecipkm = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
_COORDINATES = TypeAdapter(tuple[StrictInt, StrictInt])

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


class EssentialityRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    b_number: BNumber
    lb_call_raw: SourceCall | None = None
    lb_ecipkm: Ecipkm | None = None
    m9_call_raw: SourceCall | None = None
    m9_ecipkm: Ecipkm | None = None

    @model_validator(mode="after")
    def consistent_measurements(self) -> Self:
        pairs = ((self.lb_call_raw, self.lb_ecipkm), (self.m9_call_raw, self.m9_ecipkm))
        if all(call is None and value is None for call, value in pairs):
            return self
        for call, value in pairs:
            if call is None or value is None:
                raise ValueError(
                    "measured coverage requires both LB and M9 calls and ecIPKM"
                )
            if (value <= 2.2) != (call == "E"):
                raise ValueError("author call disagrees with ecIPKM threshold")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def coverage(self) -> Coverage:
        return "unknown" if self.lb_call_raw is None else "measured"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def classification(self) -> EssentialityClass:
        if self.coverage == "unknown":
            return "unknown"
        if self.m9_call_raw == "E":
            return "essential" if self.lb_call_raw == "E" else "conditionally_essential"
        return "ambiguous" if self.lb_call_raw == "E" else "nonessential"

    @property
    def condition_disagreement(self) -> bool:
        return self.lb_call_raw != self.m9_call_raw

    @property
    def evidence_conflict(self) -> bool:
        return self.classification == "ambiguous"


class EssentialityDataset:
    def __init__(
        self,
        records: Iterable[EssentialityRecord],
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        records = list(records)
        self._by_id = {r.b_number: r for r in records}
        if len(self._by_id) != len(records) or not records:
            raise DataValidationError(
                "essentiality data must contain unique b-numbers and be nonempty"
            )
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
        return self._by_id[b_number]

    summary = record

    def detail(self, b_number: str) -> dict[str, object]:
        record = self.record(b_number)
        return {
            **record.model_dump(),
            "condition_disagreement": record.condition_disagreement,
            "evidence_conflict": record.evidence_conflict,
            "source": self.metadata,
        }

    @classmethod
    def from_parquet(cls, path: str | Path) -> EssentialityDataset:
        table = pq.read_table(path)
        metadata = json.loads((table.schema.metadata or {})[_METADATA_KEY])
        records = []
        for row in table.to_pylist():
            labels = {key: row.pop(key) for key in ("classification", "coverage")}
            record = EssentialityRecord(**row)
            if any(getattr(record, key) != value for key, value in labels.items()):
                raise DataValidationError(
                    f"{record.b_number}: stored labels disagree with measurements"
                )
            records.append(record)
        return cls(records, metadata=metadata)

    def to_parquet(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist([r.model_dump() for r in self])
        table = table.replace_schema_metadata(
            {_METADATA_KEY: json.dumps(self.metadata, sort_keys=True).encode()}
        )
        pq.write_table(table, path, compression="zstd")


def parse_choe_workbook(
    path: str | Path,
    registry: GeneRegistry,
    *,
    expected_source_counts: dict[str, int] | None = _EXPECTED_SOURCE_COUNTS,
    metadata: Mapping[str, object] | None = None,
) -> tuple[EssentialityDataset, dict[str, object]]:
    """Prepare Table S1; experiments only need the resulting Parquet."""
    import pandas as pd

    sheet = pd.read_excel(
        path, sheet_name="Table S1", header=None, dtype=object, keep_default_na=False
    )
    if (
        sheet.iloc[0, [1, 2, 5, 6, 7, 10, 15]].tolist()
        != [
            "Start",
            "End",
            "Locus Tag",
            "CDS",
            "Pseudo",
            "LB medium",
            "M9 glucose (0.2%) medium",
        ]
        or sheet.iloc[1, [13, 14, 18, 19]].tolist() != ["ecIPKM", "Essentiality"] * 2
    ):
        raise DataValidationError("unexpected Choe Table S1 column contract")
    source = sheet.iloc[2:, [1, 2, 5, 6, 7, 13, 14, 18, 19]].copy()
    source.columns = [
        "start",
        "end",
        "b_number",
        "cds",
        "pseudo",
        "lb_ecipkm",
        "lb_call_raw",
        "m9_ecipkm",
        "m9_call_raw",
    ]
    source.index += 1  # Preserve worksheet row numbers through filtering.
    protein = source.loc[(source.cds == "Y") & (source.pseudo == "N")]
    counts = {
        f"{medium}_{label}": int((protein[f"{medium}_call_raw"] == call).sum())
        for medium in ("lb", "m9")
        for label, call in (("essential", "E"), ("nonessential", "NE"))
    }
    observed = {
        "source_rows": len(source),
        "protein_coding_nonpseudo_rows": len(protein),
        **counts,
    }
    if expected_source_counts is not None and observed != expected_source_counts:
        raise DataValidationError(f"Choe source snapshot contract changed: {observed}")
    if protein.b_number.duplicated().any():
        raise DataValidationError("duplicate Choe source locus tag")

    mapped: dict[str, EssentialityRecord] = {}
    unmapped, mismatches = [], []
    for row_number, row in protein.iterrows():
        try:
            record = EssentialityRecord.model_validate(
                row[list(EssentialityRecord.model_fields)].to_dict()
            )
            coordinates = _COORDINATES.validate_python((row.start, row.end))
        except ValueError as exc:
            raise DataValidationError(f"row {row_number}: {exc}") from exc
        reference = registry.get(record.b_number)
        if reference is None:
            unmapped.append(record.b_number)
            continue
        if coordinates != (reference.start, reference.end):
            mismatches.append(record.b_number)
        mapped[record.b_number] = record

    missing = tuple(sorted(registry.search_universe - mapped.keys()))
    dataset = EssentialityDataset(
        [*mapped.values(), *(EssentialityRecord(b_number=tag) for tag in missing)],
        metadata=metadata,
    )
    return dataset, {
        "source_rows": len(source),
        "protein_coding_nonpseudo_rows": len(protein),
        "source_call_counts": counts,
        "mapped_source_genes": len(mapped),
        "unmapped_source_ids": tuple(sorted(unmapped)),
        "canonical_genes_without_measurement": missing,
        "coordinate_mismatches": tuple(sorted(mismatches)),
        "summary_counts": dict(Counter(r.classification for r in dataset)),
    }
