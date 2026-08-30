"""Build the Bristol WCM-1219 deletion universe from its pinned source."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from yggdrisil_ecoli.data.candidate_universe import (
    WCM_1219_SOURCE_GENE_COUNT,
    WCM_1219_UNIVERSE_ID,
    WCM_1219_UNMAPPED_SOURCE_IDS,
    gene_set_sha256,
)
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import (
    WCM_1219_GENE_LIST,
    WCM_1219_SOURCE_COMMIT,
    acquire_source,
)


def parse_wcm_ecocyc_ids(content: bytes) -> tuple[tuple[str, str], ...]:
    """Parse unique ``(EcoCyc RNA id, symbol)`` rows from the pinned CSV."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataValidationError("WCM gene list is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"Gene", "RNA"}.issubset(reader.fieldnames):
        raise DataValidationError("WCM gene list is missing Gene or RNA columns")
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row_number, row in enumerate(reader, start=2):
        symbol = (row.get("Gene") or "").strip()
        raw_rna = (row.get("RNA") or "").strip()
        if not symbol or not raw_rna.endswith("_RNA"):
            raise DataValidationError(
                f"WCM gene list row {row_number} has invalid Gene/RNA values"
            )
        ecocyc_id = raw_rna.removesuffix("_RNA")
        if ecocyc_id in seen:
            raise DataValidationError(f"duplicate WCM EcoCyc id: {ecocyc_id}")
        seen.add(ecocyc_id)
        rows.append((ecocyc_id, symbol))
    if not rows:
        raise DataValidationError("WCM gene list is empty")
    return tuple(rows)


def build_wcm_candidate_universe(
    *,
    registry_path: str | Path,
    raw_dir: str | Path,
    output_path: str | Path,
    refresh: bool = False,
) -> Path:
    """Acquire, crosswalk, validate, and persist the comparison universe."""

    source_path, source_record = acquire_source(
        WCM_1219_GENE_LIST,
        raw_dir,
        refresh=refresh,
    )
    rows = parse_wcm_ecocyc_ids(source_path.read_bytes())
    if len(rows) != WCM_1219_SOURCE_GENE_COUNT:
        raise DataValidationError(
            f"expected {WCM_1219_SOURCE_GENE_COUNT} WCM genes, found {len(rows)}"
        )
    registry_artifact = Path(registry_path)
    registry = GeneRegistry.from_parquet(registry_artifact)
    by_ecocyc: dict[str, str] = {}
    for record in registry:
        if record.ecocyc_id is None:
            continue
        if record.ecocyc_id in by_ecocyc:
            raise DataValidationError(
                f"duplicate registry EcoCyc id: {record.ecocyc_id}"
            )
        by_ecocyc[record.ecocyc_id] = record.b_number
    source_ids = {ecocyc_id for ecocyc_id, _symbol in rows}
    unmapped = source_ids - set(by_ecocyc)
    if unmapped != WCM_1219_UNMAPPED_SOURCE_IDS:
        raise DataValidationError(
            "unexpected WCM-to-registry mapping gap: "
            f"expected {sorted(WCM_1219_UNMAPPED_SOURCE_IDS)}, got {sorted(unmapped)}"
        )
    genes = frozenset(by_ecocyc[source_id] for source_id in source_ids - unmapped)
    destination = Path(output_path)
    atomic_json(
        destination,
        {
            "schema_version": 1,
            "universe_id": WCM_1219_UNIVERSE_ID,
            "purpose": (
                "Matched search over the canonical protein-coding intersection "
                "with the 1,219-gene WCM universe used for EMine-737"
            ),
            "gene_ids": sorted(genes),
            "gene_set_sha256": gene_set_sha256(genes),
            "registry_sha256": file_sha256(registry_artifact),
            "source_ids_outside_registry": sorted(unmapped),
            "counts": {
                "source_wcm_genes": len(rows),
                "canonical_candidate_genes": len(genes),
                "source_ids_outside_registry": len(unmapped),
            },
            "source": {
                **source_record.as_dict(),
                "commit": WCM_1219_SOURCE_COMMIT,
                "paper_doi": "10.1016/j.cels.2025.101392",
                "paper_name": "Gherman et al. EMine-737",
            },
        },
    )
    return destination
