#!/usr/bin/env python3
"""Split the working ledger into small, lossless Git records; restore offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

MAX_RECORD_BYTES = 200_000


def encoded(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def export_ledger(source: Path, output: Path, backup: dict[str, Any]) -> dict[str, Any]:
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if backup["sha256"] != digest or backup["bytes"] != len(raw):
        raise ValueError("S3 receipt does not match the current working ledger")
    ledger = json.loads(raw)
    records = []
    pending = {}
    for entry in ledger["experiments"]:
        identifier = entry["id"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", identifier):
            raise ValueError(f"Unsafe experiment ID: {identifier!r}")
        name = f"records/{identifier}.json"
        if name in pending:
            raise ValueError(f"Duplicate experiment ID: {identifier}")
        data = encoded(entry)
        if len(data) > MAX_RECORD_BYTES:
            raise ValueError(f"Ledger record exceeds Git size limit: {identifier}")
        pending[name] = data
        records.append(
            {
                "id": identifier,
                "path": name,
                "status": entry["status"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    index = {
        "export_schema_version": 1,
        "full_ledger": backup,
        "ledger_metadata": {k: v for k, v in ledger.items() if k != "experiments"},
        "experiments": records,
    }
    expected = {Path(name).name for name in pending}
    if (output / "records").exists():
        extra = {p.name for p in (output / "records").glob("*.json")} - expected
        if extra:
            raise ValueError(f"Unmapped records would be left behind: {sorted(extra)}")
    for name, data in pending.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (output / "index.json").write_bytes(encoded(index))
    if restore_ledger(output) != ledger:
        raise RuntimeError("Export does not reconstruct the original ledger")
    return index


def restore_ledger(folder: Path) -> dict[str, Any]:
    index = json.loads((folder / "index.json").read_bytes())
    if index["export_schema_version"] != 1:
        raise ValueError("Unsupported ledger export schema")
    ledger = dict(index["ledger_metadata"])
    entries = []
    seen = set()
    for ref in index["experiments"]:
        path = Path(ref["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe ledger record path")
        raw = (folder / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"] or len(raw) != ref["bytes"]:
            raise ValueError(f"Ledger record checksum mismatch: {path}")
        entry = json.loads(raw)
        if entry["id"] != ref["id"] or entry["id"] in seen:
            raise ValueError("Duplicate or mismatched ledger record ID")
        seen.add(entry["id"])
        entries.append(entry)
    ledger["experiments"] = entries
    return ledger


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--source", type=Path, default=Path("experiments.json"))
    export.add_argument("--output", type=Path, default=Path("experiments"))
    export.add_argument("--receipt", type=Path, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--folder", type=Path, default=Path("experiments"))
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        result = export_ledger(
            args.source, args.output, json.loads(args.receipt.read_bytes())
        )
        print(f"Exported and verified {len(result['experiments'])} ledger records")
    else:
        data = encoded(restore_ledger(args.folder))
        # Never replace a potentially newer working ledger implicitly.
        with args.output.open("xb") as handle:
            handle.write(data)
        print(f"Restored ledger to {args.output}")


if __name__ == "__main__":
    main()
