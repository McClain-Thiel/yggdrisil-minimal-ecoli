"""Exercise preservation failures without network access or scientific execution."""

from __future__ import annotations

import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from yggdrisil_ecoli.baseline_storage import BaselineArchive


class FakeS3:
    def __init__(self, corrupt: bool = False) -> None:
        self.objects = {}
        self.corrupt = corrupt

    def head_bucket(self, **kwargs):
        return {}

    def get_public_access_block(self, **kwargs):
        return {
            "PublicAccessBlockConfiguration": {
                k: True
                for k in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            }
        }

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"].read()

    def get_object(self, **kwargs):
        return {
            "Body": io.BytesIO(
                b"corrupt" if self.corrupt else self.objects[kwargs["Key"]]
            )
        }


class FakeWandb:
    def __init__(self, state="COMMITTED", corrupt=False):
        self.state, self.corrupt, self.files = state, corrupt, {}
        self.logs = 0

    def Artifact(self, name, type):
        return SimpleNamespace(
            add_file=lambda path, name: self.files.update(
                {name: Path(path).read_bytes()}
            )
        )

    def log_artifact(self, artifact):
        self.logs += 1
        return SimpleNamespace(
            qualified_name="entity/project/test:v0", wait=lambda: None
        )

    def Api(self, **kwargs):
        def entry(name):
            def download(root):
                path = Path(root) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"corrupt" if self.corrupt else self.files[name])
                return str(path)

            return SimpleNamespace(download=download)

        return SimpleNamespace(
            artifact=lambda name: SimpleNamespace(state=self.state, get_entry=entry)
        )


class BackupFailureTests(unittest.TestCase):
    def test_full_round_trip_and_failures_preserve_original_database(self):
        for s3_bad, state, wandb_bad, message in (
            (True, "COMMITTED", False, "S3 full-GET"),
            (False, "PENDING", False, "not committed"),
            (False, "COMMITTED", True, "checksum mismatch"),
            (False, "COMMITTED", False, None),
        ):
            with (
                self.subTest(s3_bad=s3_bad, state=state, wandb_bad=wandb_bad),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                bundle, folder = root / "bundle", root / "job"
                bundle.mkdir()
                folder.mkdir()
                for name in (
                    "protocol.json",
                    "manifest.json",
                    "jobs.json",
                    "source-manifest.json",
                    "inputs/starting-genotype.json",
                ):
                    path = bundle / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{}")
                graph = folder / "graph.sqlite"
                with sqlite3.connect(graph) as connection:
                    connection.execute("CREATE TABLE preserved (value TEXT)")
                    connection.execute("INSERT INTO preserved VALUES ('original')")
                original = graph.read_bytes()
                tracking = FakeWandb(state, wandb_bad)
                archive = BaselineArchive(
                    s3=FakeS3(s3_bad),
                    wandb=tracking,
                    tracked=tracking,
                    owner="owner",
                    bucket="private",
                    prefix="study/",
                    run_id="job",
                    secrets=[],
                )
                if message:
                    with self.assertRaisesRegex(RuntimeError, message):
                        archive.checkpoint(bundle, folder)
                    self.assertFalse((folder / "checkpoint.json").exists())
                else:
                    result = archive.checkpoint(bundle, folder)
                    self.assertTrue(result["full_get_verified"])
                    self.assertTrue(result["wandb_download_verified"])
                self.assertEqual(
                    hashlib.sha256(graph.read_bytes()).hexdigest(),
                    hashlib.sha256(original).hexdigest(),
                )
                if s3_bad:
                    self.assertEqual(tracking.logs, 0)

    def test_configured_secret_always_rejects_before_upload(self):
        from yggdrisil_ecoli.baseline_storage import screen

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "progress.json"
            path.write_bytes(b'{"value":"test-secret-that-must-never-leave"}')
            with self.assertRaisesRegex(RuntimeError, "Credential detected"):
                screen(path, [b"test-secret-that-must-never-leave"])


if __name__ == "__main__":
    unittest.main()
