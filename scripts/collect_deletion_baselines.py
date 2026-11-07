#!/usr/bin/env python3
"""Build a compact study catalog; accept only results in verified checkpoints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.run_deletion_baseline import load_bundle
from yggdrisil_ecoli.data.io import atomic_json, file_sha256


def collect(bundle: Path) -> dict[str, Any]:
    spec, _ = load_bundle(bundle, 0)
    jobs = json.loads((bundle / "jobs.json").read_text())
    rows, nominations = [], {}
    for i in range(len(jobs)):
        _, job = load_bundle(bundle, i)
        folder = bundle / job["directory"]
        row = {**job, "status": "not_started"}
        completion = folder / "completion.json"
        if completion.exists():
            done = json.loads(completion.read_text())
            ref = done["checkpoint"]
            result_hash = file_sha256(folder / "result.json")
            members = {m["path"]: m["sha256"] for m in ref["files"]}
            if (
                done["status"] != "completed_verified" or ref["status"] != "verified"
                or ref.get("full_get_verified") is not True
                or ref.get("wandb_download_verified") is not True
                or done["result_sha256"] != result_hash
                or members.get("result.json") != result_hash
                or members.get("protocol.json") != file_sha256(bundle / "protocol.json")
            ):
                raise ValueError(f"Unverified or changed result: {job['run_id']}")
            report = json.loads((folder / "result.json").read_text())
            row.update(status="completed_verified", result_sha256=result_hash,
                       checkpoint=ref, stop_reason=report["stop_reason"],
                       new_evaluated_states=report["new_evaluated_states"],
                       finalist=report["finalist"])
            finalist = report["finalist"]
            if finalist:
                nomination = nominations.setdefault(finalist["state_id"], {
                    "state_id": finalist["state_id"],
                    "deleted_gene_ids": finalist["deleted_gene_ids"],
                    "genes_deleted": finalist["genes_deleted"], "source_run_ids": [],
                    "validation_status": "not_launched",
                })
                nomination["source_run_ids"].append(job["run_id"])
        elif (folder / "progress.json").exists():
            row.update(json.loads((folder / "progress.json").read_text()))
            row["completion_verified"] = False
        rows.append(row)
    complete = all(r["status"] == "completed_verified" for r in rows)
    return {
        "schema_version": 1, "experiment_id": spec["experiment_id"],
        "protocol_sha256": file_sha256(bundle / "protocol.json"),
        "status": "search_completed_verified" if complete else "incomplete",
        "jobs": rows, "finalist_selection_frozen": complete,
        "nominations": sorted(nominations.values(), key=lambda n: n["state_id"]),
        "interpretation": spec["analysis"]["inference_limit"],
        "validation_launch_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.bundle)
    atomic_json(args.bundle / "catalog.json", result)
    print(json.dumps({"status": result["status"], "jobs": len(result["jobs"])}))


if __name__ == "__main__":
    main()
