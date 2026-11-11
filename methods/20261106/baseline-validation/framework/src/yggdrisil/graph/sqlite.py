from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

from yggdrisil.evaluation import EvaluationResult
from yggdrisil.exceptions import CycleError, UnknownStateError
from yggdrisil.graph.base import StateGraph
from yggdrisil.memory import MemoryConflictError, MemoryEntry, MemoryUpdate
from yggdrisil.serialize import dumps, loads, stable_hash
from yggdrisil.types import (
    DecisionRecord,
    Edge,
    EvaluationRecord,
    ProposalEvent,
    RunRecord,
    StateNode,
)

State = TypeVar("State")
Action = TypeVar("Action")

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS states (
    state_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_step INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL REFERENCES states(state_id),
    child_id TEXT NOT NULL REFERENCES states(state_id),
    action_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_step INTEGER NOT NULL,
    UNIQUE(parent_id, child_id, action_json)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    evaluator_id TEXT NOT NULL,
    state_id TEXT NOT NULL REFERENCES states(state_id),
    evaluator TEXT NOT NULL,
    version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(state_id, evaluator_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    policy TEXT NOT NULL,
    role TEXT NOT NULL,
    model TEXT,
    selected_state_ids_json TEXT NOT NULL DEFAULT '[]',
    input_context_json TEXT NOT NULL DEFAULT 'null',
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    output_json TEXT NOT NULL DEFAULT 'null',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_step INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal_events (
    event_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    parent_id TEXT NOT NULL,
    child_id TEXT,
    edge_id TEXT REFERENCES edges(edge_id),
    action_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    outcome TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    created_step INTEGER NOT NULL,
    proposal_index INTEGER NOT NULL,
    sequence_index INTEGER NOT NULL,
    UNIQUE(decision_id, proposal_index)
);

CREATE INDEX IF NOT EXISTS idx_edges_parent ON edges(parent_id);
CREATE INDEX IF NOT EXISTS idx_edges_child ON edges(child_id);
CREATE INDEX IF NOT EXISTS idx_states_created_step ON states(created_step);
CREATE INDEX IF NOT EXISTS idx_edges_created_step ON edges(created_step);
CREATE INDEX IF NOT EXISTS idx_runs_updated_at ON runs(updated_at);
CREATE INDEX IF NOT EXISTS idx_evaluations_state ON evaluations(state_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run_step ON decisions(run_id, created_step);
CREATE INDEX IF NOT EXISTS idx_proposal_events_decision ON proposal_events(decision_id);
CREATE INDEX IF NOT EXISTS idx_proposal_events_run_step ON proposal_events(run_id, created_step);
CREATE INDEX IF NOT EXISTS idx_proposal_events_parent ON proposal_events(parent_id);
CREATE INDEX IF NOT EXISTS idx_proposal_events_child ON proposal_events(child_id);
CREATE INDEX IF NOT EXISTS idx_proposal_events_edge ON proposal_events(edge_id);

CREATE TABLE IF NOT EXISTS memory_entries (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(namespace, key, revision)
);
"""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class SQLiteStateGraph(StateGraph[State, Action], Generic[State, Action]):
    """SQLite-backed DAG of unique states and labeled actions."""

    def __init__(self, path: str | Path) -> None:
        import sqlite3

        self.path = Path(path)
        if self.path.name != ":memory:" and str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db_path = str(self.path)
        else:
            db_path = ":memory:"
            self.path = Path(":memory:")
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate_proposal_events()
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SQLiteStateGraph[State, Action]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add_state(
        self,
        state_id: str,
        state: State,
        metadata: dict[str, Any] | None = None,
        *,
        created_step: int = 0,
    ) -> StateNode[State]:
        existing = self._conn.execute(
            "SELECT 1 FROM states WHERE state_id = ?", (state_id,)
        ).fetchone()
        if existing:
            return self.get_state(state_id)
        payload = dumps(state)
        meta = dumps(metadata or {})
        self._conn.execute(
            """
            INSERT INTO states (state_id, state_json, metadata_json, created_at, created_step)
            VALUES (?, ?, ?, ?, ?)
            """,
            (state_id, payload, meta, _utcnow(), created_step),
        )
        self._conn.commit()
        return self.get_state(state_id)

    def add_edge(
        self,
        parent_id: str,
        child_id: str,
        action: Action,
        metadata: dict[str, Any] | None = None,
        *,
        created_step: int = 0,
    ) -> Edge[Action]:
        if not self.has_state(parent_id):
            raise UnknownStateError(parent_id)
        if not self.has_state(child_id):
            raise UnknownStateError(child_id)
        if parent_id == child_id or self._reaches(child_id, parent_id):
            raise CycleError(f"edge {parent_id} -> {child_id} would create a cycle")
        action_json = dumps(action)
        edge_id = stable_hash(
            {"parent_id": parent_id, "child_id": child_id, "action": action}
        )
        existing = self._conn.execute(
            "SELECT edge_id FROM edges WHERE edge_id = ?", (edge_id,)
        ).fetchone()
        if existing:
            return self._load_edge(edge_id)
        self._conn.execute(
            """
            INSERT INTO edges (
                edge_id, parent_id, child_id, action_json,
                metadata_json, created_at, created_step
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge_id,
                parent_id,
                child_id,
                action_json,
                dumps(metadata or {}),
                _utcnow(),
                created_step,
            ),
        )
        self._conn.commit()
        return self._load_edge(edge_id)

    def add_transition(
        self,
        *,
        parent_id: str,
        child_id: str,
        child: State,
        action: Action,
        state_metadata: dict[str, Any] | None = None,
        edge_metadata: dict[str, Any] | None = None,
        created_step: int = 0,
    ) -> tuple[StateNode[State], Edge[Action], bool, bool]:
        """Insert a child and its incoming edge in one transaction."""

        if not self.has_state(parent_id):
            raise UnknownStateError(parent_id)
        if parent_id == child_id or self._reaches(child_id, parent_id):
            raise CycleError(f"edge {parent_id} -> {child_id} would create a cycle")

        child_json = dumps(child)
        action_json = dumps(action)
        state_meta_json = dumps(state_metadata or {})
        edge_meta_json = dumps(edge_metadata or {})
        edge_id = stable_hash(
            {"parent_id": parent_id, "child_id": child_id, "action": action}
        )
        now = _utcnow()
        state_created = not self.has_state(child_id)
        edge_created = (
            self._conn.execute(
                "SELECT 1 FROM edges WHERE edge_id = ?",
                (edge_id,),
            ).fetchone()
            is None
        )

        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO states (
                    state_id, state_json, metadata_json, created_at, created_step
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (child_id, child_json, state_meta_json, now, created_step),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO edges (
                    edge_id, parent_id, child_id, action_json,
                    metadata_json, created_at, created_step
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge_id,
                    parent_id,
                    child_id,
                    action_json,
                    edge_meta_json,
                    now,
                    created_step,
                ),
            )

        row = self._conn.execute(
            """
            SELECT edge_id FROM edges
            WHERE parent_id = ? AND child_id = ? AND action_json = ?
            """,
            (parent_id, child_id, action_json),
        ).fetchone()
        if row is None:
            raise RuntimeError("transition transaction did not create an edge")
        return (
            self.get_state(child_id),
            self._load_edge(row["edge_id"]),
            state_created,
            edge_created,
        )

    def get_state(self, state_id: str) -> StateNode[State]:
        row = self._conn.execute(
            "SELECT * FROM states WHERE state_id = ?", (state_id,)
        ).fetchone()
        if row is None:
            raise UnknownStateError(state_id)
        return self._row_to_state(row)

    def has_state(self, state_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM states WHERE state_id = ?", (state_id,)
        ).fetchone()
        return row is not None

    def update_state_metadata(
        self,
        state_id: str,
        metadata: dict[str, Any],
    ) -> StateNode[State]:
        self.get_state(state_id)
        self._conn.execute(
            "UPDATE states SET metadata_json = ? WHERE state_id = ?",
            (dumps(metadata), state_id),
        )
        self._conn.commit()
        return self.get_state(state_id)

    def add_evaluation(
        self,
        state_id: str,
        *,
        evaluator_id: str,
        evaluator: str,
        version: str,
        config_hash: str,
        result: EvaluationResult,
    ) -> EvaluationRecord:
        self.get_state(state_id)
        evaluation_id = stable_hash(
            {"state_id": state_id, "evaluator_id": evaluator_id}
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO evaluations (
                evaluation_id, evaluator_id, state_id, evaluator, version,
                config_hash, metrics_json, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation_id,
                evaluator_id,
                state_id,
                evaluator,
                version,
                config_hash,
                dumps(result.metrics),
                dumps(result.metadata),
                _utcnow(),
            ),
        )
        self._conn.commit()
        record = self.get_evaluation(state_id, evaluator_id)
        if record is None:
            raise RuntimeError("evaluation insert did not create a record")
        return record

    def get_evaluation(
        self,
        state_id: str,
        evaluator_id: str,
    ) -> EvaluationRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM evaluations
            WHERE state_id = ? AND evaluator_id = ?
            """,
            (state_id, evaluator_id),
        ).fetchone()
        return self._row_to_evaluation(row) if row is not None else None

    def evaluations(self, state_id: str) -> list[EvaluationRecord]:
        self.get_state(state_id)
        rows = self._conn.execute(
            """
            SELECT * FROM evaluations
            WHERE state_id = ?
            ORDER BY evaluator, version, config_hash
            """,
            (state_id,),
        ).fetchall()
        return [self._row_to_evaluation(row) for row in rows]

    def parents(self, state_id: str) -> list[StateNode[State]]:
        self.get_state(state_id)
        rows = self._conn.execute(
            """
            SELECT s.* FROM states s
            JOIN edges e ON e.parent_id = s.state_id
            WHERE e.child_id = ?
            ORDER BY s.state_id
            """,
            (state_id,),
        ).fetchall()
        return [self._row_to_state(r) for r in rows]

    def children(self, state_id: str) -> list[StateNode[State]]:
        self.get_state(state_id)
        rows = self._conn.execute(
            """
            SELECT s.* FROM states s
            JOIN edges e ON e.child_id = s.state_id
            WHERE e.parent_id = ?
            ORDER BY s.state_id
            """,
            (state_id,),
        ).fetchall()
        return [self._row_to_state(r) for r in rows]

    def ancestors(self, state_id: str) -> list[StateNode[State]]:
        return self._walk(state_id, upward=True)

    def descendants(self, state_id: str) -> list[StateNode[State]]:
        return self._walk(state_id, upward=False)

    def frontier(self, limit: int | None = None) -> list[StateNode[State]]:
        sql = """
            SELECT s.* FROM states s
            LEFT JOIN edges e ON e.parent_id = s.state_id
            WHERE e.edge_id IS NULL
            ORDER BY s.state_id
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)
        rows = self._conn.execute(sql, parameters).fetchall()
        return [self._row_to_state(r) for r in rows]

    def states(
        self,
        limit: int | None = None,
        *,
        newest: bool = False,
    ) -> list[StateNode[State]]:
        direction = "DESC" if newest else "ASC"
        sql = (
            "SELECT * FROM states "
            f"ORDER BY created_step {direction}, state_id {direction}"
        )
        parameters: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)
        rows = self._conn.execute(sql, parameters).fetchall()
        return [self._row_to_state(r) for r in rows]

    def edges(self) -> list[Edge[Action]]:
        rows = self._conn.execute(
            "SELECT edge_id FROM edges ORDER BY created_step, edge_id"
        ).fetchall()
        return [self._load_edge(r["edge_id"]) for r in rows]

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM states").fetchone()
        return int(row["n"])

    def edge_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM edges").fetchone()
        return int(row["n"])

    def save_run(
        self,
        run_id: str,
        *,
        step: int,
        status: str,
        config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunRecord:
        now = _utcnow()
        existing = self._conn.execute(
            "SELECT created_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        created = existing["created_at"] if existing else now
        self._conn.execute(
            """
            INSERT INTO runs (
                run_id, created_at, updated_at, step, status, config_json, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                step = excluded.step,
                status = excluded.status,
                config_json = excluded.config_json,
                metadata_json = excluded.metadata_json
            """,
            (
                run_id,
                created,
                now,
                step,
                status,
                dumps(config or {}),
                dumps(metadata or {}),
            ),
        )
        self._conn.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord(
            run_id=row["run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            step=row["step"],
            status=row["status"],
            config=loads(row["config_json"]),
            metadata=loads(row["metadata_json"]),
        )

    def latest_run(self) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT run_id FROM runs ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self.get_run(row["run_id"])

    def add_decision(
        self,
        decision_id: str,
        *,
        run_id: str,
        policy: str,
        role: str,
        model: str | None,
        selected_state_ids: list[str],
        input_context: Any,
        tool_calls: list[dict[str, Any]],
        output: Any,
        metadata: dict[str, Any],
        created_step: int,
        memory_updates: list[MemoryUpdate] | None = None,
        memory_namespace: str | None = None,
    ) -> DecisionRecord:
        self.get_run(run_id)
        existing = self._conn.execute(
            "SELECT 1 FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if existing is not None:
            return self.get_decision(decision_id)
        # A decision and its memory writes must either both commit or both fail.
        with self._conn:
            cursor = self._conn.execute(
                """
            INSERT OR IGNORE INTO decisions (
                decision_id, run_id, policy, role, model,
                selected_state_ids_json, input_context_json, tool_calls_json,
                output_json, metadata_json, created_at, created_step
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    decision_id,
                    run_id,
                    policy,
                    role,
                    model,
                    dumps(selected_state_ids),
                    dumps(input_context),
                    dumps(tool_calls),
                    dumps(output),
                    dumps(metadata),
                    _utcnow(),
                    created_step,
                ),
            )
            if cursor.rowcount == 0:
                return self.get_decision(decision_id)
            for update in memory_updates or ():
                namespace = memory_namespace if memory_namespace is not None else run_id
                if not namespace.strip():
                    raise ValueError("memory namespace must be nonempty")
                row = self._conn.execute(
                    "SELECT MAX(revision) FROM memory_entries "
                    "WHERE namespace = ? AND key = ?",
                    (namespace, update.key),
                ).fetchone()
                revision = row[0] or 0
                if revision != update.expected_revision:
                    raise MemoryConflictError(
                        f"memory {namespace}/{update.key}: expected revision "
                        f"{update.expected_revision}, found {revision}"
                    )
                self._conn.execute(
                    "INSERT INTO memory_entries VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        namespace,
                        update.key,
                        revision + 1,
                        update.content,
                        dumps(update.metadata),
                        decision_id,
                        _utcnow(),
                    ),
                )
        return self.get_decision(decision_id)

    def memory(self, namespace: str, key: str) -> MemoryEntry:
        row = self._conn.execute(
            "SELECT * FROM memory_entries WHERE namespace = ? AND key = ? "
            "ORDER BY revision DESC LIMIT 1",
            (namespace, key),
        ).fetchone()
        if row is None:
            raise KeyError(f"memory document not found: {namespace}/{key}")
        return self._row_to_memory(row)

    def memories(self, namespace: str, limit: int | None = None) -> list[MemoryEntry]:
        sql = (
            "SELECT m.* FROM memory_entries m JOIN "
            "(SELECT key, MAX(revision) AS revision FROM memory_entries "
            "WHERE namespace = ? GROUP BY key) latest "
            "ON m.key = latest.key AND m.revision = latest.revision "
            "WHERE m.namespace = ? ORDER BY m.created_at DESC, m.key"
        )
        parameters: list[Any] = [namespace, namespace]
        if limit is not None:
            if limit < 0:
                raise ValueError("memory limit must be non-negative")
            sql += " LIMIT ?"
            parameters.append(limit)
        return [self._row_to_memory(row) for row in self._conn.execute(sql, parameters)]

    @staticmethod
    def _row_to_memory(row: Any) -> MemoryEntry:
        return MemoryEntry(
            namespace=row["namespace"],
            key=row["key"],
            content=row["content"],
            revision=row["revision"],
            metadata=loads(row["metadata_json"]),
            decision_id=row["decision_id"],
            created_at=row["created_at"],
        )

    def get_decision(self, decision_id: str) -> DecisionRecord:
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise KeyError(decision_id)
        return self._row_to_decision(row)

    def decisions(
        self,
        run_id: str | None = None,
        limit: int | None = None,
        *,
        newest: bool = False,
    ) -> list[DecisionRecord]:
        direction = "DESC" if newest else "ASC"
        parameters: list[Any] = []
        if run_id is None:
            sql = (
                "SELECT * FROM decisions "
                f"ORDER BY created_at {direction}, decision_id {direction}"
            )
        else:
            self.get_run(run_id)
            sql = (
                "SELECT * FROM decisions WHERE run_id = ? "
                f"ORDER BY created_step {direction}, "
                f"created_at {direction}, decision_id {direction}"
            )
            parameters.append(run_id)
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        rows = self._conn.execute(sql, parameters).fetchall()
        return [self._row_to_decision(row) for row in rows]

    def provenance_counts(self, run_id: str, created_step: int) -> tuple[int, int]:
        self.get_run(run_id)
        row = self._conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM decisions
                 WHERE run_id = ? AND created_step = ?) AS decisions,
                (SELECT COUNT(*) FROM proposal_events
                 WHERE run_id = ? AND created_step = ?) AS events
            """,
            (run_id, created_step, run_id, created_step),
        ).fetchone()
        return int(row["decisions"]), int(row["events"])

    def add_proposal_event(
        self,
        event_id: str,
        *,
        decision_id: str,
        run_id: str,
        parent_id: str,
        action: Action,
        metadata: dict[str, Any],
        created_step: int,
        proposal_index: int,
        sequence_index: int,
    ) -> ProposalEvent[Action]:
        self.get_decision(decision_id)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO proposal_events (
                event_id, decision_id, run_id, parent_id, action_json,
                metadata_json, outcome, created_at, created_step,
                proposal_index, sequence_index
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                event_id,
                decision_id,
                run_id,
                parent_id,
                dumps(action),
                dumps(metadata),
                _utcnow(),
                created_step,
                proposal_index,
                sequence_index,
            ),
        )
        self._conn.commit()
        return self.get_proposal_event(event_id)

    def finish_proposal_event(
        self,
        event_id: str,
        *,
        outcome: str,
        child_id: str | None = None,
        edge_id: str | None = None,
        error: str | None = None,
    ) -> ProposalEvent[Action]:
        self.get_proposal_event(event_id)
        self._conn.execute(
            """
            UPDATE proposal_events
            SET outcome = ?, child_id = ?, edge_id = ?, error = ?
            WHERE event_id = ?
            """,
            (outcome, child_id, edge_id, error, event_id),
        )
        self._conn.commit()
        return self.get_proposal_event(event_id)

    def get_proposal_event(self, event_id: str) -> ProposalEvent[Action]:
        row = self._conn.execute(
            "SELECT * FROM proposal_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._row_to_proposal_event(row)

    def proposal_events(
        self,
        *,
        run_id: str | None = None,
        decision_id: str | None = None,
        state_id: str | None = None,
        edge_id: str | None = None,
    ) -> list[ProposalEvent[Action]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if run_id is not None:
            self.get_run(run_id)
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if decision_id is not None:
            clauses.append("decision_id = ?")
            parameters.append(decision_id)
        if state_id is not None:
            clauses.append("(parent_id = ? OR child_id = ?)")
            parameters.extend([state_id, state_id])
        if edge_id is not None:
            clauses.append("edge_id = ?")
            parameters.append(edge_id)
        sql = "SELECT * FROM proposal_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_step, sequence_index, decision_id, proposal_index"
        rows = self._conn.execute(sql, parameters).fetchall()
        return [self._row_to_proposal_event(row) for row in rows]

    def _row_to_state(self, row: Any) -> StateNode[State]:
        return StateNode(
            state_id=row["state_id"],
            state=loads(row["state_json"]),
            metadata=loads(row["metadata_json"]),
            created_at=row["created_at"],
            created_step=row["created_step"],
        )

    def _row_to_evaluation(self, row: Any) -> EvaluationRecord:
        return EvaluationRecord(
            evaluation_id=row["evaluation_id"],
            evaluator_id=row["evaluator_id"],
            state_id=row["state_id"],
            evaluator=row["evaluator"],
            version=row["version"],
            config_hash=row["config_hash"],
            metrics=loads(row["metrics_json"]),
            metadata=loads(row["metadata_json"]),
            created_at=row["created_at"],
        )

    def _row_to_decision(self, row: Any) -> DecisionRecord:
        return DecisionRecord(
            decision_id=row["decision_id"],
            run_id=row["run_id"],
            policy=row["policy"],
            role=row["role"],
            model=row["model"],
            selected_state_ids=loads(row["selected_state_ids_json"]),
            input_context=loads(row["input_context_json"]),
            tool_calls=loads(row["tool_calls_json"]),
            output=loads(row["output_json"]),
            metadata=loads(row["metadata_json"]),
            created_at=row["created_at"],
            created_step=row["created_step"],
        )

    def _row_to_proposal_event(self, row: Any) -> ProposalEvent[Action]:
        return ProposalEvent(
            event_id=row["event_id"],
            decision_id=row["decision_id"],
            run_id=row["run_id"],
            parent_id=row["parent_id"],
            action=loads(row["action_json"]),
            outcome=row["outcome"],
            metadata=loads(row["metadata_json"]),
            child_id=row["child_id"],
            edge_id=row["edge_id"],
            error=row["error"],
            created_at=row["created_at"],
            created_step=row["created_step"],
            proposal_index=row["proposal_index"],
            sequence_index=row["sequence_index"],
        )

    def _migrate_proposal_events(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(proposal_events)")
        }
        if "metadata_json" not in columns:
            self._conn.execute(
                "ALTER TABLE proposal_events "
                "ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "sequence_index" not in columns:
            self._conn.execute(
                "ALTER TABLE proposal_events "
                "ADD COLUMN sequence_index INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.execute("UPDATE proposal_events SET sequence_index = rowid")

    def _load_edge(self, edge_id: str) -> Edge[Action]:
        row = self._conn.execute(
            "SELECT * FROM edges WHERE edge_id = ?", (edge_id,)
        ).fetchone()
        return Edge(
            edge_id=row["edge_id"],
            parent_id=row["parent_id"],
            child_id=row["child_id"],
            action=loads(row["action_json"]),
            metadata=loads(row["metadata_json"]),
            created_at=row["created_at"],
            created_step=row["created_step"],
        )

    def _adjacency(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        children: dict[str, list[str]] = {}
        parents: dict[str, list[str]] = {}
        for row in self._conn.execute("SELECT parent_id, child_id FROM edges"):
            children.setdefault(row["parent_id"], []).append(row["child_id"])
            parents.setdefault(row["child_id"], []).append(row["parent_id"])
        return parents, children

    def _reaches(self, start: str, target: str) -> bool:
        row = self._conn.execute(
            """
            WITH RECURSIVE reachable(state_id) AS (
                SELECT child_id FROM edges WHERE parent_id = ?
                UNION
                SELECT e.child_id
                FROM edges e
                JOIN reachable r ON e.parent_id = r.state_id
            )
            SELECT 1 FROM reachable WHERE state_id = ? LIMIT 1
            """,
            (start, target),
        ).fetchone()
        return row is not None

    def _walk(self, state_id: str, *, upward: bool) -> list[StateNode[State]]:
        self.get_state(state_id)
        parents, children = self._adjacency()
        links = parents if upward else children
        ordered: list[str] = []
        seen = {state_id}
        queue = deque([state_id])
        while queue:
            node = queue.popleft()
            nxts = sorted(links.get(node, []))
            for nxt in nxts:
                if nxt not in seen:
                    seen.add(nxt)
                    ordered.append(nxt)
                    queue.append(nxt)
        return [self.get_state(sid) for sid in ordered]
