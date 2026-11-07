"""Shared helpers for native Yggdrisil scientific evaluators."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias

from yggdrisil import EvaluationResult, Evaluator, evaluator_identity
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.state import GenomeState

ScalarMetric: TypeAlias = float | int | bool | str | None


def passes_growth_gates(evidence: Mapping[str, EvaluationRecord]) -> bool:
    """Require positive FBA growth and fixed-floor RBA feasibility."""
    fba, resource = evidence.get("fba"), evidence.get("resource_allocation")
    if fba is None or resource is None:
        return False
    growth = fba.metrics.get("growth_rate")
    return (
        fba.metrics.get("feasible") is True
        and isinstance(growth, (int, float))
        and not isinstance(growth, bool)
        and growth > 0
        and resource.metrics.get("feasible_at_growth_floor") is True
    )


def scientific_evaluation(
    metrics: Mapping[str, object],
    *,
    coverage: Mapping[str, object] | None = None,
    provenance: Mapping[str, object] | None = None,
) -> EvaluationResult:
    """Keep structured evidence out of the framework's scalar metric columns."""

    scalar_metrics: dict[str, ScalarMetric] = {}
    details: dict[str, object] = {}
    for name, value in metrics.items():
        if value is None or isinstance(value, (bool, int, float, str)):
            scalar_metrics[name] = value
        else:
            details[name] = value
    return EvaluationResult(
        metrics=scalar_metrics,
        metadata={
            "coverage": dict(coverage or {}),
            "provenance": dict(provenance or {}),
            "details": details,
        },
    )


def active_evaluator_ids(
    evaluators: Sequence[Evaluator[GenomeState]],
) -> dict[str, str]:
    """Return exact framework identities for selecting the active evidence."""

    names = [evaluator.name for evaluator in evaluators]
    if len(names) != len(set(names)):
        raise ValueError("evaluator names must be unique within a suite")
    return {
        evaluator.name: evaluator_identity(evaluator)[0] for evaluator in evaluators
    }
