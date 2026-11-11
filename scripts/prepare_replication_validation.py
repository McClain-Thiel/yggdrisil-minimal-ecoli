#!/usr/bin/env python3
"""Prepare only the already frozen, verified replication finalists on Linux."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--depth-matched", action="store_true")
    parser.add_argument("--worker-role", choices=["primary", "secondary"])
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("Native preparation is remote Linux only")
    root = args.root.resolve()
    bundle, valid = root / "bundle", root / "validation"
    import boto3
    import pandas as pd

    from scripts.collect_search_replication import collect
    from yggdrisil_ecoli.baseline_storage import sqlite_snapshot
    from yggdrisil_ecoli.data.evidence import load_genes
    from yggdrisil_ecoli.data.io import atomic_json, file_sha256
    from yggdrisil_ecoli.vecoli import install_vecoli_adapter, map_finalists
    from yggdrisil_ecoli.vecoli_results import _validate_manifest_provenance

    def read(path: Path) -> dict:
        return json.loads(path.read_bytes())

    spec, approval = read(bundle / "protocol.json"), read(bundle / "approval.json")
    frozen = read(bundle / "nominations.json")
    if (
        frozen != collect(bundle)
        or read(bundle / "nominations-verification.json")["status"] != "verified"
    ):
        raise RuntimeError(
            "All terminal nominations must be frozen and backed up first"
        )
    nomination_path = bundle / "nominations.json"
    verification_path = bundle / "nominations-verification.json"
    validation_id = spec["experiment_id"] + "-validation"
    seed_labels = [None, *spec["validation"]["seeds"]]
    calibration_label = "calibration"
    maximum_cells = 3
    selection_algorithm = spec["selection"]
    tracking_prefix = spec["tracking"]["s3_prefix"] + "validation/"
    if args.depth_matched:
        from scripts.select_depth_matched_baselines import select

        if args.worker_role is None:
            raise ValueError("Depth-matched validation requires a bound worker role")
        approval = read(root / "resource-approval.json")
        plan = read(root / "plan.json")
        if (
            approval["approved_cap_usd"] != 100
            or approval["paid_launch"] is not True
            or approval["model_calls_allowed"] is not False
            or plan["experiment_id"] != approval["experiment_id"]
        ):
            raise ValueError("Missing approved depth-matched validation bounds")
        nomination_path = bundle / "matched-nominations.json"
        verification_path = bundle / "matched-nominations-verification.json"
        frozen = read(nomination_path)
        if (
            frozen
            != select(
                bundle,
                read(root / "selection-amendment.json"),
                root / "reference-nominations.json",
            )
            or read(verification_path)["status"] != "verified"
        ):
            raise RuntimeError("Matched nominations must be reproducible and backed up")
        validation_id = approval["experiment_id"]
        seed_labels = [None, *plan["worker_allocation"][args.worker_role]["seeds"]]
        calibration_label = "calibration-" + args.worker_role
        maximum_cells = plan["worker_allocation"][args.worker_role][
            "maximum_cells_per_workflow"
        ]
        selection_algorithm = frozen["selection"]
        tracking_prefix = (
            "s3://phd-research-storage-1758274488/yggdrisil/experiments/"
            + validation_id
            + "/"
        )
    elif args.worker_role is not None:
        raise ValueError("Worker role requires depth-matched validation")
    if (valid / "prepared.json").exists() or (valid / "batch-progress.json").exists():
        raise RuntimeError(
            "Validation already prepared or running; inspect before recovery"
        )
    inputs = valid / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    cache = root / "input-archives"
    cache.mkdir(exist_ok=True)
    digest = "9e01ed0650d2de9f9f9f7b80856c71d34d27b013e1f4046aff450e80b8dfdb4b"
    archive = cache / (digest + ".tar.gz")
    if not archive.exists():
        boto3.client("s3", region_name="us-east-1").download_file(
            "phd-research-storage-1758274488",
            "yggdrisil/archives/20261105/vecoli-kimi-full-diagnostic-20261105-inputs-v0/"
            + archive.name,
            str(archive),
        )
    if file_sha256(archive) != digest:
        raise RuntimeError("Frozen native input archive checksum mismatch")
    with tarfile.open(archive) as tar:
        for name in ("manifest.json", "workflow.json", "inputs/simData.cPickle.gz"):
            member = tar.getmember(name)
            if not member.isfile():
                raise RuntimeError("Expected a regular frozen input")
            with (
                tar.extractfile(member) as src,
                (inputs / Path(name).name).open("wb") as dst,
            ):
                shutil.copyfileobj(src, dst)
    base, workflow = read(inputs / "manifest.json"), read(inputs / "workflow.json")
    if (
        not workflow["single_daughters"]
        or workflow["n_init_sims"] != 1
        or not workflow["fail_at_max_duration"]
    ):
        raise RuntimeError("Native lineage policy changed")
    sim = inputs / "simData.cPickle"
    with gzip.open(inputs / "simData.cPickle.gz", "rb") as src, sim.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    if file_sha256(sim) != base["workflow"]["sim_data_sha256"]:
        raise RuntimeError("Native simulation data changed")
    genes_path = root / "application" / spec["inputs"]["genes"]["path"]
    if file_sha256(genes_path) != spec["inputs"]["genes"]["sha256"]:
        raise RuntimeError("Gene registry changed")
    genes = load_genes(genes_path)
    sources = []
    jobs = read(bundle / "jobs.json")
    for job in jobs:
        source = bundle / job["directory"] / "graph.sqlite"
        graph = inputs / "graphs" / (job["run_id"] + ".sqlite")
        graph.parent.mkdir(parents=True, exist_ok=True)
        sqlite_snapshot(source, graph)
        with closing(sqlite3.connect(graph)) as connection:
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != (
                "delete",
            ):
                raise RuntimeError("Frozen graph backup retained WAL mode")
        if any(Path(str(graph) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise RuntimeError("Frozen graph backup retained sidecars")
        checksum = file_sha256(graph)
        sources.append(
            {
                "selection": {
                    "graph_path": str(graph),
                    "graph_files": {graph.name: checksum},
                    "run_id": job["run_id"],
                    "selection_algorithm": selection_algorithm,
                    "terminal_report_sha256": file_sha256(
                        bundle / job["directory"] / "run-report.json"
                    ),
                },
                "backup": {"frozen_path": str(graph), "frozen_sha256": checksum},
            }
        )
    finalists = copy.deepcopy(base["finalists"][:2])
    if args.depth_matched:
        import math

        # The historical starting-control table encoded absent references as NaN.
        for finalist in finalists:
            for key in ("search_data_artifact", "search_result"):
                value = finalist.get(key)
                if isinstance(value, float) and math.isnan(value):
                    finalist[key] = None
    if (
        finalists[0]["deletion_count"] != 0
        or finalists[1]["deletion_set_sha256"]
        != spec["starting_genotype"]["deletion_set_sha256"]
    ):
        raise RuntimeError("Matched controls changed")
    for index, nomination in enumerate(frozen["nominations"], start=2):
        row = {
            **nomination,
            "deletion_count": nomination["genes_deleted"],
            "fba_growth_rate": nomination["fba_growth"],
        }
        frame = pd.DataFrame([row]).set_index("state_id")
        item = map_finalists(frame, genes).reset_index().to_dict("records")[0]
        item.update(name=f"finalist-{index - 1:02d}", variant_index=index)
        finalists.append(item)
    native = root / "native-validated"
    if (
        subprocess.check_output(
            ["git", "-C", str(native), "rev-parse", "HEAD"], text=True
        ).strip()
        != spec["validation"]["native_commit"]
    ):
        raise RuntimeError("Native commit changed")
    if (
        file_sha256(native / "wholecell/utils/_netflow/nf_glpk.py")
        != "5ad8cd9ca727381032bb852b2b786465424598fbba1c261ac36741377db01686"
    ):
        raise RuntimeError("Strict numerical policy changed")
    adapter = install_vecoli_adapter(native)
    audit = valid / "knockout-audit"
    audit.mkdir(exist_ok=False)
    shutil.copyfile(
        root / "operations/audit-knockouts.py", audit / "audit-knockouts.py"
    )
    manifest = copy.deepcopy(base)
    manifest["finalists"] = finalists
    manifest["workflow"]["sim_data_path"] = str(sim)
    atomic_json(audit / "manifest.json", manifest)
    atomic_json(audit / "preflight.json", {"status": "preparing"})
    env = {
        **os.environ,
        "PYTHONPATH": str(native),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }
    for key in ("OPENROUTER_API_KEY", "EDISON_PLATFORM_API_KEY", "WANDB_API_KEY"):
        env.pop(key, None)
    subprocess.run(
        [str(native / ".venv/bin/python"), str(audit / "audit-knockouts.py")],
        cwd=native,
        env=env,
        check=True,
    )
    finalists = read(audit / "manifest.json")["finalists"]
    controller = valid / "application/scripts/track_vecoli_experiment.py"
    labels = []
    for seed in seed_labels:
        calibration = seed is None
        seed = 101 if calibration else seed
        label = calibration_label if calibration else f"seed-{seed}"
        labels.append(label)
        folder = valid / "runs" / label
        folder.mkdir(parents=True, exist_ok=False)
        for name in ("runscripts", "ecoli"):
            (folder / name).symlink_to(native / name, target_is_directory=True)
        exp = validation_id + "-" + label
        config = copy.deepcopy(workflow)
        config.update(
            experiment_id=exp.replace("-", "_"),
            generations=2 if calibration else 20,
            lineage_seed=seed,
            SIM_CPUS=16 // maximum_cells if args.depth_matched else 5,
            SIM_MEM=4,
            sim_data_path=str(sim),
        )
        config["emitter_arg"]["out_dir"] = str(folder / "output")
        config["variants"] = (
            {}
            if calibration
            else {
                "yggdrisil_multi_gene_knockout": {
                    "gene_ids": {"value": [x["vecoli_gene_ids"] for x in finalists[1:]]}
                }
            }
        )
        config["skip_baseline"] = False
        atomic_json(folder / "workflow.json", config)
        manifest = copy.deepcopy(base)
        manifest.update(
            experiment_id=exp,
            created_at=datetime.now(timezone.utc).isoformat(),
            purpose="Predeclared finalists from independent search replication",
            source_graphs=sources,
            finalists=finalists[:1] if calibration else finalists,
        )
        manifest.pop("application", None)
        manifest["parent_experiment_ids"] = [spec["experiment_id"]]
        manifest["parent_data_artifact"] = read(verification_path)["wandb_artifact"]
        manifest["frozen_nominations_sha256"] = file_sha256(nomination_path)
        if args.depth_matched:
            manifest["selection_amendment_sha256"] = frozen[
                "selection_amendment_sha256"
            ]
            manifest["purpose"] = (
                "Exploratory depth-matched baseline validation; all earlier endpoints retained"
            )
        manifest["lineage"].update(seed=seed, max_generations=config["generations"])
        manifest["validation"].update(require_generations=config["generations"])
        manifest["controller_source"] = {
            "path": str(controller),
            "sha256": file_sha256(controller),
        }
        manifest["execution"] = {
            "backend": "Nextflow local executor on dedicated remote Linux EC2",
            "vecoli_python": str(native / ".venv/bin/python"),
            "nextflow_executable": str(root / "bin/nextflow"),
            "workflow_working_directory": str(folder),
            "max_parallel_cells": 1 if calibration else maximum_cells,
            "paid_compute": True,
            "application_root": str(valid / "application"),
            "credential_directory": str(root / "credentials"),
        }
        manifest["registry"] = {
            "path": str(genes_path),
            "sha256": file_sha256(genes_path),
        }
        manifest["vecoli"].update(
            checkout=str(native),
            adapter_path=str(adapter),
            adapter_sha256=file_sha256(adapter),
            variant_knockout_audit_path=str(audit / "variant-knockout-audit.json"),
            variant_knockout_audit_sha256=file_sha256(
                audit / "variant-knockout-audit.json"
            ),
            solver_patch_sha256=spec["validation"]["solver_patch_sha256"],
            solver_after_sha256=file_sha256(
                native / "wholecell/utils/_netflow/nf_glpk.py"
            ),
        )
        manifest["workflow"].update(
            config_path=str(folder / "workflow.json"),
            config_sha256=file_sha256(folder / "workflow.json"),
            experiment_id=config["experiment_id"],
            output_root=config["emitter_arg"]["out_dir"],
            sim_data_path=str(sim),
            sim_data_sha256=file_sha256(sim),
        )
        manifest["tracking"].update(
            run_id=hashlib.sha256(exp.encode()).hexdigest()[:16],
            run_name=exp,
            artifact_name=exp + "-data",
            s3_prefix=tracking_prefix + label,
            tags=["replication", "lineage20", "strict-solver-recovery", label],
        )
        manifest["solver_policy"] = spec["validation"]["numerical_policy"]
        _validate_manifest_provenance(manifest)
        atomic_json(folder / "manifest.json", manifest)
        atomic_json(
            folder / "experiments.json",
            {"experiments": [{"id": exp, "status": "prepared", "data_backup": {}}]},
        )
    validation_spec = {
        "experiment_id": validation_id,
        "seeds": seed_labels[1:],
        "calibration_label": calibration_label,
        "tracking": {
            "s3_bucket": "phd-research-storage-1758274488",
            "s3_prefix": tracking_prefix.removeprefix(
                "s3://phd-research-storage-1758274488/"
            ),
        },
    }
    atomic_json(valid / "protocol.json", validation_spec)
    atomic_json(
        valid / "approval.json",
        {
            **approval,
            "parent_protocol_sha256": file_sha256(bundle / "protocol.json"),
            "protocol_sha256": file_sha256(valid / "protocol.json"),
        },
    )
    atomic_json(
        valid / "prepared.json",
        {
            "status": "passed",
            "genotypes": [
                {
                    k: x[k]
                    for k in (
                        "name",
                        "state_id",
                        "variant_index",
                        "deletion_count",
                        "deletion_set_sha256",
                    )
                }
                for x in finalists
            ],
            "manifest_sha256": {
                label: file_sha256(valid / "runs" / label / "manifest.json")
                for label in labels
            },
        },
    )
    # This preparation record does not bypass the independent initial checkpoint
    # inside each controller, or the supervisor's two-generation calibration.
    atomic_json(
        valid / "preflight.json",
        {
            "status": "passed",
            "knockouts": file_sha256(audit / "variant-knockout-audit.json"),
            "nominations": file_sha256(nomination_path),
            "initial_backups_required_by_controllers": True,
        },
    )
    print(
        json.dumps(
            {
                "status": "prepared",
                "genotypes": len(finalists),
                "lineages": len(finalists) * len(seed_labels[1:]),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
