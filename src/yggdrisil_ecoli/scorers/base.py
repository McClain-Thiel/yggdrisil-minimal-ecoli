"""Common asynchronous scorer result, cache, and suite."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from yggdrisil_ecoli.state import GenomeState, genome_state_key


class ScoreResult(BaseModel):
    """One independent evidence result; no combined reward is defined."""

    model_config = ConfigDict(frozen=True)

    scorer: str
    version: str
    metrics: dict[str, Any]
    coverage: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class Scorer(Protocol):
    name: str
    version: str
    config_hash: str

    async def score(self, state: GenomeState) -> ScoreResult: ...


class ScoreCache:
    """SQLite cache keyed by state, scorer identity, version, and configuration."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            database = str(self.path)
        else:
            database = ":memory:"
        self._connection = sqlite3.connect(database)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS score_results (
                cache_key TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._connection.commit()

    def make_key(self, state_id: str, scorer: Scorer) -> str:
        payload = {
            "state_id": state_id,
            "scorer": scorer.name,
            "version": scorer.version,
            "config_hash": scorer.config_hash,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def get(self, key: str) -> ScoreResult | None:
        row = self._connection.execute(
            "SELECT result_json FROM score_results WHERE cache_key = ?", (key,)
        ).fetchone()
        return ScoreResult.model_validate_json(row[0]) if row is not None else None

    def set(self, key: str, result: ScoreResult) -> None:
        self._connection.execute(
            """
            INSERT INTO score_results (cache_key, result_json)
            VALUES (?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET result_json = excluded.result_json
            """,
            (key, result.model_dump_json()),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


class ScorerSuite:
    """Run independent scorers concurrently and cache each result separately."""

    def __init__(self, scorers: tuple[Scorer, ...], cache: ScoreCache) -> None:
        names = [scorer.name for scorer in scorers]
        if len(names) != len(set(names)):
            raise ValueError("scorer names must be unique within a suite")
        self.scorers = scorers
        self.cache = cache

    async def score(self, state: GenomeState) -> dict[str, ScoreResult]:
        state_id = genome_state_key(state)
        results: dict[str, ScoreResult] = {}
        missing: list[tuple[Scorer, str]] = []
        for scorer in self.scorers:
            cache_key = self.cache.make_key(state_id, scorer)
            cached = self.cache.get(cache_key)
            if cached is None:
                missing.append((scorer, cache_key))
            else:
                results[scorer.name] = cached
        if missing:
            computed = await asyncio.gather(
                *(scorer.score(state) for scorer, _cache_key in missing)
            )
            for (scorer, cache_key), result in zip(missing, computed, strict=True):
                if result.scorer != scorer.name or result.version != scorer.version:
                    raise ValueError(
                        f"scorer {scorer.name} returned inconsistent result identity"
                    )
                self.cache.set(cache_key, result)
                results[scorer.name] = result
        return {scorer.name: results[scorer.name] for scorer in self.scorers}
