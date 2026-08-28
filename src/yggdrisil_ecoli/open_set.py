"""Deterministic, recoverable scheduling for agent genome deletion search."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from yggdrisil import ReadOnlyStateGraph, RunStatus
from yggdrisil.agents import ExplorationRequest
from yggdrisil.types import EvaluationRecord, ProposalEvent, StateNode

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

    def metadata(self, max_action_size: int) -> dict[str, object]:
        return {
            "type": "recoverable_open_set",
            "version": SCHEDULER_VERSION,
            "active_width": self.active_width,
            "parents_per_step": self.parents_per_step,
            "fallback_action_caps": list(self.fallback_action_caps),
            "effective_fallback_action_caps": list(
                _effective_caps(max_action_size, self.fallback_action_caps)
            ),
            "ordering": {
                "exploitation": [
                    "deletion_count_desc",
                    "fba_growth_desc",
                    "essential_deleted_asc",
                    "conditional_essential_deleted_asc",
                    "ambiguous_deleted_asc",
                    "unknown_deleted_asc",
                    "broken_modules_asc",
                ],
                "diversity": "alternating_jaccard_distance_slots",
                "scheduling": "fewest_completed_attempts_first",
            },
            "viability": {
                "fba_feasible": True,
                "growth_rate": ">0",
                "resource_allocation_feasible_at_growth_floor": True,
            },
            "ranking_evidence_only": [
                "essentiality",
                "module_retention",
                "unknown_evidence",
            ],
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    node: StateNode[GenomeState]
    attempts: int
    records: tuple[EvaluationRecord, ...]
    priority: tuple[int, float, int, int, int, int, int]


class RecoverableOpenSetSelector:
    """Keep every viable state recoverable until a global runner limit stops.

    Structural leaves are deliberately irrelevant: a viable state stays open after
    gaining children, so lethal children cannot make their parent unreachable.
    """

    model: str | None = None

    def __init__(
        self,
        *,
        evaluator_ids: Mapping[str, str],
        max_action_size: int,
        config: OpenSetConfig,
        seed: int,
        candidate_count: int,
        candidate_page_size: int,
        public_gene_id: Callable[[str], str] = str,
    ) -> None:
        missing = {
            "essentiality",
            "fba",
            "module_retention",
            "resource_allocation",
        } - set(evaluator_ids)
        if missing:
            raise ValueError(f"missing evaluator identities: {sorted(missing)}")
        if max_action_size < 1:
            raise ValueError("max_action_size must be positive")
        if candidate_count < 1 or candidate_page_size < 1:
            raise ValueError("candidate count and page size must be positive")
        self.evaluator_ids = dict(evaluator_ids)
        self.max_action_size = max_action_size
        self.config = config
        self.seed = seed
        self.candidate_count = candidate_count
        self.candidate_page_size = candidate_page_size
        self.public_gene_id = public_gene_id
        self._attempted: dict[str, frozenset[tuple[str, ...]]] = {}

    def attempted_actions(self, state_id: str) -> frozenset[tuple[str, ...]]:
        """Return exact actions reconstructed during the latest selection."""

        return self._attempted.get(state_id, frozenset())

    def select(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        status: RunStatus,
    ) -> list[ExplorationRequest]:
        if status.run_id is None:
            raise ValueError("recoverable open-set search requires a run_id")

        decisions = graph.decisions(run_id=status.run_id)
        events = graph.proposal_events(run_id=status.run_id)
        materialized = [event for event in events if self._materialized(graph, event)]
        materialized_ids = {event.event_id for event in materialized}
        by_decision: dict[str, list[ProposalEvent[DeleteGenes]]] = defaultdict(list)
        for event in events:
            by_decision[event.decision_id].append(event)
        attempts: Counter[str] = Counter()
        for decision in decisions:
            if (
                decision.role != "explorer"
                or decision.metadata.get("attempt_status") == "failed"
            ):
                continue
            decision_events = by_decision.get(decision.decision_id, ())
            if decision_events and not any(
                event.event_id in materialized_ids for event in decision_events
            ):
                continue
            attempts.update(decision.selected_state_ids)

        outgoing: dict[str, list[ProposalEvent[DeleteGenes]]] = defaultdict(list)
        attempted: dict[str, set[tuple[str, ...]]] = defaultdict(set)
        for event in materialized:
            outgoing[event.parent_id].append(event)
            attempted[event.parent_id].add(_action_signature(event.action))
        self._attempted = {
            state_id: frozenset(signatures)
            for state_id, signatures in attempted.items()
        }

        candidates: list[_Candidate] = []
        for node in graph.states():
            state_attempts = attempts[node.state_id]
            records = tuple(graph.evaluations(node.state_id))
            if not self._is_viable(records):
                continue
            candidates.append(
                _Candidate(
                    node=node,
                    attempts=state_attempts,
                    records=records,
                    priority=self._priority(node, records),
                )
            )

        active = self._diverse_window(candidates)
        scheduled = sorted(
            enumerate(active),
            key=lambda item: (item[1].attempts, item[0]),
        )
        return [
            ExplorationRequest(
                state_id=candidate.node.state_id,
                guidance=self._guidance(
                    graph,
                    candidate,
                    outgoing.get(candidate.node.state_id, ()),
                    status.step,
                ),
            )
            for _rank, candidate in scheduled[: self.config.parents_per_step]
        ]

    def _is_viable(self, records: Sequence[EvaluationRecord]) -> bool:
        active = self._active_records(records)
        fba = active.get("fba")
        resource = active.get("resource_allocation")
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

    def _active_records(
        self, records: Sequence[EvaluationRecord]
    ) -> dict[str, EvaluationRecord]:
        by_id = {record.evaluator_id: record for record in records}
        return {
            name: record
            for name, evaluator_id in self.evaluator_ids.items()
            if (record := by_id.get(evaluator_id)) is not None
        }

    def _priority(
        self,
        node: StateNode[GenomeState],
        records: Sequence[EvaluationRecord],
    ) -> tuple[int, float, int, int, int, int, int]:
        active = self._active_records(records)
        essential_record = active.get("essentiality")
        module_record = active.get("module_retention")
        essential = essential_record.metrics if essential_record is not None else {}
        fba = active["fba"].metrics
        modules = module_record.metrics if module_record is not None else {}
        return (
            len(node.state.deleted_genes),
            _number(fba.get("growth_rate")),
            -_count(essential.get("n_essential_deleted")),
            -_count(essential.get("n_conditional_essential_deleted")),
            -_count(essential.get("n_ambiguous_deleted")),
            -_count(essential.get("n_unknown_deleted")),
            -_count(modules.get("n_broken")),
        )

    def _diverse_window(self, candidates: Sequence[_Candidate]) -> list[_Candidate]:
        remaining = list(candidates)
        selected: list[_Candidate] = []
        while remaining and len(selected) < self.config.active_width:
            if not selected or len(selected) % 2 == 0:
                candidate = max(
                    remaining,
                    key=lambda item: (
                        item.priority,
                        -item.attempts,
                        _seeded_tie_break(self.seed, item.node.state_id),
                    ),
                )
            else:
                candidate = max(
                    remaining,
                    key=lambda item: (
                        _minimum_distance(item, selected),
                        item.priority,
                        -item.attempts,
                        _seeded_tie_break(self.seed, item.node.state_id),
                    ),
                )
            selected.append(candidate)
            remaining.remove(candidate)
        return selected

    def _guidance(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        candidate: _Candidate,
        events: Sequence[ProposalEvent[DeleteGenes]],
        step: int,
    ) -> str:
        caps = _effective_caps(
            self.max_action_size,
            self.config.fallback_action_caps,
        )
        preferred_cap = caps[min(candidate.attempts, len(caps) - 1)]
        remaining = max(
            1,
            self.candidate_count - len(candidate.node.state.deleted_genes),
        )
        page_count = max(1, math.ceil(remaining / self.candidate_page_size))
        preview_page = step % page_count
        history = []
        for event in events:
            child_records = (
                graph.evaluations(event.child_id) if event.child_id is not None else []
            )
            history.append(
                {
                    "action_gene_ids": [
                        self.public_gene_id(gene) for gene in event.action.genes
                    ],
                    "action_size": len(event.action.genes),
                    "action_sha256": _action_hash(event.action),
                    "graph_outcome": event.outcome,
                    "child_state_id": event.child_id,
                    "child_viability": self._viability_label(child_records),
                    "child_evaluations": _scalar_active_evaluations(
                        self._active_records(child_records)
                    ),
                }
            )
        return "\n".join(
            [
                f"RECOVERY_ATTEMPT: {candidate.attempts + 1}",
                f"GLOBAL_MAX_ACTION_SIZE: {self.max_action_size}",
                f"SUGGESTED_FALLBACK_CEILING: {preferred_cap}",
                f"CANDIDATE_PREVIEW_PAGE: {preview_page}",
                "The fallback ceiling is guidance, not a required bundle size. "
                f"Choose any action size from 1 to {self.max_action_size} according "
                "to the strength and confidence of the evidence.",
                "Do not repeat an exact previous sibling action. Learn from lethal "
                "siblings by choosing a smaller action or different genes.",
                "PREVIOUS_SIBLING_OUTCOMES: " + json.dumps(history, sort_keys=True),
            ]
        )

    def _viability_label(self, records: Sequence[EvaluationRecord]) -> str:
        if not records:
            return "not_evaluated"
        return "viable" if self._is_viable(records) else "nonviable"

    def _materialized(
        self,
        graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
        event: ProposalEvent[DeleteGenes],
    ) -> bool:
        child_records = (
            graph.evaluations(event.child_id) if event.child_id is not None else ()
        )
        return (
            event.outcome in {"created", "reused"}
            and event.child_id is not None
            and any(
                record.evaluator_id == self.evaluator_ids["fba"]
                for record in child_records
            )
            and any(
                record.evaluator_id == self.evaluator_ids["resource_allocation"]
                for record in child_records
            )
        )


def _effective_caps(maximum: int, configured: Sequence[int]) -> tuple[int, ...]:
    caps: list[int] = []
    for configured_cap in configured:
        cap = min(maximum, configured_cap)
        if cap not in caps:
            caps.append(cap)
    if 1 not in caps:
        caps.append(1)
    return tuple(caps)


def _action_signature(action: DeleteGenes) -> tuple[str, ...]:
    return tuple(sorted(action.genes))


def _action_hash(action: DeleteGenes) -> str:
    payload = json.dumps(_action_signature(action), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _minimum_distance(candidate: _Candidate, selected: Sequence[_Candidate]) -> float:
    if not selected:
        return 0.0
    genes = candidate.node.state.deleted_genes
    return min(
        _jaccard_distance(genes, item.node.state.deleted_genes) for item in selected
    )


def _jaccard_distance(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return 1.0 - len(left & right) / len(union)


def _seeded_tie_break(seed: int, state_id: str) -> int:
    digest = hashlib.sha256(f"open-set:{seed}:{state_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _scalar_active_evaluations(
    records: Mapping[str, EvaluationRecord],
) -> dict[str, dict[str, object]]:
    return {
        name: {
            key: value
            for key, value in record.metrics.items()
            if value is None or isinstance(value, (bool, int, float, str))
        }
        for name, record in sorted(records.items())
    }
