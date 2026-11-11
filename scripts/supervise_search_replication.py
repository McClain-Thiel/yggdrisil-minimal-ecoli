#!/usr/bin/env python3
"""Bounded remote study supervisor; halt dispatch on unverified preservation."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.run_search_replication import (
    save,
    transient_transport_error,
    validate_launch,
)


def resume_jobs(
    root: Path, bundle: Path, jobs: list[dict[str, Any]], approval: dict[str, Any]
) -> dict[str, Any]:
    """Adopt closed, verified jobs after an explicitly recorded setup repair."""
    transport = bool(approval.get("transport_recovery_sha256"))
    repair_path = root / (
        "transport-recovery.json" if transport else "operational-repair.json"
    )
    repair = json.loads(repair_path.read_bytes())
    if hashlib.sha256(repair_path.read_bytes()).hexdigest() != approval.get(
        "transport_recovery_sha256" if transport else "operational_repair_sha256"
    ):
        raise ValueError("Unbound operational repair")
    if repair["status"] != "verified" or (bundle / "nominations.json").exists():
        raise ValueError("Repair cannot alter a nominated study")
    result = {}
    for label, item in repair["retained_jobs"].items():
        job = jobs[int(label)]
        folder = bundle / job["directory"]
        raw = (folder / "run-report.json").read_bytes()
        done = json.loads((folder / "completion.json").read_bytes())
        if (
            item["run_id"] != job["run_id"]
            or done != item["completion"]
            or done["status"] != "terminal_verified"
            or hashlib.sha256(raw).hexdigest() != done["report_sha256"]
        ):
            raise ValueError("Retained terminal result changed")
        result[label] = item
    for run_id, retry in repair.get("retries", {}).items():
        preserved = (root / retry["preserved_directory"]).resolve()
        if not preserved.is_relative_to(root.resolve()) or run_id not in {
            j["run_id"] for j in jobs
        }:
            raise ValueError("Invalid preserved attempt path or run ID")
        raw = (preserved / "run-report.json").read_bytes()
        report = json.loads(raw)
        receipt = retry["report_verification"]
        if (
            report["run_id"] != run_id
            or report["candidates"]
            or report["model_spend_usd"] != 0
            or report["validation_started"]
            or "No module named 'mcp'" not in report["error"]["message"]
            or hashlib.sha256(raw).hexdigest() != receipt["report_sha256"]
            or not receipt["full_s3_get_verified"]
            or not receipt["wandb_download_verified"]
        ):
            raise ValueError(
                "Scientific attempts cannot be replaced by a dependency retry"
            )
    for run_id, continuation in repair.get("resumes", {}).items():
        job = next(j for j in jobs if j["run_id"] == run_id)
        preserved = (root / continuation["preserved_directory"]).resolve()
        if not preserved.is_relative_to(root.resolve()):
            raise ValueError("Unsafe preserved transport attempt")
        raw = (preserved / "run-report.json").read_bytes()
        report = json.loads(raw)
        if (
            report["run_id"] != run_id
            or not transient_transport_error(report.get("error") or {})
            or report["validation_started"]
            or len(report["candidates"]) > 100
            or hashlib.sha256(raw).hexdigest() != continuation["report_sha256"]
        ):
            raise ValueError("Only recorded transport failures may resume")
        for name, expected in continuation["database_hashes"].items():
            path = (bundle / job["directory"] / name).resolve()
            if (
                not path.is_relative_to((bundle / job["directory"]).resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError("Transport resume must retain exact graph and session")
    return result


def publish(
    paths: dict[str, Path],
    archive: Any,
    tracked: Any,
    wandb: Any,
    category: str,
    secrets: list[bytes],
) -> dict[str, Any]:
    from yggdrisil_ecoli.baseline_storage import screen, sqlite_snapshot
    from yggdrisil_ecoli.data.io import file_sha256

    with tempfile.TemporaryDirectory(prefix="study-backup-") as scratch:
        temp = Path(scratch)
        entries = []
        for name, source in paths.items():
            if (
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or source.is_symlink()
            ):
                raise ValueError("Unsafe study backup member")
            target = temp / "data" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".sqlite":
                sqlite_snapshot(source, target)
            else:
                target.write_bytes(source.read_bytes())
            screen(target, secrets)
            entries.append(
                {
                    "path": name,
                    "sha256": file_sha256(target),
                    "bytes": target.stat().st_size,
                }
            )
        packed = temp / "data.tar.gz"
        with tarfile.open(packed, "w:gz") as tar:
            for entry in entries:
                tar.add(
                    temp / "data" / entry["path"],
                    arcname=entry["path"],
                    recursive=False,
                )
        ref = archive.upload(packed, category)
        index = temp / "index.json"
        save(index, {"archive": ref, "files": entries, "category": category})
        screen(index, secrets)
        index_ref = archive.upload(index, category)
        artifact = wandb.Artifact(
            archive.run_id + "-" + category, type="experiment-data"
        )
        artifact.add_file(str(packed), name="data.tar.gz")
        artifact.add_file(str(index), name="index.json")
        logged = tracked.log_artifact(artifact)
        logged.wait()
        remote = wandb.Api(timeout=120).artifact(logged.qualified_name)
        if remote.state != "COMMITTED":
            raise RuntimeError("Study artifact did not commit")
        for name, expected in (("data.tar.gz", ref), ("index.json", index_ref)):
            path = Path(remote.get_entry(name).download(root=str(temp / "verify")))
            if (
                file_sha256(path) != expected["sha256"]
                or path.stat().st_size != expected["bytes"]
            ):
                raise RuntimeError("Study artifact failed full download verification")
        # Independently inspect the remote tar stream, rather than trusting only
        # the archive digest or the manifest produced during packing.
        location = urlparse(ref["uri"])
        body = archive.s3.get_object(
            Bucket=location.netloc,
            Key=location.path.lstrip("/"),
            ExpectedBucketOwner=archive.owner,
        )["Body"]
        expected = {e["path"]: e for e in entries}
        seen = set()
        try:
            with tarfile.open(fileobj=body, mode="r|gz") as tar:
                for member in tar:
                    if (
                        member.name in seen
                        or member.name not in expected
                        or not member.isfile()
                    ):
                        raise RuntimeError("Unexpected final archive member")
                    h, count = hashlib.sha256(), 0
                    with tar.extractfile(member) as stream:
                        while chunk := stream.read(1024 * 1024):
                            h.update(chunk)
                            count += len(chunk)
                    item = expected[member.name]
                    if count != item["bytes"] or h.hexdigest() != item["sha256"]:
                        raise RuntimeError("Final archive member audit failed")
                    seen.add(member.name)
        finally:
            body.close()
        if seen != set(expected):
            raise RuntimeError("Missing final archive members")
        return {
            "status": "verified",
            "verified_at_unix": time.time(),
            "archive": ref,
            "index": index_ref,
            "wandb_artifact": logged.qualified_name,
            "wandb_state": "COMMITTED",
            "full_s3_get_verified": True,
            "wandb_download_verified": True,
            "independent_member_audit": True,
            "members": len(entries),
        }


def search_files(bundle: Path, jobs: list[dict[str, Any]]) -> dict[str, Path]:
    names = (
        "protocol.json",
        "jobs.json",
        "manifest.json",
        "source-manifest.json",
        "parent-protocol.json",
        "runtime-manifest.json",
        "approval.json",
        "inputs/starting-genotype.json",
        "inputs/dossier.json",
        "input-verification.json",
        "nominations.json",
        "nominations-verification.json",
    )
    result = {n: bundle / n for n in names if (bundle / n).exists()}
    if (bundle.parent / "operational-repair.json").exists():
        result["operational-repair.json"] = bundle.parent / "operational-repair.json"
    if (bundle.parent / "transport-recovery.json").exists():
        result["transport-recovery.json"] = bundle.parent / "transport-recovery.json"
    for name in (
        "batch-progress.json",
        "queue-timing-correction.json",
        "health-latest.json",
        "health-publication.json",
    ):
        if (bundle.parent / name).exists():
            result["operation/" + name] = bundle.parent / name
    allowed = {
        "config.json",
        "parent-checkpoint.json",
        "launch.json",
        "process.json",
        "environment.json",
        "graph.sqlite",
        "agents.sqlite",
        "tracking.json",
        "run-report.json",
        "terminal.json",
        "report-verification.json",
        "completion.json",
        "checkpoint.json",
        "checkpoints.jsonl",
        "failure.json",
        "model-usage.json",
        "provider-usage.jsonl",
        "progress.json",
        "controller.log",
        "transport-events.jsonl",
    }
    for job in jobs:
        folder = bundle / job["directory"]
        if not folder.exists():
            continue
        for name in allowed:
            if (folder / name).exists():
                result[job["directory"] + "/" + name] = folder / name
        if (folder / "literature").exists():
            for path in (folder / "literature").iterdir():
                if path.is_file() and (
                    path.suffix == ".response" or path.name == "requests.jsonl"
                ):
                    result[str(path.relative_to(bundle))] = path
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--resume-operational-repair", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("Remote Linux execution only")
    root = args.root.resolve()
    repo, bundle = root / "application", root / "bundle"

    def read(path: Path) -> dict[str, Any]:
        return json.loads(path.read_bytes())

    approval = read(bundle / "approval.json")
    spec, jobs = validate_launch(bundle, repo, approval)
    extension = spec.get("study_kind") == "baseline_depth_continuation_v1"
    if extension and args.resume_operational_repair:
        raise ValueError("Inspect continuation identities before designing a separate recovery")
    lock = (root / "batch.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (root / "batch-progress.json").exists() and not args.resume_operational_repair:
        raise RuntimeError(
            "Study already started; inspect/adopt live identities, never duplicate"
        )
    if read(root / "preflight.json")["status"] != "passed":
        raise RuntimeError("Verified remote preflight required")
    retained = (
        resume_jobs(root, bundle, jobs, approval)
        if args.resume_operational_repair
        else {}
    )
    if not extension:
        from litellm.responses.mcp.chat_completions_handler import acompletion_with_mcp

        assert callable(acompletion_with_mcp)
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

    from scripts.collect_search_replication import collect
    from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets

    transport = (
        read(root / "transport-recovery.json")
        if approval.get("transport_recovery_sha256")
        else None
    )
    study_suffix = (
        transport.get("study_suffix", "-study-transport-recovery1")
        if transport
        else "-study-dependency-retry1"
        if args.resume_operational_repair
        else "-study"
    )
    max_concurrent = 2 if transport else 4
    children: dict[str, tuple[Any, Any]] = {}
    state = {
        "experiment_id": spec["experiment_id"],
        "status": "search_running",
        "jobs": retained,
        "approval": approval,
        "supervisor": {
            "pid": os.getpid(),
            "start_ticks": Path(f"/proc/{os.getpid()}/stat").read_text().split()[21],
        },
        "started_at_unix": time.time(),
    }

    def persist() -> None:
        state["updated_at_unix"] = time.time()
        save(root / "batch-progress.json", state)

    def stop_children() -> None:
        live = [p for p, log in children.values() if p.poll() is None]
        for p in live:
            os.killpg(p.pid, signal.SIGINT)
        deadline = time.monotonic() + 120
        for p in live:
            try:
                p.wait(timeout=max(1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                print(
                    "Child did not stop; preserving its partial data", p.pid, flush=True
                )
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()

    def interrupted(signum: int, frame: Any) -> None:
        raise InterruptedError(f"Supervisor received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    persist()
    secrets = configured_secrets(repo, root / "credentials")
    location = urlparse(spec["tracking"]["s3_prefix"])
    with wandb.init(
        entity="mcclain",
        project="yggdrisil",
        id=spec["experiment_id"] + study_suffix,
        name=spec["experiment_id"] + study_suffix,
        job_type="replication",
        tags=spec["tracking"]["tags"],
        config={"approval": approval, "protocol_sha256": approval["protocol_sha256"]},
        dir=str(root),
        settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False),
    ) as tracked:
        state["wandb_url"] = tracked.url
        persist()
        archive = BaselineArchive(
            s3=boto3.client("s3", region_name="us-east-1"),
            wandb=wandb,
            tracked=tracked,
            owner=boto3.client("sts").get_caller_identity()["Account"],
            bucket=location.netloc,
            prefix=location.path.lstrip("/"),
            run_id=spec["experiment_id"] + study_suffix,
            secrets=secrets,
        )
        try:
            state["initial_backup"] = publish(
                search_files(bundle, []), archive, tracked, wandb, "initial", secrets
            )
            persist()
            pending = [i for i in range(len(jobs)) if str(i) not in retained]
            last_metadata_backup = time.monotonic()
            while pending or any(p.poll() is None for p, log in children.values()):
                if time.time() >= approval["science_deadline_unix"]:
                    raise TimeoutError("Original science deadline reached")
                for label, (p, log) in children.items():
                    if (
                        p.poll() is not None
                        and state["jobs"][label]["status"] == "running"
                    ):
                        log.close()
                        job = jobs[int(label)]
                        folder = bundle / job["directory"]
                        if (
                            p.returncode != 0
                            or not (folder / "completion.json").exists()
                        ):
                            raise RuntimeError(
                                "Unverified job exit; halt further dispatch: "
                                + job["run_id"]
                            )
                        done = read(folder / "completion.json")
                        if done["status"] != "terminal_verified":
                            raise RuntimeError(
                                "Job did not preserve its terminal result"
                            )
                        terminal = read(folder / "terminal.json")
                        if transport and transient_transport_error(
                            terminal.get("error") or {}
                        ):
                            raise RuntimeError(
                                "Pinned provider still unavailable after bounded retries; preserve remaining attempts and stop before further dispatch"
                            )
                        state["jobs"][label].update(
                            status="terminal_verified",
                            completion=done,
                            exit_code=p.returncode,
                            finished_at_unix=time.time(),
                        )
                        persist()
                live = sum(p.poll() is None for p, log in children.values())
                while pending and live < max_concurrent:
                    i = pending.pop(0)
                    job = jobs[i]
                    folder = bundle / job["directory"]
                    folder.mkdir(parents=True, exist_ok=True)
                    continuation = transport and job["run_id"] in transport["resumes"]
                    if not continuation and (
                        (folder / "process.json").exists()
                        or (folder / "graph.sqlite").exists()
                        or (folder / "controller.log").exists()
                    ):
                        raise RuntimeError("Duplicate dispatch refused")
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
                        env=os.environ,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    children[str(i)] = (p, log)
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
                if time.monotonic() - last_metadata_backup >= 300:
                    state["progress_backup"] = publish(
                        {"batch-progress.json": root / "batch-progress.json"},
                        archive,
                        tracked,
                        wandb,
                        "progress",
                        secrets,
                    )
                    persist()
                    last_metadata_backup = time.monotonic()
                time.sleep(15)
            # Inspect final exits even if the last children all exited together.
            for label, (p, log) in children.items():
                if not log.closed:
                    log.close()
                done = read(bundle / jobs[int(label)]["directory"] / "completion.json")
                if p.returncode != 0 or done["status"] != "terminal_verified":
                    raise RuntimeError("Unverified final search exit")
                terminal = read(
                    bundle / jobs[int(label)]["directory"] / "terminal.json"
                )
                if transport and transient_transport_error(terminal.get("error") or {}):
                    raise RuntimeError(
                        "Pinned provider unavailable; do not nominate an outage-truncated study"
                    )
                state["jobs"][label].update(
                    status="terminal_verified", completion=done, exit_code=p.returncode
                )
            save(bundle / "nominations.json", collect(bundle))
            receipt = publish(
                {"nominations.json": bundle / "nominations.json"},
                archive,
                tracked,
                wandb,
                "nominations",
                secrets,
            )
            save(bundle / "nominations-verification.json", receipt)
            state["search_final_backup"] = publish(
                search_files(bundle, jobs),
                archive,
                tracked,
                wandb,
                "search-final",
                secrets,
            )
            if extension:
                state["status"] = "search_completed_pending_independent_review"
                state["validation_launch_authorized"] = False
                persist()
                state["final_catalog_backup"] = publish(
                    {"batch-progress.json": root / "batch-progress.json",
                     "nominations.json": bundle / "nominations.json"},
                    archive, tracked, wandb, "final-catalog", secrets,
                )
                persist()
                return
            state["status"] = "validation_preparing"
            persist()
            subprocess.run(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "scripts.prepare_replication_validation",
                    "--root",
                    str(root),
                ],
                cwd=repo,
                check=True,
                timeout=max(1, approval["science_deadline_unix"] - time.time()),
            )
            log = (root / "validation/supervisor.log").open("xb")
            p = subprocess.Popen(
                [sys.executable, "-u", str(root / "validation/supervisor.py")],
                cwd=root / "validation",
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            children["validation"] = (p, log)
            state["status"] = "validation_running"
            state["validation_process"] = {
                "pid": p.pid,
                "start_ticks": Path(f"/proc/{p.pid}/stat").read_text().split()[21],
            }
            persist()
            while p.poll() is None:
                if time.time() >= approval["science_deadline_unix"]:
                    raise TimeoutError(
                        "Original science deadline reached during validation"
                    )
                time.sleep(15)
            log.close()
            final = read(root / "validation/final-backup.json")
            if p.returncode != 0 or final["status"] != "verified":
                raise RuntimeError("Validation exit has no verified final backup")
            state["validation_final_backup"] = final
            state["status"] = "completed_pending_independent_analysis"
            persist()
            state["final_catalog_backup"] = publish(
                {
                    "batch-progress.json": root / "batch-progress.json",
                    "nominations.json": bundle / "nominations.json",
                    "validation/batch-progress.json": root
                    / "validation/batch-progress.json",
                    "validation/final-backup.json": root
                    / "validation/final-backup.json",
                },
                archive,
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
            # Preserve partial attempts. An archival error propagates and the
            # independent absolute system timer still stops billing.
            state["failure_backup"] = publish(
                search_files(bundle, jobs), archive, tracked, wandb, "failure", secrets
            )
            persist()
            raise
        finally:
            stop_children()
            for process, log in children.values():
                if not log.closed:
                    log.close()
            subprocess.run(["sudo", "shutdown", "-h", "+2"], check=True)


if __name__ == "__main__":
    main()
