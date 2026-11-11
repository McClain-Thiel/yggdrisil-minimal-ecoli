"""Offline continuation safety checks; synthetic graphs only, no scientific runs."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.baseline_depth_extension import (
    build_jobs,
    check_parent_graph,
    prepare,
    restore_parent,
    validate_approval,
    verify_archive,
    verify_bundle,
)
from scripts.plan_search_replication import digest, encoded

ROOT = Path(__file__).resolve().parents[1]


class ExtensionTests(unittest.TestCase):
    def test_prepare_all_parents_and_reject_changed_science(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "bundle"
            manifest = prepare(ROOT, bundle)
            spec, jobs = verify_bundle(bundle)
            self.assertFalse(manifest["launch_ready"])
            self.assertEqual(len(jobs), 10)
            self.assertEqual(sum(j["additional_candidate_slots"] for j in jobs), 19000)
            self.assertTrue(
                all(j["agent_config"] is None and j["model_cap_usd"] == 0 for j in jobs)
            )
            self.assertFalse(list(bundle.rglob("*.sqlite")))
            with self.assertRaises(FileExistsError):
                prepare(ROOT, bundle)
            spec["search"]["new_candidate_slots"] += 1
            with self.assertRaises(ValueError):
                build_jobs(spec)
            spec["search"]["new_candidate_slots"] -= 1
            spec["growth_gates"]["rba_growth_floor_per_hour"] = 0.01
            raw = encoded(spec)
            (bundle / "protocol.json").write_bytes(raw)
            manifest["files"]["protocol.json"] = digest(raw)
            (bundle / "manifest.json").write_bytes(encoded(manifest))
            with self.assertRaisesRegex(ValueError, "frozen science"):
                verify_bundle(bundle)

    def test_cap_deadline_and_no_implicit_validation(self):
        now = time.time()
        approval = dict(
            authorized_start_unix=now - 1,
            science_deadline_unix=now + 100,
            stop_deadline_unix=now + 7400,
            total_cap_usd=25,
            model_calls_allowed=False,
            validation_launch_authorized=False,
        )
        validate_approval(approval)
        for change in [
            dict(total_cap_usd=26),
            dict(total_cap_usd=float("nan")),
            dict(science_deadline_unix=now - 5),
            dict(stop_deadline_unix=now + 86400),
            dict(model_calls_allowed=True),
            dict(validation_launch_authorized=True),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_approval(approval | change)

    def test_remote_guard_precedes_bundle_reads(self):
        from scripts.run_search_replication import validate_launch

        with patch("scripts.run_search_replication.sys.platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "remote Linux"):
                validate_launch(Path("/missing"), Path("/missing"), {})

    def parent_fixture(self, root):
        graph = root / "parent.sqlite"
        ids = {"fba": "f", "resource_allocation": "r"}
        parent = {"run_id": "parent", "protocol_sha256": "a" * 64}
        with sqlite3.connect(graph) as db:
            db.executescript(
                "CREATE TABLE runs(run_id,status,metadata_json); CREATE TABLE states(state_id); CREATE TABLE evaluations(state_id,evaluator_id);"
            )
            db.execute(
                "INSERT INTO runs VALUES(?,?,?)",
                (
                    "parent",
                    "completed",
                    json.dumps({"evaluators": ids, "protocol_sha256": "a" * 64}),
                ),
            )
            for i in range(101):
                db.execute("INSERT INTO states VALUES(?)", (str(i),))
                db.executemany(
                    "INSERT INTO evaluations VALUES(?,?)",
                    [(str(i), v) for v in ids.values()],
                )
        report = encoded(
            {"candidates": [{"candidate_index": i} for i in range(1, 101)]}
        )
        parent["report_sha256"] = digest(report)
        payloads = {
            "graph.sqlite": graph.read_bytes(),
            "run-report.json": report,
            "progress.json": b"{}",
        }
        archive = root / "parent.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, raw in payloads.items():
                item = tarfile.TarInfo(name)
                item.size = len(raw)
                tar.addfile(item, io.BytesIO(raw))
        archive_ref = {
            "uri": "s3://bucket/parent.tar.gz",
            "sha256": digest(archive.read_bytes()),
            "bytes": archive.stat().st_size,
        }
        index = {
            "archive": archive_ref,
            "files": [
                {"path": k, "sha256": digest(v), "bytes": len(v)}
                for k, v in payloads.items()
            ],
        }
        raw = encoded(index)
        parent["checkpoint"] = {
            "archive": archive_ref,
            "index": {
                "uri": "s3://bucket/index.json",
                "sha256": digest(raw),
                "bytes": len(raw),
            },
            "files": index["files"],
            "wandb_artifact": "parent:v0",
        }
        return graph, parent, ids, archive, index, raw

    def test_indexed_member_corruption_never_exposes_graph(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph, parent, ids, archive, index, _ = self.parent_fixture(root)
            original = digest(graph.read_bytes())
            index["files"][-1]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify_archive(archive, index, root / "target.sqlite")
            self.assertFalse((root / "target.sqlite").exists())
            self.assertEqual(digest(graph.read_bytes()), original)
            check_parent_graph(graph, parent, ids)
            with sqlite3.connect(graph) as db:
                db.execute("DELETE FROM evaluations WHERE state_id='100'")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                check_parent_graph(graph, parent, ids)

    def test_s3_wandb_and_member_verification_precede_restore(self):
        for failure in (None, "s3", "wandb", "uncommitted"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                graph, parent, ids, archive, index, index_raw = self.parent_fixture(
                    root
                )
                original = digest(graph.read_bytes())
                objects = {
                    "parent.tar.gz": archive.read_bytes(),
                    "index.json": index_raw,
                }

                def get_object(**kw):
                    self.assertEqual(kw["ExpectedBucketOwner"], "owner")
                    return {
                        "Body": io.BytesIO(
                            b"corrupt" if failure == "s3" else objects[kw["Key"]]
                        )
                    }

                def entry(name):
                    def download(root, skip_cache=False):
                        self.assertTrue(skip_cache)
                        p = Path(root) / name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        data = objects[
                            "parent.tar.gz" if name == "checkpoint.tar.gz" else name
                        ]
                        p.write_bytes(b"corrupt" if failure == "wandb" else data)
                        return p

                    return SimpleNamespace(download=download)

                remote = SimpleNamespace(
                    state="PENDING" if failure == "uncommitted" else "COMMITTED",
                    get_entry=entry,
                )
                wb = SimpleNamespace(
                    Api=lambda **kw: SimpleNamespace(artifact=lambda name: remote)
                )
                archiver = SimpleNamespace(
                    s3=SimpleNamespace(get_object=get_object),
                    bucket="bucket",
                    owner="owner",
                )
                folder = root / "new"
                folder.mkdir()
                if failure:
                    with self.assertRaises(ValueError):
                        restore_parent(
                            folder,
                            {"parent": parent},
                            {"evaluator_ids": ids},
                            archiver,
                            wb,
                        )
                    self.assertFalse((folder / "graph.sqlite").exists())
                else:
                    proof = restore_parent(
                        folder, {"parent": parent}, {"evaluator_ids": ids}, archiver, wb
                    )
                    self.assertTrue(proof["every_indexed_member_verified"])
                    check_parent_graph(folder / "graph.sqlite", parent, ids)
                    with self.assertRaises(FileExistsError):
                        restore_parent(
                            folder,
                            {"parent": parent},
                            {"evaluator_ids": ids},
                            archiver,
                            wb,
                        )
                self.assertEqual(digest(graph.read_bytes()), original)

    def test_archive_path_and_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph, parent, ids, archive, index, _ = self.parent_fixture(root)
            with tarfile.open(root / "unsafe.tar.gz", "w:gz") as tar:
                item = tarfile.TarInfo("../outside")
                item.size = 1
                tar.addfile(item, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "unsafe"):
                verify_archive(root / "unsafe.tar.gz", index, root / "target.sqlite")
            index["files"].append(index["files"][0])
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                verify_archive(archive, index, root / "target.sqlite")

    def test_export_keeps_parent_and_checks_inherited_candidate_prefix(self):
        from scripts.collect_search_replication import export_graph_report
        from scripts.plan_search_replication import state_id

        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "bundle"
            prepare(ROOT, bundle)
            spec, jobs = verify_bundle(bundle)
            job = jobs[0]
            root = json.loads((bundle / "inputs/starting-genotype.json").read_text())
            available = [
                f"b{i:04}"
                for i in range(1, 4291)
                if f"b{i:04}" not in root["deleted_gene_ids"]
            ][:100]
            records = []
            for i, gene in enumerate(available, 1):
                genes = sorted(root["deleted_gene_ids"] + [gene])
                metrics = {
                    "fba": {"feasible": True, "growth_rate": 1.0},
                    "resource_allocation": {
                        "feasible_at_growth_floor": True,
                        "growth_rate_floor_h": 0.1,
                    },
                    "genome_size": {"genes_deleted": len(genes)},
                    "essentiality": {},
                    "module_retention": {},
                }
                records.append(
                    {
                        "candidate_index": i,
                        "state_id": state_id(genes),
                        "deleted_gene_ids": genes,
                        "evaluation_complete": True,
                        "incomplete_reason": None,
                        "evaluations": {
                            k: {"evaluator_id": v, "metrics": metrics[k]}
                            for k, v in spec["evaluator_ids"].items()
                        },
                    }
                )
            graph = Path(temp) / "graph.sqlite"
            protocol_hash = digest((bundle / "protocol.json").read_bytes())
            with sqlite3.connect(graph) as db:
                db.executescript(
                    "CREATE TABLE runs(run_id,status,metadata_json); CREATE TABLE states(state_id,state_json); CREATE TABLE evaluations(state_id,evaluator_id,metrics_json);"
                )
                for run_id, checksum in [
                    (job["parent"]["run_id"], job["parent"]["protocol_sha256"]),
                    (job["run_id"], protocol_hash),
                ]:
                    meta = {
                        "evaluators": spec["evaluator_ids"],
                        "protocol_sha256": checksum,
                        "parent_checkpoint": {
                            "candidate_prefix_sha256": digest(encoded(records))
                        },
                    }
                    db.execute(
                        "INSERT INTO runs VALUES(?,?,?)",
                        (run_id, "completed", json.dumps(meta)),
                    )
                db.execute(
                    "INSERT INTO states VALUES(?,?)",
                    (
                        root["state_id"],
                        json.dumps({"deleted_genes": root["deleted_gene_ids"]}),
                    ),
                )
                for row in records:
                    db.execute(
                        "INSERT INTO states VALUES(?,?)",
                        (
                            row["state_id"],
                            json.dumps({"deleted_genes": row["deleted_gene_ids"]}),
                        ),
                    )
                    for record in row["evaluations"].values():
                        db.execute(
                            "INSERT INTO evaluations VALUES(?,?,?)",
                            (
                                row["state_id"],
                                record["evaluator_id"],
                                json.dumps(record["metrics"]),
                            ),
                        )
            terminal = {
                "status": "deadline",
                "stop_reason": "max_wall_time_s",
                "validation_started": False,
                "model_spend_usd": 0,
                "wall_time_s": 1,
                "cpu_hours": 0,
            }
            before = digest(graph.read_bytes())
            report = export_graph_report(bundle, 0, graph, terminal)
            self.assertEqual(report["additional_attempted_candidates"], 0)
            self.assertEqual(report["parent_run_id"], job["parent"]["run_id"])
            self.assertEqual(digest(graph.read_bytes()), before)
            with sqlite3.connect(graph) as db:
                db.execute(
                    "UPDATE evaluations SET metrics_json=? WHERE state_id=? AND evaluator_id=?",
                    (
                        json.dumps({"feasible": True, "growth_rate": 2.0}),
                        records[0]["state_id"],
                        spec["evaluator_ids"]["fba"],
                    ),
                )
            with self.assertRaisesRegex(ValueError, "Inherited candidate records"):
                export_graph_report(bundle, 0, graph, terminal)

    def test_synthetic_clone_continues_without_resetting_candidates(self):
        async def fixture():
            import pandas as pd
            from yggdrisil import EvaluatorSuite, RunLimits, Runner, SQLiteStateGraph

            from yggdrisil_ecoli.baseline_storage import sqlite_snapshot
            from yggdrisil_ecoli.policies import BlockDeletionPolicy
            from yggdrisil_ecoli.problem import EcoliProblem
            from yggdrisil_ecoli.scorers.base import (
                active_evaluator_ids,
                scientific_evaluation,
            )
            from yggdrisil_ecoli.state import GenomeState

            class Scorer:
                version = "fixture"
                config = "synthetic-offline"

                def __init__(self, name):
                    self.name = name

                async def evaluate(self, state):
                    if self.name == "fba":
                        return scientific_evaluation(
                            {"feasible": True, "growth_rate": 1.0}
                        )
                    return scientific_evaluation(
                        {"feasible_at_growth_floor": "b0001" not in state.deleted_genes}
                    )

            scorers = [Scorer("fba"), Scorer("resource_allocation")]
            ids = active_evaluator_ids(scorers)
            genes = pd.DataFrame(index=[f"b{i:04}" for i in range(1, 141)])

            def policy(kind):
                return BlockDeletionPolicy(
                    gene_ids=list(genes.index),
                    evaluator_ids=ids,
                    initial_state=GenomeState(frozenset()),
                    kind=kind,
                    seed=17,
                    block_sizes=(4, 2, 1),
                    neighborhood_size=3,
                )

            for kind in ["greedy_block", "randomized_block"]:
                with tempfile.TemporaryDirectory() as temp:
                    parent = Path(temp) / "parent.sqlite"
                    child = Path(temp) / "child.sqlite"
                    full = Path(temp) / "full.sqlite"
                    with SQLiteStateGraph(parent) as graph:
                        await Runner(
                            EcoliProblem(genes),
                            policy(kind),
                            graph,
                            RunLimits(max_states=7, max_steps=50),
                            evaluators=EvaluatorSuite(scorers),
                            run_id="parent",
                            resume=False,
                        ).run()
                    before = digest(parent.read_bytes())
                    sqlite_snapshot(parent, child)
                    sqlite_snapshot(parent, full)
                    for path, run_id, resume in [
                        (child, "continuation", False),
                        (full, "parent", True),
                    ]:
                        with SQLiteStateGraph(path) as graph:
                            await Runner(
                                EcoliProblem(genes),
                                policy(kind),
                                graph,
                                RunLimits(max_states=15, max_steps=50),
                                evaluators=EvaluatorSuite(scorers),
                                run_id=run_id,
                                resume=resume,
                            ).run()
                            self.assertEqual(len(graph), 15)
                            for edge in graph.edges():
                                incoming = [
                                    e
                                    for e in graph.edges()
                                    if e.child_id == edge.parent_id
                                ]
                                self.assertTrue(
                                    all(
                                        edge.metadata["block_size"]
                                        <= e.metadata["block_size"]
                                        for e in incoming
                                    )
                                )
                    with SQLiteStateGraph(child) as a, SQLiteStateGraph(full) as b:
                        self.assertEqual(
                            {n.state_id for n in a.states()},
                            {n.state_id for n in b.states()},
                        )
                        self.assertEqual(a.get_run("parent").status, "completed")
                        self.assertEqual(a.get_run("continuation").status, "completed")
                    self.assertEqual(digest(parent.read_bytes()), before)

        asyncio.run(fixture())


if __name__ == "__main__":
    unittest.main()
