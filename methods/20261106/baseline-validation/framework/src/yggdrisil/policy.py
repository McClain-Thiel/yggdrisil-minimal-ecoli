from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from yggdrisil.graph.base import ReadOnlyStateGraph
from yggdrisil.limits import RunStatus
from yggdrisil.memory import MemoryUpdate

Action = TypeVar("Action")


@dataclass(frozen=True)
class Proposal(Generic[Action]):
    parent_id: str
    action: Action
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision(Generic[Action]):
    """One policy operation and the transitions it proposed."""

    role: str
    proposals: list[Proposal[Action]] = field(default_factory=list)
    selected_state_ids: list[str] = field(default_factory=list)
    model: str | None = None
    input_context: Any = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    continue_on_empty: bool = False
    memory_updates: list[MemoryUpdate] = field(default_factory=list)
    memory_namespace: str | None = None


class PolicyStepError(Exception, Generic[Action]):
    """A policy failure with decisions that must be persisted before raising."""

    def __init__(
        self,
        message: str,
        *,
        decisions: list[Decision[Action]],
        cause: Exception,
    ) -> None:
        super().__init__(message)
        self.decisions = decisions
        self.cause = cause


class InterruptedDecisionProvider(Protocol[Action]):
    """Supplies staged policy decisions after task cancellation."""

    def drain_interrupted_decisions(self) -> list[Decision[Action]]: ...


class Policy(Protocol[Action]):
    """Decides where to search. Must not mutate the graph."""

    async def step(
        self,
        graph: ReadOnlyStateGraph[Any, Action],
        status: RunStatus,
    ) -> list[Decision[Action]]: ...
