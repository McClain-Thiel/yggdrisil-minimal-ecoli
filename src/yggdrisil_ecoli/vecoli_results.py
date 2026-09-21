"""Summarize vEcoli lineage outcomes from durable workflow artifacts."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from pydantic import BaseModel, Field, StrictInt, TypeAdapter

from yggdrisil_ecoli.data.io import atomic_json, file_sha256
from yggdrisil_ecoli.vecoli import _frozen_sqlite_hashes, validate_vecoli_checkout

_VARIANT = re.compile(r"--variant\s+(\d+)")
_GENERATION = re.compile(r"generation=(\d+)")
_DIVISION_TIME = re.compile(r"export division_time=([0-9.]+)")


@dataclass(frozen=True, slots=True)
class SimulationTask:
    """One vEcoli simulation task recovered from a Nextflow work directory."""

    variant_index: int
    generation: int
    workdir: Path
    exit_code: int | None
    division_global_time_s: float | None
    wall_time_ms: int | None


class _FinalistIdentity(BaseModel):
    variant_index: StrictInt
    state_id: str = Field(strict=True, min_length=1)
    deletion_count: StrictInt
    deletion_set_sha256: str = Field(strict=True, min_length=1)


def summarize_vecoli_lineages(
    manifest_path: str | Path, result_path: str | Path
) -> dict[str, Any]:
    """Report consecutive divisions only after checking frozen input provenance."""
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text())
    _validate_manifest_provenance(manifest)
    workflow, lineage = manifest["workflow"], manifest["lineage"]
    seed, maximum = TypeAdapter(tuple[StrictInt, StrictInt]).validate_python(
        (lineage["seed"], lineage["max_generations"])
    )
    if not 1 <= maximum <= 20 or not manifest["finalists"]:
        raise ValueError("expected finalists and a maximum of 1 to 20 generations")
    experiment_dir = Path(workflow["output_root"]) / workflow["experiment_id"]
    tasks = _simulation_tasks(experiment_dir / "nextflow" / "nextflow_workdirs")
    finalists = [
        _summarize_finalist(
            item,
            experiment_dir=experiment_dir,
            tasks=tasks,
            seed=seed,
            max_generations=maximum,
        )
        for item in manifest["finalists"]
    ]
    result = {
        "schema_version": 1,
        "purpose": "vEcoli finalist lineage outcomes",
        "manifest_path": str(manifest_file.resolve()),
        "manifest_sha256": file_sha256(manifest_file),
        "workflow_config_sha256": workflow["config_sha256"],
        "experiment_dir": str(experiment_dir),
        "finalists": finalists,
        "all_reached_max_generations": all(
            item["terminal_reason"] == "reached_max_generations" for item in finalists
        ),
        "all_biologically_failed": all(
            item["terminal_reason"] == "nondivision_max_duration" for item in finalists
        ),
    }
    atomic_json(Path(result_path), result)
    return result


def _verify_file(path: str | Path, expected_hash: str, label: str) -> None:
    if file_sha256(path) != expected_hash:
        raise ValueError(f"{label} hash no longer matches the manifest")


def _validate_manifest_provenance(manifest: dict[str, Any]) -> None:
    application = manifest.get("application")
    if application is not None:
        _verify_file(
            application["selection_source_path"],
            application["selection_source_sha256"],
            "selection source",
        )
        sources = [{"selection": manifest["selection"]}]
    else:
        sources = manifest["source_graphs"]
        if not isinstance(sources, list) or not sources:
            raise ValueError("combined manifest source_graphs must be a nonempty list")
    for source in sources:
        selection = source["selection"]
        path = Path(selection["graph_path"])
        expected_hash = selection["graph_files"][path.name]
        if application is None:
            backup = source["backup"]
            if (path.resolve(), expected_hash) != (
                Path(backup["frozen_path"]).resolve(),
                backup["frozen_sha256"],
            ):
                raise ValueError("selection and backup graph paths or hashes differ")
        if _frozen_sqlite_hashes(path)[path.name] != expected_hash:
            raise ValueError("source graph hash no longer matches the manifest")

    registry, vecoli, workflow = (
        manifest[key] for key in ("registry", "vecoli", "workflow")
    )
    _verify_file(registry["path"], registry["sha256"], "registry")
    _verify_file(workflow["config_path"], workflow["config_sha256"], "workflow config")
    if (
        workflow.get("sim_data_path") is not None
        or workflow.get("sim_data_sha256") is not None
    ):
        _verify_file(
            workflow["sim_data_path"], workflow["sim_data_sha256"], "vEcoli simData"
        )
    current = validate_vecoli_checkout(Path(vecoli["checkout"]))
    for key in ("git_commit", "uv_lock_sha256", "nextflow_version"):
        if current[key] != vecoli[key]:
            raise ValueError(f"vEcoli {key} no longer matches the manifest")
    for prefix in ("adapter", "variant_knockout_audit"):
        path, digest = vecoli.get(f"{prefix}_path"), vecoli.get(f"{prefix}_sha256")
        if prefix == "adapter" or path is not None or digest is not None:
            _verify_file(path, digest, f"vEcoli {prefix}")


def _summarize_finalist(
    finalist: dict[str, Any],
    *,
    experiment_dir: Path,
    tasks: dict[tuple[int, int], SimulationTask],
    seed: int,
    max_generations: int,
) -> dict[str, object]:
    identity = _FinalistIdentity.model_validate(finalist)
    variant_index = identity.variant_index
    generations: list[dict[str, object]] = []
    previous_division_global_time_s = 0.0
    for generation in range(1, max_generations + 1):
        task = tasks.get((variant_index, generation))
        daughter_dir = (
            experiment_dir
            / "daughter_states"
            / f"variant={variant_index}"
            / f"seed={seed}"
            / f"generation={generation}"
            / f"agent_id={'0' * generation}"
        )
        daughters = [
            daughter_dir / "daughter_state_0.json",
            daughter_dir / "daughter_state_1.json",
        ]
        if (
            task is None
            or task.exit_code != 0
            or not all(path.is_file() for path in daughters)
        ):
            break
        generations.append(
            {
                "generation": generation,
                "division_global_time_s": task.division_global_time_s,
                "generation_duration_s": (
                    task.division_global_time_s - previous_division_global_time_s
                    if task.division_global_time_s is not None
                    else None
                ),
                "wall_time_ms": task.wall_time_ms,
                **_final_mass_measurements(
                    experiment_dir,
                    variant_index=variant_index,
                    seed=seed,
                    generation=generation,
                ),
                "daughter_state_sha256": {
                    path.name: file_sha256(path) for path in daughters
                },
            }
        )
        if task.division_global_time_s is not None:
            previous_division_global_time_s = task.division_global_time_s
    completed = len(generations)
    terminal_task = tasks.get((variant_index, completed + 1))
    terminal_reason, terminal = _terminal_outcome(
        terminal_task, completed=completed, maximum=max_generations
    )
    return {
        **identity.model_dump(),
        "generations_completed": completed,
        "maximum_generations": max_generations,
        "terminal_reason": terminal_reason,
        "terminal_task": terminal,
        "generations": generations,
    }


def _final_mass_measurements(
    experiment_dir: Path,
    *,
    variant_index: int,
    seed: int,
    generation: int,
) -> dict[str, float]:
    history = (
        experiment_dir
        / "history"
        / f"experiment_id={experiment_dir.name}"
        / f"variant={variant_index}"
        / f"lineage_seed={seed}"
        / f"generation={generation}"
        / f"agent_id={'0' * generation}"
    )
    chunks = list(history.glob("*.pq"))
    if not chunks:
        raise ValueError(f"successful generation lacks emitted history: {history}")
    try:
        final_chunk = max(chunks, key=lambda path: float(path.stem))
    except ValueError as exc:
        raise ValueError(f"history chunk has a nonnumeric time: {history}") from exc
    columns = {
        "final_global_time_s": "global_time",
        "final_cell_mass_fg": "listeners__mass__cell_mass",
        "final_dry_mass_fg": "listeners__mass__dry_mass",
        "final_dry_mass_fold_change": "listeners__mass__dry_mass_fold_change",
    }
    table = pq.read_table(final_chunk, columns=list(columns.values()))
    final = table.slice(table.num_rows - 1).to_pylist()[0]
    values = {name: final[column] for name, column in columns.items()}
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        for value in values.values()
    ):
        raise ValueError(f"generation emitted non-finite mass values: {history}")
    return {name: float(value) for name, value in values.items()}


def _terminal_outcome(
    task: SimulationTask | None, *, completed: int, maximum: int
) -> tuple[str, dict[str, object] | None]:
    if completed == maximum:
        return "reached_max_generations", None
    if task is None:
        return "not_scheduled_or_workflow_incomplete", None
    if task.exit_code is None:
        return "running", _task_payload(task, None)
    error_path = task.workdir / ".command.err"
    error = error_path.read_text(errors="replace") if error_path.is_file() else ""
    error_hash = file_sha256(error_path) if error_path.is_file() else None
    if task.exit_code in {9, 137}:
        reason = "resource_failure"
    elif task.exit_code == 0:
        reason = "orchestration_failure"
    elif "TimeLimitError" in error or "reached max duration" in error.lower():
        reason = "nondivision_max_duration"
    else:
        reason = "model_exception"
    return reason, _task_payload(task, error_hash)


def _task_payload(task: SimulationTask, error_sha256: str | None) -> dict[str, object]:
    return {
        "generation": task.generation,
        "exit_code": task.exit_code,
        "workdir": str(task.workdir),
        "wall_time_ms": task.wall_time_ms,
        "stderr_sha256": error_sha256,
    }


def _simulation_tasks(workdirs: Path) -> dict[tuple[int, int], SimulationTask]:
    if not workdirs.is_dir():
        raise ValueError(f"Nextflow work directory does not exist: {workdirs}")
    tasks: dict[tuple[int, int], SimulationTask] = {}
    for command_path in workdirs.rglob(".command.sh"):
        command = command_path.read_text(errors="replace")
        if "ecoli_master_sim.py" not in command:
            continue
        variant_match = _VARIANT.search(command)
        generation_matches = _GENERATION.findall(command)
        if variant_match is None or not generation_matches:
            raise ValueError(f"cannot identify vEcoli task: {command_path.parent}")
        variant = int(variant_match.group(1))
        # Daughter simulations also mention their parent's generation in the
        # inherited-state URI. The output directory is last and is the task's
        # actual generation.
        generation = int(generation_matches[-1])
        exit_path = command_path.with_name(".exitcode")
        exit_code = int(exit_path.read_text()) if exit_path.is_file() else None
        division_path = command_path.with_name("division_time.sh")
        division_time = None
        if division_path.is_file():
            match = _DIVISION_TIME.fullmatch(division_path.read_text().strip())
            if match is None:
                raise ValueError(f"malformed division time: {division_path}")
            division_time = float(match.group(1))
        wall_time = _wall_time_ms(command_path.with_name(".command.trace"))
        key = (variant, generation)
        task = SimulationTask(
            variant_index=variant,
            generation=generation,
            workdir=command_path.parent,
            exit_code=exit_code,
            division_global_time_s=division_time,
            wall_time_ms=wall_time,
        )
        existing = tasks.get(key)
        if existing is None or _prefer_task(task, existing):
            tasks[key] = task
    return tasks


def _prefer_task(candidate: SimulationTask, current: SimulationTask) -> bool:
    """Prefer a successful retry, otherwise the most recently changed task."""

    if candidate.exit_code == 0 and current.exit_code != 0:
        return True
    if current.exit_code == 0 and candidate.exit_code != 0:
        return False
    return candidate.workdir.stat().st_mtime > current.workdir.stat().st_mtime


def _wall_time_ms(path: Path) -> int | None:
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        if line.startswith("realtime="):
            value = line.split("=", 1)[1]
            return int(value) if value else None
    return None
