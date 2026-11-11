"""Verify preserved baseline graph rows before and after a logging-only resume."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

_TABLES = {
    "states": "state_id,state_json",
    "evaluations": "state_id,evaluator_id,metrics_json",
}


def row_proof(graph: Path, limits: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {}
    with closing(
        sqlite3.connect(graph.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("Preserved graph failed SQLite integrity check")
        for table, columns in _TABLES.items():
            maximum = (
                limits[table]["max_rowid"]
                if limits
                else db.execute(
                    f"SELECT COALESCE(MAX(rowid),0) FROM {table}"
                ).fetchone()[0]
            )
            h, count = hashlib.sha256(), 0
            for row in db.execute(
                f"SELECT rowid,{columns} FROM {table} WHERE rowid<=? ORDER BY rowid",
                (maximum,),
            ):
                h.update(
                    (
                        json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n"
                    ).encode()
                )
                count += 1
            result[table] = {
                "max_rowid": maximum,
                "count": count,
                "sha256": h.hexdigest(),
            }
    return result


def verify_preserved_rows(graph: Path, proof: dict[str, Any]) -> None:
    if row_proof(graph, proof) != proof:
        raise RuntimeError("A preserved genotype or evaluation changed during resume")


def verify_resume(folder: Path, record: dict[str, Any], job: dict[str, Any]) -> None:
    graph = folder / "graph.sqlite"
    with graph.open("rb") as body:
        digest = hashlib.file_digest(body, "sha256").hexdigest()
    if (
        digest != record["graph_sha256"]
        or record["run_id"] != job["run_id"]
        or record["status"] != "verified"
        or not record["failed_attempt_backup_verified"]
        or record["max_states_including_root"] != job["max_states_including_root"]
        or record["deadline_unix"] > record["started_at_unix"] + 21600
        or record["prior_cpu_hours"] < 0
    ):
        raise ValueError("Unverified logging recovery or changed budget")
    verify_preserved_rows(graph, record["preserved_rows"])
