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
Eligibility = Callable[[StateNode[State], Sequence[EvaluationRecord]], bool]


class RandomPolicy(Generic[State, Action]):
    """Baseline policy: sample actions from existing states."""

    def __init__(
        self,
        sample_actions: ActionSampler[State, Action],
        *,
        n_proposals: int = 1,
        seed: int | None = None,
        frontier_only: bool = False,
        eligible: Eligibility[State] | None = None,
    ) -> None:
        if n_proposals < 0:
            raise ValueError("n_proposals must be non-negative")
        self.sample_actions = sample_actions
        self.n_proposals = n_proposals
        self.frontier_only = frontier_only
        self.eligible = eligible
        self._seed = seed
        self._rng = random.Random(seed)

    async def step(
        self,
        graph: ReadOnlyStateGraph[State, Action],
        status: RunStatus,
    ) -> list[Decision[Action]]:
        rng = self._rng_for_step(status)
        nodes = graph.frontier() if self.frontier_only else graph.states()
        if self.eligible is not None:
            nodes = [
                node
                for node in nodes
                if self.eligible(node, graph.evaluations(node.state_id))
            ]
        if not nodes:
            return []
        proposals: list[Proposal[Action]] = []
        for _ in range(self.n_proposals):
            parent = self._pick(nodes, rng)
            actions = list(self.sample_actions(parent.state, rng))
            if not actions:
                continue
            action = actions[0] if len(actions) == 1 else rng.choice(list(actions))
            proposals.append(Proposal(parent_id=parent.state_id, action=action))
        if not proposals:
            return []
        selected = list(dict.fromkeys(proposal.parent_id for proposal in proposals))
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

    def _pick(
        self,
        nodes: Sequence[StateNode[State]],
        rng: random.Random,
    ) -> StateNode[State]:
        ordered = sorted(nodes, key=lambda n: n.state_id)
        return ordered[rng.randrange(len(ordered))]
