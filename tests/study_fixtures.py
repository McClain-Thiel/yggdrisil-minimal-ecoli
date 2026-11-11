"""Synthetic, offline study inputs; never restore private experiments for tests."""

import json
from pathlib import Path

from scripts.plan_search_replication import digest, encoded, gene_set_sha256, state_id

REPO = Path(__file__).resolve().parents[1]


def write(root: Path, name: str, raw: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def replication_inputs(root: Path) -> Path:
    spec = json.loads((REPO / "benchmarks/search_replication.json").read_bytes())
    ids = [f"b{i:04d}" for i in range(1, 1031)]
    raw = encoded(
        {
            "name": "synthetic-test-root",
            "deleted_gene_ids": ids,
            "state_id": state_id(ids),
            "deletion_set_sha256": gene_set_sha256(ids),
        }
    )
    spec["starting_genotype"]["deletion_set_sha256"] = gene_set_sha256(ids)
    ref = spec["starting_genotype"]["local_reference"]
    ref["sha256"] = digest(raw)
    write(root, ref["path"], raw)
    for ref in spec["source_references"].values():
        write(root, ref["path"], (REPO / ref["path"]).read_bytes())
    for directory in ("scripts", "src"):
        for path in (REPO / directory).rglob("*.py"):
            write(root, str(path.relative_to(REPO)), path.read_bytes())
    protocol = root / "benchmarks/search_replication.json"
    write(root, str(protocol.relative_to(root)), encoded(spec))
    return protocol


def depth_inputs(root: Path) -> None:
    protocol = replication_inputs(root)
    raw = protocol.read_bytes()
    spec = json.loads(raw)
    prior = root / "runs/search-replication-20261109"
    write(prior, "parallel-validation/finalization/active-protocol.json", raw)
    start = root / spec["starting_genotype"]["local_reference"]["path"]
    write(prior, "inputs/starting-genotype.json", start.read_bytes())
    runs = []
    for method in ("greedy_block", "randomized_block"):
        for seed in spec["search_seeds"]:
            runs.append(
                {
                    "method": method,
                    "seed": seed,
                    "run_id": f"fixture-{method}-{seed}",
                    "status": "completed",
                    "completed_evaluations": 100,
                    "wall_time_s": 1.0,
                    "cpu_hours": 0.001,
                    "report_sha256": "1" * 64,
                    "finalist": {
                        "state_id": state_id(
                            json.loads(start.read_bytes())["deleted_gene_ids"]
                        )
                    },
                    "verification": {
                        "wandb_state": "COMMITTED",
                        "full_s3_get_verified": True,
                        "wandb_download_verified": True,
                        "protocol_sha256": digest(raw),
                        "checkpoint": {
                            "archive": {
                                "uri": "s3://fixture/data",
                                "sha256": "2" * 64,
                                "bytes": 1,
                            }
                        },
                    },
                }
            )
    write(
        prior,
        "parallel-validation/finalization/catalog/nominations.json",
        encoded({"protocol_sha256": digest(raw), "runs": runs}),
    )
    files = {
        f"application/{p.relative_to(root)}": digest(p.read_bytes())
        for p in (root / "src/yggdrisil_ecoli").rglob("*.py")
    }
    write(root, "methods/20261109/runtime-manifest.json", encoded({"files": files}))
