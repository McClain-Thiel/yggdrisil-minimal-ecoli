from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yggdrisil.graph.base import ReadOnlyStateGraph
from yggdrisil.serialize import dumps
from yggdrisil.types import DecisionRecord, EvaluationRecord, ProposalEvent


def to_networkx(graph: ReadOnlyStateGraph[Any, Any]) -> Any:
    import networkx as nx

    g: Any = nx.MultiDiGraph()
    for node in graph.states():
        g.add_node(
            node.state_id,
            metadata=node.metadata,
            created_at=node.created_at,
            created_step=node.created_step,
            state=node.state,
            evaluations=[
                _evaluation_payload(record)
                for record in graph.evaluations(node.state_id)
            ],
        )
    for edge in graph.edges():
        g.add_edge(
            edge.parent_id,
            edge.child_id,
            edge_id=edge.edge_id,
            action=edge.action,
            metadata=edge.metadata,
            created_at=edge.created_at,
            created_step=edge.created_step,
            proposal_events=[
                _event_payload(event)
                for event in graph.proposal_events(edge_id=edge.edge_id)
            ],
        )
    g.graph["decisions"] = [
        _decision_payload(decision) for decision in graph.decisions()
    ]
    return g


def export_json(
    graph: ReadOnlyStateGraph[Any, Any],
    path: str | Path,
) -> None:
    payload: dict[str, Any] = {
        "states": [
            {
                "state_id": n.state_id,
                "state": json.loads(dumps(n.state)),
                "metadata": json.loads(dumps(n.metadata)),
                "created_at": n.created_at,
                "created_step": n.created_step,
            }
            for n in graph.states()
        ],
        "edges": [
            {
                "edge_id": e.edge_id,
                "parent_id": e.parent_id,
                "child_id": e.child_id,
                "action": json.loads(dumps(e.action)),
                "metadata": json.loads(dumps(e.metadata)),
                "created_at": e.created_at,
                "created_step": e.created_step,
            }
            for e in graph.edges()
        ],
        "evaluations": [
            {
                key: json.loads(dumps(value))
                for key, value in _evaluation_payload(record).items()
            }
            for node in graph.states()
            for record in graph.evaluations(node.state_id)
        ],
        "decisions": [
            {
                key: json.loads(dumps(value))
                for key, value in _decision_payload(record).items()
            }
            for record in graph.decisions()
        ],
        "proposal_events": [
            {
                key: json.loads(dumps(value))
                for key, value in _event_payload(record).items()
            }
            for record in graph.proposal_events()
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def export_graphml(
    graph: ReadOnlyStateGraph[Any, Any],
    path: str | Path,
) -> None:
    import networkx as nx

    g = to_networkx(graph)
    g.graph["decisions"] = dumps(g.graph.get("decisions") or [])
    for _, data in g.nodes(data=True):
        data["state"] = dumps(data.get("state"))
        data["metadata"] = dumps(data.get("metadata") or {})
        data["evaluations"] = dumps(data.get("evaluations") or [])
    for _, _, _, data in g.edges(keys=True, data=True):
        data["action"] = dumps(data.get("action"))
        data["metadata"] = dumps(data.get("metadata") or {})
        data["proposal_events"] = dumps(data.get("proposal_events") or [])
    nx.write_graphml(g, path)


def _evaluation_payload(record: EvaluationRecord) -> dict[str, Any]:
    return {
        "evaluation_id": record.evaluation_id,
        "evaluator_id": record.evaluator_id,
        "state_id": record.state_id,
        "evaluator": record.evaluator,
        "version": record.version,
        "config_hash": record.config_hash,
        "metrics": record.metrics,
        "metadata": record.metadata,
        "created_at": record.created_at,
    }


def _decision_payload(record: DecisionRecord) -> dict[str, Any]:
    return {
        "decision_id": record.decision_id,
        "run_id": record.run_id,
        "policy": record.policy,
        "role": record.role,
        "model": record.model,
        "selected_state_ids": record.selected_state_ids,
        "input_context": record.input_context,
        "tool_calls": record.tool_calls,
        "output": record.output,
        "metadata": record.metadata,
        "created_at": record.created_at,
        "created_step": record.created_step,
    }


def _event_payload(record: ProposalEvent[Any]) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "decision_id": record.decision_id,
        "run_id": record.run_id,
        "parent_id": record.parent_id,
        "child_id": record.child_id,
        "edge_id": record.edge_id,
        "action": record.action,
        "metadata": record.metadata,
        "outcome": record.outcome,
        "error": record.error,
        "created_at": record.created_at,
        "created_step": record.created_step,
        "proposal_index": record.proposal_index,
        "sequence_index": record.sequence_index,
    }
