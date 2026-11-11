"""Offline handoff safety checks; no simulation or cloud calls."""

import copy
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.close_depth_search import close_snapshot
from scripts.depth_matched_validation import (
    child_environment,
    prepared_resume,
    validate_bounds,
    wait_search,
)


def bounds():
    approval = {"paid_launch": True, "approved_cap_usd": 100,
                "model_calls_allowed": False, "new_instances": 0,
                "workers": [{"role": "primary", "instance_id": "approved-primary"}],
                "science_deadline_unix": 1794436222, "stop_deadline_unix": 1794443422,
                "experiment_id": "matched"}
    plan = {"experiment_id": "matched", "worker_allocation": {
        "primary": {"seeds": [101, 211], "maximum_cells_per_workflow": 4},
        "secondary": {"seeds": [307], "maximum_cells_per_workflow": 8}}}
    return approval, plan


def test_bounds_reject_budget_deadline_worker_and_duplicate_seeds():
    a, p = bounds()
    assert validate_bounds(a, p, "primary", "approved-primary")["role"] == "primary"
    for key, value in [("approved_cap_usd", 101), ("new_instances", 1),
                       ("science_deadline_unix", 1794436223), ("stop_deadline_unix", 1794443423),
                       ("model_calls_allowed", True), ("paid_launch", False)]:
        changed = {**a, key: value}
        with pytest.raises(ValueError):
            validate_bounds(changed, p, "primary", "approved-primary")
    with pytest.raises(ValueError):
        validate_bounds(a, p, "primary", "shared-worker")
    changed = copy.deepcopy(p)
    changed["worker_allocation"]["secondary"]["seeds"] = [211]
    with pytest.raises(ValueError):
        validate_bounds(a, changed, "primary", "approved-primary")


def test_child_tracking_service_is_independent_and_model_keys_absent():
    with patch.dict(os.environ, {"WANDB_SERVICE": "parent-service", "WANDB_API_KEY": "tracking-only", "OPENROUTER_API_KEY": "model"}):
        env = child_environment(Path("/approved"))
    assert "WANDB_SERVICE" not in env and "OPENROUTER_API_KEY" not in env
    assert env["WANDB_API_KEY"] == "tracking-only"
    assert env["OMP_NUM_THREADS"] == "1"


def test_live_search_child_prevents_shutdown_cancellation(tmp_path):
    root, search = tmp_path / "new", tmp_path / "old"
    root.mkdir()
    search.mkdir()
    (search / "batch-progress.json").write_text(json.dumps({
        "status": "search_completed_pending_independent_review",
        "supervisor": {"pid": 11, "start_ticks": "1"},
        "jobs": {"0": {"pid": 12, "start_ticks": "2"}}}))
    with patch("scripts.depth_matched_validation.identity", side_effect=[False, True]), patch("scripts.depth_matched_validation.subprocess.run") as run:
        with pytest.raises(RuntimeError, match="child still alive"):
            wait_search(root, search, "primary", {})
        run.assert_not_called()


def test_failed_search_does_not_cancel_existing_shutdown(tmp_path):
    (tmp_path / "batch-progress.json").write_text(json.dumps({"status": "failed_preserving_disk"}))
    with patch("scripts.depth_matched_validation.subprocess.run") as run:
        with pytest.raises(RuntimeError, match="operational failure"):
            wait_search(tmp_path, tmp_path, "primary", {})
        run.assert_not_called()


def test_closure_changes_only_open_status_in_separate_snapshot(tmp_path):
    graph = tmp_path / "snapshot.sqlite"
    db = sqlite3.connect(graph)
    db.executescript("CREATE TABLE runs(run_id TEXT,status TEXT,metadata_json TEXT);"
                     "CREATE TABLE states(state_id TEXT,state_json TEXT);"
                     "CREATE TABLE evaluations(state_id TEXT,metrics_json TEXT);"
                     "INSERT INTO runs VALUES('parent','completed','parent evidence');"
                     "INSERT INTO runs VALUES('current','running','current evidence');"
                     "INSERT INTO states VALUES('state','exact genotype');"
                     "INSERT INTO evaluations VALUES('state','exact evaluation');")
    db.commit()
    db.close()
    original = graph.read_bytes()
    proof = close_snapshot(graph, "current")
    assert proof["all_scientific_rows_unchanged"] is True
    assert graph.read_bytes() != original
    db = sqlite3.connect(graph)
    assert db.execute("SELECT * FROM runs ORDER BY run_id").fetchall() == [
        ("current", "failed", "current evidence"), ("parent", "completed", "parent evidence")]
    assert db.execute("SELECT * FROM states").fetchall() == [("state", "exact genotype")]
    db.close()
    again = close_snapshot(graph, "current")
    assert again["original_archived_snapshot_sha256"] == again["closed_snapshot_sha256"]
    with pytest.raises(RuntimeError, match="Unexpected saved run status"):
        close_snapshot(graph, "unknown")


def test_prepared_recovery_preserves_selection_and_refuses_started_workflow(tmp_path):
    import hashlib
    root = tmp_path
    valid = root / "validation"
    attempt = root / "attempts/preflight-policy-link"
    attempt.mkdir(parents=True)
    (root / "bundle").mkdir()
    (root / "bundle/matched-nominations.json").write_text('{"frozen": true}')
    manifests = {}
    for label in ["calibration-secondary", "seed-307"]:
        folder = valid / "runs" / label
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text('{}')
        manifests[label] = hashlib.sha256(b'{}').hexdigest()
    (valid / "prepared.json").write_text(json.dumps({"status": "passed", "manifest_sha256": manifests}))
    (valid / "preflight.json").write_text('{"status":"passed"}')
    (root / "credential-pattern-policy.json").write_text('{}')
    (valid / "credential-pattern-policy.json").write_text('{}')
    previous = {"role": "secondary", "status": "failed_preserving_disk",
                "error": {"type": "ValueError", "message": "Unsafe study backup member"},
                "selection": {"selection_sha256": hashlib.sha256((root / "bundle/matched-nominations.json").read_bytes()).hexdigest()}}
    (attempt / "handoff-progress.json").write_text(json.dumps(previous))
    assert prepared_resume(root, "secondary") == previous
    policy = valid / "credential-pattern-policy.json"
    policy.unlink()
    policy.symlink_to(root / policy.name)
    with pytest.raises(RuntimeError, match="identical regular-file copy"):
        prepared_resume(root, "secondary")
    policy.unlink()
    policy.write_text('{"changed":true}')
    with pytest.raises(RuntimeError, match="identical regular-file copy"):
        prepared_resume(root, "secondary")
    policy.write_text('{}')
    with pytest.raises(RuntimeError, match="preserved preflight-only failure"):
        prepared_resume(root, "primary")
    (valid / "runs/seed-307/process.json").write_text('{}')
    with pytest.raises(RuntimeError, match="already launched"):
        prepared_resume(root, "secondary")
    (valid / "runs/seed-307/process.json").unlink()
    (root / "bundle/matched-nominations.json").write_text('{"changed": true}')
    with pytest.raises(RuntimeError, match="preserved preflight-only failure"):
        prepared_resume(root, "secondary")
