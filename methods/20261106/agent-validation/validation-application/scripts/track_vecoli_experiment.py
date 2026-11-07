#!/usr/bin/env python3
"""Run the official vEcoli workflow with resumable data backups and a catalog."""

from __future__ import annotations

import argparse
import ast
import fcntl
import gzip
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from urllib.parse import urlparse

import pyarrow.parquet as pq

from yggdrisil_ecoli.data.io import atomic_json, file_sha256
from yggdrisil_ecoli.vecoli import VECOLI_NEXTFLOW_VERSION
from yggdrisil_ecoli.vecoli_results import (
    _simulation_tasks,
    _validate_manifest_provenance,
    summarize_vecoli_lineages,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_catalog(path: Path, experiment_id: str, **changes: Any) -> None:
    catalog = json.loads(path.read_text())
    entries = [e for e in catalog["experiments"] if e["id"] == experiment_id]
    if len(entries) != 1:
        raise ValueError(f"Expected one catalog entry for {experiment_id}")
    entries[0].update(changes)
    catalog["updated_at"] = utc_now()
    atomic_json(path, catalog)


def stability_metrics(generations: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe late-cycle behavior without equating division with stability."""
    window = generations[-4:]
    if not window:
        return {"window_generations": 0}
    durations = [g["generation_duration_s"] for g in window]
    birth_mass = [
        g["final_dry_mass_fg"] / g["final_dry_mass_fold_change"] for g in window
    ]
    if min(durations) <= 0 or min(birth_mass) <= 0:
        raise ValueError("Nonpositive cycle duration or birth mass")
    return {
        "window_generations": len(window),
        "mean_duration_s": mean(durations),
        "duration_cv": pstdev(durations) / mean(durations),
        "birth_mass_last_over_first": birth_mass[-1] / birth_mass[0],
        "mean_dry_mass_fold_change": mean(
            g["final_dry_mass_fold_change"] for g in window
        ),
    }


def check_transcripts(path: Path, targets: set[int]) -> None:
    if not targets:
        return
    state = next(iter(json.loads(path.read_text())["agents"].values()))
    fields = [field[0] for field in ast.literal_eval(state["unique_dtypes"]["RNA"])]
    tu, active = fields.index("TU_index"), fields.index("_entryState")
    expressed = {
        row[tu] for row in state["unique"]["RNA"] if row[active] and row[tu] in targets
    }
    if expressed:
        raise RuntimeError(
            f"Knocked-out transcripts reappeared in {path}: {sorted(expressed)}"
        )


def check_history(paths: list[Path]) -> None:
    columns = [
        "global_time",
        "listeners__mass__cell_mass",
        "listeners__mass__dry_mass",
        "listeners__mass__dry_mass_fold_change",
    ]
    for path in paths:
        if "history" not in path.parts or path.suffix != ".pq":
            continue
        for row in pq.read_table(path, columns=columns).to_pylist():
            for name, value in row.items():
                if name == "global_time" and value is None:
                    continue  # The initial emission precedes the first clock update.
                if value is None or not math.isfinite(value):
                    raise RuntimeError(
                        f"Invalid {name} in cell history {path}: {value}"
                    )


def cell_files(experiment: Path, task: Any, seed: int) -> list[Path]:
    """Only closed simulation data and logs belong in a checkpoint."""
    if task.exit_code is None:
        return []
    files = [
        task.workdir / name
        for name in (".command.out", ".command.err", ".command.trace", ".exitcode")
        if (task.workdir / name).is_file()
    ]
    for kind in ("history", "configuration", "success"):
        folder = (
            experiment
            / kind
            / f"experiment_id={experiment.name}"
            / f"variant={task.variant_index}"
            / f"lineage_seed={seed}"
            / f"generation={task.generation}"
        )
        files.extend(folder.rglob("*.pq"))
    folder = (
        experiment
        / "daughter_states"
        / f"variant={task.variant_index}"
        / f"seed={seed}"
        / f"generation={task.generation}"
    )
    files.extend(folder.rglob("*.json"))
    files.extend(p for p in (task.workdir / "solver-diagnostics").glob("*")
                 if p.suffix in {".json", ".glp"})
    return files


def solver_diagnostics(workdir):
    records = []
    for path in sorted((workdir / "solver-diagnostics").glob("*.json")):
        check = json.loads(path.read_text())
        before, after = path.with_suffix(".before.glp"), path.with_suffix(".after.glp")
        if (not check["model_unchanged"] or check["return_code"] != 0
                or not check["status"].startswith("GLP_OPT")
                or len(check["kkt"]) != 4
                or any(not math.isfinite(v["relative_error"]) or v["relative_error"] > 1e-7
                       for v in check["kkt"].values())
                or file_sha256(before) != check["original_lp_sha256"]
                or file_sha256(after) != check["original_lp_sha256"]):
            raise RuntimeError("Rejected solver diagnostic in completed cell: " + str(path))
        records.append({"path": str(path), "sha256": file_sha256(path),
                        "max_kkt_relative_error": max(v["relative_error"] for v in check["kkt"].values())})
    return records


def simulation_environment(
    manifest: dict[str, Any], parent: dict[str, str]
) -> dict[str, str]:
    checkout = Path(manifest["vecoli"]["checkout"])
    python = Path(manifest["execution"]["vecoli_python"])
    env = parent.copy()
    # Local simulations have no reason to inherit service credentials.
    for credential in (
        "WANDB_API_KEY",
        "OPENROUTER_API_KEY",
        "EDISON_PLATFORM_API_KEY",
    ):
        env.pop(credential, None)
    nextflow = Path(manifest["execution"]["nextflow_executable"])
    env.update(
        {
            "PATH": os.pathsep.join(
                (str(python.parent), str(nextflow.parent), env["PATH"])
            ),
            "VIRTUAL_ENV": str(python.parent.parent),
            "NXF_VER": VECOLI_NEXTFLOW_VERSION,
            "YGGDRISIL_SOLVER_DIAGNOSTICS": "solver-diagnostics",
            "PYTHONPATH": str(checkout),
            "PYTHONUNBUFFERED": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "POLARS_MAX_THREADS": "1",
        }
    )
    return env


def workflow_status(result: dict[str, Any]) -> str:
    if result["all_reached_max_generations"]:
        return "completed"
    reasons = {item["terminal_reason"] for item in result["finalists"]}
    return (
        "completed_with_nondivision"
        if reasons <= {"nondivision_max_duration", "reached_max_generations"}
        else "failed"
    )


def save_s3_checkpoint(
    client: Any,
    prefix: str,
    files: dict[str, Path],
    previous: dict[str, Any],
) -> dict[str, Any]:
    """Publish immutable data objects and a fully verified checkpoint index."""
    location = urlparse(prefix)
    if location.scheme != "s3" or not location.netloc or not location.path.strip("/"):
        raise ValueError("Expected an S3 bucket and experiment prefix")
    bucket, root = location.netloc, location.path.strip("/")

    def upload(data: bytes, key: str) -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ServerSideEncryption="AES256",
            Metadata={"sha256": digest},
        )
        response = client.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            verified = hashlib.sha256()
            for chunk in response.iter_chunks(chunk_size=1024**2):
                verified.update(chunk)
        finally:
            response.close()
        if verified.hexdigest() != digest:
            raise RuntimeError(f"S3 checkpoint verification failed: {key}")
        return {"uri": f"s3://{bucket}/{key}", "sha256": digest, "bytes": len(data)}

    entries = dict(previous.get("files", {}))
    for name, path in files.items():
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if entries.get(name, {}).get("sha256") != digest:
            entries[name] = upload(data, f"{root}/objects/{digest}")
    index = {"verified_at": utc_now(), "files": entries}
    data = (json.dumps(index, sort_keys=True, indent=2) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    index["manifest"] = upload(data, f"{root}/checkpoints/{digest}.json")
    return index


def run_experiment(manifest_path: Path, catalog_path: Path, *, resume: bool) -> int:
    import wandb

    manifest = json.loads(manifest_path.read_text())
    _validate_manifest_provenance(manifest)
    if file_sha256(__file__) != manifest["controller_source"]["sha256"]:
        raise RuntimeError("Controller source differs from the experiment manifest")
    folder = manifest_path.parent
    experiment_id = manifest["experiment_id"]
    workflow = manifest["workflow"]
    experiment = Path(workflow["output_root"]) / workflow["experiment_id"]
    checkout = Path(manifest["vecoli"]["checkout"])
    env = simulation_environment(manifest, dict(os.environ))
    nextflow = Path(manifest["execution"]["nextflow_executable"])
    version = subprocess.run(
        [str(nextflow), "-version"], env=env, check=True, capture_output=True, text=True
    ).stdout
    if f"version {VECOLI_NEXTFLOW_VERSION} " not in version:
        raise RuntimeError(f"Unexpected workflow engine: {version}")
    if resume:
        report = experiment / "nextflow" / f"{workflow['experiment_id']}_report.html"
        if report.exists():
            report.rename(
                report.with_name(report.stem + f"-before-resume-{time.time_ns()}.html")
            )
    elif (folder / "progress.json").exists():
        raise FileExistsError("Experiment already started; use --resume")

    tracking = manifest["tracking"]
    s3 = None
    cloud_files: dict[str, Path] = {}
    cloud_checkpoint: dict[str, Any] = {}
    cloud_checkpoint_path = folder / "s3-checkpoint.json"
    if tracking.get("s3_prefix"):
        import boto3

        s3 = boto3.client("s3", region_name=tracking.get("aws_region", "us-east-1"))
        if resume and cloud_checkpoint_path.exists():
            cloud_checkpoint = json.loads(cloud_checkpoint_path.read_text())
    os.environ["WANDB_BASE_URL"] = tracking["base_url"]
    os.environ["WANDB_CACHE_DIR"] = str(folder / "wandb-cache")
    os.environ["WANDB_DATA_DIR"] = str(folder / "wandb-data")
    run = wandb.init(
        entity=tracking["entity"],
        project=tracking["project"],
        id=tracking["run_id"],
        resume="allow",
        name=tracking["run_name"],
        job_type="vecoli-lineage-validation",
        tags=[
            "yggdrisil",
            "validation",
            f"lineage{manifest['lineage']['max_generations']}",
            "fixed-genotypes",
            *tracking.get("tags", []),
        ],
        dir=str(folder),
        config={
            "lineage": manifest["lineage"],
            "execution": manifest["execution"],
            "parent_data_artifact": manifest["parent_data_artifact"],
            "purpose": manifest["purpose"],
            "model_budget_usd": 0,
        },
        settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False),
    )
    for spec in manifest["finalists"]:
        prefix = f"vecoli/{spec['name']}"
        run.define_metric(prefix + "/generation")
        run.define_metric(prefix + "/*", step_metric=prefix + "/generation")
    event_log = folder / "events.jsonl"

    def event(kind: str, **data: Any) -> None:
        with event_log.open("a") as handle:
            handle.write(json.dumps({"time": utc_now(), "event": kind, **data}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    catalog = json.loads(catalog_path.read_text())
    entry = next(e for e in catalog["experiments"] if e["id"] == experiment_id)
    backup = entry["data_backup"]
    artifact = (
        wandb.Api().artifact(backup["wandb_artifact"]).new_draft()
        if resume and "wandb_artifact" in backup
        else wandb.Artifact(tracking["artifact_name"], type="experiment-data")
    )
    uploaded = set(artifact.manifest.entries)
    checked: set[tuple[int, int]] = set()
    seen: set[tuple[int, int]] = set()
    previous: dict[str, Any] | None = None
    process: subprocess.Popen[bytes] | None = None
    credentials = Path("/home/ubuntu/yggdrisil-continuation-20261106/credentials")
    secrets = [value.encode() for path in credentials.glob("*.json")
               for key, value in json.loads(path.read_text()).items()
               if key.endswith("_KEY") and isinstance(value, str)]
    if len(secrets) != 4:
        raise RuntimeError("Expected all four configured run secrets for backup screening")
    exit_code = 1

    def add_data(path: Path, name: str, *, replace: bool = False) -> None:
        if not replace and (name in uploaded or name + ".gz" in uploaded):
            return
        if path.suffix in {".py", ".nf", ".sh", ".patch"} or path.name.startswith(
            ".env"
        ):
            raise ValueError(
                f"Source or credential file is not experiment data: {path}"
            )
        if any(secret in path.read_bytes() for secret in secrets):
            raise RuntimeError(f"Credential found in data backup: {path}")
        source = path
        if path.suffix in {".json", ".cPickle"} and path.stat().st_size > 1_000_000:
            source = folder / "backup-cache" / (name + ".gz")
            source.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as src, gzip.open(source, "wb") as dst:
                shutil.copyfileobj(src, dst)
            name += ".gz"
        artifact.add_file(str(source), name=name, overwrite=replace)
        cloud_files[name] = source
        uploaded.add(name)

    def checkpoint(result: dict[str, Any], tasks: dict[Any, Any]) -> None:
        nonlocal artifact, cloud_checkpoint
        result["experiment_id"] = experiment_id
        for item in result["finalists"]:
            spec = next(
                f for f in manifest["finalists"] if f["state_id"] == item["state_id"]
            )
            item["name"] = spec["name"]
            item["late_cycle_metrics"] = stability_metrics(item["generations"])
            for generation in item["generations"]:
                key = (item["variant_index"], generation["generation"])
                generation["solver_fallbacks"] = solver_diagnostics(tasks[key].workdir)
                generation["solver_fallback_assisted"] = bool(generation["solver_fallbacks"])
                if key in checked:
                    continue
                daughters = (
                    experiment
                    / "daughter_states"
                    / f"variant={key[0]}"
                    / f"seed={manifest['lineage']['seed']}"
                    / f"generation={key[1]}"
                    / f"agent_id={'0' * key[1]}"
                )
                for path in daughters.glob("daughter_state_*.json"):
                    check_transcripts(path, set(spec["target_rna_indices"]))
                check_history(
                    cell_files(experiment, tasks[key], manifest["lineage"]["seed"])
                )
                checked.add(key)
                event(
                    "generation_completed",
                    variant=key[0],
                    name=spec["name"],
                    **generation,
                )
                prefix = f"vecoli/{spec['name']}"
                run.log(
                    {
                        f"{prefix}/generation": key[1],
                        f"{prefix}/duration_s": generation["generation_duration_s"],
                        f"{prefix}/dry_mass_fold_change": generation[
                            "final_dry_mass_fold_change"
                        ],
                    }
                )
            run.summary[f"{item['name']}/generations_completed"] = item[
                "generations_completed"
            ]
            run.summary[f"{item['name']}/late_cycle_metrics"] = item[
                "late_cycle_metrics"
            ]
        result["updated_at"] = utc_now()
        atomic_json(folder / "progress.json", result)
        atomic_json(folder / "results.json", result)
        for path, name in [
            (manifest_path, "manifest.json"),
            (Path(workflow["config_path"]), "workflow.json"),
            (catalog_path, "experiments.json"),
            (folder / "progress.json", "progress.json"),
            (event_log, "events.jsonl"),
            (folder / "workflow.log", "workflow.log"),
            (folder / "process.json", "process.json"),
        ]:
            add_data(
                path,
                name,
                replace=name
                in {
                    "progress.json",
                    "events.jsonl",
                    "experiments.json",
                    "manifest.json",
                    "workflow.log",
                    "process.json",
                },
            )
        for task in tasks.values():
            for path in cell_files(experiment, task, manifest["lineage"]["seed"]):
                add_data(path, "output/" + str(path.relative_to(experiment)))
        for label, source in manifest.get("benchmark_inputs", {}).items():
            path = Path(source["path"])
            add_data(path, f"inputs/{label}/{path.name}")
        audit = manifest["vecoli"].get("variant_knockout_audit_path")
        if audit is not None:
            add_data(Path(audit), "variant-knockout-audit.json")
        # Retried tasks may have a different work directory for the same cell.
        for marker in (experiment / "nextflow/nextflow_workdirs").rglob(".exitcode"):
            for name in (".command.out", ".command.err", ".command.trace", ".exitcode"):
                path = marker.with_name(name)
                if path.is_file():
                    add_data(path, "output/" + str(path.relative_to(experiment)))
        for marker in (experiment / "nextflow/nextflow_workdirs").rglob(".exitcode"):
            for diagnostic in (marker.parent / "solver-diagnostics").glob("*"):
                if diagnostic.suffix in {".json", ".glp"}:
                    add_data(diagnostic, "output/" + str(diagnostic.relative_to(experiment)))
        if tasks:
            for path in (experiment / "variant_sim_data").glob("*"):
                if path.suffix in {".json", ".cPickle"}:
                    add_data(path, "output/variant_sim_data/" + path.name)
        if s3 is not None:
            cloud_checkpoint = save_s3_checkpoint(
                s3, tracking["s3_prefix"], cloud_files, cloud_checkpoint
            )
            atomic_json(cloud_checkpoint_path, cloud_checkpoint)
            cloud_files.clear()
            event("s3_checkpoint_verified", **cloud_checkpoint["manifest"])
            run.summary["latest_s3_checkpoint"] = cloud_checkpoint["manifest"]["uri"]
        logged = run.log_artifact(artifact, aliases=["latest", "checkpoint"])
        logged.wait()
        downloaded = logged.get_path("progress.json").download(
            root=str(folder / "backup-verification")
        )
        if file_sha256(downloaded) != file_sha256(folder / "progress.json"):
            raise RuntimeError("Downloaded checkpoint does not match local progress")
        update_catalog(
            catalog_path,
            experiment_id,
            data_backup={
                "status": "verified",
                "wandb_artifact": logged.qualified_name,
                "parent_artifact": manifest["parent_data_artifact"],
                "verified_at": utc_now(),
                "progress_sha256": file_sha256(downloaded),
                "completed_cell_cycles": sum(
                    item["generations_completed"] for item in result["finalists"]
                ),
                "scope": "Closed cell outputs; running cells are backed up after completion.",
                "s3_checkpoint": cloud_checkpoint.get("manifest"),
            },
        )
        event("checkpoint_verified", artifact=logged.qualified_name)
        run.summary["latest_data_artifact"] = logged.qualified_name
        artifact = logged.new_draft()

    try:
        if s3 is not None:
            initial_inputs = {
                "manifest.json": manifest_path,
                "workflow.json": Path(workflow["config_path"]),
                "inputs/genes.parquet": Path(manifest["registry"]["path"]),
            }
            for index, graph in enumerate(manifest.get("source_graphs", [])):
                initial_inputs[f"inputs/graph-{index}.sqlite"] = Path(graph["backup"]["frozen_path"])
            audit_path = manifest["vecoli"].get("variant_knockout_audit_path")
            if audit_path:
                initial_inputs["variant-knockout-audit.json"] = Path(audit_path)
            for name, path in initial_inputs.items():
                add_data(path, name, replace=True)
            cloud_checkpoint = save_s3_checkpoint(
                s3, tracking["s3_prefix"], cloud_files, cloud_checkpoint
            )
            atomic_json(cloud_checkpoint_path, cloud_checkpoint)
            cloud_files.clear()
            event("s3_inputs_verified", **cloud_checkpoint["manifest"])
        command = [
            manifest["execution"]["vecoli_python"],
            str(checkout / "runscripts/workflow.py"),
            "--config",
            workflow["config_path"],
        ]
        if resume:
            command += ["--resume", workflow["experiment_id"]]
        event(
            "workflow_resumed" if resume else "workflow_started",
            command=command,
            nextflow_version=version.strip(),
            model_budget_usd=0,
        )
        update_catalog(
            catalog_path,
            experiment_id,
            status="running",
            started_at=utc_now(),
            wandb_run_urls=[run.url],
            controller_pid=os.getpid(),
            finished_at=None,
            failed_at=None,
            error=None,
            workflow_exit_code=None,
        )
        with (folder / "workflow.log").open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=manifest["execution"].get("workflow_working_directory", checkout),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            atomic_json(
                folder / "process.json",
                {
                    "controller_pid": os.getpid(),
                    "workflow_pid": process.pid,
                    "command": command,
                },
            )
            while True:
                if shutil.disk_usage(folder).free < 5 * 1024**3:
                    raise RuntimeError(
                        "Less than 5 GiB free; stopping before data loss"
                    )
                workdirs = experiment / "nextflow/nextflow_workdirs"
                if workdirs.is_dir():
                    tasks = _simulation_tasks(workdirs)
                    for key, task in tasks.items():
                        if key not in seen:
                            seen.add(key)
                            event(
                                "generation_started",
                                variant=key[0],
                                generation=key[1],
                                workdir=str(task.workdir),
                            )
                    result = summarize_vecoli_lineages(
                        manifest_path, folder / "results.json"
                    )
                    signature = {str(k): t.exit_code for k, t in tasks.items()}
                    if previous != signature or process.poll() is not None:
                        checkpoint(result, tasks)
                        previous = signature
                if process.poll() is not None:
                    break
                time.sleep(30)
        if not (folder / "results.json").exists():
            raise RuntimeError(
                "Workflow exited before producing simulation results; see workflow.log"
            )
        result = json.loads((folder / "results.json").read_text())
        status = workflow_status(result)
        event("workflow_finished", status=status, exit_code=process.returncode)
        update_catalog(
            catalog_path,
            experiment_id,
            status=status,
            finished_at=utc_now(),
            workflow_exit_code=process.returncode,
        )
        run.summary["status"] = status
        checkpoint(result, _simulation_tasks(workdirs))
        exit_code = int(process.returncode or 0)
        return exit_code
    except BaseException as exc:
        event("controller_failed", error=repr(exc))
        update_catalog(
            catalog_path,
            experiment_id,
            status="failed",
            error=repr(exc),
            failed_at=utc_now(),
        )
        raise
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                event("workflow_force_stopped", pid=process.pid)
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        run.finish(exit_code=exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--catalog", type=Path, default=Path("experiments.json"))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if sys.platform == "darwin":
        parser.error("Experiment execution is remote-only. Run this job on Linux compute.")
    # A held lock prevents two controllers from launching the same experiment.
    with args.manifest.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise SystemExit(
            run_experiment(
                args.manifest.resolve(), args.catalog.resolve(), resume=args.resume
            )
        )


if __name__ == "__main__":
    main()
