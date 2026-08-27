"""iML1515 flux-balance evidence in explicit aerobic M9/glucose medium."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping

from cobra import Model
from cobra.io import load_json_model

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import GeneRegistry, file_sha256
from yggdrisil_ecoli.scorers.base import ScoreResult
from yggdrisil_ecoli.state import GenomeState

IML1515_OBJECTIVE_REACTION = "BIOMASS_Ec_iML1515_core_75p37M"
IML1515_ATPM_REACTION = "ATPM"

M9_GLUCOSE_AEROBIC_MEDIUM: Mapping[str, float] = MappingProxyType(
    {
        "EX_glc__D_e": 10.0,
        "EX_o2_e": 1000.0,
        "EX_pi_e": 1000.0,
        "EX_nh4_e": 1000.0,
        "EX_so4_e": 1000.0,
        "EX_k_e": 1000.0,
        "EX_na1_e": 1000.0,
        "EX_cl_e": 1000.0,
        "EX_mg2_e": 1000.0,
        "EX_ca2_e": 1000.0,
        "EX_h_e": 1000.0,
        "EX_h2o_e": 1000.0,
        "EX_co2_e": 1000.0,
        "EX_fe2_e": 1000.0,
        "EX_fe3_e": 1000.0,
        "EX_mn2_e": 1000.0,
        "EX_zn2_e": 1000.0,
        "EX_cu2_e": 1000.0,
        "EX_cobalt2_e": 1000.0,
        "EX_ni2_e": 1000.0,
        "EX_mobd_e": 1000.0,
        "EX_sel_e": 1000.0,
        "EX_slnt_e": 1000.0,
        "EX_tungs_e": 1000.0,
    }
)


@dataclass(frozen=True, slots=True)
class FBAEnvironment:
    name: str
    medium: Mapping[str, float]
    oxygenation: str
    temperature_c: float
    solver: str = "glpk"

    def config_hash(self) -> str:
        payload = {
            "name": self.name,
            "medium": dict(sorted(self.medium.items())),
            "oxygenation": self.oxygenation,
            "temperature_c": self.temperature_c,
            "solver": self.solver,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


DEFAULT_FBA_ENVIRONMENT = FBAEnvironment(
    name="aerobic_m9_minimal_glucose",
    medium=M9_GLUCOSE_AEROBIC_MEDIUM,
    oxygenation="aerobic_unlimited_oxygen",
    temperature_c=37.0,
)


@dataclass(frozen=True, slots=True)
class FBAResult:
    feasible: bool
    growth_rate: float | None
    solver_status: str
    deleted_genes_total: int
    deleted_genes_modeled: int
    deleted_genes_unmodeled: int
    modeled_gene_ids: tuple[str, ...]
    unmodeled_gene_ids: tuple[str, ...]
    model_sha256: str
    registry_mapping_hash: str
    environment_config_hash: str
    solver: str
    cobra_version: str
    optlang_version: str
    solver_package: str
    solver_package_version: str

    def metrics(self) -> dict[str, object]:
        return {
            "feasible": self.feasible,
            "growth_rate": self.growth_rate,
            "solver_status": self.solver_status,
        }

    def coverage(self) -> dict[str, object]:
        return {
            "deleted_genes_total": self.deleted_genes_total,
            "deleted_genes_modeled": self.deleted_genes_modeled,
            "deleted_genes_unmodeled": self.deleted_genes_unmodeled,
            "modeled_gene_ids": list(self.modeled_gene_ids),
            "unmodeled_gene_ids": list(self.unmodeled_gene_ids),
        }

    def provenance(self) -> dict[str, object]:
        return {
            "model_sha256": self.model_sha256,
            "registry_mapping_hash": self.registry_mapping_hash,
            "environment_config_hash": self.environment_config_hash,
            "solver": self.solver,
            "cobra_version": self.cobra_version,
            "optlang_version": self.optlang_version,
            "solver_package": self.solver_package,
            "solver_package_version": self.solver_package_version,
        }


class FBAEvaluator:
    """Copy-on-score iML1515 evaluator; the frozen base model is never mutated."""

    def __init__(
        self,
        *,
        model_path: str | Path,
        registry: GeneRegistry,
        environment: FBAEnvironment = DEFAULT_FBA_ENVIRONMENT,
    ) -> None:
        self.model_path = Path(model_path)
        self.registry = registry
        self.environment = environment
        if environment.solver != "glpk":
            raise DataValidationError("v1 FBA supports only the pinned GLPK solver")
        self.model_sha256 = file_sha256(self.model_path)
        mapping_payload = [
            (record.b_number, record.iml1515_gene_id) for record in self.registry
        ]
        self.registry_mapping_hash = hashlib.sha256(
            json.dumps(mapping_payload, separators=(",", ":")).encode()
        ).hexdigest()
        self.cobra_version = version("cobra")
        self.optlang_version = version("optlang")
        self.solver_package = "swiglpk"
        self.solver_package_version = version(self.solver_package)
        self._base_model = load_json_model(str(self.model_path))
        self._validate_model(self._base_model)

    def score_deleted(self, deleted_genes: set[str] | frozenset[str]) -> FBAResult:
        deleted, modeled, unmodeled = self._partition_deletions(deleted_genes)
        with self._candidate_model(modeled) as model:
            solution = model.optimize()
            status = str(solution.status)
            feasible = status == "optimal"
            growth: float | None = None
            if feasible and solution.objective_value is not None:
                candidate = float(solution.objective_value)
                if not math.isfinite(candidate):
                    raise DataValidationError("FBA returned non-finite biomass flux")
                growth = 0.0 if abs(candidate) < 1e-9 else candidate
        return FBAResult(
            feasible=feasible,
            growth_rate=growth,
            solver_status=status,
            deleted_genes_total=len(deleted),
            deleted_genes_modeled=len(modeled),
            deleted_genes_unmodeled=len(unmodeled),
            modeled_gene_ids=modeled,
            unmodeled_gene_ids=unmodeled,
            model_sha256=self.model_sha256,
            registry_mapping_hash=self.registry_mapping_hash,
            environment_config_hash=self.environment.config_hash(),
            solver=self.environment.solver,
            cobra_version=self.cobra_version,
            optlang_version=self.optlang_version,
            solver_package=self.solver_package,
            solver_package_version=self.solver_package_version,
        )

    async def score_deleted_async(
        self, deleted_genes: set[str] | frozenset[str]
    ) -> FBAResult:
        """Run blocking solver work outside an async scorer event loop."""

        return await asyncio.to_thread(self.score_deleted, deleted_genes)

    def reaction_bounds_after_deletion(
        self,
        deleted_genes: set[str] | frozenset[str],
        reaction_ids: tuple[str, ...],
    ) -> dict[str, tuple[float, float]]:
        """Diagnostic used by GPR correctness checks and environment validation."""

        _deleted, modeled, _unmodeled = self._partition_deletions(deleted_genes)
        with self._candidate_model(modeled) as model:
            return {
                reaction_id: tuple(model.reactions.get_by_id(reaction_id).bounds)
                for reaction_id in reaction_ids
            }

    def base_reaction_bounds(
        self, reaction_ids: tuple[str, ...]
    ) -> dict[str, tuple[float, float]]:
        return {
            reaction_id: tuple(self._base_model.reactions.get_by_id(reaction_id).bounds)
            for reaction_id in reaction_ids
        }

    def _partition_deletions(
        self, deleted_genes: set[str] | frozenset[str]
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        deleted = tuple(sorted(deleted_genes))
        modeled: list[str] = []
        unmodeled: list[str] = []
        for b_number in deleted:
            record = self.registry.require(b_number)
            if record.iml1515_gene_id is None:
                unmodeled.append(b_number)
            else:
                modeled.append(record.iml1515_gene_id)
        return deleted, tuple(modeled), tuple(unmodeled)

    @contextmanager
    def _candidate_model(self, modeled_gene_ids: tuple[str, ...]) -> Iterator[Model]:
        model = self._base_model.copy()
        try:
            model.solver = self.environment.solver
            model.medium = dict(self.environment.medium)
            for model_gene_id in modeled_gene_ids:
                model.genes.get_by_id(model_gene_id).knock_out()
            yield model
        finally:
            # COBRA models own solver resources; a copied model is discarded here.
            del model

    def _validate_model(self, model: Model) -> None:
        if model.id != "iML1515":
            raise DataValidationError(f"expected iML1515 model, got {model.id!r}")
        try:
            objective = model.reactions.get_by_id(IML1515_OBJECTIVE_REACTION)
            atpm = model.reactions.get_by_id(IML1515_ATPM_REACTION)
        except KeyError as exc:
            raise DataValidationError(
                "iML1515 objective or ATPM reaction is absent"
            ) from exc
        if objective.objective_coefficient != 1.0:
            raise DataValidationError(
                f"expected objective {IML1515_OBJECTIVE_REACTION} with coefficient 1"
            )
        if tuple(atpm.bounds) != (6.86, 1000.0):
            raise DataValidationError(f"unexpected ATPM bounds: {atpm.bounds}")
        missing_exchanges = sorted(
            set(self.environment.medium) - {reaction.id for reaction in model.exchanges}
        )
        if missing_exchanges:
            raise DataValidationError(
                f"medium references missing exchanges: {missing_exchanges}"
            )
        model_gene_ids = {gene.id for gene in model.genes}
        mapped_gene_ids = [
            record.iml1515_gene_id
            for record in self.registry
            if record.iml1515_gene_id is not None
        ]
        missing_model_genes = sorted(set(mapped_gene_ids) - model_gene_ids)
        if missing_model_genes:
            raise DataValidationError(
                "registry maps genes absent from the frozen iML1515 model: "
                f"{missing_model_genes}"
            )
        if len(mapped_gene_ids) != len(set(mapped_gene_ids)):
            raise DataValidationError("registry maps multiple genes to one iML1515 ID")


class FBAScorer:
    name = "fba"
    version = "1"

    def __init__(self, evaluator: FBAEvaluator) -> None:
        self.evaluator = evaluator
        self.config_hash = hashlib.sha256(
            (
                evaluator.model_sha256
                + ":"
                + evaluator.registry_mapping_hash
                + ":"
                + evaluator.environment.config_hash()
                + ":"
                + evaluator.cobra_version
                + ":"
                + evaluator.optlang_version
                + ":"
                + evaluator.solver_package_version
            ).encode()
        ).hexdigest()

    async def score(self, state: GenomeState) -> ScoreResult:
        result = await self.evaluator.score_deleted_async(state.deleted_genes)
        return ScoreResult(
            scorer=self.name,
            version=self.version,
            metrics=result.metrics(),
            coverage=result.coverage(),
            provenance=result.provenance(),
        )
