"""Atomic writers shared by local artifact builders."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, content: str) -> None:
    atomic_bytes(path, (content.rstrip("\n") + "\n").encode())


def atomic_json(path: Path, payload: object) -> None:
    atomic_text(path, json.dumps(payload, indent=2, sort_keys=True))
