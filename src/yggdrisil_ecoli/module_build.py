"""Build a local snapshot of KEGG modules complete in wild-type MG1655."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from yggdrisil_ecoli import __version__
from yggdrisil_ecoli.constants import KEGG_ORGANISM
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.kegg_modules import (
    PARSER_SEMANTICS_VERSION,
    KeggModuleEntry,
    evaluate_module_expression,
    parse_kegg_module_flat_file,
    referenced_modules,
)
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import (
    KEGG_MODULE_INFO,
    KEGG_MODULE_LINKS,
    SourceRecord,
    SourceSpec,
    acquire_source,
)
from yggdrisil_ecoli.scorers.modules import registry_ko_mapping_hash


def build_kegg_modules(
    *,
    registry_path: Path,
    ko_links_path: Path,
    data_dir: Path,
    accept_kegg_terms: bool,
    refresh: bool,
) -> Path:
    if not accept_kegg_terms:
        raise ValueError(
            "KEGG access requires --accept-kegg-terms; the REST API is limited "
            "to academic use and its snapshots must not be redistributed"
        )
    registry = GeneRegistry.from_parquet(registry_path)
    if not any(record.ko_ids for record in registry):
        raise DataValidationError(
            "registry has no KO mappings; rebuild it with --include-kegg first"
        )
    ko_links_source = _validated_ko_links_source(registry_path, ko_links_path)
    background_kos = _background_kos(ko_links_path, registry)
    raw_dir = data_dir / "raw" / "kegg_modules"
    processed_dir = data_dir / "processed"
    sources: list[SourceRecord] = []

    link_path, link_source = acquire_source(KEGG_MODULE_LINKS, raw_dir, refresh=refresh)
    sources.append(link_source)
    wt_complete_ids = _parse_wt_module_ids(link_path)
    time.sleep(0.35)
    info_path, info_source = acquire_source(KEGG_MODULE_INFO, raw_dir, refresh=refresh)
    sources.append(info_source)

    entries: dict[str, KeggModuleEntry] = {}
    needed = set(wt_complete_ids)
    while pending := sorted(needed - set(entries)):
        batch = pending[:10]
        source_spec = _definition_source(batch)
        time.sleep(0.35)
        batch_path, source_record = acquire_source(
            source_spec, raw_dir, refresh=refresh
        )
        sources.append(source_record)
        parsed = parse_kegg_module_flat_file(batch_path)
        missing = set(batch) - set(parsed)
        if missing:
            raise DataValidationError(
                f"KEGG did not return requested module definitions: {sorted(missing)}"
            )
        entries.update(parsed)
        for entry in parsed.values():
            needed.update(referenced_modules(entry.expression))

    expressions = {module_id: entry.expression for module_id, entry in entries.items()}
    wt_kos = {ko_id for record in registry for ko_id in record.ko_ids}
    wt_kos.update(background_kos)
    locally_incomplete = []
    for module_id in wt_complete_ids:
        result = evaluate_module_expression(
            expressions[module_id],
            wt_kos,
            module_definitions=expressions,
        )
        if not result.complete:
            locally_incomplete.append(
                {
                    "module_id": module_id,
                    "minimal_missing_ko_sets": result.minimal_missing_ko_sets,
                }
            )
    if locally_incomplete:
        raise DataValidationError(
            "local completeness disagrees with KEGG for WT modules: "
            + json.dumps(locally_incomplete[:10], sort_keys=True)
        )

    payload = {
        "schema_version": 1,
        "parser_semantics_version": PARSER_SEMANTICS_VERSION,
        "reference_registry_sha256": file_sha256(registry_path),
        "reference_registry_ko_mapping_hash": registry_ko_mapping_hash(registry),
        "background_ko_source_sha256": ko_links_source["sha256"],
        "background_kos": sorted(background_kos),
        "wt_complete_module_ids": sorted(wt_complete_ids),
        "definitions": {
            module_id: entry.as_dict() for module_id, entry in sorted(entries.items())
        },
    }
    output_path = processed_dir / "kegg_modules.json"
    _atomic_json(output_path, payload)
    manifest = {
        "schema_version": 1,
        "built_at": datetime.now(UTC).isoformat(),
        "script_version": __version__,
        "parser_semantics_version": PARSER_SEMANTICS_VERSION,
        "reference_registry": {
            "path": str(registry_path),
            "sha256": file_sha256(registry_path),
        },
        "background_ko_input": ko_links_source,
        "kegg_database_info": info_path.read_text(encoding="utf-8"),
        "sources": [source.as_dict() for source in sources],
        "outputs": {
            "kegg_modules": {
                "path": str(output_path),
                "sha256": file_sha256(output_path),
                "wt_complete_modules": len(wt_complete_ids),
                "definitions_including_dependencies": len(entries),
                "fixed_background_kos": len(background_kos),
            }
        },
    }
    _atomic_json(processed_dir / "kegg_modules_manifest.json", manifest)
    print(
        f"Validated {len(wt_complete_ids)} WT-complete modules against "
        f"{len(wt_kos)} remaining-gene KOs."
    )
    print(f"Wrote {output_path}")
    return output_path


def _validated_ko_links_source(
    registry_path: Path, ko_links_path: Path
) -> dict[str, object]:
    """Tie fixed background KOs to the registry build's KEGG source snapshot."""

    manifest_path = registry_path.with_name("source_manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataValidationError(
            f"cannot validate KEGG KO links without {manifest_path}"
        ) from exc
    outputs = manifest.get("outputs")
    sources = manifest.get("sources")
    if not isinstance(outputs, dict) or not isinstance(sources, list):
        raise DataValidationError("malformed registry source manifest")
    registry_output = outputs.get("gene_registry")
    if not isinstance(registry_output, dict) or registry_output.get(
        "sha256"
    ) != file_sha256(registry_path):
        raise DataValidationError(
            "registry does not match the artifact recorded in its source manifest"
        )
    matches = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("name") == "kegg_eco_ko_links"
    ]
    if len(matches) != 1:
        raise DataValidationError(
            "registry source manifest lacks one KEGG gene-to-KO snapshot"
        )
    source = matches[0]
    ko_links_sha256 = file_sha256(ko_links_path)
    if source.get("sha256") != ko_links_sha256:
        raise DataValidationError(
            "KEGG KO links differ from the snapshot used to build the registry"
        )
    return {
        "path": str(ko_links_path),
        "sha256": ko_links_sha256,
        "bytes": ko_links_path.stat().st_size,
        "registry_source_manifest": str(manifest_path),
        "registry_source_manifest_sha256": file_sha256(manifest_path),
        "source": source,
    }


