from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

State = TypeVar("State")


def _never(_: object) -> bool:
    return False


@dataclass(frozen=True)
class Objective(Generic[State]):
    """Optional scalar objective for optimization-oriented searches."""

    score: Callable[[State], float]
    goal_reached: Callable[[State], bool] = _never
    maximize: bool = True

    def better(self, candidate: float, incumbent: float | None) -> bool:
        if incumbent is None:
            return True
        if self.maximize:
            return candidate > incumbent
        return candidate < incumbent
