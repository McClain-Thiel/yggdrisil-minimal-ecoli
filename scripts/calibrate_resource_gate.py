#!/usr/bin/env python3
"""Reproduce the pre-run RBA discriminator calibration with full provenance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import BaseModel, StrictBool
from yggdrisil import evaluator_identity
from yggdrisil.serialize import loads

from yggdrisil_ecoli import __version__
from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.data.io import atomic_json, file_sha256
from yggdrisil_ecoli.scorers.fba import FBAScorer
from yggdrisil_ecoli.scorers.rba import RBAScorer
from yggdrisil_ecoli.state import GenomeState, genome_state_key


class Control(BaseModel):
    deleted_gene_ids: list[str]
    source: dict[str, Any]


class Controls(BaseModel):
    agent_visible: StrictBool
    strains: dict[str, Control]


async def calibrate(
    *,
    data_dir: Path,
    controls_path: Path,
    prior_graph_path: Path,
    prior_state_id: str,
) -> dict[str, object]:
    """Evaluate two positive controls and the prior FBA-only design."""

    controls = Controls.model_validate_json(controls_path.read_text(), strict=True)
    if controls.agent_visible:
        raise ValueError("reduced-genome controls must be marked agent_visible=false")
    if not {"MDS42", "MS56"} <= controls.strains.keys():
        raise ValueError("calibration requires MDS42 and MS56 controls")
    cases = {
        name: frozenset(controls.strains[name].deleted_gene_ids)
        for name in ("MDS42", "MS56")
    }
    # Immutable SQLite reads must not silently ignore uncheckpointed changes.
    if any(Path(f"{prior_graph_path}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise ValueError("prior graph must be checkpointed and its writer closed")
    prior_graph_files = {prior_graph_path.name: file_sha256(prior_graph_path)}
    cases["prior_fba_only_candidate"] = _read_state_read_only(
        prior_graph_path, prior_state_id
    ).deleted_genes

    genes_path = data_dir / "processed" / "genes.parquet"
    model_path = data_dir / "external" / "iML1515.json"
    rba_dir = data_dir / "external" / "rba_ecoli_k12_wt"
    genes = load_genes(genes_path)
    evaluators = (
        FBAScorer(model_path=model_path, genes=genes),
        RBAScorer(artifact_dir=rba_dir, genes=genes),
    )
    case_results = {}
    fba_positive, resource_feasible = {}, {}
    for name, deleted_genes in cases.items():
        state = GenomeState(deleted_genes)
        started = time.monotonic()
        results = await asyncio.gather(
            *(scorer.evaluate(state) for scorer in evaluators)
        )
        fba, resource = (result.metrics for result in results)
        growth = fba.get("growth_rate")
        fba_positive[name] = (
            fba.get("feasible") is True
            and isinstance(growth, (int, float))
            and not isinstance(growth, bool)
            and growth > 0
        )
        resource_feasible[name] = resource.get("feasible_at_growth_floor") is True
        gene_ids = sorted(deleted_genes)
        case_results[name] = {
            "deleted_genes": len(gene_ids),
            "deleted_gene_ids": gene_ids,
            "deleted_gene_set_sha256": hashlib.sha256(
                json.dumps(gene_ids, separators=(",", ":")).encode()
            ).hexdigest(),
            "elapsed_s": time.monotonic() - started,
            **{
                scorer.name: {
                    "metrics": result.metrics,
                    "coverage": result.metadata["coverage"],
                    "provenance": result.metadata["provenance"],
                }
                for scorer, result in zip(evaluators, results, strict=True)
            },
        }

    assertions = {
        "positive_controls_feasible": all(
            fba_positive[name] and resource_feasible[name] for name in ("MDS42", "MS56")
        ),
        "all_cases_fba_positive": all(fba_positive.values()),
        "prior_fba_only_candidate_rejected": not resource_feasible[
            "prior_fba_only_candidate"
        ],
    }
    if not all(assertions.values()):
        raise RuntimeError("RBA discriminator calibration did not meet its contract")
    return {
        "schema_version": 2,
        "purpose": "pre-run evaluator calibration; not held-out validation",
        "agent_visible_during_search": False,
        "selection_timing": "before paid resource-gated search",
        "status": "passed",
        "assertions": assertions,
        "application": {
            "distribution": f"yggdrisil-ecoli=={__version__}",
            "git_commit": _git_output("rev-parse", "HEAD"),
            "working_tree_dirty": bool(_git_output("status", "--porcelain")),
            "source_tree_sha256": _source_tree_sha256(),
        },
        "artifacts": {
            "genes_sha256": file_sha256(genes_path),
            "iml1515_sha256": file_sha256(model_path),
            "rba_manifest_sha256": file_sha256(rba_dir / "rba_artifact_manifest.json"),
            "reduced_genome_controls_sha256": file_sha256(controls_path),
            "prior_graph_files": prior_graph_files,
            "prior_state_id": prior_state_id,
        },
        "control_sources": {
            name: controls.strains[name].source for name in ("MDS42", "MS56")
        },
        "evaluators": {
            scorer.name: dict(
                zip(
                    ("evaluator_id", "config_sha256"),
                    evaluator_identity(scorer),
                    strict=True,
                )
            )
            for scorer in evaluators
        },
        "cases": case_results,
    }


def _read_state_read_only(path: Path, state_id: str) -> GenomeState:
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    ) as connection:
        row = connection.execute(
            "SELECT state_json FROM states WHERE state_id = ?", (state_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"prior graph has no state {state_id!r}")
    state = loads(str(row[0]))
    if not isinstance(state, GenomeState):
        raise ValueError("prior graph state is not a GenomeState")
    if genome_state_key(state) != state_id:
        raise ValueError("prior graph state payload does not match its canonical ID")
    return state


def _source_tree_sha256() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parents[1]
    paths = sorted((root / "src").rglob("*.py")) + sorted(
        (root / "scripts").glob("*.py")
    )
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_output(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=Path(__file__).parents[1], text=True
    ).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--controls", type=Path, required=True)
    parser.add_argument("--prior-graph", type=Path, required=True)
    parser.add_argument("--prior-state-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(
        calibrate(
            data_dir=args.data_dir,
            controls_path=args.controls,
            prior_graph_path=args.prior_graph,
            prior_state_id=args.prior_state_id,
        )
    )
    atomic_json(args.output, report)
    print(f"resource-gate calibration: {report['status']} ({args.output})")


if __name__ == "__main__":
    main()
