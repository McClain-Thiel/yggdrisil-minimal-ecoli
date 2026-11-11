"""Offline launch and retrieval-boundary checks; no model or scientific calls."""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]


class RetrievalBoundaryTests(unittest.TestCase):
    def setUp(self):
        stub = types.ModuleType("yggdrisil.agents")
        stub.ToolInputError = ValueError
        spec = importlib.util.spec_from_file_location(
            "checked_literature", ROOT / "src/yggdrisil_ecoli/tools/literature.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"yggdrisil.agents": stub}):
            spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "YGGDRISIL_LITERATURE_CUTOFF": "2026-11-08",
                "YGGDRISIL_LITERATURE_ARCHIVE": self.temp.name,
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_query_date_is_outer_constraint(self):
        query = parse_qs(urlparse(self.module._search_url("foo OR bar", 5)).query)[
            "query"
        ][0]
        self.assertEqual(
            query, "(foo OR bar) AND FIRST_PDATE:[1000-01-01 TO 2026-11-08]"
        )

    def test_missing_or_newer_date_rejected(self):
        for paper in ({}, {"firstPublicationDate": "2026-11-09"}):
            with self.assertRaises(RuntimeError):
                self.module._check_dates([paper])
        self.module._check_dates([{"firstPublicationDate": "2026-11-08"}])

    def test_direct_fulltext_cannot_bypass_cutoff(self):
        with patch.object(
            self.module, "_get", return_value=b'{"resultList":{"result":[]}}'
        ) as fetch:
            with self.assertRaises(ValueError):
                asyncio.run(self.module.read_literature("PMC123"))
            self.assertEqual(fetch.call_count, 1)
            self.assertNotIn("fullTextXML", fetch.call_args.args[0])

    def test_fulltext_requires_matching_identifier_and_date(self):
        metadata = json.dumps(
            {
                "resultList": {
                    "result": [
                        {"pmcid": "PMC123", "firstPublicationDate": "2020-01-01"}
                    ]
                }
            }
        ).encode()
        with patch.object(
            self.module,
            "_get",
            side_effect=[metadata, b"<article><p>Evidence</p></article>"],
        ) as fetch:
            result = asyncio.run(self.module.read_literature("PMC123"))
            self.assertEqual(result["text"], "Evidence")
            self.assertEqual(fetch.call_count, 2)

    def test_exact_response_and_each_request_are_durable(self):
        self.module._archive("https://example.org/query", b"exact response")
        self.module._archive("https://example.org/query", b"exact response")
        root = Path(self.temp.name)
        self.assertEqual(len(list(root.glob("*.response"))), 1)
        self.assertEqual(len((root / "requests.jsonl").read_text().splitlines()), 2)
        self.assertEqual(next(root.glob("*.response")).read_bytes(), b"exact response")

    def test_archive_failure_propagates(self):
        with patch.object(self.module.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.module._archive("https://example.org/query", b"response")

    def test_literature_timeout_retries_identical_url_and_archives_success(self):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"exact literature"
        with (
            patch.object(
                self.module, "urlopen", side_effect=[TimeoutError("read"), response]
            ) as fetch,
            patch.object(self.module.time, "sleep"),
        ):
            self.assertEqual(
                self.module._get("https://example.org/frozen"), b"exact literature"
            )
        self.assertIs(fetch.call_args_list[0].args[0], fetch.call_args_list[1].args[0])
        self.assertEqual(len(list(Path(self.temp.name).glob("*.response"))), 1)

    def test_persistent_literature_timeout_is_not_empty_evidence(self):
        with (
            patch.object(
                self.module, "urlopen", side_effect=TimeoutError("read")
            ) as fetch,
            patch.object(self.module.time, "sleep"),
        ):
            with self.assertRaises(TimeoutError):
                self.module._get("https://example.org/frozen")
        self.assertEqual(fetch.call_count, 3)
        self.assertFalse(list(Path(self.temp.name).glob("*.response")))


class LaunchGuards(unittest.TestCase):
    def test_queue_wait_does_not_reset_or_consume_execution_allowance(self):
        from scripts.run_search_replication import remaining_job_window

        self.assertEqual(remaining_job_window(50000, 3600, 100000), 68000)
        self.assertEqual(remaining_job_window(60000, 3600, 100000), 78000)
        self.assertEqual(remaining_job_window(60000, 3600, 65000), 65000)
        self.assertEqual(remaining_job_window(60000, 21600, 100000), 60000)
        for spent in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                remaining_job_window(60000, spent, 100000)

    def test_transport_resume_keeps_exact_existing_databases(self):
        from scripts.plan_search_replication import digest
        from scripts.run_search_replication import save
        from scripts.supervise_search_replication import resume_jobs

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            folder = bundle / "search/job"
            old = root / "attempts/job"
            folder.mkdir(parents=True)
            old.mkdir(parents=True)
            (folder / "graph.sqlite").write_bytes(b"preserved candidate graph")
            (folder / "agents.sqlite").write_bytes(b"preserved invocation and cost")
            save(
                old / "run-report.json",
                {
                    "run_id": "job",
                    "error": {"type": "RateLimitError"},
                    "validation_started": False,
                    "candidates": [{}, {}],
                },
            )
            recovery = {
                "status": "verified",
                "retained_jobs": {},
                "resumes": {
                    "job": {
                        "preserved_directory": "attempts/job",
                        "report_sha256": digest((old / "run-report.json").read_bytes()),
                        "database_hashes": {
                            p.name: digest(p.read_bytes()) for p in folder.iterdir()
                        },
                    }
                },
            }
            save(root / "transport-recovery.json", recovery)
            approval = {
                "transport_recovery_sha256": digest(
                    (root / "transport-recovery.json").read_bytes()
                )
            }
            jobs = [{"run_id": "job", "directory": "search/job"}]
            self.assertEqual(resume_jobs(root, bundle, jobs, approval), {})
            (folder / "graph.sqlite").write_bytes(b"reset graph")
            with self.assertRaisesRegex(ValueError, "exact graph and session"):
                resume_jobs(root, bundle, jobs, approval)

    def test_dependency_retry_requires_zero_science_and_verified_old_attempt(self):
        from scripts.plan_search_replication import digest
        from scripts.run_search_replication import save
        from scripts.supervise_search_replication import resume_jobs

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            bundle.mkdir()
            old = root / "attempts/job"
            old.mkdir(parents=True)
            report = {
                "run_id": "job",
                "candidates": [],
                "model_spend_usd": 0,
                "validation_started": False,
                "error": {"message": "No module named 'mcp'"},
            }
            for change, allowed in (
                ({}, True),
                ({"candidates": [{}]}, False),
                ({"model_spend_usd": 0.01}, False),
                ({"validation_started": True}, False),
                ({"error": {"message": "solver failed"}}, False),
            ):
                save(old / "run-report.json", {**report, **change})
                repair = {
                    "status": "verified",
                    "retained_jobs": {},
                    "retries": {
                        "job": {
                            "preserved_directory": "attempts/job",
                            "report_verification": {
                                "report_sha256": digest(
                                    (old / "run-report.json").read_bytes()
                                ),
                                "full_s3_get_verified": True,
                                "wandb_download_verified": True,
                            },
                        }
                    },
                }
                save(root / "operational-repair.json", repair)
                approval = {
                    "operational_repair_sha256": digest(
                        (root / "operational-repair.json").read_bytes()
                    )
                }
                if allowed:
                    self.assertEqual(
                        resume_jobs(root, bundle, [{"run_id": "job"}], approval), {}
                    )
                else:
                    with self.assertRaises(ValueError):
                        resume_jobs(root, bundle, [{"run_id": "job"}], approval)

    def test_approval_binds_budget_deadlines_worker_and_runtime(self):
        from scripts.plan_search_replication import digest, prepare
        from scripts.run_search_replication import validate_launch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            from tests.study_fixtures import replication_inputs

            inputs = root / "fixture-inputs"
            protocol = replication_inputs(inputs)
            prepare(protocol, inputs, bundle)
            repo = root / "application"
            repo.mkdir()
            marker = repo / "marker.txt"
            marker.write_bytes(b"frozen runtime")
            runtime = {"files": {"application/marker.txt": digest(marker.read_bytes())}}
            (bundle / "runtime-manifest.json").write_text(json.dumps(runtime))
            now = time.time()
            approval = {
                "paid_launch": True,
                "science_dispatch_ready": True,
                "experiment_id": "search-replication-20261109",
                "total_cap_usd": 550,
                "protocol_sha256": digest((bundle / "protocol.json").read_bytes()),
                "manifest_sha256": digest((bundle / "manifest.json").read_bytes()),
                "runtime_manifest_sha256": digest(
                    (bundle / "runtime-manifest.json").read_bytes()
                ),
                "user_authorization": "Explicit launch approval",
                "authorized_start_unix": now - 1,
                "science_deadline_unix": now + 3600,
                "stop_deadline_unix": now + 10800,
                "instance_id": "dedicated",
            }
            with (
                patch("scripts.run_search_replication.sys.platform", "linux"),
                patch(
                    "scripts.run_search_replication.worker_identity",
                    return_value="dedicated",
                ),
            ):
                self.assertEqual(len(validate_launch(bundle, repo, approval)[1]), 25)
                for change in (
                    {"total_cap_usd": 551},
                    {"science_dispatch_ready": False},
                    {"instance_id": "shared"},
                    {"science_deadline_unix": now - 10},
                    {"stop_deadline_unix": now + 73 * 3600},
                    {"runtime_manifest_sha256": "0" * 64},
                ):
                    with self.subTest(change=change), self.assertRaises(ValueError):
                        validate_launch(bundle, repo, {**approval, **change})
                marker.write_bytes(b"changed runtime")
                with self.assertRaisesRegex(ValueError, "Changed deployed runtime"):
                    validate_launch(bundle, repo, approval)

    def test_mac_rejected_before_reading_bundle_or_importing_science(self):
        from scripts.run_search_replication import validate_launch

        with patch("scripts.run_search_replication.sys.platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "remote Linux"):
                validate_launch(Path("missing"), Path("missing"), {})


class TransientRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, effects, *, enabled=True, backup_error=None, model_cap=0):
        from unittest.mock import AsyncMock, Mock

        from scripts.run_search_replication import CheckpointPolicy

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy = types.SimpleNamespace(step=AsyncMock(side_effect=effects))
        self.archive = types.SimpleNamespace(checkpoint=Mock(side_effect=backup_error))
        folder = Path(self.temp.name)
        self.wrapped = CheckpointPolicy(
            self.policy, self.archive, Mock(), folder, folder, {}, model_cap
        )
        self.graph, self.status = [object(), object()], types.SimpleNamespace(step=4)
        self.sleeps = AsyncMock()
        with (
            patch.dict(
                os.environ,
                {"YGGDRISIL_RECOVER_TRANSIENT_STEPS": "1" if enabled else "0"},
            ),
            patch("scripts.run_search_replication.asyncio.sleep", self.sleeps),
        ):
            return await self.wrapped._step_with_transport_recovery(
                self.graph, self.status, {"attempted_candidates": 1}
            )

    async def test_timeout_reuses_exact_graph_and_status(self):
        result = [object()]
        actual = await self.invoke(
            [RuntimeError("ADK TimeoutError: read timed out"), result]
        )
        self.assertIs(actual, result)
        self.assertEqual(self.policy.step.await_count, 2)
        for call in self.policy.step.await_args_list:
            self.assertIs(call.args[0], self.graph)
            self.assertIs(call.args[1], self.status)
        self.assertEqual(self.archive.checkpoint.call_count, 1)
        self.assertEqual(
            json.loads((Path(self.temp.name) / "transport-events.jsonl").read_text())[
                "step"
            ],
            4,
        )

    async def test_unknown_and_budget_errors_are_not_retried(self):
        for error in (
            ValueError("bad input"),
            RuntimeError("ADK request_limit exhausted"),
            RuntimeError("GLP_UNBND"),
        ):
            with self.subTest(error=error), self.assertRaises(type(error)):
                await self.invoke([error])
            self.assertEqual(self.policy.step.await_count, 1)
            self.assertEqual(self.archive.checkpoint.call_count, 0)

    async def test_persistent_transient_error_fails_loudly_after_bound(self):
        with self.assertRaisesRegex(RuntimeError, "ADK TimeoutError"):
            await self.invoke(
                [RuntimeError("ADK TimeoutError: offline") for _ in range(7)]
            )
        self.assertEqual(self.policy.step.await_count, 7)
        self.assertEqual(self.sleeps.await_count, 6)

    async def test_backup_failure_prevents_retry(self):
        with self.assertRaisesRegex(OSError, "backup failed"):
            await self.invoke(
                [RuntimeError("ADK TimeoutError: offline")],
                backup_error=OSError("backup failed"),
            )
        self.assertEqual(self.policy.step.await_count, 1)

    async def test_unconfigured_calls_do_not_retry(self):
        with self.assertRaisesRegex(RuntimeError, "ADK TimeoutError"):
            await self.invoke(
                [RuntimeError("ADK TimeoutError: offline")], enabled=False
            )
        self.assertEqual(self.policy.step.await_count, 1)

    async def test_cancellation_stops_waiting(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke([asyncio.CancelledError()])
        self.assertEqual(self.archive.checkpoint.call_count, 0)

    async def test_original_spending_cap_checked_before_retry(self):
        usage = {"limit": 30, "limit_reset": None, "usage": 29.1}
        with patch("scripts.run_literature_ablation.key_usage", return_value=usage):
            result = await self.invoke(
                [RuntimeError("ADK TimeoutError: offline")], model_cap=30
            )
        self.assertTrue(self.wrapped.stopped_for_budget)
        self.assertEqual(self.policy.step.await_count, 1)
        self.assertEqual(result[0].role, "budget")


if __name__ == "__main__":
    unittest.main()
