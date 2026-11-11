"""Offline checks for study isolation, accounting and irreversible nomination."""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.collect_search_replication import (
    audit_report,
    collect,
    decode_graph_value,
    export_graph_report,
    validation_outcome,
)
from scripts.plan_search_replication import (
    build_jobs,
    digest,
    encoded,
    gene_set_sha256,
    prepare,
    state_id,
    validate_protocol,
    verify_bundle,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "benchmarks/search_replication.json"


class ReplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = json.loads(PROTOCOL.read_text())
        self.jobs = build_jobs(self.spec)
        self.job = next(j for j in self.jobs if j["method"] == "no_retrieval")
        self.root_ids = ["b0001", "b0002"]

    def candidate(self, index: int, additions: list[str], growth: float = 0.8) -> dict:
        ids = sorted(self.root_ids + additions)
        metrics = {
            "fba": {"feasible": True, "growth_rate": growth},
            "resource_allocation": {
                "feasible_at_growth_floor": True,
                "growth_rate_floor_h": 0.1,
            },
            "genome_size": {"genes_deleted": len(ids)},
            "essentiality": {},
            "module_retention": {},
        }
        return {
            "candidate_index": index,
            "state_id": state_id(ids),
            "deleted_gene_ids": ids,
            "evaluation_complete": True,
            "evaluations": {
                name: {"evaluator_id": identity, "metrics": metrics[name]}
                for name, identity in self.spec["evaluator_ids"].items()
            },
        }

    def report(self, candidates: list[dict], job: dict | None = None) -> dict:
        job = self.job if job is None else job
        return {
            "run_id": job["run_id"],
            "method": job["method"],
            "seed": job["seed"],
            "protocol_sha256": "f" * 64,
            "starting_deletion_set_sha256": gene_set_sha256(self.root_ids),
            "evaluator_ids": self.spec["evaluator_ids"],
            "status": "exhausted",
            "stop_reason": "no_unseen_proposals",
            "validation_started": False,
            "model_spend_usd": 0,
            "wall_time_s": 10,
            "cpu_hours": 0.01,
            "candidates": candidates,
        }

    def audit(self, report: dict) -> dict:
        return audit_report(self.spec, self.job, report, self.root_ids, "f" * 64)

    def test_twenty_five_isolated_jobs_and_reproducible_blocks(self) -> None:
        self.assertEqual(self.jobs, build_jobs(copy.deepcopy(self.spec)))
        self.assertEqual(len(self.jobs), 25)
        self.assertEqual(len({j["graph_path"] for j in self.jobs}), 25)
        agents = [j for j in self.jobs if j["agent_config"]]
        self.assertEqual(len(agents), 15)
        self.assertEqual(len({j["agent_config"]["session_path"] for j in agents}), 15)
        for block in range(5):
            self.assertEqual(
                {j["method"] for j in self.jobs if j["block"] == block},
                set(self.spec["methods"]),
            )
        for j in self.jobs:
            self.assertEqual(j["max_states_including_root"], 101)
            self.assertEqual(j["live_literature"], j["method"] == "adaptive_literature")
            if j["method"] == "no_retrieval":
                self.assertIsNone(j["dossier"])
            if j["method"] in ("frozen_dossier", "adaptive_literature"):
                self.assertEqual(j["dossier"], "inputs/dossier.json")
        agents[0]["agent_config"]["open_set"]["active_width"] = 1
        self.assertEqual(agents[1]["agent_config"]["open_set"]["active_width"], 12)

    def test_protocol_rejects_unfair_or_authorized_mutations(self) -> None:
        changes = [
            ("search_seeds", [103] * 5),
            ("methods", self.spec["methods"][:-1]),
            ("experiment_id", "../../bad"),
        ]
        for key, value in changes:
            with self.subTest(key=key):
                spec = copy.deepcopy(self.spec)
                spec[key] = value
                with self.assertRaises(ValueError):
                    validate_protocol(spec)
        changes = [
            ("authorization", "paid_launch", True),
            ("search", "fresh_sessions", False),
            ("search", "validation_feedback", True),
            ("search", "new_candidate_slots", 500),
            ("agent", "edison", True),
            ("agent", "session_path", "shared.sqlite"),
            ("budget_proposal", "model_per_run_cap_usd", 100),
            ("budget_proposal", "total_cap_usd", float("nan")),
            ("selection", "replacement_after_validation", True),
            ("validation", "solver_patch_sha256", "short"),
            ("literature", "publication_cutoff", "tomorrow"),
            ("execution", "platform", "darwin"),
        ]
        for section, key, value in changes:
            with self.subTest(section=section, key=key):
                spec = copy.deepcopy(self.spec)
                spec[section][key] = value
                with self.assertRaises(ValueError):
                    validate_protocol(spec)

    def test_prepare_and_verify_actual_root_without_science_or_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "bundle"
            manifest = prepare(PROTOCOL, ROOT, out)
            self.assertFalse(manifest["launch_ready"])
            self.assertEqual(manifest["maximum_new_candidates"], 2500)
            self.assertEqual(manifest["maximum_validation_lineages"], 81)
            self.assertEqual(manifest["maximum_validation_cell_cycles"], 1620)
            self.assertEqual(manifest["calibration_cell_cycles"], 2)
            self.assertEqual(len(verify_bundle(out)[1]), 25)
            self.assertFalse(list(out.rglob("*.sqlite")))
            self.assertFalse(list(out.rglob("*.py")))
            with self.assertRaises(FileExistsError):
                prepare(PROTOCOL, ROOT, out)
            (out / "jobs.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "Changed study input"):
                verify_bundle(out)

    def test_changed_sources_fail_before_creating_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "protocol.json"
            spec = copy.deepcopy(self.spec)
            spec["source_references"]["agent_protocol"]["sha256"] = "0" * 64
            path.write_bytes(encoded(spec))
            out = Path(temp) / "bundle"
            with self.assertRaisesRegex(ValueError, "Changed source"):
                prepare(path, ROOT, out)
            self.assertFalse(out.exists())

    def test_finalist_order_is_deletions_then_growth_then_state_hash(self) -> None:
        candidates = [
            self.candidate(1, ["b0003"], growth=2),
            self.candidate(2, ["b0003", "b0004"], growth=0.6),
            self.candidate(3, ["b0005", "b0006"], growth=0.8),
            self.candidate(4, ["b0007", "b0008"], growth=0.8),
        ]
        row = self.audit(self.report(candidates))
        self.assertEqual(
            row["finalist"]["state_id"],
            min(candidates[2]["state_id"], candidates[3]["state_id"]),
        )
        self.assertEqual(row["finalist"]["additional_deletions"], 2)
        self.assertIsNone(row["checkpoints"]["100"])

    def test_failed_growth_gates_consume_slots_but_cannot_win(self) -> None:
        rows = [self.candidate(1, ["b0003"]), self.candidate(2, ["b0003", "b0004"])]
        rows[1]["evaluations"]["resource_allocation"]["metrics"][
            "feasible_at_growth_floor"
        ] = False
        result = self.audit(self.report(rows))
        self.assertEqual(result["attempted_unique_candidates"], 2)
        self.assertEqual(result["completed_evaluations"], 2)
        self.assertEqual(result["growth_gate_passing_candidates"], 1)
        self.assertEqual(result["finalist"]["state_id"], rows[0]["state_id"])

    def test_depth_match_keeps_full_search_but_selects_below_ceiling(self) -> None:
        rows = [
            self.candidate(1, ["b0003"]),
            self.candidate(2, ["b0004", "b0005"]),
            self.candidate(3, ["b0006", "b0007", "b0008"]),
        ]
        report = self.report(rows)
        original = self.audit(report)
        matched = audit_report(
            self.spec,
            self.job,
            report,
            self.root_ids,
            "f" * 64,
            maximum_deletions=4,
        )
        self.assertEqual(matched["finalist"]["state_id"], rows[1]["state_id"])
        self.assertEqual(original["finalist"]["state_id"], rows[2]["state_id"])
        for key in [
            "attempted_unique_candidates",
            "completed_evaluations",
            "growth_gate_passing_candidates",
            "checkpoints",
        ]:
            self.assertEqual(original[key], matched[key])
        rows[1]["evaluations"]["resource_allocation"]["metrics"][
            "feasible_at_growth_floor"
        ] = False
        matched = audit_report(
            self.spec,
            self.job,
            report,
            self.root_ids,
            "f" * 64,
            maximum_deletions=4,
        )
        self.assertEqual(matched["finalist"]["state_id"], rows[0]["state_id"])

    def test_depth_match_never_fabricates_a_nominee(self) -> None:
        report = self.report([self.candidate(1, ["b0003", "b0004"])])
        matched = audit_report(
            self.spec,
            self.job,
            report,
            self.root_ids,
            "f" * 64,
            maximum_deletions=3,
        )
        self.assertIsNone(matched["finalist"])
        for invalid in [True, 2, 4291, 4.5]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                audit_report(
                    self.spec,
                    self.job,
                    report,
                    self.root_ids,
                    "f" * 64,
                    maximum_deletions=invalid,
                )

    def test_partial_failure_retains_incumbent_without_budget_reset(self) -> None:
        rows = [self.candidate(1, ["b0003"]), self.candidate(2, ["b0003", "b0004"])]
        rows[1].update(
            evaluation_complete=False, incomplete_reason="solver process failed"
        )
        report = self.report(rows)
        report.update(
            status="failed", stop_reason="evaluator_exception", error="captured"
        )
        result = self.audit(report)
        self.assertEqual(result["attempted_unique_candidates"], 2)
        self.assertEqual(result["completed_evaluations"], 1)
        self.assertFalse(result["full_evaluation_budget_reached"])
        self.assertEqual(result["finalist"]["state_id"], rows[0]["state_id"])
        self.assertEqual(result["error"], "captured")

    def test_no_improvement_or_failure_is_retained_without_a_finalist(self) -> None:
        report = self.report([])
        report.update(status="failed", stop_reason="startup_error", error="explicit")
        result = self.audit(report)
        self.assertFalse(result["search_improved"])
        self.assertIsNone(result["finalist"])

    def test_wrong_identity_early_completion_or_validation_feedback_rejected(
        self,
    ) -> None:
        changes = {
            "run_id": "different-run",
            "protocol_sha256": "0" * 64,
            "starting_deletion_set_sha256": "0" * 64,
            "status": "completed",
            "validation_started": True,
            "model_spend_usd": 31,
            "cpu_hours": float("inf"),
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                report = self.report([self.candidate(1, ["b0003"])])
                report[key] = value
                with self.assertRaises(ValueError):
                    self.audit(report)

    def test_duplicate_candidates_do_not_inflate_evaluation_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate candidate"):
            self.audit(
                self.report(
                    [
                        self.candidate(1, ["b0003"]),
                        self.candidate(2, ["b0003"]),
                    ]
                )
            )

    def test_changed_floor_missing_evaluator_and_nonfinite_growth_rejected(
        self,
    ) -> None:
        for case in ("floor", "evaluator", "nan", "size", "root"):
            with self.subTest(case=case):
                row = self.candidate(1, ["b0003"])
                if case == "floor":
                    row["evaluations"]["resource_allocation"]["metrics"][
                        "growth_rate_floor_h"
                    ] = 0.01
                elif case == "evaluator":
                    del row["evaluations"]["essentiality"]
                elif case == "nan":
                    row["evaluations"]["fba"]["metrics"]["growth_rate"] = float("nan")
                elif case == "size":
                    row["evaluations"]["genome_size"]["metrics"]["genes_deleted"] = 4000
                else:
                    row["deleted_gene_ids"] = ["b0003"]
                    row["state_id"] = state_id(["b0003"])
                with self.assertRaises(ValueError):
                    self.audit(self.report([row]))

    def test_baselines_cannot_report_model_spend(self) -> None:
        job = next(j for j in self.jobs if j["method"] == "greedy_block")
        report = self.report([], job)
        report["model_spend_usd"] = 0.01
        with self.assertRaisesRegex(ValueError, "cap"):
            audit_report(self.spec, job, report, self.root_ids, "f" * 64)

    def test_complete_budget_checkpoints_include_failed_screens(self) -> None:
        rows = [self.candidate(i, [f"b{i + 2:04d}"]) for i in range(1, 101)]
        for row in rows[::2]:
            row["evaluations"]["fba"]["metrics"].update(feasible=False, growth_rate=0)
        report = self.report(rows)
        report.update(status="completed", stop_reason="candidate_budget")
        result = self.audit(report)
        self.assertTrue(result["full_evaluation_budget_reached"])
        self.assertEqual(result["growth_gate_passing_candidates"], 50)
        self.assertEqual(result["checkpoints"]["100"]["candidate_index"], 100)

    def test_collector_requires_all_reports_and_verified_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "bundle"
            prepare(PROTOCOL, ROOT, out)
            with self.assertRaises(FileNotFoundError):
                collect(out)
            self.root_ids = json.loads(
                (out / "inputs/starting-genotype.json").read_text()
            )["deleted_gene_ids"]
            addition = next(
                f"b{i:04d}" for i in range(1, 4000) if f"b{i:04d}" not in self.root_ids
            )
            protocol_hash = digest((out / "protocol.json").read_bytes())
            for job in self.jobs:
                folder = out / job["directory"]
                folder.mkdir(parents=True)
                report = self.report([self.candidate(1, [addition])], job)
                report["protocol_sha256"] = protocol_hash
                raw = encoded(report)
                (folder / "run-report.json").write_bytes(raw)
                receipt = {
                    "report_sha256": digest(raw),
                    "protocol_sha256": protocol_hash,
                    "full_s3_get_verified": True,
                    "wandb_download_verified": True,
                    "wandb_state": "COMMITTED",
                    "source_and_inputs_verified": True,
                    "s3_uri": self.spec["tracking"]["s3_prefix"] + "test/report.json",
                    "wandb_artifact": "fixture/data:v0",
                }
                (folder / "report-verification.json").write_bytes(encoded(receipt))
            catalog = collect(out)
            self.assertEqual(len(catalog["runs"]), 25)
            self.assertEqual(len(catalog["nominations"]), 1)
            self.assertEqual(len(catalog["nominations"][0]["source_run_ids"]), 25)
            self.assertEqual(catalog["maximum_validation_lineages"], 9)
            self.assertFalse(catalog["validation_launch_authorized"])
            receipt["wandb_state"] = "PENDING"
            (folder / "report-verification.json").write_bytes(encoded(receipt))
            with self.assertRaisesRegex(ValueError, "verified report"):
                collect(out)

    def test_validation_requires_all_seeds_and_preserves_model_exceptions(self) -> None:
        rows = [
            {
                "seed": seed,
                "generations": 20,
                "terminal_reason": "reached_max_generations",
                "finite_output": True,
                "exact_knockouts": True,
                "numerical_policy_passed": True,
            }
            for seed in [101, 211, 307]
        ]
        self.assertTrue(
            validation_outcome(rows, [101, 211, 307])["all_three_seeds_passed"]
        )
        rows[-1].update(generations=14, terminal_reason="model_exception")
        result = validation_outcome(rows, [101, 211, 307])
        self.assertFalse(result["all_three_seeds_passed"])
        self.assertEqual(result["passed_lineages"], 2)
        self.assertEqual(result["lineages"][-1]["terminal_reason"], "model_exception")
        with self.assertRaises(ValueError):
            validation_outcome(rows[:2], [101, 211, 307])
        rows[-1].update(generations=20)
        with self.assertRaises(ValueError):
            validation_outcome(rows, [101, 211, 307])

    def test_graph_export_is_read_only_and_preserves_insertion_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "bundle"
            prepare(PROTOCOL, ROOT, bundle)
            root = json.loads((bundle / "inputs/starting-genotype.json").read_text())
            self.root_ids = root["deleted_gene_ids"]
            job_index = next(
                i for i, j in enumerate(self.jobs) if j["method"] == "no_retrieval"
            )
            job = self.jobs[job_index]
            added = [
                f"b{i:04d}" for i in range(1, 4000) if f"b{i:04d}" not in self.root_ids
            ][:2]
            candidates = sorted(
                [self.candidate(1, [gene]) for gene in added],
                key=lambda row: row["state_id"],
                reverse=True,
            )
            graph = Path(temp) / "graph.sqlite"
            protocol_hash = digest((bundle / "protocol.json").read_bytes())

            def tagged_state(ids: list[str]) -> str:
                return json.dumps(
                    {
                        "__yggdrisil__": "object",
                        "kind": "dataclass",
                        "qualname": "yggdrisil_ecoli.state:GenomeState",
                        "fields": [
                            [
                                "deleted_genes",
                                {"__yggdrisil__": "frozenset", "items": ids},
                            ]
                        ],
                    }
                )

            with sqlite3.connect(graph) as db:
                db.executescript("""
                    CREATE TABLE runs (run_id TEXT, status TEXT, metadata_json TEXT);
                    CREATE TABLE states (state_id TEXT, state_json TEXT);
                    CREATE TABLE evaluations (state_id TEXT, evaluator_id TEXT, metrics_json TEXT);
                """)
                meta = {
                    "evaluators": self.spec["evaluator_ids"],
                    "protocol_sha256": protocol_hash,
                }
                db.execute(
                    "INSERT INTO runs VALUES (?,?,?)",
                    (job["run_id"], "completed", json.dumps(meta)),
                )
                db.execute(
                    "INSERT INTO states VALUES (?,?)",
                    (root["state_id"], tagged_state(self.root_ids)),
                )
                for row in candidates:
                    db.execute(
                        "INSERT INTO states VALUES (?,?)",
                        (row["state_id"], tagged_state(row["deleted_gene_ids"])),
                    )
                    for record in row["evaluations"].values():
                        metrics = {
                            "__yggdrisil__": "dict",
                            "items": list(record["metrics"].items()),
                        }
                        db.execute(
                            "INSERT INTO evaluations VALUES (?,?,?)",
                            (
                                row["state_id"],
                                record["evaluator_id"],
                                json.dumps(metrics),
                            ),
                        )
            before = digest(graph.read_bytes())
            terminal = self.report([], job)
            result = export_graph_report(bundle, job_index, graph, terminal)
            self.assertEqual(before, digest(graph.read_bytes()))
            self.assertEqual(
                [r["state_id"] for r in result["candidates"]],
                [r["state_id"] for r in candidates],
            )
            self.assertEqual(
                [r["candidate_index"] for r in result["candidates"]], [1, 2]
            )
            with sqlite3.connect(graph) as db:
                db.execute("UPDATE runs SET status='running'")
            with self.assertRaisesRegex(ValueError, "still running"):
                export_graph_report(bundle, job_index, graph, terminal)
            with sqlite3.connect(graph) as db:
                db.execute(
                    "UPDATE runs SET status='completed', run_id='historical-parent'"
                )
            with self.assertRaisesRegex(ValueError, "inherited run"):
                export_graph_report(bundle, job_index, graph, terminal)

    def test_unrecognized_serialized_objects_cannot_be_imported(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported graph"):
            decode_graph_value(
                {"__yggdrisil__": "object", "qualname": "untrusted.module:Code"}
            )

    def test_candidate_limit_cannot_be_exceeded(self) -> None:
        rows = [self.candidate(i, [f"b{i + 2:04d}"]) for i in range(1, 102)]
        with self.assertRaisesRegex(ValueError, "budget exceeded"):
            self.audit(self.report(rows))


if __name__ == "__main__":
    unittest.main()
