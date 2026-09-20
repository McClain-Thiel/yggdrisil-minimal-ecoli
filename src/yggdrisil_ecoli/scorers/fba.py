"""iML1515 flux-balance evidence in explicit aerobic M9/glucose medium."""

from __future__ import annotations

import asyncio
import math
from importlib.metadata import version
from pathlib import Path
from types import MappingProxyType

import pandas as pd
from cobra import Model
from cobra.io import load_json_model
from cobra.util.solver import linear_reaction_coefficients
from yggdrisil import EvaluationResult, stable_hash

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import file_sha256
from yggdrisil_ecoli.scorers.base import scientific_evaluation
from yggdrisil_ecoli.state import GenomeState

IML1515_OBJECTIVE_REACTION = "BIOMASS_Ec_iML1515_core_75p37M"
M9_GLUCOSE_AEROBIC_MEDIUM = MappingProxyType(
    {
        "EX_glc__D_e": 10.0,
        **dict.fromkeys(
            (
                "EX_o2_e",
                "EX_pi_e",
                "EX_nh4_e",
                "EX_so4_e",
                "EX_k_e",
                "EX_na1_e",
                "EX_cl_e",
                "EX_mg2_e",
                "EX_ca2_e",
                "EX_h_e",
                "EX_h2o_e",
                "EX_co2_e",
                "EX_fe2_e",
                "EX_fe3_e",
                "EX_mn2_e",
                "EX_zn2_e",
                "EX_cu2_e",
                "EX_cobalt2_e",
                "EX_ni2_e",
                "EX_mobd_e",
                "EX_sel_e",
                "EX_slnt_e",
                "EX_tungs_e",
            ),
            1000.0,
        ),
    }
)
_ENVIRONMENT = {
    "name": "aerobic_m9_minimal_glucose",
    "medium": dict(M9_GLUCOSE_AEROBIC_MEDIUM),
    "oxygenation": "aerobic_unlimited_oxygen",
    "temperature_c": 37.0,
    "solver": "glpk",
}


class FBAScorer:
    """Score isolated COBRApy model copies; retain coverage and input identities."""

    name = "fba"
    version = "2"

    def __init__(self, *, model_path: str | Path, genes: pd.DataFrame) -> None:
        self.model_ids = genes.iml1515_gene_id.copy()
        self.config = {
            "model_sha256": file_sha256(Path(model_path)),
            "registry_mapping_hash": stable_hash(self.model_ids.to_dict()),
            "environment_config_hash": stable_hash(_ENVIRONMENT),
            "solver": "glpk",
            "cobra_version": version("cobra"),
            "optlang_version": version("optlang"),
            "solver_package": "swiglpk",
            "solver_package_version": version("swiglpk"),
        }
        self.model = load_json_model(str(model_path))
        self._validate_model()
        self.model.solver = "glpk"
        self.model.medium = dict(M9_GLUCOSE_AEROBIC_MEDIUM)

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        return await asyncio.to_thread(self._evaluate, state)

    def model_for(self, deleted_genes: set[str] | frozenset[str]) -> Model:
        """Return an independent knockout model for scoring or inspecting GPRs."""

        model = self.model.copy()
        for model_id in self.model_ids.loc[sorted(deleted_genes)].dropna():
            model.genes.get_by_id(model_id).knock_out()
        return model

    def _evaluate(self, state: GenomeState) -> EvaluationResult:
        solution = self.model_for(state.deleted_genes).optimize()
        feasible = solution.status == "optimal"
        growth = float(solution.objective_value) if feasible else None
        if growth is not None:
            if not math.isfinite(growth):
                raise DataValidationError("FBA returned non-finite biomass flux")
            growth = 0.0 if abs(growth) < 1e-9 else growth
        mapping = self.model_ids.loc[sorted(state.deleted_genes)]
        modeled = mapping.dropna().tolist()
        unmodeled = mapping.loc[mapping.isna()].index.tolist()
        return scientific_evaluation(
            {
                "feasible": feasible,
                "growth_rate": growth,
                "solver_status": str(solution.status),
            },
            coverage={
                "deleted_genes_total": len(mapping),
                "deleted_genes_modeled": len(modeled),
                "deleted_genes_unmodeled": len(unmodeled),
                "modeled_gene_ids": modeled,
                "unmodeled_gene_ids": unmodeled,
            },
            provenance=self.config,
        )

    def _validate_model(self) -> None:
        model = self.model
        if model.id != "iML1515":
            raise DataValidationError(f"expected iML1515 model, got {model.id!r}")
        objective = {
            r.id: value for r, value in linear_reaction_coefficients(model).items()
        }
        if (
            objective != {IML1515_OBJECTIVE_REACTION: 1.0}
            or model.objective.direction != "max"
        ):
            raise DataValidationError(
                "expected the iML1515 biomass maximization objective"
            )
        if tuple(model.reactions.get_by_id("ATPM").bounds) != (6.86, 1000.0):
            raise DataValidationError("unexpected iML1515 ATPM bounds")
        missing = set(M9_GLUCOSE_AEROBIC_MEDIUM) - {r.id for r in model.exchanges}
        if missing:
            raise DataValidationError(
                f"medium references missing exchanges: {sorted(missing)}"
            )
        mapped = self.model_ids.dropna().tolist()
        absent = set(mapped) - {gene.id for gene in model.genes}
        if absent:
            raise DataValidationError(
                f"registry maps genes absent from iML1515: {sorted(absent)}"
            )
        if len(mapped) != len(set(mapped)):
            raise DataValidationError("registry maps multiple genes to one iML1515 ID")
