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
from pathlib import Path
from typing import Any

from yggdrisil import EvaluationResult, evaluator_identity
from yggdrisil.serialize import loads

from yggdrisil_ecoli import __version__
from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.registry import file_sha256
from yggdrisil_ecoli.search import SearchArtifacts, load_standard_evaluators
from yggdrisil_ecoli.state import GenomeState, genome_state_key


async def calibrate(
    *,
    artifacts: SearchArtifacts,
    controls_path: Path,
    prior_graph_path: Path,
    prior_state_id: str,
) -> dict[str, object]:
    """Evaluate two positive controls and the prior FBA-only design."""

    controls = _json_object(controls_path)
    if controls.get("agent_visible") is not False:
        raise ValueError("reduced-genome controls must be marked agent_visible=false")
    strains = controls.get("strains")
    if not isinstance(strains, dict):
        raise ValueError("reduced-genome controls lack strains")
    cases = {
        name: _deleted_gene_ids(_mapping(strains, name)) for name in ("MDS42", "MS56")
    }
    prior_graph_files = _frozen_sqlite_hashes(prior_graph_path)
    prior_state = _read_state_read_only(prior_graph_path, prior_state_id)
    cases["prior_fba_only_candidate"] = prior_state.deleted_genes

    _registry, _essentiality, evaluators = load_standard_evaluators(artifacts)
    selected = {
        evaluator.name: evaluator
        for evaluator in evaluators
        if evaluator.name in {"fba", "resource_allocation"}
    }
    if set(selected) != {"fba", "resource_allocation"}:
        raise ValueError("standard evaluator suite lacks FBA or RBA")

    case_results: dict[str, object] = {}
    for name, deleted_genes in cases.items():
        state = GenomeState(frozenset(deleted_genes))
        started = time.monotonic()
        fba_result, resource_result = await asyncio.gather(
            selected["fba"].evaluate(state),
            selected["resource_allocation"].evaluate(state),
        )
        case_results[name] = {
            "deleted_genes": len(deleted_genes),
            "deleted_gene_ids": sorted(deleted_genes),
            "deleted_gene_set_sha256": _json_sha256(sorted(deleted_genes)),
            "elapsed_s": time.monotonic() - started,
            "fba": _compact_result(fba_result),
            "resource_allocation": _compact_result(resource_result),
        }

    positive_controls_pass = all(
        _fba_positive(case_results[name]) and _resource_feasible(case_results[name])
        for name in ("MDS42", "MS56")
    )
    all_cases_fba_positive = all(_fba_positive(case) for case in case_results.values())
    prior_candidate_rejected = not _resource_feasible(
        case_results["prior_fba_only_candidate"]
    )
    if (
        not positive_controls_pass
        or not all_cases_fba_positive
        or not prior_candidate_rejected
    ):
        raise RuntimeError("RBA discriminator calibration did not meet its contract")

    return {
        "schema_version": 1,
        "purpose": "pre-run evaluator calibration; not held-out validation",
        "agent_visible_during_search": False,
        "selection_timing": "before paid resource-gated search",
        "status": "passed",
        "assertions": {
            "positive_controls_feasible": positive_controls_pass,
            "all_cases_fba_positive": all_cases_fba_positive,
            "prior_fba_only_candidate_rejected": prior_candidate_rejected,
        },
        "application": {
            "distribution": f"yggdrisil-ecoli=={__version__}",
            "git_commit": _git_output("rev-parse", "HEAD"),
            "working_tree_dirty": bool(_git_output("status", "--porcelain")),
            "source_tree_sha256": _source_tree_sha256(),
        },
        "artifacts": {
            "registry_sha256": file_sha256(artifacts.registry),
            "essentiality_sha256": file_sha256(artifacts.essentiality),
            "kegg_modules_sha256": file_sha256(artifacts.kegg_modules),
            "iml1515_sha256": file_sha256(artifacts.iml1515),
            "rba_manifest_sha256": file_sha256(
                artifacts.rba / "rba_artifact_manifest.json"
            ),
            "reduced_genome_controls_sha256": file_sha256(controls_path),
            "prior_graph_files": prior_graph_files,
            "prior_state_id": prior_state_id,
        },
        "control_sources": {
            name: _mapping(_mapping(strains, name), "source")
            for name in ("MDS42", "MS56")
        },
        "evaluators": {
            name: {
                "evaluator_id": evaluator_identity(evaluator)[0],
                "config_sha256": evaluator_identity(evaluator)[1],
            }
            for name, evaluator in sorted(selected.items())
        },
        "cases": case_results,
    }


def _compact_result(result: EvaluationResult) -> dict[str, object]:
    coverage = result.metadata.get("coverage")
    provenance = result.metadata.get("provenance")
    return {
        "metrics": result.metrics,
        "coverage": coverage if isinstance(coverage, dict) else {},
        "provenance": provenance if isinstance(provenance, dict) else {},
    }


def _resource_feasible(case: object) -> bool:
    if not isinstance(case, dict):
        return False
    resource = case.get("resource_allocation")
    if not isinstance(resource, dict):
        return False
    metrics = resource.get("metrics")
    return isinstance(metrics, dict) and metrics.get("feasible_at_growth_floor") is True


def _fba_positive(case: object) -> bool:
    if not isinstance(case, dict):
        return False
    fba = case.get("fba")
    if not isinstance(fba, dict):
        return False
    metrics = fba.get("metrics")
    if not isinstance(metrics, dict) or metrics.get("feasible") is not True:
        return False
    growth = metrics.get("growth_rate")
    return (
        isinstance(growth, (int, float)) and not isinstance(growth, bool) and growth > 0
    )


def _read_state_read_only(path: Path, state_id: str) -> GenomeState:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        row = connection.execute(
            "SELECT state_json FROM states WHERE state_id = ?", (state_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError(f"prior graph has no state {state_id!r}")
    state = loads(str(row[0]))
    if not isinstance(state, GenomeState):
        raise ValueError("prior graph state is not a GenomeState")
    if genome_state_key(state) != state_id:
        raise ValueError("prior graph state payload does not match its canonical ID")
    return state


def _frozen_sqlite_hashes(path: Path) -> dict[str, str]:
    sidecars = [Path(f"{path}-wal"), Path(f"{path}-shm")]
    present = [candidate for candidate in sidecars if candidate.exists()]
    if present:
        raise ValueError(
            "prior graph must be checkpointed before calibration; remove SQLite "
            f"sidecars only after closing its writer: {[item.name for item in present]}"
        )
    return {path.name: file_sha256(path)}


def _json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    candidate = value.get(key)
    if not isinstance(candidate, dict):
        raise ValueError(f"calibration input lacks object {key!r}")
    return candidate


def _deleted_gene_ids(value: dict[str, Any]) -> frozenset[str]:
    candidate = value.get("deleted_gene_ids")
    if not isinstance(candidate, list) or any(
        not isinstance(item, str) for item in candidate
    ):
        raise ValueError("calibration control lacks deleted_gene_ids")
    return frozenset(candidate)


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


def _json_sha256(value: object) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _git_output(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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
            artifacts=SearchArtifacts(args.data_dir),
            controls_path=args.controls,
            prior_graph_path=args.prior_graph,
            prior_state_id=args.prior_state_id,
        )
    )
    atomic_json(args.output, report)
    print(f"resource-gate calibration: {report['status']} ({args.output})")


if __name__ == "__main__":
    main()
