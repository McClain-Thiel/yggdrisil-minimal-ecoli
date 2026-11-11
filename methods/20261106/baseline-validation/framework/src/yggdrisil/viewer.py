from __future__ import annotations

import json
import sqlite3
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/viewer.css": ("viewer.css", "text/css; charset=utf-8"),
    "/viewer.js": ("viewer.js", "text/javascript; charset=utf-8"),
}


class GraphReader:
    """Read raw graph rows without importing serialized application types."""

    def __init__(self, path: str | Path, *, batch_size: int = 1_000) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"graph database does not exist: {self.path}")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size

    def updates(
        self,
        *,
        state_after: int,
        edge_after: int,
        evaluation_after: int = 0,
        decision_after: int = 0,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            states = connection.execute(
                """
                SELECT rowid AS cursor, state_id, state_json, metadata_json,
                       created_at, created_step
                FROM states
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT ?
                """,
                (state_after, self.batch_size + 1),
            ).fetchall()
            edges = connection.execute(
                """
                SELECT rowid AS cursor, edge_id, parent_id, child_id,
                       action_json, metadata_json, created_at, created_step
                FROM edges
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT ?
                """,
                (edge_after, self.batch_size + 1),
            ).fetchall()
            evaluations = connection.execute(
                """
                SELECT rowid AS cursor, evaluation_id, evaluator_id, state_id,
                       evaluator, version, config_hash, metrics_json,
                       metadata_json, created_at
                FROM evaluations
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT ?
                """,
                (evaluation_after, self.batch_size + 1),
            ).fetchall()
            decisions = connection.execute(
                """
                SELECT rowid AS cursor, decision_id, run_id, policy, role, model,
                       selected_state_ids_json, input_context_json,
                       tool_calls_json, output_json, metadata_json,
                       created_at, created_step
                FROM decisions
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT ?
                """,
                (decision_after, self.batch_size + 1),
            ).fetchall()
            # Proposal outcomes update in place, so a rowid cursor would miss
            # pending -> terminal changes. Keep this table as a full snapshot.
            events = connection.execute(
                """
                SELECT rowid AS cursor, event_id, decision_id, run_id,
                       parent_id, child_id, edge_id, action_json, metadata_json,
                       outcome, error, created_at, created_step, proposal_index,
                       sequence_index
                FROM proposal_events
                ORDER BY rowid
                """
            ).fetchall()
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM states) AS states,
                    (SELECT COUNT(*) FROM edges) AS edges
                """
            ).fetchone()
            run = connection.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()

        state_pending = len(states) > self.batch_size
        edge_pending = len(edges) > self.batch_size
        evaluation_pending = len(evaluations) > self.batch_size
        decision_pending = len(decisions) > self.batch_size
        states = states[: self.batch_size]
        edges = edges[: self.batch_size]
        evaluations = evaluations[: self.batch_size]
        decisions = decisions[: self.batch_size]
        return {
            "graph": str(self.path),
            "states": [self._state(row) for row in states],
            "edges": [self._edge(row) for row in edges],
            "evaluations": [self._evaluation(row) for row in evaluations],
            "decisions": [self._decision(row) for row in decisions],
            "proposal_events": [self._proposal_event(row) for row in events],
            "state_cursor": states[-1]["cursor"] if states else state_after,
            "edge_cursor": edges[-1]["cursor"] if edges else edge_after,
            "evaluation_cursor": (
                evaluations[-1]["cursor"] if evaluations else evaluation_after
            ),
            "decision_cursor": (
                decisions[-1]["cursor"] if decisions else decision_after
            ),
            "counts": {"states": counts["states"], "edges": counts["edges"]},
            "run": self._run(run) if run is not None else None,
            "pending": any(
                (
                    state_pending,
                    edge_pending,
                    evaluation_pending,
                    decision_pending,
                )
            ),
        }

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    @staticmethod
    def _state(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cursor": row["cursor"],
            "state_id": row["state_id"],
            "state": json.loads(row["state_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "created_step": row["created_step"],
        }

    @staticmethod
    def _edge(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cursor": row["cursor"],
            "edge_id": row["edge_id"],
            "parent_id": row["parent_id"],
            "child_id": row["child_id"],
            "action": json.loads(row["action_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "created_step": row["created_step"],
        }

    @staticmethod
    def _evaluation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cursor": row["cursor"],
            "evaluation_id": row["evaluation_id"],
            "evaluator_id": row["evaluator_id"],
            "state_id": row["state_id"],
            "evaluator": row["evaluator"],
            "version": row["version"],
            "config_hash": row["config_hash"],
            "metrics": json.loads(row["metrics_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _decision(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cursor": row["cursor"],
            "decision_id": row["decision_id"],
            "run_id": row["run_id"],
            "policy": row["policy"],
            "role": row["role"],
            "model": row["model"],
            "selected_state_ids": json.loads(row["selected_state_ids_json"]),
            "input_context": json.loads(row["input_context_json"]),
            "tool_calls": json.loads(row["tool_calls_json"]),
            "output": json.loads(row["output_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "created_step": row["created_step"],
        }

    @staticmethod
    def _proposal_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cursor": row["cursor"],
            "event_id": row["event_id"],
            "decision_id": row["decision_id"],
            "run_id": row["run_id"],
            "parent_id": row["parent_id"],
            "child_id": row["child_id"],
            "edge_id": row["edge_id"],
            "action": json.loads(row["action_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "outcome": row["outcome"],
            "error": row["error"],
            "created_at": row["created_at"],
            "created_step": row["created_step"],
            "proposal_index": row["proposal_index"],
            "sequence_index": row["sequence_index"],
        }

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "step": row["step"],
            "status": row["status"],
            "config": json.loads(row["config_json"]),
            "metadata": json.loads(row["metadata_json"]),
        }


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        reader: GraphReader,
    ) -> None:
        super().__init__(address, ViewerHandler)
        self.reader = reader


class ViewerHandler(BaseHTTPRequestHandler):
    server: ViewerServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/updates":
            self._updates(parse_qs(parsed.query))
            return
        static = _STATIC_FILES.get(parsed.path)
        if static is not None:
            self._static(*static)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        if self.path.startswith("/api/updates"):
            return
        super().log_message(format, *args)

    def _updates(self, query: dict[str, list[str]]) -> None:
        try:
            state_after = _cursor(query, "state_after")
            edge_after = _cursor(query, "edge_after")
            evaluation_after = _cursor(query, "evaluation_after")
            decision_after = _cursor(query, "decision_after")
            payload = self.server.reader.updates(
                state_after=state_after,
                edge_after=edge_after,
                evaluation_after=evaluation_after,
                decision_after=decision_after,
            )
        except (ValueError, sqlite3.Error) as exc:
            self._json(
                {"error": f"could not read graph: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._json(payload)

    def _static(self, name: str, content_type: str) -> None:
        data = files("yggdrisil.web").joinpath(name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy", "default-src 'self'; style-src 'self'"
        )
        self.end_headers()
        self.wfile.write(data)

    def _json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def serve(
    graph: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    reader = GraphReader(graph)
    server = ViewerServer((host, port), reader)
    url = f"http://{host}:{server.server_port}"
    print(f"Inspecting {reader.path}")
    print(f"Open {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _cursor(query: dict[str, list[str]], name: str) -> int:
    raw = query.get(name, ["0"])[0]
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value
