"""Versioned shared documents, separate from search state and agent checkpoints."""

from dataclasses import dataclass, field
from typing import Any

from yggdrisil.exceptions import GraphError
from yggdrisil.serialize import serializable


class MemoryConflictError(GraphError):
    """A document changed since the agent read it."""


@serializable
@dataclass(frozen=True)
class MemoryUpdate:
    """Replace a shared document at its current revision; use revision 0 to create it."""

    key: str
    content: str
    expected_revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("memory key must be a nonempty string")
        if not isinstance(self.content, str):
            raise TypeError("memory content must be text")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")


@serializable
@dataclass(frozen=True)
class MemoryEntry:
    namespace: str
    key: str
    content: str
    revision: int
    metadata: dict[str, Any]
    decision_id: str
    created_at: str
