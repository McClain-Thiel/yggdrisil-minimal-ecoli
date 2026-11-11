from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.track_vecoli_experiment import (
    cell_files,
    check_history,
    check_transcripts,
    save_s3_checkpoint,
    simulation_environment,
    stability_metrics,
    update_catalog,
    workflow_status,
)


def test_mac_launcher_stops_before_opening_a_manifest(monkeypatch, capsys) -> None:
    import scripts.track_vecoli_experiment as tracker

    monkeypatch.setattr(tracker.sys, "platform", "darwin")
    monkeypatch.setattr(tracker.sys, "argv", ["tracker", "--manifest", "missing.json"])
    with pytest.raises(SystemExit) as stopped:
        tracker.main()
    assert stopped.value.code == 2
    assert "remote-only" in capsys.readouterr().err


def test_completed_divisions_do_not_hide_progressive_mass_loss() -> None:
    generations = [
        {
            "generation_duration_s": 2000,
            "final_dry_mass_fg": 100 / 2**i,
            "final_dry_mass_fold_change": 1,
        }
        for i in range(12)
    ]
    result = stability_metrics(generations)
    assert result["window_generations"] == 4
    assert result["duration_cv"] == 0
    assert result["birth_mass_last_over_first"] == 0.125
    assert result["mean_dry_mass_fold_change"] == 1


def test_knockout_audit_checks_active_transcripts(tmp_path: Path) -> None:
    path = tmp_path / "daughter.json"
    state = {
        "agents": {
            "00": {
                "unique_dtypes": {
                    "RNA": "[('TU_index', '<i8'), ('_entryState', 'i1')]"
                },
                "unique": {"RNA": [[7, 0], [8, 1]]},
            }
        }
    }
    path.write_text(json.dumps(state))
    check_transcripts(path, {7})
    with pytest.raises(RuntimeError, match="reappeared"):
        check_transcripts(path, {8})


def test_snapshot_excludes_running_cells_and_workflow_source(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    for name in (".command.out", ".command.sh", ".exitcode"):
        (work / name).write_text("data")
    task = SimpleNamespace(exit_code=None, workdir=work, variant_index=2, generation=1)
    assert cell_files(tmp_path, task, 101) == []
    task.exit_code = 0
    names = {p.name for p in cell_files(tmp_path, task, 101)}
    assert names == {".command.out", ".exitcode"}


def test_history_audit_distinguishes_initial_null_time_from_invalid_mass(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "history"
    folder.mkdir()
    path = folder / "399.pq"
    row = {
        "global_time": [None],
        "listeners__mass__cell_mass": [100.0],
        "listeners__mass__dry_mass": [30.0],
        "listeners__mass__dry_mass_fold_change": [1.0],
    }
    pq.write_table(pa.table(row), path)
    check_history([path])
    row["listeners__mass__dry_mass"] = [float("nan")]
    pq.write_table(pa.table(row), path)
    with pytest.raises(RuntimeError, match="Invalid listeners__mass__dry_mass"):
        check_history([path])


def test_catalog_update_preserves_other_experiments_and_requires_known_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "experiments.json"
    path.write_text(
        json.dumps({"experiments": [{"id": "a", "notes": "keep"}, {"id": "b"}]})
    )
    update_catalog(path, "a", status="running")
    assert json.loads(path.read_text())["experiments"] == [
        {"id": "a", "notes": "keep", "status": "running"},
        {"id": "b"},
    ]
    with pytest.raises(ValueError, match="Expected one catalog entry"):
        update_catalog(path, "unknown", status="running")


def test_native_tasks_use_the_simulator_python_and_no_api_credentials() -> None:
    manifest = {
        "vecoli": {"checkout": "/sim"},
        "execution": {
            "vecoli_python": "/sim/.venv/bin/python",
            "nextflow_executable": "/tools/nextflow",
        },
    }
    parent = {"PATH": "/app/.venv/bin:/usr/bin", "WANDB_API_KEY": "private"}
    env = simulation_environment(manifest, parent)
    assert env["PATH"].split(":")[:2] == ["/sim/.venv/bin", "/tools"]
    assert env["VIRTUAL_ENV"] == "/sim/.venv"
    assert "WANDB_API_KEY" not in env
    assert parent["WANDB_API_KEY"] == "private"


def test_setup_failure_is_not_reported_as_biological_nondivision() -> None:
    result = {
        "all_reached_max_generations": False,
        "finalists": [{"terminal_reason": "not_scheduled_or_workflow_incomplete"}],
    }
    assert workflow_status(result) == "failed"
    result["finalists"][0]["terminal_reason"] = "nondivision_max_duration"
    assert workflow_status(result) == "completed_with_nondivision"


def test_s3_checkpoint_verifies_bytes_and_preserves_prior_files(tmp_path: Path) -> None:
    import io

    from botocore.response import StreamingBody

    objects = {}

    class Store:
        corrupt = False

        def put_object(self, *, Bucket, Key, Body, **kwargs):
            objects[Key] = Body

        def get_object(self, *, Bucket, Key):
            data = b"corrupt" if self.corrupt else objects[Key]
            return {"Body": StreamingBody(io.BytesIO(data), len(data))}

    store = Store()
    data = tmp_path / "progress.json"
    data.write_text('{"generation": 1}')
    first = save_s3_checkpoint(store, "s3://private/run", {"progress.json": data}, {})
    data.write_text('{"generation": 2}')
    second = save_s3_checkpoint(store, "s3://private/run", {"other.json": data}, first)
    assert set(second["files"]) == {"progress.json", "other.json"}
    assert first["files"]["progress.json"] == second["files"]["progress.json"]
    store.corrupt = True
    with pytest.raises(RuntimeError, match="verification failed"):
        save_s3_checkpoint(store, "s3://private/run", {"new.json": data}, second)
    assert "new.json" not in second["files"]
