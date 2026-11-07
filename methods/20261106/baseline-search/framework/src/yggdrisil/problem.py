from __future__ import annotations

from typing import Protocol, TypeVar

State = TypeVar("State")
Action = TypeVar("Action", contravariant=True)


class Problem(Protocol[State, Action]):
    """Required state-space semantics.

    ``validate_state`` and ``validate_action`` are optional runtime hooks rather
    than protocol requirements.
    """

    @property
    def initial_state(self) -> State: ...

    def state_key(self, state: State) -> str: ...

    def apply(self, state: State, action: Action) -> State: ...
