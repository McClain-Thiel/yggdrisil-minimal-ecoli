#!/usr/bin/env python3
"""Run the fixed biological sanity panel against local real artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path

from yggdrisil import EvaluationResult, EvaluatorSuite

from yggdrisil_ecoli.data.registry import file_sha256
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.search import SearchArtifacts, load_standard_evaluators
from yggdrisil_ecoli.state import GenomeState


async def validate(data_dir: Path, seed: int) -> dict[str, object]:
    artifacts = SearchArtifacts(data_dir)
    registry, _essentiality, evaluators = load_standard_evaluators(artifacts)
    problem = EcoliProblem(registry)
    suite = EvaluatorSuite(list(evaluators), concurrent=True)
    rng = random.Random(seed)
    universe = sorted(registry.search_universe)
    cases = {
        "wild_type": frozenset(),
        "known_essential_dnaA": frozenset({"b3702"}),
        "resource_essential_rpsT": frozenset({"b0023"}),
        "known_nonessential_thrA": frozenset({"b0002"}),
        "metabolic_and_rule_trpA": frozenset({"b1260"}),
        "random_5": frozenset(rng.sample(universe, 5)),
        "random_20": frozenset(rng.sample(universe, 20)),
    }
    evaluated: dict[str, dict[str, EvaluationResult]] = {}
    report_cases: dict[str, object] = {}
    for case, deleted in cases.items():
        state = GenomeState(deleted)
        problem.state_key(state)
        results = await suite.evaluate(state)
        evaluated[case] = {
            evaluator.name: result
            for evaluator, result in zip(evaluators, results, strict=True)
        }
        report_cases[case] = {
            "deleted_genes": sorted(deleted),
            "evaluations": {
                name: {"metrics": result.metrics, **result.metadata}
                for name, result in evaluated[case].items()
            },
        }

    failures: list[str] = []
    wild_type = evaluated["wild_type"]
    if wild_type["genome_size"].metrics != {
        "genes_deleted": 0,
        "genes_remaining": len(registry),
    }:
        failures.append("wild-type genome size disagrees with the registry")
    if wild_type["module_retention"].metrics["n_broken"] != 0:
        failures.append("wild type breaks a frozen WT-complete module")
    wild_growth = wild_type["fba"].metrics["growth_rate"]
    if not isinstance(wild_growth, (int, float)) or wild_growth <= 0:
        failures.append("wild type has no positive FBA biomass solution")
    if wild_type["resource_allocation"].metrics["feasible_at_growth_floor"] is not True:
        failures.append("wild type is infeasible at the RBA growth floor")
    if evaluated["metabolic_and_rule_trpA"]["fba"].metrics["growth_rate"] != 0.0:
        failures.append("b1260 deletion did not eliminate iML1515 biomass")
    dna_a_coverage = evaluated["known_essential_dnaA"]["fba"].metadata["coverage"]
    if not isinstance(dna_a_coverage, dict) or (
        dna_a_coverage.get("deleted_genes_unmodeled") != 1
    ):
        failures.append("non-model b3702 was not reported as uncovered by FBA")
    if (
        evaluated["resource_essential_rpsT"]["resource_allocation"].metrics[
            "feasible_at_growth_floor"
        ]
        is not False
    ):
        failures.append("b0023 deletion did not fail the RBA growth floor")
    neutral_resource = evaluated["known_nonessential_thrA"]["resource_allocation"]
    if neutral_resource.metrics["feasible_at_growth_floor"] is not True:
        failures.append("modeled b0002 control did not pass the RBA growth floor")
    neutral_coverage = neutral_resource.metadata["coverage"]
    if not isinstance(neutral_coverage, dict) or (
        neutral_coverage.get("deleted_genes_modeled") != 1
    ):
        failures.append("b0002 was not reported as modeled by RBA")
    rps_t_fba_coverage = evaluated["resource_essential_rpsT"]["fba"].metadata[
        "coverage"
    ]
    if not isinstance(rps_t_fba_coverage, dict) or (
        rps_t_fba_coverage.get("deleted_genes_unmodeled") != 1
    ):
        failures.append("FBA unexpectedly modeled the RBA-only b0023 control")

    return {
        "schema_version": 3,
        "seed": seed,
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "artifacts": {
            "registry_sha256": file_sha256(artifacts.registry),
            "essentiality_sha256": file_sha256(artifacts.essentiality),
            "kegg_modules_sha256": file_sha256(artifacts.kegg_modules),
            "iml1515_sha256": file_sha256(artifacts.iml1515),
            "rba_manifest_sha256": file_sha256(
                artifacts.rba / "rba_artifact_manifest.json"
            ),
        },
        "cases": report_cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    output = args.output or args.data_dir / "processed" / "environment_validation.json"
    report = asyncio.run(validate(args.data_dir, args.seed))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"environment validation: {report['status']} ({output})")
    failures = report["failures"]
    if not isinstance(failures, list):
        raise TypeError("malformed validation failures")
    for failure in failures:
        print(f"- {failure}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
