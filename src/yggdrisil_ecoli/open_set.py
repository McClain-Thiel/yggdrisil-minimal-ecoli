"""Deterministic, recoverable scheduling for agent genome deletion search."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from yggdrisil import ReadOnlyStateGraph, RunStatus
from yggdrisil.agents import ExplorationRequest
from yggdrisil.types import EvaluationRecord, StateNode

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.state import GenomeState

SCHEDULER_VERSION = 2


@dataclass(frozen=True, slots=True)
class OpenSetConfig:
    """Secret-free controls for the persistent open-state scheduler."""

    active_width: int = 16
    parents_per_step: int = 4
    fallback_action_caps: tuple[int, ...] = (20, 10, 5, 1)

    def __post_init__(self) -> None:
        for name in ("active_width", "parents_per_step"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if self.parents_per_step > self.active_width:
            raise ValueError("parents_per_step must not exceed active_width")
        if not self.fallback_action_caps or any(
            cap < 1 for cap in self.fallback_action_caps
        ):
            raise ValueError("fallback_action_caps must contain positive values")


@dataclass(kw_only=True)
class RecoverableOpenSetSelector:
    """Reconstruct viable open states and completed attempts from the run history.

    A viable parent remains eligible after gaining children, including lethal ones.
    """

    evaluator_ids: Mapping[str, str]
    max_action_size: int
    config: OpenSetConfig
    seed: int
    candidate_count: int
    candidate_page_size: int
    public_gene_id: Callable[[str], str] = str
    model: ClassVar[str | None] = None
    _attempted: dict[str, set[tuple[str, ...]]] = field(
        default_factory=dict, init=False
    )

    def __post_init__(self) -> None:
        missing = {"fba"} - self.evaluator_ids.keys()
        if missing:
            raise ValueError(
                f"missing active growth-gate identities: {sorted(missing)}"
            )
        if (
            min(self.max_action_size, self.candidate_count, self.candidate_page_size)
            < 1
        ):
            raise ValueError(
                "action size, candidate count and page size must be positive"
            )
        self.evaluator_ids = dict(self.evaluator_ids)

    def attempted_actions(self, state_id: str) -> frozenset[tuple[str, ...]]:
        return frozenset(self._attempted.get(state_id, ()))

    def select(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        status: RunStatus,
    ) -> list[ExplorationRequest]:
        if status.run_id is None:
            raise ValueError("recoverable open-set search requires a run_id")
        nodes = graph.states()
        evidence = {
            node.state_id: self._active_records(graph.evaluations(node.state_id))
            for node in nodes
        }
        events = graph.proposal_events(run_id=status.run_id)
        completed_decisions = set()
        history: dict[str, list[dict[str, object]]] = defaultdict(list)
        self._attempted = defaultdict(set)
        for event in events:
            active = evidence.get(event.child_id or "", {})
            if (
                event.outcome not in {"created", "reused"}
                or not {"fba"} <= active.keys()
            ):
                continue
            completed_decisions.add(event.decision_id)
            self._attempted[event.parent_id].add(event.action.genes)
            history[event.parent_id].append(
                {
                    "action_gene_ids": [
                        self.public_gene_id(gene) for gene in event.action.genes
                    ],
                    "action_size": len(event.action.genes),
                    "graph_outcome": event.outcome,
                    "child_state_id": event.child_id,
                    "child_viability": "viable"
                    if passes_growth_gates(active)
                    else "nonviable",
                    "child_evaluations": {
                        name: record.metrics for name, record in active.items()
                    },
                }
            )
        proposed = {event.decision_id for event in events}
        attempts = Counter(
            state_id
            for decision in graph.decisions(run_id=status.run_id)
            if decision.role == "explorer"
            and decision.metadata.get("attempt_status") != "failed"
            and (
                decision.decision_id not in proposed
                or decision.decision_id in completed_decisions
            )
            for state_id in decision.selected_state_ids
        )
        candidates = [
            node for node in nodes if passes_growth_gates(evidence[node.state_id])
        ]
        priority = {
            node.state_id: (
                self._priority(node, evidence[node.state_id]),
                -attempts[node.state_id],
                _seeded_tie_break(self.seed, node.state_id),
            )
            for node in candidates
        }
        active_nodes: list[StateNode[GenomeState]] = []

        def rank(node: StateNode[GenomeState]) -> tuple[object, ...]:
            # Alternate exploitation and maximum distance from selected deletion sets.
            diversity = 0.0
            if len(active_nodes) % 2:
                diversity = min(
                    _jaccard_distance(
                        node.state.deleted_genes, chosen.state.deleted_genes
                    )
                    for chosen in active_nodes
                )
            return diversity, priority[node.state_id]

        while candidates and len(active_nodes) < self.config.active_width:
            chosen = max(candidates, key=rank)
            active_nodes.append(chosen)
            candidates.remove(chosen)
        scheduled = sorted(active_nodes, key=lambda node: attempts[node.state_id])
        caps = _effective_caps(self.max_action_size, self.config.fallback_action_caps)
        requests = []
        for node in scheduled[: self.config.parents_per_step]:
            count = attempts[node.state_id]
            remaining = self.candidate_count - len(node.state.deleted_genes)
            pages = max(1, math.ceil(remaining / self.candidate_page_size))
            requests.append(
                ExplorationRequest(
                    node.state_id,
                    guidance=json.dumps(
                        {
                            "attempt": count + 1,
                            "suggested_fallback_ceiling": caps[
                                min(count, len(caps) - 1)
                            ],
                            "candidate_preview_page": status.step % pages,
                            "previous_sibling_outcomes": history[node.state_id],
                        },
                        sort_keys=True,
                    ),
                )
            )
        return requests

    def _active_records(
        self, records: Sequence[EvaluationRecord]
    ) -> dict[str, EvaluationRecord]:
        by_id = {record.evaluator_id: record for record in records}
        return {
            name: by_id[identity]
            for name, identity in self.evaluator_ids.items()
            if identity in by_id
        }

    def _priority(
        self,
        node: StateNode[GenomeState],
        active: Mapping[str, EvaluationRecord],
    ) -> tuple[int, float, int, int, int, int, int]:
        metrics = {name: record.metrics for name, record in active.items()}
        essential = metrics.get("essentiality", {})
        return (
            len(node.state.deleted_genes),
            _number(metrics["fba"].get("growth_rate")),
            -_count(essential.get("n_essential_deleted")),
            -_count(essential.get("n_conditional_essential_deleted")),
            -_count(essential.get("n_ambiguous_deleted")),
            -_count(essential.get("n_unknown_deleted")),
            -_count(metrics.get("module_retention", {}).get("n_broken")),
        )


def _effective_caps(maximum: int, configured: Sequence[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys([min(maximum, cap) for cap in configured] + [1]))


def _number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _jaccard_distance(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return 1.0 - len(left & right) / len(union)


def _seeded_tie_break(seed: int, state_id: str) -> int:
    digest = hashlib.sha256(f"open-set:{seed}:{state_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def passes_growth_gates(evidence: Mapping[str, EvaluationRecord]) -> bool:
    fba = evidence.get("fba")
    return (
        fba is not None
        and fba.metrics.get("feasible") is True
        and _number(fba.metrics.get("growth_rate")) > 0
    )
