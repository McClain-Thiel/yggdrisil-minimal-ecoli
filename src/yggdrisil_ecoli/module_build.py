"""Prepare the local catalog of modules complete in wild-type MG1655."""

import json
import re
from dataclasses import asdict
from pathlib import Path

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.kegg_modules import (
    PARSER_SEMANTICS_VERSION,
    KeggModuleEntry,
    parse_kegg_module_flat_file,
    referenced_ids,
    registry_ko_mapping_hash,
)
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.data.sources import (
    KEGG_KO_LINKS,
    KEGG_MODULE_INFO,
    KEGG_MODULE_LINKS,
    SourceSpec,
    acquire_source,
)
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator


def build_kegg_modules(
    *,
    registry_path: Path,
    ko_links_path: Path,
    data_dir: Path,
    accept_kegg_terms: bool,
    refresh: bool = False,
) -> Path:
    if not accept_kegg_terms:
        raise ValueError("KEGG source preparation requires accept_kegg_terms=True")
    registry = GeneRegistry.from_parquet(registry_path)
    ko_source = _validated_ko_links_source(registry_path, ko_links_path)
    background_kos = _background_kos(ko_links_path, registry)
    raw_dir = data_dir / "raw" / "kegg_modules"
    manifest_path = registry_path.with_name("source_manifest.json")
    manifest = json.loads(manifest_path.read_text())

    def fetch(spec: SourceSpec) -> Path:
        path = acquire_source(spec, raw_dir, refresh=refresh)
        manifest["inputs"][spec.filename] = {
            "url": spec.url,
            "sha256": file_sha256(path),
        }
        return path

    wt_complete_ids = _parse_wt_module_ids(fetch(KEGG_MODULE_LINKS))
    manifest["kegg_database_info"] = fetch(KEGG_MODULE_INFO).read_text()
    entries: dict[str, KeggModuleEntry] = {}
    needed = set(wt_complete_ids)
    while pending := sorted(needed - entries.keys()):
        batch = pending[:10]  # KEGG's get endpoint accepts at most ten entries.
        spec = SourceSpec(
            f"https://rest.kegg.jp/get/{'+'.join(batch)}",
            f"definitions_{'_'.join(batch)}.txt",
        )
        parsed = parse_kegg_module_flat_file(fetch(spec))
        if set(batch) - parsed.keys():
            raise DataValidationError(
                f"KEGG returned incomplete definitions for {batch}"
            )
        entries.update(parsed)
        needed.update(
            key
            for entry in parsed.values()
            for key in referenced_ids(entry.expression)
            if key.startswith("M")
        )
    evaluator = ModuleEvaluator(
        registry=registry,
        entries=entries,
        wt_complete_module_ids=tuple(wt_complete_ids),
        parser_semantics_version=PARSER_SEMANTICS_VERSION,
        background_kos=tuple(background_kos),
    )
    broken = evaluator.score_deleted(set()).broken_modules
    if broken:
        raise DataValidationError(f"local completeness disagrees with KEGG: {broken}")

    output = data_dir / "processed" / "kegg_modules.json"
    definitions = {key: asdict(entry) for key, entry in sorted(entries.items())}
    for entry in definitions.values():
        del entry["module_id"]  # The JSON mapping key already carries the ID.
    atomic_json(
        output,
        {
            "schema_version": 1,
            "parser_semantics_version": PARSER_SEMANTICS_VERSION,
            "reference_registry_sha256": file_sha256(registry_path),
            "reference_registry_ko_mapping_hash": registry_ko_mapping_hash(registry),
            "background_ko_source_sha256": ko_source["sha256"],
            "background_kos": sorted(background_kos),
            "wt_complete_module_ids": sorted(wt_complete_ids),
            "definitions": definitions,
        },
    )
    manifest["outputs"][output.name] = file_sha256(output)
    atomic_json(manifest_path, manifest)
    return output


def _validated_ko_links_source(
    registry_path: Path, ko_links_path: Path
) -> dict[str, str]:
    manifest = json.loads(registry_path.with_name("source_manifest.json").read_text())
    if manifest["outputs"][registry_path.name] != file_sha256(registry_path):
        raise DataValidationError("registry differs from its source manifest")
    source: dict[str, str] = manifest["inputs"][KEGG_KO_LINKS.filename]
    if source["sha256"] != file_sha256(ko_links_path):
        raise DataValidationError(
            "KO links differ from the snapshot used to build the registry"
        )
    return source


def _parse_wt_module_ids(path: Path) -> frozenset[str]:
    ids = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"eco:[^\t]+\tmd:eco_(M[0-9]{5})", line)
        if match is None:
            raise DataValidationError(f"{path}: malformed organism-module link")
        ids.add(match[1])
    if not ids:
        raise DataValidationError("KEGG returned no WT-complete MG1655 modules")
    return frozenset(ids)


def _background_kos(path: Path, registry: GeneRegistry) -> frozenset[str]:
    """KOs outside the protein-coding deletion universe remain fixed."""
    background = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"eco:([^\t]+)\tko:(K[0-9]{5})", line)
        if match is None:
            raise DataValidationError(f"{path}: malformed KEGG gene-KO link")
        if match[1] not in registry.search_universe:
            background.add(match[2])
    return frozenset(background)
