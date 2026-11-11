from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from typing import Generic, TypeVar

from yggdrisil.graph.base import ReadOnlyStateGraph
from yggdrisil.limits import RunStatus
from yggdrisil.policy import Decision, Proposal
from yggdrisil.types import EvaluationRecord, StateNode

State = TypeVar("State")
Action = TypeVar("Action")

ActionSampler = Callable[[State, random.Random], Sequence[Action]]
Priority = Callable[[StateNode[State], Sequence[EvaluationRecord]], float]
Eligibility = Callable[[StateNode[State], Sequence[EvaluationRecord]], bool]


class BestFirstPolicy(Generic[State, Action]):
    """Expand the highest-scoring frontier states first."""

    def __init__(
        self,
        sample_actions: ActionSampler[State, Action],
        priority: Priority[State],
        *,
        n_proposals: int = 1,
        maximize: bool = True,
        seed: int | None = None,
        frontier_limit: int = 1_000,
        eligible: Eligibility[State] | None = None,
    ) -> None:
        if n_proposals < 0:
            raise ValueError("n_proposals must be non-negative")
        if frontier_limit <= 0:
            raise ValueError("frontier_limit must be positive")
        self.sample_actions = sample_actions
        self.priority = priority
        self.n_proposals = n_proposals
        self.maximize = maximize
        self.frontier_limit = frontier_limit
        self.eligible = eligible
        self._seed = seed
        self._rng = random.Random(seed)

    async def step(
        self,
        graph: ReadOnlyStateGraph[State, Action],
        status: RunStatus,
    ) -> list[Decision[Action]]:
        if self.n_proposals == 0:
            return []
        rng = self._rng_for_step(status)
        nodes = graph.frontier(limit=self.frontier_limit)
        scored = []
        for node in nodes:
            evaluations = graph.evaluations(node.state_id)
            if self.eligible is not None and not self.eligible(node, evaluations):
                continue
            scored.append((self.priority(node, evaluations), node))
        scored.sort(
            key=lambda item: (item[0], item[1].state_id),
            reverse=self.maximize,
        )

        proposals: list[Proposal[Action]] = []
        selected: list[str] = []
        for _, node in scored:
            actions = list(self.sample_actions(node.state, rng))
            rng.shuffle(actions)
            for action in actions:
                if node.state_id not in selected:
                    selected.append(node.state_id)
                proposals.append(
                    Proposal(
                        parent_id=node.state_id,
                        action=action,
                    )
                )
                if len(proposals) >= self.n_proposals:
                    return [
                        Decision(
                            role="policy",
                            proposals=proposals,
                            selected_state_ids=selected,
                        )
                    ]
        if not proposals:
            return []
        return [
            Decision(
                role="policy",
                proposals=proposals,
                selected_state_ids=selected,
            )
        ]

    def _rng_for_step(self, status: RunStatus) -> random.Random:
        if self._seed is None:
            return self._rng
        return random.Random(f"{self._seed}:{status.step}")
