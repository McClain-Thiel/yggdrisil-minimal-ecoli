"""Synthetic safety checks for disjoint dispatch and verified result import."""

import hashlib
import io
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.supervise_baseline_depth_parallel import (
    identity,
    unpack_verified,
    validate_assignment,
)


class ParallelSafetyTests(unittest.TestCase):
    def assignment(self):
        now = time.time()
        return {
            "workers": {
                "primary": {"instance_id": "one", "job_indices": [0, 1, 2, 3, 8]},
                "secondary": {"instance_id": "two", "job_indices": [4, 5, 6, 7, 9]},
            },
            "combined_cap_usd": 25,
            "maximum_jobs_per_worker": 4,
            "maximum_total_jobs": 8,
            "model_calls_allowed": False,
            "validation_launch_authorized": False,
            "authorized_start_unix": now,
            "science_deadline_unix": now + 36000,
            "stop_deadline_unix": now + 43200,
        }

    def test_disjoint_complete_assignment(self):
        validate_assignment(self.assignment(), [{}] * 10)

    def test_duplicate_and_missing_job_rejected(self):
        a = self.assignment()
        a["workers"]["secondary"]["job_indices"][-1] = 0
        with self.assertRaises(ValueError):
            validate_assignment(a, [{}] * 10)

    def test_same_instance_rejected(self):
        a = self.assignment()
        a["workers"]["secondary"]["instance_id"] = "one"
        with self.assertRaises(ValueError):
            validate_assignment(a, [{}] * 10)

    def test_extended_cost_window_rejected(self):
        a = self.assignment()
        a["stop_deadline_unix"] += 1
        with self.assertRaises(ValueError):
            validate_assignment(a, [{}] * 10)

    def test_pid_reuse_rejected_before_signalling(self):
        fields = ["0"] * 22
        fields[2] = "S"
        fields[21] = "new"
        with (
            patch.object(Path, "read_text", return_value=" ".join(fields)),
            patch.object(Path, "read_bytes", return_value=b"python\0"),
        ):
            with self.assertRaisesRegex(RuntimeError, "reused"):
                identity(123, "old", 2)

    def test_wrong_search_pid_rejected(self):
        fields = ["0"] * 22
        fields[2] = "S"
        fields[21] = "old"
        with (
            patch.object(Path, "read_text", return_value=" ".join(fields)),
            patch.object(
                Path,
                "read_bytes",
                return_value=b"\0".join(
                    [
                        b"python",
                        b"scripts.run_search_replication",
                        b"--job-index",
                        b"4",
                        b"",
                    ]
                ),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "assigned"):
                identity(123, "old", 2)

    def test_verified_members_and_corruption(self):
        with tempfile.TemporaryDirectory() as t:
            r = Path(t)
            p = r / "archive.tar.gz"
            raw = b"preserved result"
            name = "search/run/run-report.json"
            with tarfile.open(p, "w:gz") as tar:
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                tar.addfile(member, io.BytesIO(raw))
            index = {
                "files": [
                    {
                        "path": name,
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ]
            }
            unpack_verified(p, index, r / "valid")
            self.assertEqual((r / "valid" / name).read_bytes(), raw)
            index["files"][0]["sha256"] = "0" * 64
            with self.assertRaisesRegex(RuntimeError, "member mismatch"):
                unpack_verified(p, index, r / "corrupt")

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            r = Path(t)
            p = r / "archive.tar.gz"
            with tarfile.open(p, "w:gz") as tar:
                member = tarfile.TarInfo("../escape")
                member.size = 1
                tar.addfile(member, io.BytesIO(b"x"))
            index = {
                "files": [
                    {
                        "path": "../escape",
                        "bytes": 1,
                        "sha256": hashlib.sha256(b"x").hexdigest(),
                    }
                ]
            }
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                unpack_verified(p, index, r / "target")
            self.assertFalse((r / "escape").exists())


def test_restore_rejects_parent_symlink_and_validates_before_writing(tmp_path):
    import pytest

    archive = tmp_path / "data.tar.gz"
    files = {"first.json": b"first", "nested/second.json": b"second"}
    index = {
        "files": [
            {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in files.items()
        ]
    }
    with tarfile.open(archive, "w:gz") as tar:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            tar.addfile(member, io.BytesIO(raw))
    destination = tmp_path / "restore"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Unsafe archive destination"):
        unpack_verified(archive, index, destination)
    assert not (outside / "second.json").exists()
    assert not (destination / "first.json").exists()

    index["files"][1]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="member mismatch"):
        unpack_verified(archive, index, tmp_path / "corrupt")
    assert not (tmp_path / "corrupt").exists()
