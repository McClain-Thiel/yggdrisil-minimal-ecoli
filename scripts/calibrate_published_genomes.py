#!/usr/bin/env python3
"""Prepare published genome projections and audit the existing growth gates."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yggdrisil import evaluator_identity

from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.data.io import atomic_json, file_sha256
from yggdrisil_ecoli.data.reduced_genomes import (
    build_benchmark_catalog,
    deletion_reachability,
)
from yggdrisil_ecoli.data.sources import SourceSpec, acquire_source
from yggdrisil_ecoli.scorers.fba import FBAScorer
from yggdrisil_ecoli.scorers.rba import RBAScorer
from yggdrisil_ecoli.state import GenomeState, genome_state_key


def fba_diagnostics(fba: FBAScorer, deleted: frozenset[str]) -> dict[str, Any]:
    """Test the published DGF bottleneck without changing production gates."""
    from cobra import Reaction

    results = {}
    for permissive in (False, True):
        for sink in (False, True):
            model = fba.model_for(deleted)
            if permissive:
                model.medium = {reaction.id: 1000.0 for reaction in model.exchanges}
            if sink:
                reaction = Reaction("SK_gcald_c")
                reaction.bounds = (0.0, 1000.0)
                reaction.add_metabolites({model.metabolites.get_by_id("gcald_c"): -1})
                model.add_reactions([reaction])
            solution = model.optimize()
            if solution.status not in {"optimal", "infeasible"}:
                raise RuntimeError(f"Diagnostic FBA solve failed: {solution.status}")
            growth = (
                float(solution.objective_value)
                if solution.status == "optimal"
                else None
            )
            if growth is not None and not math.isfinite(growth):
                raise RuntimeError("Diagnostic FBA emitted nonfinite growth")
            if growth is not None and abs(growth) < 1e-9:
                growth = 0.0
            label = ("permissive_imports" if permissive else "m9") + (
                "_with_sink" if sink else ""
            )
            results[label] = {"growth_rate_h": growth, "solver_status": solution.status}
    return {
        "source_doi": "10.1128/mbio.00873-24",
        "interpretation": "Artificial sink and unrestricted exchange tests are diagnostics, not physiological validation or reconstructed AM3 medium.",
        "production_evaluators_changed": False,
        "tests": results,
    }


async def evaluate_projections(
    catalog: dict[str, Any], data_dir: Path
) -> dict[str, Any]:
    """Keep diagnostic disagreement separate from native-strain nonviability."""
    genes = load_genes(data_dir / "processed/genes.parquet")
    fba = FBAScorer(model_path=data_dir / "external/iML1515.json", genes=genes)
    rba = RBAScorer(artifact_dir=data_dir / "external/rba_ecoli_k12_wt", genes=genes)
    cases = {}

    async def evaluate(deleted: frozenset[str]) -> dict[str, Any]:
        state = GenomeState(deleted)
        flux, resource = await asyncio.gather(fba.evaluate(state), rba.evaluate(state))
        growth = flux.metrics["growth_rate"]
        passed = (
            flux.metrics["feasible"] is True
            and isinstance(growth, (int, float))
            and growth > 0
            and resource.metrics["feasible_at_growth_floor"] is True
        )
        return {
            "state_id": genome_state_key(state),
            "protein_coding_targets": len(state.deleted_genes),
            "passes_current_growth_gates": passed,
            "native_strain_viability_established": False,
            "interpretation": (
                "Deletion projection passes the current screening gates."
                if passed
                else "Deletion projection disagrees with current screening gates; this does not establish native-strain nonviability."
            ),
            "fba": {"metrics": flux.metrics, "metadata": flux.metadata},
            "resource_allocation": {
                "metrics": resource.metrics,
                "metadata": resource.metadata,
            },
            "fba_diagnostics": fba_diagnostics(fba, state.deleted_genes),
        }

    for name, strain in catalog["strains"].items():
        complete = frozenset(strain["deleted_gene_ids"])
        cases[name] = await evaluate(complete)
        partial = frozenset(strain["partially_deleted_gene_ids"])
        if partial:
            cases[name]["partial_gene_sensitivity"] = await evaluate(complete | partial)
            cases[name]["partial_gene_sensitivity"]["interpretation"] = (
                "Treat every partially deleted gene as a full knockout; compare this "
                "with keeping all partial genes active. Neither assumption establishes "
                "the biological function of the remaining gene fragments."
            )
        print(
            f"{name}: {len(complete)} complete targets, {len(partial)} partial; "
            f"FBA {cases[name]['fba']['metrics']['growth_rate']}/h; RBA "
            f"{cases[name]['resource_allocation']['metrics']['feasible_at_growth_floor']}",
            flush=True,
        )
    return {
        "purpose": "existing-model diagnostics on published deletion projections",
        "environment": {
            "fba": "existing aerobic M9/glucose configuration",
            "resource_allocation": "pinned K-12 model medium, growth floor 0.1/h",
            "published_media_reconstructed": False,
            "published_background_alleles_applied": False,
        },
        "native_strain_calibration_ready": False,
        "limitations": [
            "These are diagnostic gene-set projections, not exact native-strain calibration.",
            "The existing evaluators do not reconstruct AM3 or apply evolved/repaired alleles.",
            "Partial genes are excluded; substitutions and reference gaps below 1000 bp are not knockout actions.",
        ],
        "evaluators": {
            scorer.name: {
                "identity": evaluator_identity(scorer)[0],
                "config": scorer.config,
            }
            for scorer in (fba, rba)
        },
        "cases": cases,
    }


def prepare(output: Path, source_manifest: Path, data_dir: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    sources = json.loads(source_manifest.read_text())
    source_dir = output / "sources"
    for spec in [sources["reference"], sources["ms56"], *sources["genomes"].values()]:
        acquire_source(
            SourceSpec(spec["url"], spec["filename"], spec["sha256"]), source_dir
        )
    catalog = build_benchmark_catalog(
        genes_path=data_dir / "processed/genes.parquet",
        source_manifest=source_manifest,
        source_dir=source_dir,
    )
    atomic_json(output / "catalog.json", catalog)
    strains = catalog["strains"]
    reachability: dict[str, dict[str, Any]] = {}
    for start, initial in strains.items():
        reachability[start] = {}
        for target, destination in strains.items():
            if start == target:
                continue
            a, b = (
                set(initial["deleted_gene_ids"]),
                set(destination["deleted_gene_ids"]),
            )
            result = deletion_reachability(a, b)
            uncertain = set(destination["partially_deleted_gene_ids"])
            result["definite_genes_requiring_restoration"] = sorted(a - b - uncertain)
            result["definite_restoration_count"] = len(a - b - uncertain)
            result["ambiguous_partial_gene_overlap"] = sorted(a & uncertain)
            reachability[start][target] = result
    atomic_json(output / "reachability.json", reachability)
    report = asyncio.run(evaluate_projections(catalog, data_dir))
    report["catalog_sha256"] = file_sha256(output / "catalog.json")
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["source_code_sha256"] = {
        str(path): file_sha256(path)
        for path in [
            Path(__file__),
            Path(__file__).parents[1] / "src/yggdrisil_ecoli/data/reduced_genomes.py",
        ]
    }
    atomic_json(output / "evaluations.json", report)
    shutil.copyfile(source_manifest, output / "source_manifest.json")
    with tarfile.open(output / "sources.tar.gz", "w:gz") as archive:
        for spec in [
            sources["reference"],
            sources["ms56"],
            *sources["genomes"].values(),
        ]:
            archive.add(source_dir / spec["filename"], arcname=spec["filename"])
    return [
        output / name
        for name in [
            "catalog.json",
            "reachability.json",
            "evaluations.json",
            "source_manifest.json",
            "sources.tar.gz",
        ]
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=Path("benchmarks/published_genomes.json"),
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    if not os.environ.get("WANDB_API_KEY"):
        raise RuntimeError("WANDB_API_KEY is required for benchmark logging")
    import wandb

    with wandb.init(
        entity=os.environ.get("WANDB_ENTITY"),
        project="yggdrisil",
        name=args.output.name,
        job_type="published-genome-calibration",
        tags=["yggdrisil", "calibration", "published-genomes"],
        dir=str(args.output.parent),
        config={
            "source_manifest_sha256": file_sha256(args.source_manifest),
            "model_calls": 0,
            "edison_jobs": 0,
        },
        settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False),
    ) as run:
        atomic_json(
            args.output / "tracking.json",
            {"wandb_run_id": run.id, "wandb_run_url": run.url},
        )
        files = prepare(args.output, args.source_manifest, args.data_dir)
        artifact = wandb.Artifact(args.output.name + "-data", type="experiment-data")
        for path in files:
            artifact.add_file(str(path), name=path.name)
        logged = run.log_artifact(artifact, aliases=["latest", "calibration"])
        logged.wait()
        for path in files:
            if path.suffix == ".json":
                downloaded = logged.get_entry(path.name).download(
                    root=str(args.output / "backup-verification")
                )
                if file_sha256(downloaded) != file_sha256(path):
                    raise RuntimeError(f"Backup verification failed: {path.name}")
        atomic_json(
            args.output / "backup.json",
            {
                "artifact": logged.qualified_name,
                "wandb_run_url": run.url,
                "verified_json_files": [p.name for p in files if p.suffix == ".json"],
                "source_archive_sha256": file_sha256(args.output / "sources.tar.gz"),
            },
        )
        report = json.loads((args.output / "evaluations.json").read_text())
        run.summary["native_strain_calibration_ready"] = False
        for name, result in report["cases"].items():
            run.summary[f"{name}/passes_growth_gates"] = result[
                "passes_current_growth_gates"
            ]
            run.summary[f"{name}/fba_growth_rate"] = result["fba"]["metrics"][
                "growth_rate"
            ]
        run.summary["data_artifact"] = logged.qualified_name
        print(f"Saved benchmark diagnostics: {args.output}; {run.url}")


if __name__ == "__main__":
    main()
