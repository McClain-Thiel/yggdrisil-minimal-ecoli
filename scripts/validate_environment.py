#!/usr/bin/env python3
"""Run pre-agent biological sanity checks and write a human-readable report."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path
from typing import Any, cast

from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import ScoreCache, ScorerSuite
from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
from yggdrisil_ecoli.scorers.fba import FBAEvaluator, FBAScorer
from yggdrisil_ecoli.scorers.modules import ModuleCatalog, ModuleRetentionScorer
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.state import GenomeState


async def validate(args: argparse.Namespace) -> dict[str, Any]:
    registry = GeneRegistry.from_parquet(args.registry)
    essentiality = EssentialityDataset.from_parquet(
        args.essentiality_summary, args.essentiality_observations
    )
    modules = ModuleCatalog.from_json(args.kegg_modules)
    fba = FBAEvaluator(model_path=args.iml1515, registry=registry)
    problem = EcoliProblem(registry)
    cache = ScoreCache(args.cache)
    suite = ScorerSuite(
        (
            GenomeSizeScorer(registry),
            EssentialityScorer(
                registry=registry,
                dataset=essentiality,
                artifact_hash=file_sha256(args.essentiality_summary),
            ),
            ModuleRetentionScorer(
                registry=registry,
                catalog=modules,
                artifact_hash=file_sha256(args.kegg_modules),
            ),
            FBAScorer(fba),
        ),
        cache,
    )
    rng = random.Random(args.seed)
    universe = sorted(registry.search_universe)
    cases = {
        "wild_type": frozenset(),
        "known_essential_dnaA": frozenset({"b3702"}),
        "known_nonessential_thrA": frozenset({"b0002"}),
        "metabolic_and_rule_trpA": frozenset({"b1260"}),
        "random_5": frozenset(rng.sample(universe, 5)),
        "random_20": frozenset(rng.sample(universe, 20)),
    }
    results: dict[str, Any] = {}
    for name, deleted in cases.items():
        state = GenomeState(deleted)
        # Exercise the same canonical key from both the problem and scorer suite.
        problem.state_key(state)
        scored = await suite.score(state)
        results[name] = {
            "deleted_genes": sorted(deleted),
            "scores": {
                scorer_name: result.model_dump(mode="json")
                for scorer_name, result in scored.items()
            },
        }
    cache.close()

    failures = []
    wild_type = _scores(results, "wild_type")
    if wild_type["genome_size"]["metrics"] != {
        "genes_deleted": 0,
        "genes_remaining": len(registry),
    }:
        failures.append("wild-type genome size is inconsistent with the registry")
    if wild_type["module_retention"]["metrics"]["n_broken"] != 0:
        failures.append("wild type breaks one or more frozen WT-complete modules")
    wild_growth = wild_type["fba"]["metrics"]["growth_rate"]
    if not isinstance(wild_growth, (int, float)) or wild_growth <= 0:
        failures.append("wild type has no positive FBA biomass solution")
    trp_a_growth = _scores(results, "metabolic_and_rule_trpA")["fba"]["metrics"][
        "growth_rate"
    ]
    if trp_a_growth != 0.0:
        failures.append("b1260 deletion did not eliminate biomass in iML1515")
    dna_a_coverage = _scores(results, "known_essential_dnaA")["fba"]["coverage"]
    if dna_a_coverage["deleted_genes_unmodeled"] != 1:
        failures.append("non-model b3702 was not reported as uncovered by FBA")
    return {
        "schema_version": 1,
        "seed": args.seed,
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "artifacts": {
            "registry_sha256": file_sha256(args.registry),
            "essentiality_summary_sha256": file_sha256(args.essentiality_summary),
            "kegg_modules_sha256": file_sha256(args.kegg_modules),
            "iml1515_sha256": file_sha256(args.iml1515),
        },
        "cases": results,
    }


def _scores(results: dict[str, Any], case: str) -> dict[str, Any]:
    case_result = results[case]
    if not isinstance(case_result, dict) or not isinstance(
        case_result.get("scores"), dict
    ):
        raise TypeError(f"malformed validation result for {case}")
    return cast(dict[str, Any], case_result["scores"])


def _render_text(report: dict[str, Any]) -> str:
    lines = [
        "Yggdrisil Minimal E. coli — environment validation",
        f"status: {report['status']}",
        f"seed: {report['seed']}",
        "",
    ]
    cases = report["cases"]
    if not isinstance(cases, dict):
        raise TypeError("malformed validation report")
    for name, raw_case in cases.items():
        if not isinstance(raw_case, dict) or not isinstance(
            raw_case.get("scores"), dict
        ):
            raise TypeError(f"malformed validation case: {name}")
        scores = raw_case["scores"]
        size = scores["genome_size"]["metrics"]
        essentiality = scores["essentiality"]["metrics"]
        modules = scores["module_retention"]["metrics"]
        fba = scores["fba"]["metrics"]
        lines.extend(
            [
                str(name),
                f"  deleted: {size['genes_deleted']}",
                (
                    "  essentiality: "
                    f"essential={essentiality['n_essential_deleted']}, "
                    "conditional="
                    f"{essentiality['n_conditional_essential_deleted']}, "
                    f"unknown={essentiality['n_unknown_deleted']}"
                ),
                (
                    f"  modules: complete={modules['n_complete']}, "
                    f"broken={modules['n_broken']}"
                ),
                (f"  FBA: feasible={fba['feasible']}, growth={fba['growth_rate']}"),
                "",
            ]
        )
    failures = report["failures"]
    if failures:
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in failures)
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("data/processed/gene_registry.parquet"),
    )
    parser.add_argument(
        "--essentiality-summary",
        type=Path,
        default=Path("data/processed/essentiality_summary.parquet"),
    )
    parser.add_argument(
        "--essentiality-observations",
        type=Path,
        default=Path("data/processed/essentiality_observations.parquet"),
    )
    parser.add_argument(
        "--kegg-modules",
        type=Path,
        default=Path("data/processed/kegg_modules.json"),
    )
    parser.add_argument(
        "--iml1515", type=Path, default=Path("data/external/iML1515.json")
    )
    parser.add_argument(
        "--cache", type=Path, default=Path("data/interim/validation_cache.sqlite")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/processed/environment_validation")
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    report = asyncio.run(validate(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    args.output.with_suffix(".txt").write_text(_render_text(report))
    print(_render_text(report), end="")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
