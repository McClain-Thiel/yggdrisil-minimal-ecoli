#!/usr/bin/env python3
"""Prepare a local, non-executable pilot bundle from existing scientific inputs.

No model requests, Edison jobs, W&B uploads, or cloud operations occur here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

ARMS = {
    "no_retrieval": (False, False),
    "frozen_dossier": (True, False),
    "adaptive_literature": (True, True),
}


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def checked_file(root: Path, reference: dict[str, str]) -> Path:
    path = root / reference["path"]
    if file_sha256(path) != reference["sha256"]:
        raise ValueError(f"Input checksum changed: {path}")
    return path


def frozen_dossier(root: Path, source: dict[str, str]) -> dict[str, Any]:
    path = (root / source["cache_path"]).resolve()
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT query, status, result_json FROM edison_queries "
            "WHERE namespace=? AND task_id=?",
            (source["namespace"], source["task_id"]),
        ).fetchall()
    if len(rows) != 1 or rows[0][1] != "success":
        raise ValueError("Expected exactly one successful cached Edison report")
    query, _, raw = rows[0]
    result = json.loads(raw)
    answer = result["formatted_answer"]
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Cached Edison report has no answer")
    if hashlib.sha256(answer.encode()).hexdigest() != source["answer_sha256"]:
        raise ValueError("Edison dossier checksum changed")
    return {
        "query": query,
        "content": answer,
        "source_url": f"https://platform.edisonscientific.com/trajectories/{source['task_id']}",
        "task_id": source["task_id"],
        "answer_sha256": source["answer_sha256"],
        "reported_original_cost": result.get("total_cost"),
        "cost_note": "Missing original cost is unknown, not zero; this preparation submits no jobs.",
        "scope": "Unedited cached report; excludes previous agent plans and search outcomes.",
    }


def build_jobs(
    spec: dict[str, Any], starting_gene_set_sha256: str
) -> list[dict[str, Any]]:
    if spec["schema_version"] != 1 or spec["status"] != "draft":
        raise ValueError("This planner supports version 1 draft protocols only")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", spec["experiment_id"]):
        raise ValueError("experiment_id must be a lowercase path-safe identifier")
    if len(spec["arms"]) != len(ARMS) or set(spec["arms"]) != set(ARMS):
        raise ValueError("Expected the three distinct literature ablation arms")
    seeds = spec["search_seeds"]
    if (
        not seeds
        or any(type(seed) is not int or seed < 0 for seed in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        raise ValueError("Search seeds must be distinct nonnegative integers")
    common = spec["agent"]
    if common["orchestration"] != "manager" or common["edison"] is not False:
        raise ValueError("All arms require the same manager with live Edison disabled")
    if {
        "mode",
        "seed",
        "session_path",
        "edison_cache_path",
        "require_initial_research",
    } & common.keys():
        raise ValueError(
            "Arm mode, seed and isolated storage are assigned by the planner"
        )
    if spec["spending"]["new_edison_jobs"] != 0:
        raise ValueError("This pilot must reuse the frozen dossier without Edison jobs")
    if type(spec["search"].get("require_initial_research", False)) is not bool:
        raise ValueError("search.require_initial_research must be a boolean")
    for key in (
        "unique_new_states",
        "max_steps",
        "max_wall_time_s",
        "finalists_per_run",
    ):
        value = spec["search"][key]
        if type(value) is not int or value < 1:
            raise ValueError(f"search.{key} must be a positive integer")
    jobs = []
    for index, seed in enumerate(seeds):
        offset = index % len(ARMS)
        arm_order = spec["arms"][offset:] + spec["arms"][:offset]
        for arm in arm_order:
            dossier, live = ARMS[arm]
            run_id = f"{spec['experiment_id']}-{arm.replace('_', '-')}-seed-{seed}"
            folder = f"search/{run_id}"
            jobs.append(
                {
                    "run_id": run_id,
                    "arm": arm,
                    "seed": seed,
                    "status": "planned",
                    "starting_gene_set_sha256": starting_gene_set_sha256,
                    "starting_genotype": "inputs/starting_genotype.json",
                    "dossier": "inputs/dossier.json" if dossier else None,
                    "live_literature": live,
                    "agent_config": {
                        **deepcopy(common),
                        "mode": "research" if live else "tool-rich",
                        "require_initial_research": live
                        and spec["search"].get("require_initial_research", False),
                        "seed": seed,
                        "session_path": f"{folder}/agents.sqlite",
                        "edison_cache_path": f"{folder}/edison-disabled.sqlite",
                    },
                    "graph_path": f"{folder}/graph.sqlite",
                    "limits": {
                        "max_states": spec["search"]["unique_new_states"] + 1,
                        "max_steps": spec["search"]["max_steps"],
                        "max_wall_time_s": spec["search"]["max_wall_time_s"],
                    },
                }
            )
    return jobs


def prepare(config: Path, output: Path, root: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite an existing bundle: {output}")
    spec = json.loads(config.read_text())
    for reference in spec["inputs"].values():
        checked_file(root, reference)
    start = spec["starting_genotype"]
    catalog = json.loads(checked_file(root, start["catalog"]).read_text())
    if (
        catalog["reference"]["genes_sha256"] != spec["inputs"]["genes"]["sha256"]
        or catalog["reference"]["accession"] != start["reference"]
    ):
        raise ValueError("Starting catalog and planned gene registry/reference differ")
    strain = catalog["strains"][start["strain"]]
    identifiers = strain["deleted_gene_ids"]
    if not identifiers or any(
        not isinstance(gene, str) or not re.fullmatch(r"b[0-9]{4}", gene)
        for gene in identifiers
    ):
        raise ValueError("Expected nonempty canonical MG1655 b-number deletions")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Starting genotype contains duplicate gene IDs")
    if len(identifiers) != start["expected_deletion_count"]:
        raise ValueError("Starting genotype deletion count changed")
    # Hash the input gene list; the runner computes its own graph state identity.
    gene_set_sha256 = hashlib.sha256(
        json.dumps(sorted(identifiers), separators=(",", ":")).encode()
    ).hexdigest()
    dossier = frozen_dossier(root, spec["dossier"])
    jobs = build_jobs(spec, gene_set_sha256)
    evaluation = spec["validation"]
    if evaluation["followed_daughters"] != 1:
        raise ValueError(
            "This pilot follows one daughter lineage, not a branching colony"
        )
    lineages = (
        len(jobs) * spec["search"]["finalists_per_run"] + len(evaluation["controls"])
    ) * len(evaluation["lineage_seeds"])
    manifest = {
        "experiment_id": spec["experiment_id"],
        "status": "planned",
        "launch_ready": False,
        "protocol_sha256": file_sha256(config),
        "input_references": spec["inputs"],
        "search_runs": len(jobs),
        "maximum_new_states": len(jobs) * spec["search"]["unique_new_states"],
        "maximum_validation_lineages": lineages,
        "maximum_validation_cell_cycles": lineages * evaluation["generations"],
        "validation_workload_note": "Upper bound before deduplication and early nondivision; excludes infrastructure retries and deployment smoke tests.",
        "pending_before_execution": spec["pending_before_execution"],
        "storage_note": "All job paths are relative to this bundle; scientific model files are referenced, not copied. No executable launcher is included.",
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "protocol.json", spec)
    write_json(output / "inputs/dossier.json", dossier)
    write_json(
        output / "inputs/starting_genotype.json",
        {
            "name": start["strain"],
            "reference": start["reference"],
            "gene_set_sha256": gene_set_sha256,
            "deleted_gene_ids": sorted(identifiers),
            "source_catalog": start["catalog"],
            "representation": strain["representation"],
            "published_ids_outside_search_universe": strain.get(
                "published_ids_outside_search_universe", []
            ),
            "source": strain.get("source"),
        },
    )
    write_json(output / "jobs.json", jobs)
    manifest["bundle_sha256"] = {
        str(path.relative_to(output)): file_sha256(path)
        for path in sorted(output.rglob("*.json"))
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("benchmarks/literature_ablation.json")
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.config, args.output, args.repo_root)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
