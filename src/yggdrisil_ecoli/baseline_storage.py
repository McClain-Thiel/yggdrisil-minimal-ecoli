"""Verified, data-only checkpoints for remote non-LLM baseline runs."""

from __future__ import annotations

import base64
import hashlib
import json
import netrc
import os
import re
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yggdrisil_ecoli.data.io import atomic_json, file_sha256


def sqlite_snapshot(source: Path, target: Path) -> None:
    """Close both connections: context managers alone leave SQLite handles open."""
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("Baseline graph backup failed quick_check")


def configured_secrets(repo: Path, credential_dir: Path) -> list[bytes]:
    values: set[bytes] = set()

    def collect(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                collect(v, k)
        elif isinstance(value, list):
            for v in value:
                collect(v, key)
        elif isinstance(value, str) and value and any(
            token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        ):
            values.add(value.encode())

    collect(dict(os.environ))
    paths = list((repo / "runs").glob("*/credentials/*.json"))
    paths += list(credential_dir.glob("*.json"))
    for path in sorted(set(paths)):
        collect(json.loads(path.read_text()))
    env = repo / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                collect(value.strip().strip("\"'"), key)
    login = Path.home() / ".netrc"
    if login.exists():
        for _, _, password in netrc.netrc(login).hosts.values():
            if password:
                values.add(password.encode())
    return sorted(values)


def screen(path: Path, secrets: list[bytes]) -> None:
    patterns = re.compile(rb"sk-or-v1-[a-fA-F0-9]{32,}|(?:AKIA|ASIA)[A-Z0-9]{16}|\.platformv01\.[A-Za-z0-9_-]{30,}")
    overlap = max([256, *(len(x) for x in secrets)])
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            data = tail + chunk
            if any(secret in data for secret in secrets) or patterns.search(data):
                raise RuntimeError(f"Credential detected in checkpoint file: {path.name}")
            tail = data[-overlap:]


class BaselineArchive:
    """Require S3 full-GET and W&B download verification before advancing."""

    def __init__(
        self, *, s3: Any, wandb: Any, tracked: Any, owner: str,
        bucket: str, prefix: str, run_id: str, secrets: list[bytes],
    ) -> None:
        self.s3, self.wandb, self.tracked = s3, wandb, tracked
        self.owner, self.bucket = owner, bucket
        self.prefix, self.run_id, self.secrets = prefix, run_id, secrets
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=owner)
        blocks = s3.get_public_access_block(
            Bucket=bucket, ExpectedBucketOwner=owner
        )["PublicAccessBlockConfiguration"]
        if not all(blocks.get(k) is True for k in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"
        )):
            raise RuntimeError("Scientific bucket is not fully public-access blocked")

    def upload(self, path: Path, category: str) -> dict[str, Any]:
        digest, size = file_sha256(path), path.stat().st_size
        key = f"{self.prefix}{self.run_id}/{category}/{digest}{''.join(path.suffixes)}"
        with path.open("rb") as handle:
            self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=handle,
                ExpectedBucketOwner=self.owner, ServerSideEncryption="AES256",
                ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
            )
        h, count = hashlib.sha256(), 0
        with closing(self.s3.get_object(
            Bucket=self.bucket, Key=key, ExpectedBucketOwner=self.owner
        )["Body"]) as body:
            while chunk := body.read(1024 * 1024):
                h.update(chunk)
                count += len(chunk)
        if h.hexdigest() != digest or count != size:
            raise RuntimeError("S3 full-GET verification failed")
        return {"uri": f"s3://{self.bucket}/{key}", "sha256": digest, "bytes": size}

    def checkpoint(self, bundle: Path, folder: Path) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="checkpoint-", dir=folder) as scratch:
            tmp = Path(scratch)
            members = tmp / "data"
            members.mkdir()
            for name in ("protocol.json", "manifest.json", "jobs.json", "source-manifest.json", "inputs/starting-genotype.json"):
                target = members / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((bundle / name).read_bytes())
            for name in ("config.json", "launch.json", "tracking.json", "progress.json", "result.json", "failure.json", "failures.jsonl", "curves.json", "process.json", "environment.json", "checkpoints.jsonl"):
                if (folder / name).exists():
                    (members / name).write_bytes((folder / name).read_bytes())
            if (folder / "graph.sqlite").exists():
                sqlite_snapshot(folder / "graph.sqlite", members / "graph.sqlite")
            files = []
            for path in sorted(members.rglob("*")):
                if path.is_file():
                    screen(path, self.secrets)
                    files.append({"path": str(path.relative_to(members)),
                                  "sha256": file_sha256(path), "bytes": path.stat().st_size})
            archive = tmp / "checkpoint.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for entry in files:
                    tar.add(members / entry["path"], arcname=entry["path"], recursive=False)
            archive_ref = self.upload(archive, "archives")
            index = tmp / "index.json"
            atomic_json(index, {"schema_version": 1, "run_id": self.run_id,
                                "archive": archive_ref, "files": files})
            screen(index, self.secrets)
            index_ref = self.upload(index, "indexes")
            artifact = self.wandb.Artifact(self.run_id + "-data", type="experiment-data")
            artifact.add_file(str(archive), name="checkpoint.tar.gz")
            artifact.add_file(str(index), name="index.json")
            saved = self.tracked.log_artifact(artifact)
            saved.wait()
            remote = self.wandb.Api(timeout=120).artifact(saved.qualified_name)
            if remote.state != "COMMITTED":
                raise RuntimeError("W&B checkpoint is not committed")
            for name, expected in (("checkpoint.tar.gz", archive_ref), ("index.json", index_ref)):
                downloaded = Path(remote.get_entry(name).download(root=str(tmp / "verify")))
                if file_sha256(downloaded) != expected["sha256"]:
                    raise RuntimeError("W&B downloaded checkpoint checksum mismatch")
            result = {"verified_at": datetime.now(timezone.utc).isoformat(),
                      "archive": archive_ref, "index": index_ref,
                      "wandb_artifact": saved.qualified_name,
                      "member_count": len(files), "status": "verified",
                      "full_get_verified": True, "wandb_download_verified": True}
            result["files"] = files
            atomic_json(folder / "checkpoint.json", result)
            with (folder / "checkpoints.jsonl").open("a") as journal:
                journal.write(json.dumps(result) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
            return result
