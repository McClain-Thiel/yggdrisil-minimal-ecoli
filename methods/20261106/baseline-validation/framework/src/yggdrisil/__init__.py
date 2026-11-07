from typing import Any

from yggdrisil.cache import ToolCache, cached_tool
from yggdrisil.evaluation import (
    EvaluationResult,
    Evaluator,
    EvaluatorSuite,
    evaluate_cached,
    evaluator_cost,
    evaluator_identity,
)
from yggdrisil.exceptions import (
    CycleError,
    GraphError,
    SerializationError,
    UnknownStateError,
    YggdrisilError,
)
from yggdrisil.graph import ReadOnlyStateGraph, SQLiteStateGraph
from yggdrisil.limits import RunLimits, RunStatus
from yggdrisil.memory import MemoryConflictError, MemoryEntry, MemoryUpdate
from yggdrisil.objective import Objective
from yggdrisil.policies import BestFirstPolicy, RandomPolicy
from yggdrisil.policy import (
    Decision,
    InterruptedDecisionProvider,
    Policy,
    PolicyStepError,
    Proposal,
)
from yggdrisil.runner import Runner
from yggdrisil.serialize import serializable, stable_hash
from yggdrisil.types import (
    DecisionRecord,
    Edge,
    EvaluationRecord,
    ProposalEvent,
    RunResult,
    StateNode,
)

__all__ = [
    "BestFirstPolicy",
    "CycleError",
    "Decision",
    "DecisionRecord",
    "Edge",
    "EvaluationRecord",
    "EvaluationResult",
    "Evaluator",
    "EvaluatorSuite",
    "GraphError",
    "InterruptedDecisionProvider",
    "NavigatorExplorerPolicy",
    "MemoryConflictError",
    "MemoryEntry",
    "MemoryUpdate",
    "Objective",
    "Policy",
    "PolicyStepError",
    "Proposal",
    "ProposalEvent",
    "RandomPolicy",
    "ReadOnlyStateGraph",
    "RunLimits",
    "RunResult",
    "RunStatus",
    "Runner",
    "SQLiteStateGraph",
    "SerializationError",
    "StateNode",
    "ToolCache",
    "UnknownStateError",
    "YggdrisilError",
    "cached_tool",
    "evaluate_cached",
    "evaluator_cost",
    "evaluator_identity",
    "serializable",
    "stable_hash",
]


def __getattr__(name: str) -> Any:
    if name == "NavigatorExplorerPolicy":
        from yggdrisil.agents.navigator_explorer import NavigatorExplorerPolicy

        return NavigatorExplorerPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
