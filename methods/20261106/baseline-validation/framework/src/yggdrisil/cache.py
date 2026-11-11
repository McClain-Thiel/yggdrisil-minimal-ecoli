from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

from yggdrisil.serialize import dumps, loads, stable_hash

F = TypeVar("F", bound=Callable[..., Any])


class ToolCache:
    """Optional content-addressed cache for domain tools, keyed by state + call."""

    def __init__(self, path: str | Path) -> None:
        import sqlite3

        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db_path = str(self.path)
        else:
            db_path = ":memory:"
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_results (
                cache_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.commit()

    def make_key(
        self,
        state_key: str,
        tool_name: str,
        tool_version: str,
        arguments: Any,
    ) -> str:
        return stable_hash(
            {
                "state_key": state_key,
                "tool_name": tool_name,
                "tool_version": tool_version,
                "arguments": arguments,
            }
        )

    def get(self, key: str) -> Any | None:
        found, value = self.lookup(key)
        return value if found else None

    def lookup(self, key: str) -> tuple[bool, Any]:
        row = self._conn.execute(
            "SELECT value_json FROM tool_results WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return False, None
        return True, loads(row[0])

    def set(self, key: str, value: Any) -> None:
        self._conn.execute(
            """
            INSERT INTO tool_results (cache_key, value_json)
            VALUES (?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET value_json = excluded.value_json
            """,
            (key, dumps(value)),
        )
        self._conn.commit()

    def tool(self, name: str, version: str = "0") -> Callable[[F], F]:
        return cached_tool(self, name=name, version=version)

    def close(self) -> None:
        self._conn.close()


def cached_tool(
    cache: ToolCache,
    *,
    name: str,
    version: str = "0",
) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            state_key = str(kwargs.get("state_id", args[0] if args else ""))
            extra_args = args[1:] if args else ()
            extra_kwargs = {k: v for k, v in kwargs.items() if k != "state_id"}
            key = cache.make_key(
                state_key=state_key,
                tool_name=name,
                tool_version=version,
                arguments={"args": extra_args, "kwargs": extra_kwargs},
            )
            found, value = cache.lookup(key)
            if found:
                return value
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            cache.set(key, result)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
