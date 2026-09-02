"""Pinned resource-balance feasibility evidence for E. coli K-12."""

from __future__ import annotations

import asyncio
import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from yggdrisil import EvaluationResult

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.rba_build import (
    MODEL_STRUCTURE_PATH,
    RBA_ARTIFACT_MANIFEST,
    RBA_EXPECTED_LP_DIMENSIONS,
    RBA_EXPECTED_REGISTRY_MAPPING,
    RBA_EXPECTED_STRUCTURE_DIMENSIONS,
    RBA_GROWTH_FLOOR_H,
    RBA_MODEL_FILES,
    RBA_MODELS_COMMIT,
    RBA_NUMERICAL_DEPENDENCIES,
    RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H,
)
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState

RBA_SOLVER = "scipy-highs"
RBA_SOLVER_METHOD = "highs"
RBA_MATRIX_BACKEND = "swiglpk"
RBA_SOLVER_PRESOLVE = True
RBA_FEASIBILITY_TOLERANCE = 1e-7
RBA_SOLVER_FALLBACKS = (
    ("highs-ipm", True),
    ("highs", False),
    ("highs-ipm", False),
)


class RBAScorer:
    """Test deletion sets at a fixed growth floor in the pinned RBA model."""

    name = "resource_allocation"
    version = "3"

    def __init__(
        self,
        *,
        artifact_dir: str | Path,
        registry: GeneRegistry,
        solver: str = RBA_SOLVER,
    ) -> None:
        if solver != RBA_SOLVER:
            raise DataValidationError(
                f"RBA supports only the pinned {RBA_SOLVER} solver"
            )
        self.artifact_dir = Path(artifact_dir)
        self.registry = registry
        self.solver = solver
        manifest = _validated_manifest(self.artifact_dir)
        provenance = _require_mapping(manifest, "provenance")
        artifact_dependencies = _require_mapping(provenance, "dependencies")
        self.artifact_bundle_sha256 = _require_string(
            manifest, "artifact_bundle_sha256"
        )
        self.provenance_sha256 = _require_string(manifest, "provenance_sha256")
        self.model_structure_sha256 = _generated_file_sha256(provenance)
        self.artifact_dependency_versions = {
            str(name): str(package_version)
            for name, package_version in artifact_dependencies.items()
        }
        self.dependency_versions = _runtime_dependency_versions()
        if self.artifact_dependency_versions != self.dependency_versions:
            raise DataValidationError(
                "RBA artifact build dependencies differ from the pinned runtime"
            )

        try:
            import numpy
            from rbatools.rba_session import SessionRBA
            from scipy.optimize import linprog
            from scipy.sparse import vstack
        except ImportError as exc:  # pragma: no cover - optional dependency guard
            raise DataValidationError(
                "RBA scoring requires the project's pinned 'rba' extra"
            ) from exc

        self._numpy: Any = numpy
        self._linprog: Any = linprog
        self._vstack: Any = vstack
        self._session: Any = SessionRBA(
            str(self.artifact_dir), lp_solver=RBA_MATRIX_BACKEND
        )
        self._session.set_growth_rate(RBA_GROWTH_FLOOR_H)
        self.model_dimensions = _validate_loaded_dimensions(self._session)
        self._variables_by_gene = self._build_variable_map()
        self.registry_mapping_sha256 = _sha256_json(
            [
                (gene, list(variables))
                for gene, variables in self._variables_by_gene.items()
            ]
        )
        modeled_variables = sorted(
            {
                variable
                for variables in self._variables_by_gene.values()
                for variable in variables
            }
        )
        mapping_dimensions = {
            "genes": sum(bool(value) for value in self._variables_by_gene.values()),
            "variables": len(modeled_variables),
        }
        if mapping_dimensions != RBA_EXPECTED_REGISTRY_MAPPING:
            raise DataValidationError(
                "RBA registry mapping dimensions differ from the pinned snapshot: "
                f"expected={RBA_EXPECTED_REGISTRY_MAPPING}, "
                f"actual={mapping_dimensions}"
            )
        self._base_lower_bounds = _plain_float_mapping(
            self._session.Problem.get_lb(modeled_variables)
        )
        self._base_upper_bounds = _plain_float_mapping(
            self._session.Problem.get_ub(modeled_variables)
        )
        self._prepare_highs_problem()
        self.config = {
            "artifact_bundle_sha256": self.artifact_bundle_sha256,
            "registry_mapping_sha256": self.registry_mapping_sha256,
            "rba_models_commit": RBA_MODELS_COMMIT,
            "growth_rate_floor_h": RBA_GROWTH_FLOOR_H,
            "repository_wild_type_max_growth_rate_h": (
                RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H
            ),
            "model_dimensions": self.model_dimensions,
            "solver": self.solver,
            "solver_method": RBA_SOLVER_METHOD,
            "solver_fallbacks": _solver_fallback_metadata(),
            "matrix_backend": RBA_MATRIX_BACKEND,
            "solver_presolve": RBA_SOLVER_PRESOLVE,
            "feasibility_tolerance": RBA_FEASIBILITY_TOLERANCE,
            **{
                f"{name.lower()}_version": package_version
                for name, package_version in sorted(self.dependency_versions.items())
            },
        }

    @classmethod
    def from_artifact(
        cls,
        artifact_dir: str | Path,
        *,
        registry: GeneRegistry,
        solver: str = RBA_SOLVER,
    ) -> RBAScorer:
        """Load a scorer from a validated local artifact directory."""

        return cls(artifact_dir=artifact_dir, registry=registry, solver=solver)

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        """Evaluate without blocking Yggdrisil's event loop."""

        metrics, coverage = await asyncio.to_thread(
            self._score_deleted, state.deleted_genes
        )
        return scientific_evaluation(
            metrics,
            coverage=coverage,
            provenance={
                "artifact_bundle_sha256": self.artifact_bundle_sha256,
                "provenance_sha256": self.provenance_sha256,
                "model_structure_sha256": self.model_structure_sha256,
                "registry_mapping_sha256": self.registry_mapping_sha256,
                "rba_models_commit": RBA_MODELS_COMMIT,
                "growth_rate_floor_h": RBA_GROWTH_FLOOR_H,
                "repository_wild_type_max_growth_rate_h": (
                    RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H
                ),
                "model_dimensions": self.model_dimensions,
                "solver": self.solver,
                "solver_method": RBA_SOLVER_METHOD,
                "solver_fallbacks": _solver_fallback_metadata(),
                "matrix_backend": RBA_MATRIX_BACKEND,
                "solver_presolve": RBA_SOLVER_PRESOLVE,
                "feasibility_tolerance": RBA_FEASIBILITY_TOLERANCE,
                "dependency_versions": dict(sorted(self.dependency_versions.items())),
                "artifact_dependency_versions": dict(
                    sorted(self.artifact_dependency_versions.items())
                ),
            },
        )

    def variables_for_gene(self, b_number: str) -> tuple[str, ...]:
        """Return the precomputed exact LP columns for a canonical gene."""

        self.registry.require(b_number)
        return self._variables_by_gene[b_number]

    def _score_deleted(
        self, deleted_genes: set[str] | frozenset[str]
    ) -> tuple[dict[str, object], dict[str, object]]:
        deleted = tuple(sorted(deleted_genes))
        for b_number in deleted:
            self.registry.require(b_number)
        modeled_variables_by_gene = {
            gene: list(self._variables_by_gene[gene])
            for gene in deleted
            if self._variables_by_gene[gene]
        }
        unmodeled = [gene for gene in deleted if not self._variables_by_gene[gene]]
        knocked_out_variables = sorted(
            {
                variable
                for variables in modeled_variables_by_gene.values()
                for variable in variables
            }
        )

        status, solution_type, solver_attempts = self._solve_with_knockouts(
            knocked_out_variables
        )
        feasible = status in {"optimal", "feasible"}
        metrics: dict[str, object] = {
            "feasible_at_growth_floor": feasible,
            "growth_rate_floor_h": RBA_GROWTH_FLOOR_H,
            "repository_wild_type_max_growth_rate_h": (
                RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H
            ),
            "solver_status": {
                "status": status,
                "solution_type": solution_type,
                "method": solver_attempts[-1]["method"],
                "presolve": solver_attempts[-1]["presolve"],
                "attempts": solver_attempts,
            },
            "modeled_variables_by_gene": modeled_variables_by_gene,
            "knocked_out_variable_ids": knocked_out_variables,
            "unmodeled_gene_ids": unmodeled,
        }
        coverage: dict[str, object] = {
            "deleted_genes_total": len(deleted),
            "deleted_genes_modeled": len(modeled_variables_by_gene),
            "deleted_genes_unmodeled": len(unmodeled),
        }
        return metrics, coverage

    def _solve_with_knockouts(
        self, variables: list[str]
    ) -> tuple[str, str, list[dict[str, object]]]:
        bounds = self._base_bounds.copy()
        for variable in variables:
            bounds[self._variable_indices[variable]] = 0.0
        attempts: list[dict[str, object]] = []
        methods = ((RBA_SOLVER_METHOD, RBA_SOLVER_PRESOLVE), *RBA_SOLVER_FALLBACKS)
        for method, presolve in methods:
            result = self._linprog(
                self._objective,
                A_ub=self._a_ub,
                b_ub=self._b_ub,
                A_eq=self._a_eq,
                b_eq=self._b_eq,
                bounds=bounds,
                method=method,
                options={
                    "presolve": presolve,
                    "primal_feasibility_tolerance": RBA_FEASIBILITY_TOLERANCE,
                    "dual_feasibility_tolerance": RBA_FEASIBILITY_TOLERANCE,
                },
            )
            attempts.append(
                {
                    "method": method,
                    "presolve": presolve,
                    "status_code": int(result.status),
                }
            )
            if result.status == 0:
                return "optimal", "HiGHS", attempts
            if result.status == 2:
                return "infeasible", "HiGHS", attempts
            if result.status != 4:
                break
        raise DataValidationError(
            "RBA HiGHS solve did not establish feasibility: "
            f"status={result.status}, message={result.message}"
        )

    def _prepare_highs_problem(self) -> None:
        lp = self._session.Problem.LP
        matrix = lp.A.tocsr()
        rhs = self._numpy.asarray(lp.b, dtype=float)
        row_signs = self._numpy.asarray(lp.row_signs)
        unexpected = sorted(set(row_signs) - {"E", "L", "G"})
        if unexpected:
            raise DataValidationError(f"RBA LP has unknown row signs: {unexpected}")
        equal = self._numpy.flatnonzero(row_signs == "E")
        less = self._numpy.flatnonzero(row_signs == "L")
        greater = self._numpy.flatnonzero(row_signs == "G")
        self._a_eq = matrix[equal]
        self._b_eq = rhs[equal]
        self._a_ub = self._vstack([matrix[less], -matrix[greater]], format="csr")
        self._b_ub = self._numpy.concatenate([rhs[less], -rhs[greater]])
        self._objective = self._numpy.asarray(lp.f, dtype=float)
        self._base_bounds = self._numpy.column_stack(
            (
                self._numpy.asarray(lp.LB, dtype=float),
                self._numpy.asarray(lp.UB, dtype=float),
            )
        )
        self._variable_indices = {
            str(variable): index for index, variable in enumerate(lp.col_names)
        }

    def _build_variable_map(self) -> dict[str, tuple[str, ...]]:
        structure = self._session.ModelStructure
        lp_columns = set(self._session.Problem.LP.col_names)
        process_variables = {
            str(name): f"{record['ID']}_machinery"
            for name, record in structure.ProcessInfo.Elements.items()
        }
        variables_by_gene: dict[str, set[str]] = {
            record.b_number: set() for record in self.registry
        }
        for protein in structure.ProteinInfo.Elements.values():
            b_number = str(protein.get("ProtoID", ""))
            if b_number not in variables_by_gene:
                continue
            associated_enzymes = protein.get("associatedEnzymes", [])
            if not isinstance(associated_enzymes, list):
                raise DataValidationError(
                    f"RBA protein {protein.get('ID')!r} has invalid associatedEnzymes"
                )
            variables_by_gene[b_number].update(map(str, associated_enzymes))
            supported_processes = protein.get("SupportsProcess", [])
            if not isinstance(supported_processes, list):
                raise DataValidationError(
                    f"RBA protein {protein.get('ID')!r} has invalid SupportsProcess"
                )
            for process in supported_processes:
                try:
                    variables_by_gene[b_number].add(process_variables[str(process)])
                except KeyError as exc:
                    raise DataValidationError(
                        f"RBA protein references unknown process {process!r}"
                    ) from exc

        mapped_columns = {
            variable
            for variables in variables_by_gene.values()
            for variable in variables
        }
        missing_columns = sorted(mapped_columns - lp_columns)
        if missing_columns:
            raise DataValidationError(
                "RBA gene mapping references columns absent from the exact LP: "
                f"{missing_columns}"
            )
        return {
            gene: tuple(sorted(variables))
            for gene, variables in sorted(variables_by_gene.items())
        }


