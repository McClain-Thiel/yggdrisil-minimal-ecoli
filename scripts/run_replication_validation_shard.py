"""Run one calibration and three independent, bounded validation workflows."""

import csv
import fcntl
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/ubuntu/yggdrisil-replication-20261109/validation")
sys.path[:0] = [
    str(ROOT / "application"),
    str(ROOT / "application/src"),
    str(ROOT / "framework/src"),
]
from yggdrisil_ecoli.baseline_storage import (  # noqa: E402 — the remote checkout is added above.
    BaselineArchive,
    configured_secrets,
    screen,
    verify_wandb_files,
)
from yggdrisil_ecoli.data.io import atomic_json, file_sha256  # noqa: E402 — the remote checkout is added above.

CHILDREN = {}
SEED = 0


def read(path):
    return json.loads(path.read_bytes())


def now():
    return datetime.now(timezone.utc).isoformat()


def walk_error(error):
    raise error


def archive_paths(root):
    excluded = {
        "wandb",
        "wandb-cache",
        "wandb-data",
        "backup-cache",
        "backup-verification",
        "__pycache__",
        ".nextflow",
        "runscripts",
        "ecoli",
        "credentials",
        "source",
    }
    allowed = {
        ".json",
        ".jsonl",
        ".log",
        ".pq",
        ".parquet",
        ".cPickle",
        ".gz",
        ".sqlite",
        ".glp",
        ".csv",
    }
    names = {".command.out", ".command.err", ".command.trace", ".exitcode"}
    result = [
        p
        for p in root.iterdir()
        if p.is_file()
        and p.suffix in {".json", ".log"}
        and p.name not in {"parallel-worker.json", "batch-progress.json"}
    ]
    for label in ["runs", "inputs", "knockout-audit", "attempts"]:
        if not (root / label).exists():
            continue
        for directory, dirs, files in os.walk(root / label, onerror=walk_error):
            dirs[:] = [
                d
                for d in dirs
                if d not in excluded and not (Path(directory) / d).is_symlink()
            ]
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink() and (path.suffix in allowed or name in names):
                    result.append(path)
    return sorted(set(result))