def _parse_wt_module_ids(path: Path) -> frozenset[str]:
    ids: set[str] = set()
    expected_prefix = f"md:{KEGG_ORGANISM}_"
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 2 or not fields[1].startswith(expected_prefix):
                raise DataValidationError(
                    f"{path}:{line_number}: malformed organism-module link"
                )
            module_id = fields[1].removeprefix(expected_prefix)
            if (
                len(module_id) != 6
                or not module_id.startswith("M")
                or not module_id[1:].isdigit()
            ):
                raise DataValidationError(
                    f"{path}:{line_number}: malformed module ID {module_id!r}"
                )
            ids.add(module_id)
    if not ids:
        raise DataValidationError("KEGG returned no WT-complete MG1655 modules")
    return frozenset(ids)


def _background_kos(path: Path, registry: GeneRegistry) -> frozenset[str]:
    """KO assignments for MG1655 genes fixed outside the deletion universe."""

    background: set[str] = set()
    universe = registry.search_universe
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 2:
                raise DataValidationError(
                    f"{path}:{line_number}: malformed KEGG gene-KO link"
                )
            raw_gene, raw_ko = fields
            gene_prefix, separator, b_number = raw_gene.partition(":")
            ko_prefix, ko_separator, ko_id = raw_ko.partition(":")
            if (
                not separator
                or gene_prefix != KEGG_ORGANISM
                or not ko_separator
                or ko_prefix != "ko"
                or len(ko_id) != 6
                or not ko_id.startswith("K")
                or not ko_id[1:].isdigit()
            ):
                raise DataValidationError(
                    f"{path}:{line_number}: malformed KEGG gene-KO link"
                )
            if b_number not in universe:
                background.add(ko_id)
    return frozenset(background)


def _definition_source(module_ids: list[str]) -> SourceSpec:
    joined = "+".join(module_ids)
    return SourceSpec(
        name=f"kegg_module_definitions_{module_ids[0]}_{module_ids[-1]}",
        url=f"https://rest.kegg.jp/get/{joined}",
        filename=f"definitions_{'_'.join(module_ids)}.txt",
        source_version="live REST snapshot (content hash frozen in manifest)",
        redistribution="academic-use API snapshot; do not redistribute",
    )


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("data/processed/gene_registry.parquet"),
    )
    parser.add_argument(
        "--kegg-ko-links",
        type=Path,
        default=Path("data/raw/kegg_eco_ko_links.tsv"),
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--accept-kegg-terms", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        build_kegg_modules(
            registry_path=args.registry,
            ko_links_path=args.kegg_ko_links,
            data_dir=args.data_dir,
            accept_kegg_terms=args.accept_kegg_terms,
            refresh=args.refresh,
        )
    except (ValueError, DataValidationError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
