from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from yggdrisil.evaluation import EvaluationResult
from yggdrisil.memory import MemoryEntry
from yggdrisil.types import (
    DecisionRecord,
    Edge,
    EvaluationRecord,
    ProposalEvent,
    StateNode,
)

State = TypeVar("State")
Action = TypeVar("Action")


class ReadOnlyStateGraph(Protocol[State, Action]):
    def memory(self, namespace: str, key: str) -> MemoryEntry: ...

    def memories(
        self, namespace: str, limit: int | None = None
    ) -> list[MemoryEntry]: ...

    def get_state(self, state_id: str) -> StateNode[State]: ...

    def has_state(self, state_id: str) -> bool: ...

    def parents(self, state_id: str) -> list[StateNode[State]]: ...

    def children(self, state_id: str) -> list[StateNode[State]]: ...

    def ancestors(self, state_id: str) -> list[StateNode[State]]: ...

    def descendants(self, state_id: str) -> list[StateNode[State]]: ...

    def frontier(self, limit: int | None = None) -> list[StateNode[State]]: ...

    def states(
        self,
        limit: int | None = None,
        *,
        newest: bool = False,
    ) -> list[StateNode[State]]: ...

    def edges(self) -> list[Edge[Action]]: ...

    def evaluations(self, state_id: str) -> list[EvaluationRecord]: ...

    def decisions(
        self,
        run_id: str | None = None,
        limit: int | None = None,
        *,
        newest: bool = False,
    ) -> list[DecisionRecord]: ...

    def proposal_events(
        self,
        *,
        run_id: str | None = None,
        decision_id: str | None = None,
        state_id: str | None = None,
        edge_id: str | None = None,
    ) -> list[ProposalEvent[Action]]: ...

    def __len__(self) -> int: ...

    def edge_count(self) -> int: ...


class StateGraph(ABC, Generic[State, Action]):
    def memory(self, namespace: str, key: str) -> MemoryEntry:
        raise NotImplementedError("this graph backend does not support shared memory")

    def memories(self, namespace: str, limit: int | None = None) -> list[MemoryEntry]:
        raise NotImplementedError("this graph backend does not support shared memory")

    @abstractmethod
    def add_state(
        self,
        state_id: str,
        state: State,
        metadata: dict[str, Any] | None = None,
        *,
        created_step: int = 0,
    ) -> StateNode[State]: ...

    @abstractmethod
    def add_edge(
        self,
        parent_id: str,
        child_id: str,
        action: Action,
        metadata: dict[str, Any] | None = None,
        *,
        created_step: int = 0,
    ) -> Edge[Action]: ...

    @abstractmethod
    def get_state(self, state_id: str) -> StateNode[State]: ...

    @abstractmethod
    def has_state(self, state_id: str) -> bool: ...

    @abstractmethod
    def parents(self, state_id: str) -> list[StateNode[State]]: ...

    @abstractmethod
    def children(self, state_id: str) -> list[StateNode[State]]: ...

    @abstractmethod
    def ancestors(self, state_id: str) -> list[StateNode[State]]: ...

    @abstractmethod
    def descendants(self, state_id: str) -> list[StateNode[State]]: ...

    @abstractmethod
    def frontier(self, limit: int | None = None) -> list[StateNode[State]]: ...

    @abstractmethod
    def states(
        self,
        limit: int | None = None,
        *,
        newest: bool = False,
    ) -> list[StateNode[State]]: ...

    @abstractmethod
    def edges(self) -> list[Edge[Action]]: ...

    @abstractmethod
    def add_evaluation(
        self,
        state_id: str,
        *,
        evaluator_id: str,
        evaluator: str,
        version: str,
        config_hash: str,
        result: EvaluationResult,
    ) -> EvaluationRecord: ...

    @abstractmethod
    def get_evaluation(
        self,
        state_id: str,
        evaluator_id: str,
    ) -> EvaluationRecord | None: ...

    @abstractmethod
    def evaluations(self, state_id: str) -> list[EvaluationRecord]: ...

    @abstractmethod
    def decisions(
        self,
        run_id: str | None = None,
        limit: int | None = None,
        *,
        newest: bool = False,
    ) -> list[DecisionRecord]: ...

    @abstractmethod
    def proposal_events(
        self,
        *,
        run_id: str | None = None,
        decision_id: str | None = None,
        state_id: str | None = None,
        edge_id: str | None = None,
    ) -> list[ProposalEvent[Action]]: ...

    @abstractmethod
    def __len__(self) -> int: ...

    @abstractmethod
    def edge_count(self) -> int: ...

    def readonly(self) -> ReadOnlyStateGraph[State, Action]:
        return ReadOnlyGraph(self)

    def to_networkx(self) -> Any:
        from yggdrisil.graph.export import to_networkx

        return to_networkx(self)

    def export_json(self, path: str | Path) -> None:
        from yggdrisil.graph.export import export_json

        export_json(self, path)

    def export_graphml(self, path: str | Path) -> None:
        from yggdrisil.graph.export import export_graphml

        export_graphml(self, path)


class ReadOnlyGraph(Generic[State, Action]):
    """Read-only policy interface for trusted Python code.

    Mutators are not exposed through the public API. This is an interface
    boundary, not a sandbox for hostile policy implementations.
    """

    __slots__ = ("_graph",)

    _graph: StateGraph[State, Action]

    def __init__(self, graph: StateGraph[State, Action]) -> None:
        object.__setattr__(self, "_graph", graph)

    def get_state(self, state_id: str) -> StateNode[State]:
        return self._graph.get_state(state_id)

    def memory(self, namespace: str, key: str) -> MemoryEntry:
        return self._graph.memory(namespace, key)

    def memories(self, namespace: str, limit: int | None = None) -> list[MemoryEntry]:
        return self._graph.memories(namespace, limit)

    def has_state(self, state_id: str) -> bool:
        return self._graph.has_state(state_id)

    def parents(self, state_id: str) -> list[StateNode[State]]:
        return self._graph.parents(state_id)

    def children(self, state_id: str) -> list[StateNode[State]]:
        return self._graph.children(state_id)

    def ancestors(self, state_id: str) -> list[StateNode[State]]:
        return self._graph.ancestors(state_id)

    def descendants(self, state_id: str) -> list[StateNode[State]]:
        return self._graph.descendants(state_id)

    def frontier(self, limit: int | None = None) -> list[StateNode[State]]:
        return self._graph.frontier(limit=limit)

    def states(
        self,
        limit: int | None = None,
        *,
        newest: bool = False,
    ) -> list[StateNode[State]]:
        return self._graph.states(limit=limit, newest=newest)

    def edges(self) -> list[Edge[Action]]:
        return self._graph.edges()

    def evaluations(self, state_id: str) -> list[EvaluationRecord]:
        return self._graph.evaluations(state_id)

    def decisions(
        self,
        run_id: str | None = None,
        limit: int | None = None,
        *,
        newest: bool = False,
    ) -> list[DecisionRecord]:
        return self._graph.decisions(run_id, limit, newest=newest)

    def proposal_events(
        self,
        *,
        run_id: str | None = None,
        decision_id: str | None = None,
        state_id: str | None = None,
        edge_id: str | None = None,
    ) -> list[ProposalEvent[Action]]:
        return self._graph.proposal_events(
            run_id=run_id,
            decision_id=decision_id,
            state_id=state_id,
            edge_id=edge_id,
        )

    def __len__(self) -> int:
        return len(self._graph)

    def edge_count(self) -> int:
        return self._graph.edge_count()
