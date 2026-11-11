#!/usr/bin/env python3
"""Verified handoff and bounded validation on the two approved existing workers."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from scripts.parallel_replication_validation import inspect_members
from scripts.run_search_replication import save, worker_identity
from scripts.supervise_baseline_depth_parallel import identity, verified_download
from scripts.supervise_search_replication import publish, search_files
from yggdrisil_ecoli.baseline_storage import (
    BaselineArchive,
    configured_secrets,
    verify_wandb_files,
)
from yggdrisil_ecoli.data.io import file_sha256


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def validate_bounds(approval: dict, plan: dict, role: str, instance_id: str) -> dict:
    assignments = plan["worker_allocation"]
    worker = next(w for w in approval["workers"] if w["role"] == role)
    if (
        not approval["paid_launch"]
        or approval["approved_cap_usd"] != 100
        or approval["model_calls_allowed"]
        or approval["new_instances"] != 0
        or worker["instance_id"] != instance_id
        or assignments
        != {
            "primary": {"seeds": [101, 211], "maximum_cells_per_workflow": 4},
            "secondary": {"seeds": [307], "maximum_cells_per_workflow": 8},
        }
        or approval["science_deadline_unix"] != 1794436222
        or approval["stop_deadline_unix"] != 1794443422
        or plan["experiment_id"] != approval["experiment_id"]
    ):
        raise ValueError("Worker, budget, seed allocation or original deadline changed")
    return worker


def child_environment(root: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"WANDB_SERVICE", "OPENROUTER_API_KEY", "EDISON_PLATFORM_API_KEY"}
    }
    env.update(
        PYTHONPATH=":".join(
            map(
                str,
                [
                    root / "application",
                    root / "application/src",
                    root / "framework/src",
                ],
            )
        ),
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        PYDANTIC_DISABLE_PLUGINS="__all__",
        NXF_VER="25.10.4",
    )
    return env


def prepared_resume(root: Path, role: str) -> dict:
    previous = read(root / "attempts/preflight-policy-link/handoff-progress.json")
    valid = root / "validation"
    prepared = read(valid / "prepared.json")
    expected = (
        {"calibration-primary", "seed-101", "seed-211"}
        if role == "primary"
        else {"calibration-secondary", "seed-307"}
    )
    if (
        previous["status"] != "failed_preserving_disk"
        or previous["role"] != role
        or previous.get("error")
        != {"type": "ValueError", "message": "Unsafe study backup member"}
        or previous.get("jobs")
        or prepared["status"] != "passed"
        or set(prepared["manifest_sha256"]) != expected
        or read(valid / "preflight.json")["status"] != "passed"
        or file_sha256(root / "bundle/matched-nominations.json")
        != previous["selection"]["selection_sha256"]
    ):
        raise RuntimeError(
            "Prepared recovery does not match the preserved preflight-only failure"
        )
    for label, digest in prepared["manifest_sha256"].items():
        folder = valid / "runs" / label
        if file_sha256(folder / "manifest.json") != digest:
            raise RuntimeError("Prepared recovery manifest changed")
        if any(
            (folder / name).exists()
            for name in [
                "controller.log",
                "process.json",
                "progress.json",
                "s3-checkpoint.json",
            ]
        ):
            raise RuntimeError("Prepared recovery refuses an already launched workflow")
    policy = valid / "credential-pattern-policy.json"
    if policy.is_symlink() or file_sha256(policy) != file_sha256(root / policy.name):
        raise RuntimeError("Policy must be an identical regular-file copy")
    return previous


def verify_archive(store: Any, receipt: dict, destination: Path | None = None) -> dict:
    import wandb

    with tempfile.TemporaryDirectory(prefix="matched-audit-") as tmp:
        temp = Path(tmp)
        for key in ("archive", "index"):
            verified_download(store, receipt[key], temp / key)
        index = read(temp / "index")
        if index["archive"] != receipt["archive"]:
            raise RuntimeError("Archive/index binding mismatch")
        inspect_members(temp / "archive", index)
        remote = wandb.Api(timeout=120).artifact(receipt["wandb_artifact"])
        verify_wandb_files(
            remote,
            {"data.tar.gz": receipt["archive"], "index.json": receipt["index"]},
            temp / "wandb",
        )
        if destination is not None:
            inspect_members(temp / "archive", index, destination)
    return {
        "status": "verified",
        "at_unix": time.time(),
        "receipt": receipt,
        "members": len(index["files"]),
        "full_s3_get_verified": True,
        "every_indexed_member_verified": True,
        "wandb_uncached_download_verified": True,
    }


def wait_search(root: Path, search: Path, role: str, state: dict) -> None:
    """Never cancel an old shutdown while its searches or final publisher are live."""
    wanted = (
        "search_completed_pending_independent_review"
        if role == "primary"
        else "worker_completed_verified"
    )
    while True:
        batch = read(search / "batch-progress.json")
        handoff_path = root / "early-stop-handoff.json"
        handoff = read(handoff_path) if handoff_path.exists() else None
        if handoff is not None:
            if (
                handoff["status"] != "verified"
                or handoff["role"] != role
                or handoff["authorization_sha256"]
                != file_sha256(root / "early-stop-authorization.json")
            ):
                raise RuntimeError("Unbound early-stop preservation record")
        elif batch["status"] == "failed_preserving_disk":
            raise RuntimeError(
                "Search ended with an operational failure; preserve and inspect"
            )

        supervisor = batch["supervisor"]
        live = identity(supervisor["pid"], supervisor["start_ticks"])
        if handoff is not None and live:
            raise RuntimeError(
                "Original supervisor still live during early-stop handoff"
            )
        if (handoff is not None or batch["status"] == wanted) and not live:
            for item in batch["jobs"].values():
                if item.get("pid") and identity(item["pid"], item["start_ticks"]):
                    raise RuntimeError("Search child still alive after terminal status")
            with (search / "batch.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                subprocess.run(
                    ["systemctl", "is-active", "yggdrisil-matched-stop.timer"],
                    check=True,
                )
                subprocess.run(["sudo", "shutdown", "-c"], check=True)
                # The replacement budget timer must be active before removing the old one.
                subprocess.run(
                    ["sudo", "systemctl", "stop", "yggdrisil-depth-stop.timer"],
                    check=True,
                )
            closed = {"at_unix": time.time(), "batch": batch}
            if handoff is not None:
                closed.update(
                    original_batch=batch,
                    early_stop=handoff,
                    batch={"search_final_backup": handoff["receipt"]},
                )
            state["search_closed"] = closed
            return
        if (
            time.time()
            >= read(root / "resource-approval.json")["science_deadline_unix"]
        ):
            raise TimeoutError("Validation deadline passed before handoff")
        state.update(status="waiting_for_search", updated_at_unix=time.time())
        save(root / "handoff-progress.json", state)
        time.sleep(20)


def frozen_selection(
    root: Path,
    search: Path,
    role: str,
    store: Any,
    tracked: Any,
    wandb: Any,
    state: dict,
) -> None:
    from botocore.exceptions import ClientError

    from scripts.plan_search_replication import encoded
    from scripts.select_depth_matched_baselines import select

    bucket, key = store.bucket, store.prefix + "selection-ready.json"
    bundle = root / "bundle"
    if bundle.exists():
        raise RuntimeError(
            "Selection workspace already exists; inspect before recovery"
        )
    if role == "primary":
        original = state["search_closed"]["batch"]["search_final_backup"]
        proof = verify_archive(store, original, bundle)
        save(root / "search-import-verification.json", proof)
        chosen = select(
            bundle,
            read(root / "selection-amendment.json"),
            root / "reference-nominations.json",
        )
        (bundle / "matched-nominations.json").write_bytes(encoded(chosen))
        nomination = publish(
            {"matched-nominations.json": bundle / "matched-nominations.json"},
            store,
            tracked,
            wandb,
            "nominations",
            store.secrets,
        )
        verify_archive(store, nomination)
        save(bundle / "matched-nominations-verification.json", nomination)
        paths = search_files(bundle, read(bundle / "jobs.json"))
        for name in (
            "matched-nominations.json",
            "matched-nominations-verification.json",
        ):
            paths[name] = bundle / name
        receipt = publish(paths, store, tracked, wandb, "frozen-inputs", store.secrets)
        save(root / "selection-input-verification.json", verify_archive(store, receipt))
        value = {
            "experiment_id": state["experiment_id"],
            "selection_sha256": file_sha256(bundle / "matched-nominations.json"),
            "amendment_sha256": file_sha256(root / "selection-amendment.json"),
            "receipt": receipt,
        }
        raw = encoded(value)
        store.s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=raw,
            ExpectedBucketOwner=store.owner,
            ServerSideEncryption="AES256",
        )
        body = store.s3.get_object(
            Bucket=bucket, Key=key, ExpectedBucketOwner=store.owner
        )["Body"]
        try:
            if body.read() != raw:
                raise RuntimeError("Selection handoff failed round trip")
        finally:
            body.close()
    else:
        while True:
            if (
                time.time()
                >= read(root / "resource-approval.json")["science_deadline_unix"]
            ):
                raise TimeoutError("No frozen input handoff before validation deadline")
            try:
                body = store.s3.get_object(
                    Bucket=bucket, Key=key, ExpectedBucketOwner=store.owner
                )["Body"]
            except ClientError as error:
                if error.response["Error"]["Code"] != "NoSuchKey":
                    raise
                time.sleep(20)
                continue
            try:
                value = json.loads(body.read())
            finally:
                body.close()
            break
        if value["experiment_id"] != state["experiment_id"] or value[
            "amendment_sha256"
        ] != file_sha256(root / "selection-amendment.json"):
            raise RuntimeError("Wrong selection handoff")
        save(
            root / "selection-input-verification.json",
            verify_archive(store, value["receipt"], bundle),
        )
        if (
            file_sha256(bundle / "matched-nominations.json")
            != value["selection_sha256"]
        ):
            raise RuntimeError("Frozen nominees changed during handoff")
    state["selection"] = value


def run_workflows(
    root: Path, role: str, state: dict, store: Any, tracked: Any, wandb: Any
) -> None:
    from scripts import run_replication_validation_shard as prior

    valid = root / "validation"
    prior.ROOT = valid
    prior.CHILDREN = {}
    approval, protocol = (
        read(root / "resource-approval.json"),
        read(valid / "protocol.json"),
    )
    env = child_environment(root)
    children = prior.CHILDREN
    state["jobs"] = {}

    def persist() -> None:
        state["updated_at_unix"] = time.time()
        save(root / "handoff-progress.json", state)

    def launch(label: str) -> None:
        folder = valid / "runs" / label
        if (folder / "process.json").exists() or (folder / "controller.log").exists():
            raise RuntimeError("Duplicate workflow refused: " + label)
        if (
            file_sha256(folder / "manifest.json")
            != read(valid / "prepared.json")["manifest_sha256"][label]
        ):
            raise RuntimeError("Frozen manifest changed")
        log = (folder / "controller.log").open("xb")
        p = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(root / "application/scripts/track_vecoli_experiment.py"),
                "--manifest",
                str(folder / "manifest.json"),
                "--catalog",
                str(folder / "experiments.json"),
            ],
            cwd=valid,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        children[label] = (p, log)
        state["jobs"][label] = {
            "pid": p.pid,
            "start_ticks": Path(f"/proc/{p.pid}/stat").read_text().split()[21],
            "status": "running",
        }
        persist()

    def wait(labels: list[str]) -> None:
        pending = set(labels)
        while pending:
            if time.time() >= approval["science_deadline_unix"]:
                raise TimeoutError("Original validation science deadline reached")
            for label in list(pending):
                p, log = children[label]
                if p.poll() is None:
                    continue
                log.close()
                entry = read(valid / "runs" / label / "experiments.json")[
                    "experiments"
                ][0]
                status = entry["status"]
                if status in {"prepared", "running"}:
                    status = "failed_before_catalog_update"
                state["jobs"][label].update(
                    status=status, exit_code=p.returncode, catalog=entry
                )
                persist()
                pending.remove(label)
                if entry.get("failure_phase") == "backup":
                    raise RuntimeError("Workflow backup failed; preserve worker disk")
            if pending:
                time.sleep(15)

    try:
        state["status"] = "calibrating"
        label = protocol["calibration_label"]
        launch(label)
        wait([label])
        result = read(valid / "runs" / label / "results.json")
        if (
            state["jobs"][label]["status"] != "completed"
            or not result["all_reached_max_generations"]
        ):
            raise RuntimeError("Calibration did not complete two verified divisions")
        rss = []
        for trace in (valid / "runs" / label).glob("trace--*.csv"):
            with trace.open() as body:
                rss.extend(
                    int(row["peak_rss"])
                    for row in csv.DictReader(body)
                    if row.get("peak_rss", "").isdigit()
                )
        if not rss or max(rss) > 4 * 1024**3:
            raise RuntimeError("Calibration RSS missing or exceeds 4 GiB")
        state.update(
            status="running", calibration={"divisions": 2, "peak_rss_bytes": max(rss)}
        )
        labels = [f"seed-{seed}" for seed in protocol["seeds"]]
        for label in labels:
            launch(label)
        wait(labels)
        state["status"] = (
            "completed"
            if all(x["status"] == "completed" for x in state["jobs"].values())
            else "completed_with_exceptions"
        )
    except BaseException as error:
        state.update(
            status="failed_preserving_disk",
            error={"type": type(error).__name__, "message": str(error)},
        )
        persist()
        raise
    finally:
        prior.stop_children()
        for _, log in children.values():
            if not log.closed:
                log.close()
        persist()
        paths = {str(p.relative_to(valid)): p for p in prior.archive_paths(valid)}
        paths["operation/handoff-progress.json"] = root / "handoff-progress.json"
        receipt = publish(paths, store, tracked, wandb, "final", store.secrets)
        state["final_verification"] = verify_archive(store, receipt)
        save(root / "final-verification.json", state["final_verification"])
        persist()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--search-root", type=Path, required=True)
    parser.add_argument("--role", choices=["primary", "secondary"], required=True)
    parser.add_argument("--resume-prepared", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("Remote Linux only")
    root, role = args.root.resolve(), args.role
    approval, plan = read(root / "resource-approval.json"), read(root / "plan.json")
    worker = validate_bounds(approval, plan, role, worker_identity())
    lock = (root / "handoff.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (root / "handoff-progress.json").exists():
        raise RuntimeError("Handoff already started; never duplicate")
    for name, sha in read(root / "deployment-manifest.json")["files"].items():
        if file_sha256(root / name) != sha:
            raise RuntimeError("Deployed source/configuration changed: " + name)
    if (
        read(root / "native-ready.json")["status"]
        != "native_runtime_ready_no_science_launched"
    ):
        raise RuntimeError("Native runtime not ready")
    previous = prepared_resume(root, role) if args.resume_prepared else None
    os.environ.update(read(root / "credentials/tracking.json"))
    os.environ.update(child_environment(root))
    import boto3
    import wandb

    state = {
        "experiment_id": approval["experiment_id"],
        "role": role,
        "instance_id": worker["instance_id"],
        "status": "waiting_for_search",
        "started_at_unix": time.time(),
        "supervisor": {
            "pid": os.getpid(),
            "start_ticks": Path(f"/proc/{os.getpid()}/stat").read_text().split()[21],
        },
    }
    if previous:
        state["prepared_recovery"] = {
            "previous_state_sha256": file_sha256(
                root / "attempts/preflight-policy-link/handoff-progress.json"
            ),
            "science_reexecuted": False,
            "policy_content_unchanged": True,
        }
    save(root / "handoff-progress.json", state)

    def interrupted(signum: int, frame: Any) -> None:
        raise InterruptedError("Handoff supervisor signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    try:
        wait_search(root, args.search_root, role, state)
        state["status"] = "preparing_validation"
        save(root / "handoff-progress.json", state)
        run_id = (
            approval["experiment_id"]
            + "-"
            + role
            + ("-preflight-recovery1" if previous else "")
        )
        with wandb.init(
            entity="mcclain",
            project="yggdrisil",
            id=run_id,
            name=run_id,
            job_type="validation",
            tags=["yggdrisil", "baseline", "depth-matched", role],
            config={"approval": approval, "plan": plan},
            dir=str(root),
            settings=wandb.Settings(
                disable_git=True, disable_code=True, save_code=False
            ),
        ) as tracked:
            state["wandb_url"] = tracked.url
            store = BaselineArchive(
                s3=boto3.client("s3", region_name="us-east-1"),
                wandb=wandb,
                tracked=tracked,
                owner=boto3.client("sts").get_caller_identity()["Account"],
                bucket="phd-research-storage-1758274488",
                prefix="yggdrisil/experiments/" + approval["experiment_id"] + "/",
                run_id=role,
                secrets=configured_secrets(root / "application", root / "credentials"),
            )
            if previous:
                state["selection"] = previous["selection"]
                save(
                    root / "prepared-input-reverification.json",
                    verify_archive(store, previous["selection"]["receipt"]),
                )
                from yggdrisil_ecoli.vecoli_results import _validate_manifest_provenance

                for label in read(root / "validation/prepared.json")["manifest_sha256"]:
                    _validate_manifest_provenance(
                        read(root / "validation/runs" / label / "manifest.json")
                    )
            else:
                frozen_selection(
                    root, args.search_root, role, store, tracked, wandb, state
                )
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "scripts.prepare_replication_validation",
                        "--root",
                        str(root),
                        "--depth-matched",
                        "--worker-role",
                        role,
                    ],
                    cwd=root / "application",
                    env=child_environment(root),
                    check=True,
                )
            valid = root / "validation"
            from scripts.run_replication_validation_shard import archive_paths

            paths = {str(p.relative_to(valid)): p for p in archive_paths(valid)}
            if previous:
                for name in [
                    "handoff-progress.json",
                    "handoff.log",
                    "deployment-manifest.json",
                    "policy-copy-proof.json",
                ]:
                    paths["operation/preflight-policy-link/" + name] = (
                        root / "attempts/preflight-policy-link" / name
                    )
            receipt = publish(paths, store, tracked, wandb, "preflight", store.secrets)
            state["preflight_verification"] = verify_archive(store, receipt)
            save(root / "backup-preflight.json", state["preflight_verification"])
            run_workflows(root, role, state, store, tracked, wandb)
    except BaseException as error:
        state.update(
            status="failed_preserving_disk",
            error={"type": type(error).__name__, "message": str(error)},
        )
        save(root / "handoff-progress.json", state)
        raise
    finally:
        # Stop only this dedicated machine; retained disks are never deleted here.
        if "search_closed" in state:
            subprocess.run(["sudo", "shutdown", "-h", "+2"], check=True)


if __name__ == "__main__":
    main()
