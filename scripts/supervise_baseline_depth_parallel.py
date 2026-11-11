#!/usr/bin/env python3
"""Adopt or dispatch disjoint baseline jobs, then merge verified remote results."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.run_search_replication import save, validate_launch, worker_identity
from scripts.supervise_search_replication import publish, search_files
from yggdrisil_ecoli.baseline_storage import verify_wandb_files


def digest(path: Path) -> str:
    with path.open("rb") as body:
        return hashlib.file_digest(body, "sha256").hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def validate_assignment(value: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    workers = value["workers"]
    groups = [w["job_indices"] for w in workers.values()]
    indices = [i for group in groups for i in group]
    if (
        set(workers) != {"primary", "secondary"}
        or len(indices) != len(jobs)
        or set(indices) != set(range(len(jobs)))
        or any(type(i) is not int for i in indices)
        or len({w["instance_id"] for w in workers.values()}) != 2
        or value["combined_cap_usd"] != 25
        or value["maximum_jobs_per_worker"] != 4
        or value["maximum_total_jobs"] != 8
        or value["model_calls_allowed"]
        or value["validation_launch_authorized"]
    ):
        raise ValueError("Invalid disjoint assignment or authorization")
    if value["stop_deadline_unix"] - value["authorized_start_unix"] > 12 * 3600:
        raise ValueError("Combined two-worker cost window exceeds original cap")
    if value["stop_deadline_unix"] - value["science_deadline_unix"] < 7200:
        raise ValueError("Missing backup reserve")


def identity(pid: int, start_ticks: str, job_index: int | None = None) -> bool:
    root = Path(f"/proc/{pid}")
    try:
        fields = (root / "stat").read_text().split()
        args = (root / "cmdline").read_bytes().split(b"\0")
    except FileNotFoundError:
        return False
    if fields[21] != str(start_ticks):
        raise RuntimeError("PID was reused; refusing to adopt or signal it")
    if fields[2] == "Z":
        return False
    if job_index is not None:
        expected = [
            b"scripts.run_search_replication",
            b"--job-index",
            str(job_index).encode(),
        ]
        if (
            expected[0] not in args
            or expected[1] not in args
            or args[args.index(expected[1]) + 1] != expected[2]
        ):
            raise RuntimeError("Recorded PID is not the assigned search")
    return True


def closed_result(folder: Path, run_id: str) -> dict[str, Any]:
    done, report = read(folder / "completion.json"), read(folder / "run-report.json")
    if (
        done["status"] != "terminal_verified"
        or report["run_id"] != run_id
        or digest(folder / "run-report.json") != done["report_sha256"]
    ):
        raise RuntimeError("Exited search lacks its verified terminal result")
    return done


def unpack_verified(archive: Path, index: dict[str, Any], destination: Path) -> None:
    from scripts.parallel_replication_validation import inspect_members

    # Check every member and destination before writing any restored data.
    inspect_members(archive, index)
    for entry in index["files"]:
        target = destination / entry["path"]
        if (
            not target.resolve().is_relative_to(destination.resolve())
            or target.is_symlink()
        ):
            raise ValueError("Unsafe archive destination")
        if target.exists():
            raise FileExistsError(f"Refusing to replace existing data: {target}")
    inspect_members(archive, index, destination)


def verified_download(store: Any, ref: dict[str, Any], target: Path) -> None:
    uri = urlparse(ref["uri"])
    if uri.scheme != "s3" or uri.netloc != store.bucket:
        raise ValueError("Result is outside approved private bucket")
    body = store.s3.get_object(
        Bucket=store.bucket, Key=uri.path.lstrip("/"), ExpectedBucketOwner=store.owner
    )["Body"]
    try:
        with target.open("xb") as out:
            while chunk := body.read(1024 * 1024):
                out.write(chunk)
    finally:
        body.close()
    if digest(target) != ref["sha256"] or target.stat().st_size != ref["bytes"]:
        raise RuntimeError("Remote result checksum mismatch")


def merge_secondary(
    root: Path,
    assignment: dict[str, Any],
    jobs: list[dict[str, Any]],
    receipt: dict[str, Any],
    store: Any,
    wandb: Any,
) -> dict[str, Any]:
    expected_ids = [
        jobs[i]["run_id"] for i in assignment["workers"]["secondary"]["job_indices"]
    ]
    if (
        receipt.get("status") != "worker_completed_verified"
        or receipt.get("run_ids") != expected_ids
        or receipt.get("assignment_sha256") != digest(root / "parallel-assignment.json")
    ):
        raise RuntimeError("Secondary receipt failed identity/status checks")
    ref = receipt["final_backup"]
    with tempfile.TemporaryDirectory(prefix="secondary-import-", dir=root) as t:
        temp = Path(t)
        for kind in ["archive", "index"]:
            verified_download(store, ref[kind], temp / kind)
        index = read(temp / "index")
        if index["archive"] != ref["archive"]:
            raise RuntimeError("Secondary index/archive mismatch")
        remote = wandb.Api(timeout=120).artifact(ref["wandb_artifact"])
        verify_wandb_files(
            remote,
            {"data.tar.gz": ref["archive"], "index.json": ref["index"]},
            temp / "wandb",
        )
        unpack_verified(temp / "archive", index, temp / "members")
        for i in assignment["workers"]["secondary"]["job_indices"]:
            job = jobs[i]
            source, target = (
                temp / "members" / job["directory"],
                root / "bundle" / job["directory"],
            )
            if target.exists():
                raise RuntimeError("Refusing to overwrite an existing local job")
            closed_result(source, job["run_id"])
        # Verify everything before exposing any imported job to the collector.
        for i in assignment["workers"]["secondary"]["job_indices"]:
            job = jobs[i]
            target = root / "bundle" / job["directory"]
            target.parent.mkdir(parents=True, exist_ok=True)
            (temp / "members" / job["directory"]).rename(target)
    return {
        "status": "verified",
        "receipt": receipt,
        "members": len(index["files"]),
        "full_s3_get_verified": True,
        "every_indexed_member_verified": True,
        "wandb_uncached_download_verified": True,
        "imported_run_ids": expected_ids,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--role", required=True, choices=["primary", "secondary"])
    args = parser.parse_args()
    root, role = args.root.resolve(), args.role
    bundle, repo = root / "bundle", root / "application"
    approval = read(bundle / "approval.json")
    spec, jobs = validate_launch(bundle, repo, approval)
    assignment = read(root / "parallel-assignment.json")
    validate_assignment(assignment, jobs)
    assignment_hash = digest(root / "parallel-assignment.json")
    if (
        assignment_hash != approval["parallel_assignment_sha256"]
        or digest(Path(__file__)) != approval["parallel_supervisor_sha256"]
        or worker_identity() != assignment["workers"][role]["instance_id"]
        or approval["stop_deadline_unix"] != assignment["stop_deadline_unix"]
        or approval["science_deadline_unix"] != assignment["science_deadline_unix"]
    ):
        raise ValueError("Unbound parallel controller or worker assignment")
    if read(root / "preflight.json")["status"] != "passed":
        raise RuntimeError("Backup preflight is required")
    lock = (root / "batch.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (root / "parallel-launcher.json").exists():
        raise RuntimeError("Parallel controller already launched")
    os.environ.update(read(root / "credentials/tracking.json"))
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ.update(
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        PYDANTIC_DISABLE_PLUGINS="__all__",
    )
    import boto3
    import wandb
    from botocore.exceptions import ClientError

    from scripts.collect_search_replication import collect
    from yggdrisil_ecoli.baseline_storage import (
        BaselineArchive,
        configured_secrets,
        screen,
    )

    assigned = assignment["workers"][role]["job_indices"]
    state = {
        "experiment_id": spec["experiment_id"],
        "status": "search_running",
        "role": role,
        "jobs": {},
        "approval": approval,
        "assignment_sha256": assignment_hash,
        "supervisor": {
            "pid": os.getpid(),
            "start_ticks": Path(f"/proc/{os.getpid()}/stat").read_text().split()[21],
        },
        "started_at_unix": time.time(),
        "assigned_indices": assigned,
    }
    recovery = (
        read(root / "logging-recovery.json")
        if approval.get("logging_recovery_sha256")
        else None
    )
    if recovery:
        if role != "primary" or recovery["status"] != "verified":
            raise ValueError(
                "Only the recorded primary logging interruption may resume"
            )
        if (
            digest(root / "logging-recovery.json")
            != approval["logging_recovery_sha256"]
        ):
            raise ValueError("Changed logging recovery record")
        state["logging_recovery_sha256"] = approval["logging_recovery_sha256"]
    elif role == "primary":
        adoption = read(root / "parallel-adoption.json")
        if (
            digest(root / "parallel-adoption.json")
            != approval["parallel_adoption_sha256"]
        ):
            raise ValueError("Unbound live-process adoption")
        old = adoption["previous_batch"]
        if set(map(int, old["jobs"])) - set(assigned):
            raise ValueError("Cannot move a dispatched job to another worker")
        state["jobs"] = old["jobs"]
        state["previous_study_initial_backup"] = old["initial_backup"]
        for label, item in state["jobs"].items():
            if item["status"] == "running":
                identity(item["pid"], item["start_ticks"], int(label))
    elif (root / "batch-progress.json").exists():
        raise RuntimeError("Secondary worker already has a batch")
    owned: dict[str, tuple[Any, Any]] = {}

    def persist() -> None:
        state["updated_at_unix"] = time.time()
        save(root / "batch-progress.json", state)

    def stop_children() -> None:
        for label, item in state["jobs"].items():
            if item["status"] == "running" and identity(
                item["pid"], item["start_ticks"], int(label)
            ):
                os.killpg(item["pid"], signal.SIGINT)
        until = time.monotonic() + 120
        while time.monotonic() < until and any(
            item["status"] == "running"
            and identity(item["pid"], item["start_ticks"], int(label))
            for label, item in state["jobs"].items()
        ):
            time.sleep(1)
        for label, item in state["jobs"].items():
            if item["status"] == "running" and identity(
                item["pid"], item["start_ticks"], int(label)
            ):
                print(
                    "Child exceeded shutdown grace; preserving partial files",
                    item["pid"],
                    flush=True,
                )
                os.killpg(item["pid"], signal.SIGKILL)
        for process, log in owned.values():
            process.wait()
            log.close()

    def interrupt(signum: int, frame: Any) -> None:
        raise InterruptedError(f"Parallel supervisor received signal {signum}")

    signal.signal(signal.SIGTERM, interrupt)
    save(
        root / "parallel-launcher.json",
        state["supervisor"] | {"role": role, "assignment_sha256": assignment_hash},
    )
    persist()
    location = urlparse(spec["tracking"]["s3_prefix"])
    s3 = boto3.client("s3", region_name="us-east-1")
    owner = boto3.client("sts").get_caller_identity()["Account"]
    secrets = configured_secrets(repo, root / "credentials")
    coordination_key = location.path.lstrip("/") + "parallel/secondary-receipt.json"
    with wandb.init(
        entity="mcclain",
        project="yggdrisil",
        id=spec["experiment_id"]
        + "-parallel-"
        + role
        + ("-logging-resume1" if recovery else ""),
        name=spec["experiment_id"]
        + "-parallel-"
        + role
        + ("-logging-resume1" if recovery else ""),
        job_type="baseline",
        tags=spec["tracking"]["tags"],
        config={"approval": approval, "assignment_sha256": assignment_hash},
        dir=str(root),
        settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False),
    ) as tracked:
        state["wandb_url"] = tracked.url
        store = BaselineArchive(
            s3=s3,
            wandb=wandb,
            tracked=tracked,
            owner=owner,
            bucket=location.netloc,
            prefix=location.path.lstrip("/"),
            run_id=spec["experiment_id"]
            + "-parallel-"
            + role
            + ("-logging-resume1" if recovery else ""),
            secrets=secrets,
        )

        def files(indices: list[int]) -> dict[str, Path]:
            result = search_files(bundle, [jobs[i] for i in indices])
            for name in [
                "parallel-assignment.json",
                "logging-recovery.json",
                "parallel-adoption.json",
                "parallel-launcher.json",
                "preflight.json",
                "preflight-packaging-correction.json",
                "preflight-runtime-alignment.json",
                "worker.json",
            ]:
                if (root / name).exists():
                    result["operation/" + name] = root / name
            return result

        def receipt(status: str, backup: dict[str, Any]) -> None:
            value = {
                "status": status,
                "experiment_id": spec["experiment_id"],
                "assignment_sha256": assignment_hash,
                "run_ids": [jobs[i]["run_id"] for i in assigned],
                "final_backup": backup,
                "published_at_unix": time.time(),
            }
            path = root / "parallel-final-receipt.json"
            save(path, value)
            screen(path, secrets)
            raw = path.read_bytes()
            sha = hashlib.sha256(raw).digest()
            value["immutable_record"] = store.upload(path, "receipts")
            save(path, value)
            raw = path.read_bytes()
            sha = hashlib.sha256(raw).digest()
            s3.put_object(
                Bucket=store.bucket,
                Key=coordination_key,
                Body=raw,
                ExpectedBucketOwner=owner,
                ServerSideEncryption="AES256",
                ChecksumSHA256=base64.b64encode(sha).decode(),
            )
            response = s3.get_object(
                Bucket=store.bucket, Key=coordination_key, ExpectedBucketOwner=owner
            )["Body"]
            try:
                if response.read() != raw:
                    raise RuntimeError("Coordination receipt verification failed")
            finally:
                response.close()

        try:
            state["initial_backup"] = publish(
                files([]), store, tracked, wandb, "initial", secrets
            )
            persist()
            pending = [i for i in assigned if str(i) not in state["jobs"]]
            last_backup = time.monotonic()
            while pending or any(
                j["status"] == "running" for j in state["jobs"].values()
            ):
                if time.time() >= approval["science_deadline_unix"]:
                    raise TimeoutError("Parallel science deadline reached")
                for label, item in state["jobs"].items():
                    if item["status"] != "running":
                        continue
                    if label in owned:
                        owned[label][0].poll()
                    if identity(item["pid"], item["start_ticks"], int(label)):
                        continue
                    done = closed_result(
                        bundle / jobs[int(label)]["directory"], item["run_id"]
                    )
                    if label in owned and owned[label][0].returncode != 0:
                        raise RuntimeError("Nonzero search exit")
                    item.update(
                        status="terminal_verified",
                        completion=done,
                        finished_at_unix=time.time(),
                    )
                    persist()
                live = sum(
                    item["status"] == "running" for item in state["jobs"].values()
                )
                while pending and live < 4:
                    i = pending.pop(0)
                    job = jobs[i]
                    folder = bundle / job["directory"]
                    resume_record = recovery and recovery["jobs"].get(job["run_id"])
                    if folder.exists() and not resume_record:
                        raise RuntimeError(
                            "Refusing duplicate dispatch to existing job folder"
                        )
                    if resume_record:
                        from scripts.baseline_depth_logging_recovery import (
                            verify_resume,
                        )

                        verify_resume(folder, resume_record, job)
                        if (folder / "process.json").exists() or (
                            folder / "controller.log"
                        ).exists():
                            raise RuntimeError(
                                "Logging recovery was already dispatched"
                            )
                    folder.mkdir(parents=True, exist_ok=bool(resume_record))
                    log = (folder / "controller.log").open("xb")
                    p = subprocess.Popen(
                        [
                            sys.executable,
                            "-u",
                            "-m",
                            "scripts.run_search_replication",
                            "--bundle",
                            str(bundle),
                            "--repo-root",
                            str(repo),
                            "--job-index",
                            str(i),
                        ],
                        cwd=repo,
                        env={
                            k: v for k, v in os.environ.items() if k != "WANDB_SERVICE"
                        },
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    owned[str(i)] = (p, log)
                    state["jobs"][str(i)] = {
                        "run_id": job["run_id"],
                        "pid": p.pid,
                        "start_ticks": Path(f"/proc/{p.pid}/stat")
                        .read_text()
                        .split()[21],
                        "status": "running",
                        "started_at_unix": time.time(),
                    }
                    persist()
                    live += 1
                if time.monotonic() - last_backup >= 300:
                    state["progress_backup"] = publish(
                        {"batch-progress.json": root / "batch-progress.json"},
                        store,
                        tracked,
                        wandb,
                        "progress",
                        secrets,
                    )
                    persist()
                    last_backup = time.monotonic()
                time.sleep(15)
            state["status"] = "worker_completed_verified"
            state["worker_final_backup"] = publish(
                files(assigned), store, tracked, wandb, "worker-final", secrets
            )
            persist()
            if role == "secondary":
                receipt(state["status"], state["worker_final_backup"])
                return
            state["status"] = "waiting_for_secondary"
            persist()
            while True:
                if time.time() >= approval["science_deadline_unix"]:
                    raise TimeoutError(
                        "Secondary results unavailable before science deadline"
                    )
                try:
                    response = s3.get_object(
                        Bucket=store.bucket,
                        Key=coordination_key,
                        ExpectedBucketOwner=owner,
                    )["Body"]
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "NoSuchKey":
                        raise
                    time.sleep(15)
                    continue
                try:
                    remote_receipt = json.loads(response.read())
                finally:
                    response.close()
                break
            merge = merge_secondary(
                root, assignment, jobs, remote_receipt, store, wandb
            )
            save(root / "secondary-import-verification.json", merge)
            for i in assignment["workers"]["secondary"]["job_indices"]:
                job = jobs[i]
                state["jobs"][str(i)] = {
                    "run_id": job["run_id"],
                    "status": "terminal_verified",
                    "remote_worker": assignment["workers"]["secondary"]["instance_id"],
                    "completion": closed_result(
                        bundle / job["directory"], job["run_id"]
                    ),
                }
            save(bundle / "nominations.json", collect(bundle))
            nomination = publish(
                {"nominations.json": bundle / "nominations.json"},
                store,
                tracked,
                wandb,
                "nominations",
                secrets,
            )
            save(bundle / "nominations-verification.json", nomination)
            paths = files(list(range(len(jobs))))
            paths["operation/secondary-import-verification.json"] = (
                root / "secondary-import-verification.json"
            )
            state["search_final_backup"] = publish(
                paths, store, tracked, wandb, "search-final", secrets
            )
            state["status"] = "search_completed_pending_independent_review"
            state["validation_launch_authorized"] = False
            persist()
            state["final_catalog_backup"] = publish(
                {
                    "batch-progress.json": root / "batch-progress.json",
                    "nominations.json": bundle / "nominations.json",
                },
                store,
                tracked,
                wandb,
                "final-catalog",
                secrets,
            )
            persist()
        except BaseException as exc:
            state.update(
                status="failed_preserving_disk",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            persist()
            print(json.dumps(state["error"]), file=sys.stderr, flush=True)
            stop_children()
            state["failure_backup"] = publish(
                files(assigned), store, tracked, wandb, "failure", secrets
            )
            persist()
            if role == "secondary":
                receipt("failed_preserving_disk", state["failure_backup"])
            raise
        finally:
            stop_children()
            subprocess.run(["sudo", "shutdown", "-h", "+2"], check=True)


if __name__ == "__main__":
    main()
