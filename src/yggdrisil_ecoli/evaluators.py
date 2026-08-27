"""Adapters from application scorers to Yggdrisil evaluation records."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeAlias

from yggdrisil import EvaluationResult, EvaluatorSuite, evaluator_identity

from yggdrisil_ecoli.scorers.base import Scorer, ScoreResult
from yggdrisil_ecoli.state import GenomeState

ScalarMetric: TypeAlias = float | int | bool | str | None


class ScorerEvaluator:
    """Expose one scientific scorer through Yggdrisil's evaluator protocol."""

    def __init__(self, scorer: Scorer) -> None:
        self.scorer = scorer
        self.name = scorer.name
        self.version = scorer.version
        self.config = {"scorer_config_hash": scorer.config_hash}
        self.evaluator_id, _ = evaluator_identity(self)

    async def evaluate(self, state: GenomeState) -> EvaluationResult:
        result = await self.scorer.score(state)
        if result.scorer != self.name or result.version != self.version:
            raise ValueError(
                f"scorer {self.name} returned inconsistent result identity"
            )
        return score_result_to_evaluation(result)


def score_result_to_evaluation(result: ScoreResult) -> EvaluationResult:
    """Separate scalar metrics from structured scientific evidence."""

    metrics: dict[str, ScalarMetric] = {}
    details: dict[str, object] = {}
    for name, value in result.metrics.items():
        if value is None or isinstance(value, (bool, int, float, str)):
            metrics[name] = value
        else:
            details[name] = value
    return EvaluationResult(
        metrics=metrics,
        metadata={
            "coverage": result.coverage,
            "provenance": result.provenance,
            "details": details,
        },
    )


def framework_evaluator_suite(
    scorers: Sequence[Scorer],
    *,
    concurrent: bool = True,
) -> EvaluatorSuite[GenomeState]:
    """Build the standard framework suite without combining scorer evidence."""

    names = [scorer.name for scorer in scorers]
    if len(names) != len(set(names)):
        raise ValueError("scorer names must be unique within a suite")
    return EvaluatorSuite(
        [ScorerEvaluator(scorer) for scorer in scorers],
        concurrent=concurrent,
    )


def active_evaluator_ids(scorers: Sequence[Scorer]) -> dict[str, str]:
    """Return exact framework identities for selecting the active evidence."""

    adapters = [ScorerEvaluator(scorer) for scorer in scorers]
    if len(adapters) != len({adapter.name for adapter in adapters}):
        raise ValueError("scorer names must be unique within a suite")
    return {adapter.name: adapter.evaluator_id for adapter in adapters}
