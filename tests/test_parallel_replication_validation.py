import copy
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "parallel",
    Path(__file__).resolve().parents[1] / "scripts/parallel_replication_validation.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ParallelSafetyTests(unittest.TestCase):
    def plan(self):
        return {
            "workers": [
                {
                    "seed": seed,
                    "instance_id": "i-" + str(seed),
                    "existing_worker": seed == 101,
                }
                for seed in module.SEEDS
            ],
            "additional_cap_usd": 100,
            "maximum_cells_per_worker": 8,
            "additional_maximum_vm_usd": 77.4144,
            "science_deadline_unix": 100,
            "stop_deadline_unix": 200,
            "original_stop_deadline_unix": 300,
            "s3_prefix": "s3://private/study/validation/parallel/",
        }

    def test_missing_worker_waits_without_dispatch_and_deadline_still_applies(self):
        records = [
            None,
            None,
            *[
                {
                    "seed": s,
                    "instance_id": "i-" + str(s),
                    "phase": "ready_waiting_for_verified_finalists",
                    "checked_at_unix": 50,
                }
                for s in (211, 307)
            ],
        ]
        with (
            patch.object(module, "get_json", side_effect=records),
            patch.object(module, "save"),
            patch.object(module, "put_json"),
            patch.object(module.time, "time", return_value=50),
            patch.object(module.time, "sleep") as sleep,
        ):
            module.await_workers(None, "owner", self.plan(), {})
            sleep.assert_called_once_with(60)
        with (
            patch.object(module.time, "time", return_value=101),
            self.assertRaises(TimeoutError),
        ):
            module.await_workers(None, "owner", self.plan(), {})

    def test_worker_readiness_never_accepts_wrong_identity_or_existing_run(self):
        for record in [
            {
                "seed": 211,
                "instance_id": "wrong",
                "phase": "ready_waiting_for_verified_finalists",
                "checked_at_unix": 50,
            },
            {
                "seed": 211,
                "instance_id": "i-211",
                "phase": "running",
                "checked_at_unix": 50,
            },
        ]:
            with (
                patch.object(module, "get_json", return_value=record),
                patch.object(module.time, "time", return_value=50),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                module.await_workers(None, "owner", self.plan(), {})

    def test_assignment_requires_exact_seeds_and_distinct_workers(self):
        module.check_assignments(self.plan())
        for field in ("seed", "instance_id"):
            p = self.plan()
            p["workers"][1][field] = p["workers"][0][field]
            with self.assertRaises(ValueError):
                module.check_assignments(p)

    def test_budget_and_deadlines_cannot_expand(self):
        for field, value in [
            ("additional_cap_usd", 101),
            ("additional_maximum_vm_usd", 80),
            ("maximum_cells_per_worker", 9),
            ("stop_deadline_unix", 301),
            ("science_deadline_unix", 201),
        ]:
            p = self.plan()
            p[field] = value
            with self.assertRaises(ValueError):
                module.check_assignments(p)

    def test_only_absent_control_references_are_normalized(self):
        old = {
            "finalists": [
                {
                    "variant_index": 1,
                    "deletion_count": 1030,
                    "search_result": float("nan"),
                    "search_data_artifact": float("nan"),
                    "fba_growth_rate": 0.1,
                }
            ]
        }
        new = module.normalize_control_metadata(old)
        self.assertIsNone(new["finalists"][0]["search_result"])
        self.assertIsNone(new["finalists"][0]["search_data_artifact"])
        self.assertEqual(new["finalists"][0]["fba_growth_rate"], 0.1)
        self.assertTrue(__import__("math").isnan(old["finalists"][0]["search_result"]))
        json.dumps(new, allow_nan=False)

    def test_nonfinite_scientific_values_remain_rejected(self):
        for finalist in [
            {
                "variant_index": 1,
                "deletion_count": 1030,
                "fba_growth_rate": float("nan"),
            },
            {"variant_index": 2, "deletion_count": 1800, "search_result": float("nan")},
            {"variant_index": 1, "deletion_count": 1030, "search_result": float("inf")},
        ]:
            with self.assertRaises(ValueError):
                json.dumps(
                    module.normalize_control_metadata({"finalists": [finalist]}),
                    allow_nan=False,
                )

    def test_cpu_change_preserves_every_other_setting(self):
        old = {
            "SIM_CPUS": 5,
            "SIM_MEM": 4,
            "generations": 20,
            "lineage_seed": 101,
            "single_daughters": True,
            "variants": {"frozen": [1, 5]},
        }
        new = module.resource_config(old)
        self.assertEqual(old["SIM_CPUS"], 5)
        self.assertEqual(
            {k: v for k, v in old.items() if k != "SIM_CPUS"},
            {k: v for k, v in new.items() if k != "SIM_CPUS"},
        )

    def packed(self, folder, name="data.json", content=b"preserved", symlink=False):
        path = folder / "data.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo(name)
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return path, {
            "files": [
                {
                    "path": name,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ]
        }

    def test_archive_restores_only_exact_verified_members(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packed, index = self.packed(root)
            module.inspect_members(packed, index)
            dest = root / "restore"
            dest.mkdir()
            module.inspect_members(packed, index, dest)
            self.assertEqual((dest / "data.json").read_bytes(), b"preserved")
            module.inspect_members(packed, index, dest)
            (dest / "data.json").write_bytes(b"newer unique result")
            with self.assertRaises(FileExistsError):
                module.inspect_members(packed, index, dest)
            self.assertEqual((dest / "data.json").read_bytes(), b"newer unique result")

    def test_unsafe_source_and_credential_paths_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in (
                "../outside.json",
                "/outside.json",
                "credentials/tracking.json",
                "source/code.json",
                "code.py",
                "secret.pem",
                ".env",
            ):
                with self.subTest(name=name):
                    packed, index = self.packed(Path(temp), name)
                    with self.assertRaises(ValueError):
                        module.inspect_members(packed, index)

    def test_symlinks_and_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            packed, index = self.packed(Path(temp), symlink=True)
            with self.assertRaises(ValueError):
                module.inspect_members(packed, index)
            packed, index = self.packed(Path(temp))
            index["files"][0]["sha256"] = "0" * 64
            with self.assertRaises(RuntimeError):
                module.inspect_members(packed, index)

    def test_missing_and_duplicate_index_members_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            packed, index = self.packed(Path(temp))
            index["files"].append(copy.deepcopy(index["files"][0]))
            with self.assertRaises(ValueError):
                module.inspect_members(packed, index)
            index["files"][1]["path"] = "missing.json"
            with self.assertRaises(ValueError):
                module.inspect_members(packed, index)

    def test_seed_preparation_keeps_genotypes_and_science(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            valid = root / "validation"
            (valid / "runs").mkdir(parents=True)
            (root / "bundle").mkdir()
            module.save(root / "bundle/nominations.json", {"frozen": True})
            hashes = {}
            genotypes = [{"name": "control", "deletion_set_sha256": "frozen"}]
            for label, seed in [
                ("calibration", 101),
                ("seed-101", 101),
                ("seed-211", 211),
                ("seed-307", 307),
            ]:
                folder = valid / "runs" / label
                folder.mkdir()
                config = {
                    "SIM_CPUS": 5,
                    "SIM_MEM": 4,
                    "lineage_seed": seed,
                    "generations": 2 if label == "calibration" else 20,
                    "emitter_arg": {"out_dir": str(folder / "output")},
                    "variants": {"frozen": [1, 2]},
                }
                manifest = {
                    "experiment_id": label,
                    "workflow": {},
                    "execution": {},
                    "tracking": {},
                    "purpose": "fixed",
                    "finalists": genotypes,
                    "lineage": {"seed": seed},
                }
                module.save(folder / "workflow.json", config)
                module.save(folder / "manifest.json", manifest)
                hashes[label] = module.digest(folder / "manifest.json")
            module.save(
                valid / "prepared.json",
                {"genotypes": genotypes, "manifest_sha256": hashes},
            )
            fake = types.ModuleType("yggdrisil_ecoli.vecoli_results")
            fake._validate_manifest_provenance = lambda _: None
            with (
                patch.object(module, "ROOT", root),
                patch.object(module, "VALID", valid),
                patch.dict("sys.modules", {"yggdrisil_ecoli.vecoli_results": fake}),
            ):
                module.prepare_assignments(self.plan())
                first = module.digest(valid / "parallel-assignments.json")
                module.prepare_assignments(self.plan())
                self.assertEqual(
                    module.digest(valid / "parallel-assignments.json"), first
                )
            for seed in module.SEEDS:
                full = module.read(valid / "runs" / f"seed-{seed}" / "manifest.json")
                cal = module.read(
                    valid / "runs" / f"calibration-worker-{seed}" / "manifest.json"
                )
                self.assertEqual(full["finalists"], genotypes)
                self.assertEqual(full["lineage"]["seed"], seed)
                self.assertEqual(cal["lineage"]["seed"], 101)
                self.assertEqual(
                    cal["tracking"]["s3_prefix"],
                    f"s3://private/study/validation/parallel/calibration/{seed}",
                )
                self.assertEqual(full["execution"]["max_parallel_cells"], 8)
                self.assertEqual(
                    full["tracking"]["s3_prefix"],
                    f"s3://private/study/validation/parallel/seed-{seed}",
                )
            ids = [
                module.read(
                    valid / "runs" / f"calibration-worker-{s}" / "manifest.json"
                )["tracking"]["run_id"]
                for s in module.SEEDS
            ]
            self.assertEqual(len(set(ids)), 3)


if __name__ == "__main__":
    unittest.main()
