"""Reproducible finalist selection and vEcoli workflow preparation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import TypeAdapter
from yggdrisil.serialize import loads

from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.data.io import atomic_bytes, atomic_json, file_sha256
from yggdrisil_ecoli.state import GenomeState, genome_state_key

VECOLI_COMMIT = "b2078bd8e226c5d319bb9ddaa10a1f2f1fcfdbbc"
VECOLI_NEXTFLOW_VERSION = "25.10.4"
ADAPTER_MODULE = "yggdrisil_multi_gene_knockout"
SELECTION_CONTRACT_VERSION = 1
DEFAULT_FINALISTS = 5
DEFAULT_DELETION_BAND = 0.9
DEFAULT_LINEAGE_SEED = 101
DEFAULT_MAX_GENERATIONS = 20


def select_finalists(
    graph_path: str | Path,
    *,
    count: int = DEFAULT_FINALISTS,
    deletion_band: float = DEFAULT_DELETION_BAND,
) -> tuple[dict[str, object], pd.DataFrame]:
    """Select a frozen, diverse finalist set without loading validation targets."""

    path = Path(graph_path)
    graph_hashes = _frozen_sqlite_hashes(path)
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        run_row = connection.execute(
            "SELECT run_id, status, metadata_json FROM runs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if run_row is None:
            raise ValueError(f"graph has no runs: {path}")
        run_id, run_status, raw_run_metadata = run_row
        run_metadata = loads(str(raw_run_metadata))
        evaluator_ids = TypeAdapter(dict[str, str]).validate_python(
            run_metadata["evaluators"], strict=True
        )
        required = {"fba", "resource_allocation"}
        if required - evaluator_ids.keys():
            raise ValueError("run lacks active FBA or resource evaluator identity")
        finalists = _load_viable_states(connection, str(run_id), evaluator_ids)
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
    candidates: pd.DataFrame, *, count: int, deletion_band: float
) -> pd.DataFrame:
    """Choose the deepest state, then maximize minimum deletion-set distance."""
    if count < 1 or not 0 < deletion_band <= 1:
        raise ValueError("count must be positive and deletion band must be in (0, 1]")
    if candidates.empty:
        raise ValueError("no jointly feasible candidates")
    ordered = candidates.sort_values(
        ["deletion_count", "fba_growth_rate", "state_id"],
        ascending=[False, False, True],
    )
    minimum = math.ceil(ordered.deletion_count.iloc[0] * deletion_band)
    pool = ordered.loc[ordered.deletion_count >= minimum]
    if len(pool) < count:
        raise ValueError(
            f"deletion band contains {len(pool)} candidates, fewer than {count}"
        )
    deletions = pool.deleted_gene_ids.map(set).to_dict()
    selected = [pool.index[0]]
    while len(selected) < count:
        pool = pool.drop(index=selected[-1])
        # The presorted table preserves depth/growth/state-ID tie breaking.
        selected.append(
            max(
                pool.index,
                key=lambda candidate: min(
                    Fraction(
                        len(deletions[candidate] ^ deletions[other]),
                        len(deletions[candidate] | deletions[other]) or 1,
                    )
                    for other in selected
                ),
            )
        )
    return candidates.loc[selected]


def map_finalists(finalists: pd.DataFrame, genes: pd.DataFrame) -> pd.DataFrame:
    """Add exact EcoCyc mappings and workflow indices to the selected table."""
    mappings = []
    for state_id, deleted in finalists.deleted_gene_ids.items():
        mapping = genes.loc[sorted(deleted), "ecocyc_id"]
        if mapping.isna().any() or mapping.duplicated().any():
            raise ValueError(
                f"{state_id}: deletions lack EcoCyc IDs or have ambiguous mappings"
            )
        mappings.append(mapping.rename("vecoli_gene_id").rename_axis("b_number"))
    return finalists.assign(
        variant_index=range(1, len(finalists) + 1),
        vecoli_gene_ids=[mapping.tolist() for mapping in mappings],
        gene_mapping=[mapping.reset_index().to_dict("records") for mapping in mappings],
    )


def prepare_finalist_workflow(
    *,
    graph_path: str | Path,
    genes_path: str | Path,
    vecoli_checkout: str | Path,
    output_root: str | Path,
    manifest_path: str | Path,
    config_path: str | Path,
    count: int = DEFAULT_FINALISTS,
    deletion_band: float = DEFAULT_DELETION_BAND,
    lineage_seed: int = DEFAULT_LINEAGE_SEED,
    generations: int = DEFAULT_MAX_GENERATIONS,
    sim_data_path: str | Path | None = None,
) -> dict[str, Any]:
    """Freeze candidates, install the adapter, and write a vEcoli workflow."""

    if generations < 1 or generations > DEFAULT_MAX_GENERATIONS:
        raise ValueError(f"generations must be in [1, {DEFAULT_MAX_GENERATIONS}]")
    checkout = Path(vecoli_checkout).resolve()
    vecoli = validate_vecoli_checkout(checkout)
    adapter_path = install_vecoli_adapter(checkout)
    selection, finalists = select_finalists(
        graph_path, count=count, deletion_band=deletion_band
    )
    genes_file = Path(genes_path)
    variants = map_finalists(finalists, load_genes(genes_file))
    selection_hash = _json_sha256(finalists.index.tolist())
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
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "predeclared vEcoli finalist validation",
        "application": {
            "selection_source_path": str(Path(__file__).resolve()),
            "selection_source_sha256": file_sha256(Path(__file__)),
        },
        "selection": selection,
        "registry": {
            "path": str(genes_file.resolve()),
            "sha256": file_sha256(genes_file),
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
        "finalists": variants.reset_index().to_dict("records"),
    }
    atomic_json(Path(manifest_path), manifest)
    return manifest


def build_workflow_config(
    variants: pd.DataFrame,
    *,
    output_root: Path,
    experiment_id: str,
    lineage_seed: int,
    generations: int,
    sim_data_path: Path | None,
) -> dict[str, object]:
    """Build the minimal official vEcoli lineage-workflow configuration."""

    if variants.empty:
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
            ADAPTER_MODULE: {"gene_ids": {"value": variants.vecoli_gene_ids.tolist()}}
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
) -> pd.DataFrame:
    fba_id = evaluator_ids["fba"]
    resource_id = evaluator_ids["resource_allocation"]
    rows = connection.execute(
        "SELECT s.state_id, s.state_json, f.metrics_json, r.metrics_json "
        "FROM states s "
        "JOIN evaluations f ON f.state_id = s.state_id AND f.evaluator_id = ? "
        "JOIN evaluations r ON r.state_id = s.state_id AND r.evaluator_id = ? "
        "WHERE s.state_id IN ("
        "SELECT parent_id FROM proposal_events WHERE run_id = ? "
        "UNION SELECT child_id FROM proposal_events WHERE run_id = ?)",
        (fba_id, resource_id, run_id, run_id),
    ).fetchall()
    finalists = {}
    for state_id, raw_state, raw_fba, raw_resource in rows:
        state = loads(str(raw_state))
        if not isinstance(state, GenomeState) or genome_state_key(state) != state_id:
            raise ValueError(f"state payload does not match ID: {state_id}")
        fba = loads(str(raw_fba))
        resource = loads(str(raw_resource))
        growth = fba.get("growth_rate")
        fba_positive = (
            fba.get("feasible") is True
            and isinstance(growth, (int, float))
            and not isinstance(growth, bool)
            and math.isfinite(growth)
            and growth > 0
        )
        resource_positive = resource.get("feasible_at_growth_floor") is True
        if fba_positive and resource_positive:
            assert isinstance(growth, (int, float)) and not isinstance(growth, bool)
            deleted = sorted(state.deleted_genes)
            finalists[state_id] = {
                "deleted_gene_ids": deleted,
                "deletion_count": len(deleted),
                "deletion_set_sha256": _json_sha256(deleted),
                "fba_growth_rate": float(growth),
                "fba_evaluator_id": fba_id,
                "resource_evaluator_id": resource_id,
            }
    return pd.DataFrame.from_dict(finalists, orient="index").rename_axis("state_id")


def _frozen_sqlite_hashes(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"graph does not exist: {path}")
    sidecars = [Path(f"{path}-wal"), Path(f"{path}-shm")]
    present = [candidate.name for candidate in sidecars if candidate.exists()]
    if present:
        raise ValueError(f"graph must be checkpointed; found sidecars: {present}")
    return {path.name: file_sha256(path)}


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