def _validated_manifest(artifact_dir: Path) -> dict[str, object]:
    manifest_path = artifact_dir / RBA_ARTIFACT_MANIFEST
    try:
        payload = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DataValidationError(f"cannot read RBA artifact manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise DataValidationError("RBA artifact manifest must be a JSON object")
    if payload.get("schema_version") != 1 or payload.get("artifact") != (
        "ecoli_k12_wt_rba"
    ):
        raise DataValidationError("unexpected RBA artifact identity or schema")
    provenance = _require_mapping(payload, "provenance")
    if provenance.get("commit") != RBA_MODELS_COMMIT:
        raise DataValidationError("RBA artifact does not use the pinned model commit")
    if provenance.get("growth_floor_h") != RBA_GROWTH_FLOOR_H:
        raise DataValidationError("RBA artifact has an unexpected growth floor")
    if provenance.get("repository_wild_type_max_growth_rate_h") != (
        RBA_REPOSITORY_WT_MAX_GROWTH_RATE_H
    ):
        raise DataValidationError("RBA artifact has an unexpected WT reference")
    if provenance.get("model_dimensions") != RBA_EXPECTED_STRUCTURE_DIMENSIONS:
        raise DataValidationError("RBA artifact has unexpected model dimensions")
    expected_sources = {item.path: item.sha256 for item in RBA_MODEL_FILES}
    source_records = provenance.get("source_files")
    if not isinstance(source_records, list):
        raise DataValidationError("RBA artifact source_files must be a list")
    recorded_sources: dict[str, str] = {}
    for record in source_records:
        if not isinstance(record, dict):
            raise DataValidationError("invalid RBA artifact source record")
        source_path = _require_string(record, "path")
        recorded_sources[source_path] = _require_string(record, "sha256")
    if recorded_sources != expected_sources:
        raise DataValidationError("RBA artifact source inventory differs from the pin")
    for relative_path, expected_sha256 in expected_sources.items():
        _require_file_hash(artifact_dir / relative_path, expected_sha256)
    generated_sha256 = _generated_file_sha256(provenance)
    _require_file_hash(artifact_dir / MODEL_STRUCTURE_PATH, generated_sha256)
    if payload.get("provenance_sha256") != _sha256_json(provenance):
        raise DataValidationError("RBA artifact provenance hash does not match")
    bundle_entries = sorted(expected_sources.items())
    if payload.get("artifact_bundle_sha256") != _sha256_json(bundle_entries):
        raise DataValidationError("RBA artifact bundle hash does not match")
    return payload


def _generated_file_sha256(provenance: dict[str, object]) -> str:
    generated = provenance.get("generated_files")
    if not isinstance(generated, list) or len(generated) != 1:
        raise DataValidationError("RBA artifact must contain one generated structure")
    record = generated[0]
    if not isinstance(record, dict) or record.get("path") != MODEL_STRUCTURE_PATH:
        raise DataValidationError("RBA artifact has an unexpected generated file")
    return _require_string(record, "sha256")


def _require_mapping(value: dict[str, object], key: str) -> dict[str, object]:
    candidate = value.get(key)
    if not isinstance(candidate, dict):
        raise DataValidationError(f"RBA artifact {key!r} must be an object")
    return candidate


def _require_string(value: dict[str, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str):
        raise DataValidationError(f"RBA artifact {key!r} must be a string")
    return candidate


def _require_file_hash(path: Path, expected_sha256: str) -> None:
    try:
        actual_sha256 = file_sha256(path)
    except OSError as exc:
        raise DataValidationError(
            f"cannot read RBA artifact file {path}: {exc}"
        ) from exc
    if actual_sha256 != expected_sha256:
        raise DataValidationError(
            f"RBA artifact file changed: {path}; expected {expected_sha256}, "
            f"got {actual_sha256}"
        )


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _plain_float_mapping(values: dict[str, Any]) -> dict[str, float]:
    return {name: float(value) for name, value in values.items()}


def _solver_fallback_metadata() -> list[dict[str, object]]:
    return [
        {"method": method, "presolve": presolve}
        for method, presolve in RBA_SOLVER_FALLBACKS
    ]


def _runtime_dependency_versions() -> dict[str, str]:
    expected = {
        "RBApy": "3.0.3",
        "RBAtools": "2.0.1",
        "setuptools": "80.10.2",
        "swiglpk": "5.0.13",
    }
    try:
        installed = {
            package: version(package)
            for package in (*expected, *RBA_NUMERICAL_DEPENDENCIES)
        }
    except PackageNotFoundError as exc:
        raise DataValidationError(
            "RBA scoring requires the project's pinned 'rba' extra"
        ) from exc
    mismatched = {
        package: {"expected": expected[package], "installed": installed[package]}
        for package in expected
        if installed[package] != expected[package]
    }
    if mismatched:
        raise DataValidationError(
            f"RBA runtime differs from the pinned environment: mismatched={mismatched}"
        )
    return installed


def _validate_loaded_dimensions(session: Any) -> dict[str, int]:
    dimensions = {
        "rows": len(session.Problem.LP.row_names),
        "columns": len(session.Problem.LP.col_names),
    }
    if dimensions != RBA_EXPECTED_LP_DIMENSIONS:
        raise DataValidationError(
            "loaded RBA LP dimensions differ from the pinned snapshot: "
            f"expected={RBA_EXPECTED_LP_DIMENSIONS}, actual={dimensions}"
        )
    return dimensions


__all__ = ["RBAScorer"]
