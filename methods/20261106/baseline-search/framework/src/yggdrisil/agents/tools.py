from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from yggdrisil.graph.base import ReadOnlyStateGraph
from yggdrisil.serialize import dumps


class ToolInputError(ValueError):
    """An invalid agent argument that can be corrected within the tool budget."""


def bind_graph_tools(
    graph: ReadOnlyStateGraph[Any, Any],
) -> list[Callable[..., str]]:
    """Plain callables an agent library can register as tools."""

    def get_state(state_id: str) -> str:
        """Return JSON for a stored state node."""
        node = graph.get_state(state_id)
        return dumps(
            {
                "state_id": node.state_id,
                "state": node.state,
                "metadata": node.metadata,
                "created_step": node.created_step,
            }
        )

    def list_frontier() -> str:
        """Return state ids with no children."""
        return dumps([n.state_id for n in graph.frontier()])

    def list_children(state_id: str) -> str:
        """Return child state ids."""
        return dumps([n.state_id for n in graph.children(state_id)])

    def list_parents(state_id: str) -> str:
        """Return parent state ids."""
        return dumps([n.state_id for n in graph.parents(state_id)])

    def list_ancestors(state_id: str) -> str:
        """Return ancestor state ids."""
        return dumps([n.state_id for n in graph.ancestors(state_id)])

    return [get_state, list_frontier, list_children, list_parents, list_ancestors]


def as_toolset(
    graph: ReadOnlyStateGraph[Any, Any],
) -> Sequence[Callable[..., str]]:
    return bind_graph_tools(graph)
