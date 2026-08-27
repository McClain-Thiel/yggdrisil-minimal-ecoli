"""Shared helpers for native Yggdrisil scientific evaluators."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias

from yggdrisil import EvaluationResult, Evaluator, evaluator_identity

from yggdrisil_ecoli.state import GenomeState

ScalarMetric: TypeAlias = float | int | bool | str | None


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
