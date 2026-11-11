"""No science: protect cached evaluations and candidate limits across logging repair."""

import hashlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from scripts.baseline_depth_logging_recovery import (
    row_proof,
    verify_preserved_rows,
    verify_resume,
)


class LoggingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.graph = self.root / "graph.sqlite"
        with sqlite3.connect(self.graph) as db:
            db.executescript(
                "CREATE TABLE states(state_id,state_json);CREATE TABLE evaluations(state_id,evaluator_id,metrics_json);"
            )
            db.execute("INSERT INTO states VALUES(?,?)", ("s1", '{"deleted_genes":[]}'))
            db.execute(
                "INSERT INTO evaluations VALUES(?,?,?)",
                ("s1", "fixed", '{"growth":0.1}'),
            )
        self.proof = row_proof(self.graph)

    def tearDown(self):
        self.temp.cleanup()

    def test_appending_new_work_preserves_cached_prefix(self):
        with sqlite3.connect(self.graph) as db:
            db.execute(
                "INSERT INTO states VALUES(?,?)", ("s2", '{"deleted_genes":["test"]}')
            )
            db.execute(
                "INSERT INTO evaluations VALUES(?,?,?)",
                ("s2", "fixed", '{"growth":0.2}'),
            )
        verify_preserved_rows(self.graph, self.proof)

    def test_changed_cached_evaluation_rejected(self):
        with sqlite3.connect(self.graph) as db:
            db.execute("UPDATE evaluations SET metrics_json=?", ('{"growth":1}',))
        with self.assertRaisesRegex(RuntimeError, "changed"):
            verify_preserved_rows(self.graph, self.proof)

    def test_deleted_candidate_rejected(self):
        with sqlite3.connect(self.graph) as db:
            db.execute("DELETE FROM states")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            verify_preserved_rows(self.graph, self.proof)

    def test_resume_requires_verified_original_budget_and_graph(self):
        now = time.time()
        job = {"run_id": "same", "max_states_including_root": 2001}
        record = {
            "run_id": "same",
            "max_states_including_root": 2001,
            "status": "verified",
            "failed_attempt_backup_verified": True,
            "started_at_unix": now,
            "deadline_unix": now + 21600,
            "prior_cpu_hours": 0.2,
            "graph_sha256": hashlib.sha256(self.graph.read_bytes()).hexdigest(),
            "preserved_rows": self.proof,
        }
        verify_resume(self.root, record, job)
        for change in [
            {"deadline_unix": now + 21601},
            {"max_states_including_root": 3001},
            {"graph_sha256": "0" * 64},
            {"failed_attempt_backup_verified": False},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_resume(self.root, record | change, job)
