"""Reproducible finalist selection and vEcoli workflow preparation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from yggdrisil.serialize import loads

from yggdrisil_ecoli.data.io import atomic_bytes, atomic_json
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.state import GenomeState, genome_state_key

VECOLI_COMMIT = "b2078bd8e226c5d319bb9ddaa10a1f2f1fcfdbbc"
VECOLI_NEXTFLOW_VERSION = "25.10.4"
ADAPTER_MODULE = "yggdrisil_multi_gene_knockout"
SELECTION_CONTRACT_VERSION = 1
DEFAULT_FINALISTS = 5
DEFAULT_DELETION_BAND = 0.9
DEFAULT_LINEAGE_SEED = 101
DEFAULT_MAX_GENERATIONS = 20


@dataclass(frozen=True, slots=True)
class Finalist:
    """One selected state and the search evidence used to admit it."""

    state_id: str
    deleted_genes: frozenset[str]
    fba_growth_rate: float
    fba_evaluator_id: str
    resource_evaluator_id: str

    @property
    def deletion_count(self) -> int:
        return len(self.deleted_genes)

    @property
    def deletion_set_sha256(self) -> str:
        return _json_sha256(sorted(self.deleted_genes))


@dataclass(frozen=True, slots=True)
class FinalistVariant:
    """Exact canonical-to-vEcoli mapping for one finalist."""

    variant_index: int
    finalist: Finalist
    vecoli_gene_ids: tuple[str, ...]
    gene_mapping: tuple[tuple[str, str], ...]


def select_finalists(
    graph_path: str | Path,
    *,
    count: int = DEFAULT_FINALISTS,
    deletion_band: float = DEFAULT_DELETION_BAND,
) -> tuple[dict[str, object], tuple[Finalist, ...]]:
    """Select a frozen, diverse finalist set without loading validation targets."""

    if count < 1:
        raise ValueError("finalist count must be positive")
    if not 0 < deletion_band <= 1:
        raise ValueError("deletion band must be in (0, 1]")
    path = Path(graph_path)
    graph_hashes = _frozen_sqlite_hashes(path)
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        run_row = connection.execute(
            "SELECT run_id, status, metadata_json FROM runs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if run_row is None:
            raise ValueError(f"graph has no runs: {path}")
        run_id, run_status, raw_run_metadata = run_row
        run_metadata = _mapping(loads(str(raw_run_metadata)), "run metadata")
        evaluator_ids = _string_mapping(
            run_metadata.get("evaluators"), "run evaluator identities"
        )
        required = {"fba", "resource_allocation"}
        if required - evaluator_ids.keys():
            raise ValueError("run lacks active FBA or resource evaluator identity")
        finalists = _load_viable_states(connection, str(run_id), evaluator_ids)
    finally:
        connection.close()
    if len(finalists) < count:
        raise ValueError(f"graph has only {len(finalists)} jointly feasible states")
    selected = select_diverse_finalists(
        finalists, count=count, deletion_band=deletion_band
    )
    provenance: dict[str, object] = {
        "graph_path": str(path.resolve()),
        "graph_files": graph_hashes,
        "run_id": str(run_id),
        "run_status": str(run_status),
        "active_evaluator_ids": dict(sorted(evaluator_ids.items())),
        "selection_contract_version": SELECTION_CONTRACT_VERSION,
        "selection_algorithm": "deepest_then_greedy_minimum_jaccard_distance",
        "finalist_count": count,
        "deletion_band_fraction_of_deepest": deletion_band,
        "validation_inputs_loaded": [],
    }
    return provenance, selected


def select_diverse_finalists(
    candidates: tuple[Finalist, ...],
    *,
    count: int,
    deletion_band: float,
) -> tuple[Finalist, ...]:
    """Choose the deepest state, then maximize minimum deletion-set distance."""

    if count < 1:
        raise ValueError("finalist count must be positive")
    if not 0 < deletion_band <= 1:
        raise ValueError("deletion band must be in (0, 1]")
    ordered = sorted(
        candidates,
        key=lambda item: (-item.deletion_count, -item.fba_growth_rate, item.state_id),
    )
    if not ordered:
        raise ValueError("no jointly feasible candidates")
    minimum_deletions = math.ceil(ordered[0].deletion_count * deletion_band)
    pool = [item for item in ordered if item.deletion_count >= minimum_deletions]
    if len(pool) < count:
        raise ValueError(
            f"deletion band contains {len(pool)} candidates, fewer than {count}"
        )
    selected = [pool.pop(0)]
    while len(selected) < count:
        pool.sort(
            key=lambda item: (
                -_minimum_jaccard_distance(item, selected),
                -item.deletion_count,
                -item.fba_growth_rate,
                item.state_id,
            )
        )
        selected.append(pool.pop(0))
    return tuple(selected)


def map_finalists(
    finalists: tuple[Finalist, ...], registry: GeneRegistry
) -> tuple[FinalistVariant, ...]:
    """Map every deletion through the frozen canonical registry to vEcoli IDs."""

    variants: list[FinalistVariant] = []
    for variant_index, finalist in enumerate(finalists, start=1):
        mapping: list[tuple[str, str]] = []
        missing: list[str] = []
        for b_number in sorted(finalist.deleted_genes):
            ecocyc_id = registry.require(b_number).ecocyc_id
            if ecocyc_id is None:
                missing.append(b_number)
            else:
                mapping.append((b_number, ecocyc_id))
        if missing:
            raise ValueError(
                f"{finalist.state_id}: deletions lack EcoCyc IDs: {missing}"
            )
        vecoli_ids = [ecocyc_id for _, ecocyc_id in mapping]
        duplicates = sorted(
            gene_id for gene_id in set(vecoli_ids) if vecoli_ids.count(gene_id) > 1
        )
        if duplicates:
            raise ValueError(
                f"{finalist.state_id}: ambiguous EcoCyc mappings: {duplicates}"
            )
        variants.append(
            FinalistVariant(
                variant_index=variant_index,
                finalist=finalist,
                vecoli_gene_ids=tuple(vecoli_ids),
                gene_mapping=tuple(mapping),
            )
        )
    return tuple(variants)


def prepare_finalist_workflow(
    *,
    graph_path: str | Path,
    registry_path: str | Path,
    vecoli_checkout: str | Path,
    output_root: str | Path,
    manifest_path: str | Path,
    config_path: str | Path,
    count: int = DEFAULT_FINALISTS,
    deletion_band: float = DEFAULT_DELETION_BAND,
    lineage_seed: int = DEFAULT_LINEAGE_SEED,
    generations: int = DEFAULT_MAX_GENERATIONS,
    sim_data_path: str | Path | None = None,
) -> dict[str, object]:
    """Freeze candidates, install the adapter, and write a vEcoli workflow."""

    if generations < 1 or generations > DEFAULT_MAX_GENERATIONS:
        raise ValueError(f"generations must be in [1, {DEFAULT_MAX_GENERATIONS}]")
    checkout = Path(vecoli_checkout).resolve()
    vecoli = validate_vecoli_checkout(checkout)
    adapter_path = install_vecoli_adapter(checkout)
    selection, finalists = select_finalists(
        graph_path, count=count, deletion_band=deletion_band
    )
    registry_file = Path(registry_path)
    registry = GeneRegistry.from_parquet(registry_file)
    variants = map_finalists(finalists, registry)
    selection_hash = _json_sha256([item.state_id for item in finalists])
    experiment_id = f"yggdrisil_finalists_{selection_hash[:12]}_seed{lineage_seed}"
    config = build_workflow_config(
        variants,
        output_root=Path(output_root).resolve(),
        experiment_id=experiment_id,
        lineage_seed=lineage_seed,
        generations=generations,
        sim_data_path=Path(sim_data_path).resolve() if sim_data_path else None,
    )
    config_file = Path(config_path)
    atomic_json(config_file, config)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "purpose": "predeclared vEcoli finalist validation",
        "application": {
            "selection_source_path": str(Path(__file__).resolve()),
            "selection_source_sha256": file_sha256(Path(__file__)),
        },
        "selection": selection,
        "registry": {
            "path": str(registry_file.resolve()),
            "sha256": file_sha256(registry_file),
        },
        "vecoli": {
            **vecoli,
            "checkout": str(checkout),
            "adapter_module": ADAPTER_MODULE,
            "adapter_path": str(adapter_path),
            "adapter_sha256": file_sha256(adapter_path),
            "operons": False,
            "condition": "basal",
            "basal_expression_condition": "M9 Glucose minus AAs",
        },
        "lineage": {
            "seed": lineage_seed,
            "single_daughters": True,
            "max_generations": generations,
            "fail_at_max_duration": True,
        },
        "workflow": {
            "experiment_id": experiment_id,
            "config_path": str(config_file.resolve()),
            "config_sha256": file_sha256(config_file),
            "output_root": str(Path(output_root).resolve()),
            "sim_data_path": str(Path(sim_data_path).resolve())
            if sim_data_path
            else None,
            "sim_data_sha256": file_sha256(Path(sim_data_path))
            if sim_data_path
            else None,
        },
        "finalists": [_variant_payload(item) for item in variants],
    }
    atomic_json(Path(manifest_path), manifest)
    return manifest


def build_workflow_config(
    variants: tuple[FinalistVariant, ...],
    *,
    output_root: Path,
    experiment_id: str,
    lineage_seed: int,
    generations: int,
    sim_data_path: Path | None,
) -> dict[str, object]:
    """Build the minimal official vEcoli lineage-workflow configuration."""

    if not variants:
        raise ValueError("at least one finalist variant is required")
    return {
        "experiment_id": experiment_id,
        "suffix_time": False,
        "description": "Yggdrisil predeclared genome-minimization finalists",
        "sim_data_path": str(sim_data_path) if sim_data_path else None,
        "parca_options": {
            "cpus": 4,
            "operons": False,
            "basal_expression_condition": "M9 Glucose minus AAs",
        },
        "fail_at_max_duration": True,
        "generations": generations,
        "n_init_sims": 1,
        "single_daughters": True,
        "lineage_seed": lineage_seed,
        "different_seeds_per_variant": False,
        "skip_baseline": True,
        "variants": {
            ADAPTER_MODULE: {
                "gene_ids": {"value": [list(item.vecoli_gene_ids) for item in variants]}
            }
        },
        "emitter": "parquet",
        "emitter_arg": {"out_dir": str(output_root)},
        "emit_paths": [
            ["listeners", "mass", "cell_mass"],
            ["listeners", "mass", "dry_mass"],
            ["listeners", "mass", "dry_mass_fold_change"],
            ["global_time"],
        ],
        "raw_output": False,
        "analysis_options": {},
    }


def validate_vecoli_checkout(checkout: Path) -> dict[str, object]:
    """Reject a changed vEcoli revision, lock, or tracked working tree."""

    if (
        not (checkout / "pyproject.toml").is_file()
        or not (checkout / "uv.lock").is_file()
    ):
        raise ValueError(f"not a vEcoli checkout: {checkout}")
    commit = _git(checkout, "rev-parse", "HEAD")
    if commit != VECOLI_COMMIT:
        raise ValueError(f"expected vEcoli {VECOLI_COMMIT}, found {commit}")
    tracked_status = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    if tracked_status:
        raise ValueError("vEcoli checkout has tracked changes")
    environment = (checkout / ".env").read_text().splitlines()
    nextflow_versions = [
        line.split("=", 1)[1] for line in environment if line.startswith("NXF_VER=")
    ]
    if nextflow_versions != [VECOLI_NEXTFLOW_VERSION]:
        raise ValueError(
            "vEcoli .env does not pin the expected Nextflow version: "
            f"{nextflow_versions}"
        )
    return {
        "git_commit": commit,
        "origin": _git(checkout, "remote", "get-url", "origin"),
        "uv_lock_sha256": file_sha256(checkout / "uv.lock"),
        "nextflow_version": VECOLI_NEXTFLOW_VERSION,
    }


def install_vecoli_adapter(checkout: Path) -> Path:
    """Install the pinned adapter as an untracked module in a clean checkout."""

    source = Path(__file__).with_name("resources") / "vecoli_multi_gene_knockout.py.txt"
    target = checkout / "ecoli" / "variants" / f"{ADAPTER_MODULE}.py"
    content = source.read_bytes()
    if target.exists() and target.read_bytes() != content:
        raise ValueError(f"refusing to overwrite changed vEcoli adapter: {target}")
    atomic_bytes(target, content)
    return target


def _load_viable_states(
    connection: sqlite3.Connection, run_id: str, evaluator_ids: dict[str, str]
) -> tuple[Finalist, ...]:
    fba_id = evaluator_ids["fba"]
    resource_id = evaluator_ids["resource_allocation"]
    run_state_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT parent_id FROM proposal_events WHERE run_id = ? "
            "UNION SELECT child_id FROM proposal_events "
            "WHERE run_id = ? AND child_id IS NOT NULL",
            (run_id, run_id),
        ).fetchall()
    }
    if not run_state_ids:
        raise ValueError(f"run has no materialized state transitions: {run_id}")
    placeholders = ",".join("?" for _ in run_state_ids)
    rows = connection.execute(
        f"SELECT state_id, state_json FROM states WHERE state_id IN ({placeholders})",
        tuple(sorted(run_state_ids)),
    ).fetchall()
    finalists: list[Finalist] = []
    for state_id, raw_state in rows:
        state = loads(str(raw_state))
        if not isinstance(state, GenomeState):
            raise ValueError(f"state {state_id} is not a GenomeState")
        if genome_state_key(state) != state_id:
            raise ValueError(f"state payload does not match ID: {state_id}")
        raw_evaluations = connection.execute(
            "SELECT evaluator_id, metrics_json FROM evaluations "
            "WHERE state_id = ? AND evaluator_id IN (?, ?)",
            (state_id, fba_id, resource_id),
        ).fetchall()
        metrics = {
            str(evaluator_id): _mapping(loads(str(raw_metrics)), "evaluation metrics")
            for evaluator_id, raw_metrics in raw_evaluations
        }
        if fba_id not in metrics or resource_id not in metrics:
            continue
        growth = metrics[fba_id].get("growth_rate")
        fba_positive = (
            metrics[fba_id].get("feasible") is True
            and isinstance(growth, (int, float))
            and not isinstance(growth, bool)
            and growth > 0
        )
        resource_positive = metrics[resource_id].get("feasible_at_growth_floor") is True
        if fba_positive and resource_positive:
            assert isinstance(growth, (int, float)) and not isinstance(growth, bool)
            finalists.append(
                Finalist(
                    state_id=str(state_id),
                    deleted_genes=state.deleted_genes,
                    fba_growth_rate=float(growth),
                    fba_evaluator_id=fba_id,
                    resource_evaluator_id=resource_id,
                )
            )
    return tuple(finalists)


def _minimum_jaccard_distance(
    candidate: Finalist, selected: list[Finalist]
) -> Fraction:
    distances = []
    for other in selected:
        union = candidate.deleted_genes | other.deleted_genes
        intersection = candidate.deleted_genes & other.deleted_genes
        distances.append(Fraction(len(union) - len(intersection), len(union)))
    return min(distances)


def _variant_payload(variant: FinalistVariant) -> dict[str, object]:
    finalist = variant.finalist
    return {
        "variant_index": variant.variant_index,
        "state_id": finalist.state_id,
        "deletion_count": finalist.deletion_count,
        "deletion_set_sha256": finalist.deletion_set_sha256,
        "deleted_gene_ids": sorted(finalist.deleted_genes),
        "fba_growth_rate": finalist.fba_growth_rate,
        "fba_evaluator_id": finalist.fba_evaluator_id,
        "resource_evaluator_id": finalist.resource_evaluator_id,
        "vecoli_gene_ids": list(variant.vecoli_gene_ids),
        "gene_mapping": [
            {"b_number": b_number, "vecoli_gene_id": vecoli_gene_id}
            for b_number, vecoli_gene_id in variant.gene_mapping
        ],
    }


def _frozen_sqlite_hashes(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"graph does not exist: {path}")
    sidecars = [Path(f"{path}-wal"), Path(f"{path}-shm")]
    present = [candidate.name for candidate in sidecars if candidate.exists()]
    if present:
        raise ValueError(f"graph must be checkpointed; found sidecars: {present}")
    return {path.name: file_sha256(path)}


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _string_mapping(value: object, label: str) -> dict[str, str]:
    mapping = _mapping(value, label)
    if any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in mapping.items()
    ):
        raise ValueError(f"{label} must map strings to strings")
    return {str(key): str(item) for key, item in mapping.items()}


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _json_sha256(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
