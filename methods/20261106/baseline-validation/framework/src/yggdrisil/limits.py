from __future__ import annotations

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RunLimits:
    max_states: int | None = None
    max_steps: int | None = None
    max_wall_time_s: float | None = None
    max_evaluation_cost: float | None = None

    def __post_init__(self) -> None:
        if self.max_states is not None and self.max_states < 1:
            raise ValueError("max_states must be positive")
        if isinstance(self.max_evaluation_cost, bool):
            raise ValueError("max_evaluation_cost must be a finite non-negative number")
        for name, value in (
            ("max_steps", self.max_steps),
            ("max_wall_time_s", self.max_wall_time_s),
            ("max_evaluation_cost", self.max_evaluation_cost),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.max_evaluation_cost is not None and not math.isfinite(
            self.max_evaluation_cost
        ):
            raise ValueError("max_evaluation_cost must be a finite non-negative number")

    def reached(
        self,
        *,
        unique_states: int,
        step: int,
        elapsed_s: float,
        evaluation_cost: float | None = 0.0,
    ) -> str | None:
        if self.max_states is not None and unique_states >= self.max_states:
            return "max_states"
        if self.max_steps is not None and step >= self.max_steps:
            return "max_steps"
        if self.max_wall_time_s is not None and elapsed_s >= self.max_wall_time_s:
            return "max_wall_time_s"
        if (
            self.max_evaluation_cost is not None
            and evaluation_cost is not None
            and evaluation_cost >= self.max_evaluation_cost
        ):
            return "max_evaluation_cost"
        return None


@dataclass(frozen=True)
class RunStatus:
    step: int
    unique_states: int
    edges: int
    elapsed_s: float
    limits: RunLimits
    run_id: str | None = None
    evaluation_cost: float = 0.0

    def with_counts(self, *, unique_states: int, edges: int) -> RunStatus:
        return replace(self, unique_states=unique_states, edges=edges)
