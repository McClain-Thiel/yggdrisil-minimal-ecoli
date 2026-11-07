"""Bounded Edison Literature queries, persisted before waiting for remote work."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from yggdrisil.agents import ToolInputError

logger = logging.getLogger(__name__)
JOB = "job-futurehouse-paperqa3"
_API = "https://api.platform.edisonscientific.com"


async def _login(client: httpx.AsyncClient, key: str) -> None:
    response = await client.post("/auth/login", json={"api_key": key})
    response.raise_for_status()
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"


async def _get_task(
    client: httpx.AsyncClient, key: str, task_id: str, *, lite: bool
) -> dict[str, Any]:
    url = f"/v0.2/trajectories/{task_id}"
    response = await client.get(
        url, params={"lite": str(lite).lower(), "history": "false"}
    )
    if response.status_code == 401:
        logger.info("Refreshing Edison session for job %s", task_id)
        await _login(client, key)
        response = await client.get(
            url, params={"lite": str(lite).lower(), "history": "false"}
        )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


async def _cited_result(payload: dict[str, Any]) -> dict[str, Any]:
    # Edison v0.2 stores large states behind signed URLs (edison-client 0.18 protocol).
    frame = payload["environment_frame"]
    state = frame.get("state")
    if state is None:
        # Object storage must never receive the API session's authorization header.
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as storage:
            response = await storage.get(frame["download_url"])
            response.raise_for_status()
        raw = response.content
        state = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
    answer = state["state"]["response"]["answer"]
    usage = state["state"].get("info", {}).get("usage", {})
    return {
        "formatted_answer": answer["formatted_answer"],
        "has_successful_answer": answer.get("has_successful_answer"),
        "total_cost": usage.get("total_cost"),
        "total_queries": usage.get("total_queries"),
        "job_name": payload.get("crow"),
        "created_at": payload.get("created_at"),
    }


class EdisonLiterature:
    """Share paid jobs within one experiment; other experiments remain isolated."""

    def __init__(
        self,
        path: Path,
        namespace: str,
        *,
        max_queries: int = 1,
        timeout_s: float = 1800,
        poll_interval_s: float = 15,
    ) -> None:
        if not namespace or max_queries < 1 or timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("namespace and positive Edison limits are required")
        self.path = path
        self.namespace = namespace
        self.max_queries = max_queries
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self._last_poll: dict[str, float] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS edison_queries ("
                "namespace TEXT NOT NULL, query_hash TEXT NOT NULL, query TEXT NOT NULL, "
                "task_id TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, "
                "PRIMARY KEY (namespace, query_hash))"
            )

    def saved_queries(self) -> list[dict[str, Any]]:
        """Expose exact questions so a fresh planner can resume unfinished jobs."""
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT query, task_id, status FROM edison_queries WHERE namespace=?",
                    (self.namespace,),
                )
            ]

    def _rows(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM edison_queries WHERE namespace=?", (self.namespace,)
                )
            ]

    def snapshots(self) -> list[dict[str, Any]]:
        """Read complete local job records without contacting Edison."""
        return [self._view(row) for row in self._rows()]

    def _find(self, task_id: str) -> dict[str, Any]:
        for row in self._rows():
            if row["task_id"] == task_id:
                return row
        raise ToolInputError(
            f"Unknown Edison job {task_id!r}. Saved jobs: {self.saved_queries()}"
        )

    @staticmethod
    def _view(
        row: dict[str, Any], *, offset: int = 0, count: int | None = None
    ) -> dict[str, Any]:
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        text = result.get("formatted_answer", "")
        end = offset + count if count is not None else len(text)
        return {
            **result,
            **({"formatted_answer": text[offset:end]} if text else {}),
            "query": row["query"],
            "job": JOB,
            "task_id": row["task_id"],
            "status": row["status"],
            "source_url": f"https://platform.edisonscientific.com/trajectories/{row['task_id']}",
            "cached": row["result_json"] is not None,
            "submitted": False,
            "offset": offset,
            "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
        }

    def _reserve(self, query: str) -> tuple[dict[str, Any], bool]:
        digest = hashlib.sha256(f"{JOB}\n{query}".encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM edison_queries WHERE namespace=? AND query_hash=?",
                (self.namespace, digest),
            ).fetchone()
            if row is not None:
                return dict(row), False
            count = db.execute(
                "SELECT COUNT(*) FROM edison_queries WHERE namespace=?",
                (self.namespace,),
            ).fetchone()[0]
            if count >= self.max_queries:
                existing = [
                    dict(item)
                    for item in db.execute(
                        "SELECT query, task_id, status FROM edison_queries "
                        "WHERE namespace=?",
                        (self.namespace,),
                    )
                ]
                raise ToolInputError(
                    f"Edison query limit ({self.max_queries}) reached. "
                    "Reuse the exact previous query or saved shared evidence. "
                    f"Saved jobs: {json.dumps(existing)}"
                )
            if not os.environ.get("EDISON_PLATFORM_API_KEY"):
                raise RuntimeError(
                    "Set EDISON_PLATFORM_API_KEY in the environment or .env"
                )
            task_id = str(uuid4())
            db.execute(
                "INSERT INTO edison_queries VALUES (?, ?, ?, ?, 'submitting', NULL)",
                (self.namespace, digest, query, task_id),
            )
            return {
                "query_hash": digest,
                "task_id": task_id,
                "result_json": None,
                "query": query,
                "status": "submitting",
            }, True

    def _save(self, row: dict[str, Any], status: str, result: Any = None) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE edison_queries SET status=?, result_json=COALESCE(?, result_json) "
                "WHERE namespace=? AND query_hash=?",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    self.namespace,
                    row["query_hash"],
                ),
            )

    async def submit_edison(self, query: str) -> dict[str, Any]:
        """Submit ONE focused literature question and immediately return its job ID.

        The remote job takes 13–20 minutes. Do other work while it runs. An exact
        repeated question resumes the same job; the per-experiment limit is durable.
        """
        if not query.strip():
            raise ToolInputError("Provide a nonempty research question")
        row, created = self._reserve(query.strip())
        if created:
            key = os.environ["EDISON_PLATFORM_API_KEY"]
            async with httpx.AsyncClient(base_url=_API, timeout=30) as client:
                await _login(client, key)
                response = await client.post(
                    "/v0.1/crows",
                    json={
                        "id": row["task_id"],
                        "name": JOB,
                        "query": row["query"],
                        "runtime_config": {"timeout": max(1, int(self.timeout_s))},
                    },
                )
                response.raise_for_status()
                if str(response.json()["trajectory_id"]) != row["task_id"]:
                    raise RuntimeError("Edison returned a different task ID")
            self._save(row, "queued")
            row["status"] = "queued"
            logger.info("Submitted Edison Literature job: %s", row["task_id"])
        return {**self._view(row), "submitted": created}

    async def _refresh(self, row: dict[str, Any]) -> dict[str, Any]:
        if row["status"] not in {"submitting", "queued", "in progress", "success"}:
            raise RuntimeError(
                f"Edison job {row['status']}: {self._view(row)['source_url']}"
            )
        if row["result_json"] is not None:
            return row
        last = self._last_poll.get(row["task_id"])
        if last is not None and time.monotonic() - last < self.poll_interval_s:
            return row
        key = os.environ.get("EDISON_PLATFORM_API_KEY")
        if not key:
            raise RuntimeError("Set EDISON_PLATFORM_API_KEY in the environment or .env")
        async with httpx.AsyncClient(base_url=_API, timeout=30) as client:
            await _login(client, key)
            task = await _get_task(client, key, row["task_id"], lite=True)
            self._save(row, task["status"])
            self._last_poll[row["task_id"]] = time.monotonic()
            if task["status"] not in {"queued", "in progress", "success"}:
                raise RuntimeError(
                    f"Edison job {task['status']}: {self._view(row)['source_url']}"
                )
            if task["status"] == "success":
                payload = await _get_task(client, key, row["task_id"], lite=False)
                result = await _cited_result(payload)
                if not result.get("formatted_answer"):
                    raise RuntimeError(
                        f"Edison returned no cited answer: {self._view(row)['source_url']}"
                    )
                self._save(row, task["status"], result)
        return self._find(row["task_id"])

    async def check_edison(
        self, task_id: str, offset: int = 0, count: int = 16000
    ) -> dict[str, Any]:
        """Check a saved job once or read a cached answer page; never wait for completion.

        A pending job is not literature evidence. Return to the planner and continue
        other work. The coordinator polls pending jobs without additional model calls.
        """
        if offset < 0 or not 1 <= count <= 20000:
            raise ToolInputError(
                "offset must be non-negative and count must be 1 to 20000"
            )
        row = await self._refresh(self._find(task_id))
        return self._view(row, offset=offset, count=count)

    async def poll_pending(self) -> list[dict[str, Any]]:
        """Refresh at most once per polling interval; preserve jobs across policy restarts."""
        return [self._view(await self._refresh(row)) for row in self._rows()]
