#!/usr/bin/env python3
"""Preserve authorized user-stopped searches and export separately closed snapshots."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from scripts.collect_search_replication import collect, export_graph_report
from scripts.depth_matched_validation import read, validate_bounds, verify_archive
from scripts.plan_search_replication import encoded
from scripts.run_search_replication import save, worker_identity
from scripts.supervise_baseline_depth_parallel import identity
from scripts.supervise_search_replication import publish, search_files
from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets
from yggdrisil_ecoli.data.io import file_sha256


def table_hashes(db: sqlite3.Connection) -> dict[str, str]:
    result = {}
    names = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name!='runs' ORDER BY name"
        )
    ]
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        h = hashlib.sha256()
        for row in db.execute("SELECT * FROM " + quoted + " ORDER BY rowid"):
            h.update(repr(row).encode())
            h.update(b"\n")
        result[name] = h.hexdigest()
    return result


def close_snapshot(graph: Path, run_id: str) -> dict[str, Any]:
    before = file_sha256(graph)
    with closing(sqlite3.connect(graph)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("Snapshot integrity failure")
        rows = db.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        columns = [x[1] for x in db.execute("PRAGMA table_info(runs)")]
        hashes = table_hashes(db)
        status = db.execute(
            "SELECT status FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if status is None or status[0] not in {"running", "failed", "completed"}:
            raise RuntimeError("Unexpected saved run status")
        if status[0] == "running":
            db.execute(
                "UPDATE runs SET status='failed' WHERE run_id=? AND status='running'",
                (run_id,),
            )
            db.commit()
        after_rows = db.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        expected = []
        for row in rows:
            values = list(row)
            if (
                values[columns.index("run_id")] == run_id
                and values[columns.index("status")] == "running"
            ):
                values[columns.index("status")] = "failed"
            expected.append(tuple(values))
        if after_rows != expected or table_hashes(db) != hashes:
            raise RuntimeError("Snapshot closure altered scientific records")
    return {
        "original_archived_snapshot_sha256": before,
        "closed_snapshot_sha256": file_sha256(graph),
        "run_status_before": status[0],
        "run_status_after": "failed" if status[0] == "running" else status[0],
        "all_nonrun_tables_sha256": hashes,
        "all_scientific_rows_unchanged": True,
        "original_worker_database_modified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--search-root", required=True, type=Path)
    parser.add_argument("--role", required=True, choices=["primary", "secondary"])
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("Remote Linux only")
    root, search, role = args.root.resolve(), args.search_root.resolve(), args.role
    validate_bounds(
        read(root / "resource-approval.json"),
        read(root / "plan.json"),
        role,
        worker_identity(),
    )
    authorization = read(root / "early-stop-authorization.json")
    if authorization["status"] != "authorized_user_early_stop":
        raise RuntimeError("Missing explicit early-stop authorization")
    closed = read(root / "early-stop-closed.json")
    batch = closed["batch"]
    if closed["status"] != "all_search_processes_closed" or identity(
        batch["supervisor"]["pid"], batch["supervisor"]["start_ticks"]
    ):
        raise RuntimeError("Original supervisor remains active")
    for item in batch["jobs"].values():
        if item.get("pid") and identity(item["pid"], item["start_ticks"]):
            raise RuntimeError("Original search child remains active")
    lock = (search / "batch.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for name, sha in read(search / "bundle/runtime-manifest.json")["files"].items():
        if file_sha256(search / name) != sha:
            raise RuntimeError("Frozen scientific source changed: " + name)
    bundle = root / "closed-search"
    if bundle.exists():
        raise RuntimeError(
            "Closed snapshot workspace already exists; inspect before recovery"
        )
    os.environ.update(read(root / "credentials/tracking.json"))
    os.environ.pop("WANDB_SERVICE", None)
    import boto3
    import wandb
    from botocore.exceptions import ClientError

    experiment = authorization["search_experiment_id"]
    with wandb.init(
        entity="mcclain",
        project="yggdrisil",
        id=experiment + "-user-stop-" + role,
        name=experiment + "-user-stop-" + role,
        job_type="preservation",
        tags=["yggdrisil", "baseline", "user-stopped", role],
        config=authorization,
        dir=str(root),
        settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False),
    ) as tracked:
        store = BaselineArchive(
            s3=boto3.client("s3", region_name="us-east-1"),
            wandb=wandb,
            tracked=tracked,
            owner=boto3.client("sts").get_caller_identity()["Account"],
            bucket="phd-research-storage-1758274488",
            prefix="yggdrisil/experiments/" + experiment + "/",
            run_id="user-stop-" + role,
            secrets=configured_secrets(root / "application", root / "credentials"),
        )
        original_proof = verify_archive(store, batch["failure_backup"], bundle)
        save(root / "early-stop-original-verification.json", original_proof)
        jobs = read(bundle / "jobs.json")
        timing = read(root / "early-stop-processes-final.json")
        recovery = (
            read(search / "logging-recovery.json")["jobs"]
            if (search / "logging-recovery.json").exists()
            else {}
        )
        closures = {}
        for i in batch["assigned_indices"]:
            job = jobs[i]
            folder = bundle / job["directory"]
            if (folder / "completion.json").exists():
                if read(folder / "completion.json")["scientific_status"] != "completed":
                    raise RuntimeError("Unexpected previous terminal search")
                continue
            proof = close_snapshot(folder / "graph.sqlite", job["run_id"])
            save(folder / "snapshot-closure.json", proof)
            proc = timing["processes"][str(i)]
            lower = (
                proc.get("last_cpu_ticks", proc["cpu_ticks"])
                / proc["clock_ticks_per_second"]
                / 3600
            )
            upper = (
                lower
                + max(
                    0,
                    closed["at_unix"]
                    - proc.get("last_observed_unix", proc["captured_at_unix"]),
                )
                * 16
                / 3600
            )
            prior = recovery.get(job["run_id"], {}).get("prior_cpu_hours", 0)
            launch = read(folder / "launch.json")
            terminal = {
                "status": "user_stopped",
                "stop_reason": "user_early_stop_at_target_depth",
                "error": None,
                "validation_started": False,
                "model_spend_usd": 0,
                "wall_time_s": closed["at_unix"] - launch["started_at_unix"],
                "cpu_hours": prior + upper,
                "cpu_hours_is_upper_bound": True,
                "cpu_current_attempt_lower_bound": lower,
                "cpu_accounting": "Last observed process CPU plus at most16logical cores during the final observation gap; prior recorded CPU retained. Not an exact CPU measurement.",
                "early_stop_authorization_sha256": file_sha256(
                    root / "early-stop-authorization.json"
                ),
                "closure_provenance": proof,
                "original_failure_archive": batch["failure_backup"],
                "inherited_candidates": 100,
                "prior_wall_time_s": job["parent"]["prior_wall_time_s"],
                "prior_cpu_hours": job["parent"]["prior_cpu_hours"],
            }
            save(folder / "terminal.json", terminal)
            report = export_graph_report(bundle, i, folder / "graph.sqlite", terminal)
            save(folder / "run-report.json", report)
            receipt = publish(
                {
                    name: folder / name
                    for name in [
                        "run-report.json",
                        "terminal.json",
                        "graph.sqlite",
                        "snapshot-closure.json",
                    ]
                },
                store,
                tracked,
                wandb,
                "closed-" + str(i),
                store.secrets,
            )
            verify_archive(store, receipt)
            member_sha = file_sha256(folder / "run-report.json")
            save(
                folder / "report-verification.json",
                {
                    "report_sha256": member_sha,
                    "protocol_sha256": file_sha256(bundle / "protocol.json"),
                    "full_s3_get_verified": True,
                    "wandb_download_verified": True,
                    "wandb_state": "COMMITTED",
                    "source_and_inputs_verified": True,
                    "s3_uri": receipt["archive"]["uri"],
                    "archive_member": "run-report.json",
                    "wandb_artifact": receipt["wandb_artifact"],
                    "checkpoint": receipt,
                },
            )
            save(
                folder / "completion.json",
                {
                    "status": "terminal_verified",
                    "scientific_status": "user_stopped",
                    "report_sha256": member_sha,
                    "checkpoint": receipt,
                },
            )
            closures[job["run_id"]] = {
                "provenance": proof,
                "receipt": receipt,
                "attempted": len(report["candidates"]),
            }
            save(root / "early-stop-closures.json", closures)
        paths = search_files(bundle, [jobs[i] for i in batch["assigned_indices"]])
        for i in batch["assigned_indices"]:
            name = jobs[i]["directory"] + "/snapshot-closure.json"
            if (bundle / name).exists():
                paths[name] = bundle / name
        paths["operation/early-stop-authorization.json"] = (
            root / "early-stop-authorization.json"
        )
        receipt = publish(paths, store, tracked, wandb, "closed-worker", store.secrets)
        verify_archive(store, receipt)
        proof = {
            "status": "verified",
            "role": role,
            "authorization_sha256": file_sha256(root / "early-stop-authorization.json"),
            "receipt": receipt,
            "assigned_indices": batch["assigned_indices"],
            "original_verification": original_proof,
            "wandb_url": tracked.url,
            "closures": closures,
        }
        save(root / "early-stop-worker-verification.json", proof)
        key = store.prefix + "early-stop/secondary-receipt.json"
        if role == "secondary":
            raw = encoded(proof)
            store.s3.put_object(
                Bucket=store.bucket,
                Key=key,
                Body=raw,
                ExpectedBucketOwner=store.owner,
                ServerSideEncryption="AES256",
            )
            body = store.s3.get_object(
                Bucket=store.bucket, Key=key, ExpectedBucketOwner=store.owner
            )["Body"]
            try:
                if body.read() != raw:
                    raise RuntimeError("Secondary receipt round-trip failed")
            finally:
                body.close()
        else:
            while True:
                if (
                    time.time()
                    > read(root / "resource-approval.json")["science_deadline_unix"]
                ):
                    raise TimeoutError(
                        "Secondary preservation did not finish before deadline"
                    )
                try:
                    body = store.s3.get_object(
                        Bucket=store.bucket, Key=key, ExpectedBucketOwner=store.owner
                    )["Body"]
                except ClientError as error:
                    if error.response["Error"]["Code"] != "NoSuchKey":
                        raise
                    time.sleep(15)
                    continue
                try:
                    other = json.loads(body.read())
                finally:
                    body.close()
                break
            expected = read(search / "parallel-assignment.json")["workers"][
                "secondary"
            ]["job_indices"]
            if (
                other["status"] != "verified"
                or other["authorization_sha256"] != proof["authorization_sha256"]
                or other["assigned_indices"] != expected
            ):
                raise RuntimeError("Secondary closure binding mismatch")
            with tempfile.TemporaryDirectory(
                dir=root, prefix="closed-secondary-"
            ) as temp:
                dest = Path(temp)
                verify_archive(store, other["receipt"], dest)
                for i in expected:
                    src, target = (
                        dest / jobs[i]["directory"],
                        bundle / jobs[i]["directory"],
                    )
                    if target.exists():
                        raise RuntimeError("Refusing to overwrite a search")
                    src.rename(target)
            save(bundle / "nominations.json", collect(bundle))
            nominee = publish(
                {"nominations.json": bundle / "nominations.json"},
                store,
                tracked,
                wandb,
                "nominations",
                store.secrets,
            )
            verify_archive(store, nominee)
            save(bundle / "nominations-verification.json", nominee)
            paths = search_files(bundle, jobs)
            paths["operation/early-stop-authorization.json"] = (
                root / "early-stop-authorization.json"
            )
            for job in jobs:
                name = job["directory"] + "/snapshot-closure.json"
                if (bundle / name).exists():
                    paths[name] = bundle / name
            receipt = publish(
                paths, store, tracked, wandb, "all-search-final", store.secrets
            )
            save(
                root / "early-stop-final-verification.json",
                verify_archive(store, receipt),
            )
            proof["receipt"] = receipt
        save(root / "early-stop-handoff.json", proof)
        print(
            json.dumps(
                {
                    "role": role,
                    "status": "verified_user_stop_handoff",
                    "receipt": receipt,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
