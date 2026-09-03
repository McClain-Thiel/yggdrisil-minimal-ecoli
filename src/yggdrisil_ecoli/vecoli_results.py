"""Summarize vEcoli lineage outcomes from durable workflow artifacts."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.registry import file_sha256
from yggdrisil_ecoli.vecoli import validate_vecoli_checkout

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


def summarize_vecoli_lineages(
    manifest_path: str | Path, result_path: str | Path
) -> dict[str, object]:
    """Report consecutive successful divisions and explicit terminal reasons."""

    manifest_file = Path(manifest_path)
    manifest = _mapping(json.loads(manifest_file.read_text()), "manifest")
    _validate_manifest_provenance(manifest)
    workflow = _mapping(manifest.get("workflow"), "workflow")
    config_path = Path(_string(workflow.get("config_path"), "config_path"))
    expected_config_hash = _string(workflow.get("config_sha256"), "config_sha256")
    if file_sha256(config_path) != expected_config_hash:
        raise ValueError("workflow config hash no longer matches the manifest")
    sim_data_path = workflow.get("sim_data_path")
    sim_data_hash = workflow.get("sim_data_sha256")
    if sim_data_path is not None or sim_data_hash is not None:
        sim_data_file = Path(_string(sim_data_path, "sim_data_path"))
        if file_sha256(sim_data_file) != _string(sim_data_hash, "sim_data_sha256"):
            raise ValueError("vEcoli simData hash no longer matches the manifest")
    output_root = Path(_string(workflow.get("output_root"), "output_root"))
    experiment_id = _string(workflow.get("experiment_id"), "experiment_id")
    experiment_dir = output_root / experiment_id
    workdirs = experiment_dir / "nextflow" / "nextflow_workdirs"
    if not workdirs.is_dir():
        raise ValueError(f"Nextflow work directory does not exist: {workdirs}")
    tasks = _simulation_tasks(workdirs)
    lineage = _mapping(manifest.get("lineage"), "lineage")
    seed = _integer(lineage.get("seed"), "lineage seed")
    max_generations = _integer(lineage.get("max_generations"), "maximum generations")
    raw_finalists = manifest.get("finalists")
    if not isinstance(raw_finalists, list):
        raise ValueError("manifest finalists must be a list")
    finalists = [
        _summarize_finalist(
            _mapping(item, "finalist"),
            experiment_dir=experiment_dir,
            tasks=tasks,
            seed=seed,
            max_generations=max_generations,
        )
        for item in raw_finalists
    ]
    result: dict[str, object] = {
        "schema_version": 1,
        "purpose": "vEcoli finalist lineage outcomes",
        "manifest_path": str(manifest_file.resolve()),
        "manifest_sha256": file_sha256(manifest_file),
        "workflow_config_sha256": expected_config_hash,
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


def _validate_manifest_provenance(manifest: dict[str, Any]) -> None:
    raw_application = manifest.get("application")
    if raw_application is None:
        _validate_combined_source_graphs(manifest.get("source_graphs"))
    else:
        application = _mapping(raw_application, "application provenance")
        selection_source = Path(
            _string(application.get("selection_source_path"), "selection source path")
        )
        if file_sha256(selection_source) != _string(
            application.get("selection_source_sha256"), "selection source hash"
        ):
            raise ValueError("selection source hash no longer matches the manifest")

        selection = _mapping(manifest.get("selection"), "selection")
        graph_path = Path(_string(selection.get("graph_path"), "graph path"))
        graph_files = _mapping(selection.get("graph_files"), "graph files")
        expected_graph_hash = _string(
            graph_files.get(graph_path.name), "source graph hash"
        )
        _validate_source_graph(graph_path, expected_graph_hash)

    registry = _mapping(manifest.get("registry"), "registry")
    registry_path = Path(_string(registry.get("path"), "registry path"))
    if file_sha256(registry_path) != _string(registry.get("sha256"), "registry hash"):
        raise ValueError("registry hash no longer matches the manifest")

    vecoli = _mapping(manifest.get("vecoli"), "vEcoli provenance")
    checkout = Path(_string(vecoli.get("checkout"), "vEcoli checkout"))
    current = validate_vecoli_checkout(checkout)
    for key in ("git_commit", "uv_lock_sha256", "nextflow_version"):
        if current[key] != vecoli.get(key):
            raise ValueError(f"vEcoli {key} no longer matches the manifest")
    adapter_path = Path(_string(vecoli.get("adapter_path"), "adapter path"))
    if file_sha256(adapter_path) != _string(
        vecoli.get("adapter_sha256"), "adapter hash"
    ):
        raise ValueError("vEcoli adapter hash no longer matches the manifest")
    audit_path = vecoli.get("variant_knockout_audit_path")
    audit_hash = vecoli.get("variant_knockout_audit_sha256")
    if (audit_path is None) != (audit_hash is None):
        raise ValueError("variant knockout audit path and hash must appear together")
    if audit_path is not None:
        audit_file = Path(_string(audit_path, "variant knockout audit path"))
        if file_sha256(audit_file) != _string(
            audit_hash, "variant knockout audit hash"
        ):
            raise ValueError(
                "variant knockout audit hash no longer matches the manifest"
            )


def _validate_combined_source_graphs(raw_graphs: object) -> None:
    if not isinstance(raw_graphs, list) or not raw_graphs:
        raise ValueError("combined manifest source_graphs must be a nonempty list")
    for raw_record in raw_graphs:
        record = _mapping(raw_record, "source graph record")
        backup = _mapping(record.get("backup"), "source graph backup")
        selection = _mapping(record.get("selection"), "source graph selection")
        graph_path = Path(_string(selection.get("graph_path"), "graph path"))
        frozen_path = Path(_string(backup.get("frozen_path"), "frozen graph path"))
        if graph_path.resolve() != frozen_path.resolve():
            raise ValueError("selection and backup graph paths differ")
        graph_files = _mapping(selection.get("graph_files"), "graph files")
        expected_graph_hash = _string(
            graph_files.get(graph_path.name), "source graph hash"
        )
        if expected_graph_hash != _string(
            backup.get("frozen_sha256"), "frozen graph hash"
        ):
            raise ValueError("selection and backup graph hashes differ")
        _validate_source_graph(graph_path, expected_graph_hash)


def _validate_source_graph(graph_path: Path, expected_hash: str) -> None:
    if file_sha256(graph_path) != expected_hash:
        raise ValueError("source graph hash no longer matches the manifest")
    if Path(f"{graph_path}-wal").exists() or Path(f"{graph_path}-shm").exists():
        raise ValueError("source graph acquired SQLite sidecars after selection")


def _summarize_finalist(
    finalist: dict[str, Any],
    *,
    experiment_dir: Path,
    tasks: dict[tuple[int, int], SimulationTask],
    seed: int,
    max_generations: int,
) -> dict[str, object]:
    variant_index = _integer(finalist.get("variant_index"), "variant index")
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
        "variant_index": variant_index,
        "state_id": _string(finalist.get("state_id"), "state ID"),
        "deletion_count": _integer(finalist.get("deletion_count"), "deletion count"),
        "deletion_set_sha256": _string(
            finalist.get("deletion_set_sha256"), "deletion set hash"
        ),
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
    table = pq.read_table(
        final_chunk,
        columns=[
            "global_time",
            "listeners__mass__cell_mass",
            "listeners__mass__dry_mass",
            "listeners__mass__dry_mass_fold_change",
        ],
    )
    values = {
        "final_global_time_s": table["global_time"][-1].as_py(),
        "final_cell_mass_fg": table["listeners__mass__cell_mass"][-1].as_py(),
        "final_dry_mass_fg": table["listeners__mass__dry_mass"][-1].as_py(),
        "final_dry_mass_fold_change": table["listeners__mass__dry_mass_fold_change"][
            -1
        ].as_py(),
    }
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
    if "TimeLimitError" in error or "reached max duration" in error.lower():
        reason = "nondivision_max_duration"
    elif task.exit_code in {9, 137}:
        reason = "resource_failure"
    elif task.exit_code == 0:
        reason = "orchestration_failure"
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


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value
