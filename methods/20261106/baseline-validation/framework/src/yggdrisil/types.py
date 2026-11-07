from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from yggdrisil.limits import RunLimits

State = TypeVar("State")
Action = TypeVar("Action")
MetricValue = float | int | bool | str | None


@dataclass(frozen=True)
class StateNode(Generic[State]):
    state_id: str
    state: State
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    created_step: int = 0


@dataclass(frozen=True)
class Edge(Generic[Action]):
    edge_id: str
    parent_id: str
    child_id: str
    action: Action
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    created_step: int = 0


@dataclass(frozen=True)
class EvaluationRecord:
    evaluation_id: str
    evaluator_id: str
    state_id: str
    evaluator: str
    version: str
    config_hash: str
    metrics: dict[str, MetricValue]
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    run_id: str
    policy: str
    role: str
    model: str | None
    selected_state_ids: list[str]
    input_context: Any = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    created_step: int = 0


@dataclass(frozen=True)
class ProposalEvent(Generic[Action]):
    event_id: str
    decision_id: str
    run_id: str
    parent_id: str
    action: Action
    outcome: str
    metadata: dict[str, Any] = field(default_factory=dict)
    child_id: str | None = None
    edge_id: str | None = None
    error: str | None = None
    created_at: str = ""
    created_step: int = 0
    proposal_index: int = 0
    sequence_index: int = 0


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    created_at: str
    updated_at: str
    step: int
    status: str
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    stop_reason: str
    step: int
    unique_states: int
    edges: int
    elapsed_s: float
    limits: RunLimits
    best_state_id: str | None = None
    best_score: float | None = None
    evaluation_cost: float = 0.0