def final_backup(
    state, archive, secrets, tracked, wandb, receipt_name="final-backup.json"
):
    if any(p.poll() is None for p, log in CHILDREN.values()):
        raise RuntimeError(
            "Cannot make final archive while scientific controllers are live"
        )
    with tempfile.TemporaryDirectory(prefix="final-", dir=ROOT) as scratch:
        temp = Path(scratch)
        tarpath = temp / "data.tar.gz"
        inventory = []
        with tarfile.open(tarpath, "w:gz") as tar:
            for path in archive_paths(ROOT):
                screen(path, secrets)
                data = path.read_bytes()
                name = str(path.relative_to(ROOT))
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(data))
                inventory.append(
                    {
                        "path": name,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
        ref = archive.upload(tarpath, "final")
        index = temp / "index.json"
        atomic_json(
            index,
            {
                "experiment_id": state["experiment_id"],
                "archive": ref,
                "files": inventory,
                "status": state["status"],
                "exclusions": "Source, credentials, package/W&B caches, Nextflow compiled script cache and live-cell files are excluded.",
            },
        )
        screen(index, secrets)
        index_ref = archive.upload(index, "final")
        artifact = wandb.Artifact(
            state["experiment_id"] + "-final-data", type="experiment-data"
        )
        artifact.add_file(str(tarpath), name="data.tar.gz")
        artifact.add_file(str(index), name="index.json")
        saved = tracked.log_artifact(artifact)
        saved.wait()
        remote = wandb.Api(timeout=120).artifact(saved.qualified_name)
        verify_wandb_files(
            remote, {"data.tar.gz": ref, "index.json": index_ref}, temp / "verify"
        )
        result = {
            "status": "verified",
            "verified_at": now(),
            "archive": ref,
            "index": index_ref,
            "members": len(inventory),
            "wandb_artifact": saved.qualified_name,
            "full_s3_get_verified": True,
            "wandb_download_verified": True,
        }
        atomic_json(ROOT / receipt_name, result)
        return result


def stop_children():
    for process, log in CHILDREN.values():
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
    for label, (process, log) in CHILDREN.items():
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            info = ROOT / "runs" / label / "process.json"
            if info.exists():
                pid = read(info)["workflow_pid"]
                stat = Path(f"/proc/{pid}/cmdline")
                if stat.exists():
                    command = stat.read_bytes().replace(b"\x00", b" ")
                    if (
                        b"workflow.py" not in command
                        or str(ROOT).encode() not in command
                    ):
                        raise RuntimeError(
                            "Workflow PID identity changed; refusing unsafe signal"
                        )
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        print("Workflow exited during cleanup:", pid, flush=True)
            process.kill()
            process.wait()


def main():
    global SEED
    worker = read(ROOT / "parallel-worker.json")
    plan = read(ROOT / "parallel-plan.json")
    if worker not in plan["workers"] or worker["seed"] not in [101, 211, 307]:
        raise RuntimeError("Unapproved seed assignment")
    SEED = worker["seed"]
    calibration_label = f"calibration-worker-{SEED}"
    from urllib.request import Request, urlopen

    request = Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    with urlopen(request, timeout=5) as response:
        token = response.read().decode()
    request = Request(
        "http://169.254.169.254/latest/meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with urlopen(request, timeout=5) as response:
        if response.read().decode() != worker["instance_id"]:
            raise RuntimeError("Worker instance identity changed")
    if sys.platform != "linux":
        raise RuntimeError("Remote Linux only")
    lock = (ROOT / f"worker-{SEED}.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (ROOT / f"worker-{SEED}-progress.json").exists():
        raise RuntimeError("Study already started; never duplicate")
    spec, approval = read(ROOT / "protocol.json"), read(ROOT / "approval.json")
    assert approval["paid_launch"] and approval["total_cap_usd"] == 550
    approval["science_deadline_unix"] = min(
        approval["science_deadline_unix"], plan["science_deadline_unix"]
    )
    approval["stop_deadline_unix"] = min(
        approval["stop_deadline_unix"], plan["stop_deadline_unix"]
    )
    assert file_sha256(ROOT / "protocol.json") == approval["protocol_sha256"]
    assert read(ROOT / "preflight.json")["status"] == "passed"
    for name, digest in read(ROOT / "deployment-manifest.json")["files"].items():
        if file_sha256(ROOT / name) != digest:
            raise RuntimeError("Deployed source changed: " + name)
    os.environ.update(read(ROOT / "credentials/tracking.json"))
    os.environ.update(
        PYTHONPATH=":".join(
            [
                str(ROOT / "application/src"),
                str(ROOT / "application"),
                str(ROOT / "framework/src"),
            ]
        ),
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        PYDANTIC_DISABLE_PLUGINS="__all__",
    )
    import boto3
    import wandb

    secrets = configured_secrets(ROOT / "application", ROOT / "credentials")
    state = {
        "experiment_id": spec["experiment_id"]
        + f"-worker-{SEED}"
        + plan.get("attempt_suffix", ""),
        "status": "calibrating",
        "started_at": now(),
        "jobs": {},
        "approval": approval,
        "maximum_parallel_cells": 8,
        "seed": SEED,
        "instance_id": worker["instance_id"],
    }

    def save():
        state["updated_at"] = now()
        atomic_json(ROOT / f"worker-{SEED}-progress.json", state)

    def launch(label):
        folder = ROOT / "runs" / label
        resume = (
            bool(plan.get("resume_saved_calibration"))
            and (folder / "process.json").exists()
        )
        if resume:
            preflight = read(ROOT / "resume-backup-preflight.json")
            if label != calibration_label:
                preflight = preflight["additional_workflows"][label]
            preservation = read(ROOT / "resume-preservation.json")
            if preflight["status"] != "passed" or preflight[
                "controller_sha256"
            ] != file_sha256(ROOT / "application/scripts/track_vecoli_experiment.py"):
                raise RuntimeError(
                    "Saved checkpoint was not verified with this controller"
                )
            for name, digest in preservation["completed_scientific_files"].items():
                if file_sha256(ROOT / name) != digest:
                    raise RuntimeError("Completed scientific work changed: " + name)
            for entry in [read(folder / "process.json")]:
                for key in ["controller_pid", "workflow_pid"]:
                    proc = Path("/proc") / str(entry[key])
                    if (
                        proc.exists()
                        and str(ROOT).encode() in (proc / "cmdline").read_bytes()
                    ):
                        raise RuntimeError("Previous scientific process still exists")
        elif (folder / "process.json").exists() or (folder / "controller.log").exists():
            raise RuntimeError("Duplicate dispatch refused: " + label)
        expected = read(ROOT / "prepared.json")["manifest_sha256"][label]
        assert file_sha256(folder / "manifest.json") == expected
        log = (folder / "controller.log").open("ab" if resume else "xb")
        process = subprocess.Popen(
            [
                str(ROOT / "env/bin/python"),
                "-u",
                str(ROOT / "application/scripts/track_vecoli_experiment.py"),
                "--manifest",
                str(folder / "manifest.json"),
                "--catalog",
                str(folder / "experiments.json"),
                *(["--resume"] if resume else []),
            ],
            cwd=ROOT,
            env=os.environ,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        CHILDREN[label] = (process, log)
        state["jobs"][label] = {
            "pid": process.pid,
            "start_ticks": Path(f"/proc/{process.pid}/stat").read_text().split()[21],
            "status": "running",
            "started_at": now(),
        }
        save()

    def wait_for(labels):
        pending = set(labels)
        while pending:
            if time.time() >= approval["science_deadline_unix"]:
                raise TimeoutError(
                    "Original science deadline reached; preserve partial results"
                )
            for label in list(pending):
                process, log = CHILDREN[label]
                if process.poll() is not None:
                    log.close()
                    entry = read(ROOT / "runs" / label / "experiments.json")[
                        "experiments"
                    ][0]
                    status = entry["status"]
                    if status in {"prepared", "running"}:
                        status = "failed_before_catalog_update"
                    state["jobs"][label].update(
                        status=status,
                        exit_code=process.returncode,
                        finished_at=now(),
                        data_backup=entry.get("data_backup"),
                        error=entry.get("error"),
                        failure_phase=entry.get("failure_phase"),
                    )
                    pending.remove(label)
                    save()
                    if entry.get("failure_phase") == "backup":
                        raise RuntimeError(
                            "Checkpoint failure; stop science and preserve worker disk"
                        )
            if pending:
                time.sleep(15)

    def interrupted(signum, frame):
        raise InterruptedError("Supervisor received signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    with wandb.init(
        entity="mcclain",
        project="yggdrisil",
        id=f"search-replication-20261109-validation-worker-{SEED}"
        + plan.get("attempt_suffix", ""),
        name=spec["experiment_id"] + f"-worker-{SEED}" + plan.get("attempt_suffix", ""),
        job_type="validation",
        tags=["yggdrisil", "baseline", "validation", "lineage20"],
        config={
            "approval": approval,
            "genotypes": len(read(ROOT / "prepared.json")["genotypes"]),
            "lineages": len(read(ROOT / "prepared.json")["genotypes"]),
            "maximum_cells": len(read(ROOT / "prepared.json")["genotypes"]) * 20,
        },
        dir=str(ROOT),
        settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False),
    ) as tracked:
        state["wandb_url"] = tracked.url
        save()
        archive = BaselineArchive(
            s3=boto3.client("s3", region_name="us-east-1"),
            wandb=wandb,
            tracked=tracked,
            owner=boto3.client("sts").get_caller_identity()["Account"],
            bucket=spec["tracking"]["s3_bucket"],
            prefix=spec["tracking"]["s3_prefix"],
            run_id=f"worker-{SEED}" + plan.get("attempt_suffix", ""),
            secrets=secrets,
        )
        try:
            launch(calibration_label)
            wait_for([calibration_label])
            for name, digest in read(ROOT / "resume-preservation.json")[
                "completed_scientific_files"
            ].items():
                if file_sha256(ROOT / name) != digest:
                    raise RuntimeError(
                        "Resume altered completed scientific work: " + name
                    )
            result = read(ROOT / "runs" / calibration_label / "results.json")
            if (
                state["jobs"][calibration_label]["status"] != "completed"
                or not result["all_reached_max_generations"]
            ):
                raise RuntimeError(
                    "Calibration did not complete two verified divisions"
                )
            rss = []
            for trace in (ROOT / "runs" / calibration_label).glob("trace--*.csv"):
                with trace.open() as stream:
                    rss += [
                        int(row["peak_rss"])
                        for row in csv.DictReader(stream)
                        if row.get("peak_rss", "").isdigit()
                    ]
            if not rss or max(rss) > 4 * 1024**3:
                raise RuntimeError("Calibration peak RSS missing or exceeds 4GiB")
            state["calibration"] = {
                "status": "passed",
                "divisions": 2,
                "peak_rss_bytes": max(rss),
            }
            state["status"] = "running"
            save()
            labels = [f"seed-{SEED}"]
            for label in labels:
                launch(label)
            wait_for(labels)
            state["status"] = (
                "completed"
                if all(j["status"] == "completed" for j in state["jobs"].values())
                else "completed_with_exceptions"
            )
        except BaseException as error:
            state.update(
                status="failed",
                error={"type": type(error).__name__, "message": str(error)},
            )
            print(repr(error), flush=True)
            save()
        finally:
            stop_children()
            for process, log in CHILDREN.values():
                if not log.closed:
                    log.close()
            save()
            try:
                state["final_backup"] = final_backup(
                    state,
                    archive,
                    secrets,
                    tracked,
                    wandb,
                    receipt_name=f"worker-{SEED}-final-backup.json",
                )
                save()
            except BaseException as error:
                state["backup_failure"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                save()
                raise
            finally:
                print("Validation ended; parent supervisor owns shutdown", flush=True)


if __name__ == "__main__":
    main()
