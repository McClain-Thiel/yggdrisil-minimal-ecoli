"""Pinned resource-balance feasibility evidence for E. coli K-12."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pandas as pd
from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import file_sha256
from yggdrisil_ecoli.rba_build import (
    MODEL_STRUCTURE_PATH,
    RBA_ARTIFACT_MANIFEST,
    RBA_EXPECTED_LP_DIMENSIONS,
    RBA_EXPECTED_REGISTRY_MAPPING,
    RBA_EXPECTED_STRUCTURE_DIMENSIONS,
    RBA_GROWTH_FLOOR_H,
    RBA_MODEL_FILES,
    RBA_MODELS_COMMIT,
    RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H,
    _dependency_versions,
    _sha256_json,
)
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState


class RBAScorer:
    """Test deletion sets at a fixed growth floor in the pinned RBA model."""

    name = "resource_allocation"
    version = "2"

    def __init__(
        self,
        *,
        artifact_dir: str | Path,
        genes: pd.DataFrame,
        solver: str = "swiglpk",
    ) -> None:
        if solver != "swiglpk":
            raise DataValidationError("RBA supports only the pinned swiglpk solver")
        artifact_dir = Path(artifact_dir)
        manifest = _validated_manifest(artifact_dir)
        provenance = manifest["provenance"]
        dependencies = _dependency_versions()
        if provenance["dependencies"] != dependencies:
            raise DataValidationError(
                "RBA artifact build dependencies differ from the pinned runtime"
            )

        import swiglpk
        from rbatools.rba_session import SessionRBA

        self._swiglpk: Any = swiglpk
        self._session: Any = SessionRBA(str(artifact_dir), lp_solver=solver)
        _glpk_problem(self._session.Problem)
        self._session.set_growth_rate(RBA_GROWTH_FLOOR_H)
        dimensions = {
            "rows": len(self._session.Problem.LP.row_names),
            "columns": len(self._session.Problem.LP.col_names),
        }
        if dimensions != RBA_EXPECTED_LP_DIMENSIONS:
            raise DataValidationError("loaded RBA LP dimensions differ from the pin")
        self._lock = threading.Lock()
        self._variables_by_gene = self._build_variable_map(genes.index)
        modeled_variables = sorted(set().union(*self._variables_by_gene.values()))
        mapping_dimensions = {
            "genes": sum(bool(value) for value in self._variables_by_gene.values()),
            "variables": len(modeled_variables),
        }
        if mapping_dimensions != RBA_EXPECTED_REGISTRY_MAPPING:
            raise DataValidationError(
                f"RBA registry mapping dimensions differ from the pin: {mapping_dimensions}"
            )
        self._base_lower_bounds = self._session.Problem.get_lb(modeled_variables)
        self._base_upper_bounds = self._session.Problem.get_ub(modeled_variables)
        self.config = {
            "artifact_bundle_sha256": manifest["artifact_bundle_sha256"],
            "provenance_sha256": manifest["provenance_sha256"],
            "model_structure_sha256": provenance["generated_files"][0]["sha256"],
            "registry_mapping_sha256": _sha256_json(
                list(self._variables_by_gene.items())
            ),
            "rba_models_commit": RBA_MODELS_COMMIT,
            "growth_rate_floor_h": RBA_GROWTH_FLOOR_H,
            "repository_wild_type_max_growth_rate_h": (
                RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H
            ),
            "model_dimensions": dimensions,
            "solver": solver,
            "dependency_versions": dependencies,
            "artifact_dependency_versions": provenance["dependencies"],
        }

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        return await asyncio.to_thread(self._evaluate, state.deleted_genes)

    def variables_for_gene(self, b_number: str) -> tuple[str, ...]:
        """Return exact LP columns; reject genes outside the supplied universe."""
        return self._variables_by_gene[b_number]

    def _evaluate(self, deleted_genes: frozenset[str]) -> EvaluationResult:
        deleted = tuple(sorted(deleted_genes))
        modeled = {
            gene: list(self._variables_by_gene[gene])
            for gene in deleted
            if self._variables_by_gene[gene]
        }
        unmodeled = [gene for gene in deleted if not self._variables_by_gene[gene]]
        knocked_out = sorted(set().union(*modeled.values()))
        with self._lock:
            status, solution_type = self._solve_with_knockouts(knocked_out)
        return scientific_evaluation(
            {
                "feasible_at_growth_floor": status in {"optimal", "feasible"},
                "growth_rate_floor_h": RBA_GROWTH_FLOOR_H,
                "repository_wild_type_max_growth_rate_h": (
                    RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H
                ),
                "solver_status": {"status": status, "solution_type": solution_type},
                "modeled_variables_by_gene": modeled,
                "knocked_out_variable_ids": knocked_out,
                "unmodeled_gene_ids": unmodeled,
            },
            coverage={
                "deleted_genes_total": len(deleted),
                "deleted_genes_modeled": len(modeled),
                "deleted_genes_unmodeled": len(unmodeled),
            },
            provenance=self.config,
        )

    def _solve_with_knockouts(self, variables: list[str]) -> tuple[str, str]:
        problem = self._session.Problem
        zero_bounds = dict.fromkeys(variables, 0.0)
        try:
            problem.set_lb(zero_bounds, log_change=False)
            problem.set_ub(zero_bounds, log_change=False)
            # GLPK can retain an invalid basis after an infeasible sibling.
            self._swiglpk.glp_std_basis(_glpk_problem(problem))
            problem.solve_lp(feasible_stati=["optimal", "feasible"])
            return str(problem.SolutionStatus), str(problem.SolutionType)
        finally:
            problem.set_ub(
                {variable: self._base_upper_bounds[variable] for variable in variables},
                log_change=False,
            )
            problem.set_lb(
                {variable: self._base_lower_bounds[variable] for variable in variables},
                log_change=False,
            )

    def _build_variable_map(self, genes: pd.Index) -> dict[str, tuple[str, ...]]:
        structure = self._session.ModelStructure
        processes = {
            name: f"{record['ID']}_machinery"
            for name, record in structure.ProcessInfo.Elements.items()
        }
        variables: dict[str, set[str]] = {gene: set() for gene in sorted(genes)}
        for protein in structure.ProteinInfo.Elements.values():
            gene = protein["ProtoID"]
            if gene in variables:
                variables[gene].update(protein["associatedEnzymes"])
                variables[gene].update(processes[p] for p in protein["SupportsProcess"])
        missing = set().union(*variables.values()) - set(
            self._session.Problem.LP.col_names
        )
        if missing:
            raise DataValidationError(
                f"RBA mapping references absent LP columns: {missing}"
            )
        # RBAtools' high-level knockouts use fuzzy column matching. Retain the
        # same protein/consumer relations, but require exact LP identifiers.
        return {gene: tuple(sorted(columns)) for gene, columns in variables.items()}


def _validated_manifest(artifact_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((artifact_dir / RBA_ARTIFACT_MANIFEST).read_text())
        if payload["schema_version"] != 1 or payload["artifact"] != "ecoli_k12_wt_rba":
            raise DataValidationError("unexpected RBA artifact identity or schema")
        provenance = payload["provenance"]
        expected = {
            "commit": RBA_MODELS_COMMIT,
            "growth_floor_h": RBA_GROWTH_FLOOR_H,
            "repository_wild_type_max_growth_rate_h": RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H,
            "model_dimensions": RBA_EXPECTED_STRUCTURE_DIMENSIONS,
        }
        if any(provenance[key] != value for key, value in expected.items()):
            raise DataValidationError(
                "RBA artifact differs from the pinned configuration"
            )
        sources = {item["path"]: item["sha256"] for item in provenance["source_files"]}
        if sources != RBA_MODEL_FILES:
            raise DataValidationError(
                "RBA artifact source inventory differs from the pin"
            )
        generated = provenance["generated_files"]
        if len(generated) != 1 or generated[0]["path"] != MODEL_STRUCTURE_PATH:
            raise DataValidationError(
                "RBA artifact must contain one generated structure"
            )
        for relative_path, sha256 in {
            **sources,
            MODEL_STRUCTURE_PATH: generated[0]["sha256"],
        }.items():
            if file_sha256(artifact_dir / relative_path) != sha256:
                raise DataValidationError(f"RBA artifact file changed: {relative_path}")
        if payload["provenance_sha256"] != _sha256_json(provenance):
            raise DataValidationError("RBA artifact provenance hash does not match")
        if payload["artifact_bundle_sha256"] != _sha256_json(sorted(sources.items())):
            raise DataValidationError("RBA artifact bundle hash does not match")
        return dict(payload)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DataValidationError(f"invalid RBA artifact manifest: {exc}") from exc


def _glpk_problem(problem: Any) -> Any:
    """Reach the pinned RBAtools 2.0.1 GLPK handle for a clean basis reset."""
    try:
        solver = problem.LP._lp_solver
        glpk_problem = solver.glpkLP
    except AttributeError as exc:
        raise DataValidationError(
            "pinned RBAtools GLPK internals changed; cannot reset the solver basis"
        ) from exc
    if solver.name != "swiglpk":
        raise DataValidationError("RBA problem is not backed by pinned swiglpk")
    return glpk_problem
