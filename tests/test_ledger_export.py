import hashlib
import json

import pytest

from scripts.export_experiment_ledger import export_ledger, restore_ledger


def fixture_ledger(tmp_path, entries=None):
    value = {
        "schema_version": 1,
        "storage_migration": {"status": "completed_verified"},
        "experiments": entries
        if entries is not None
        else [
            {
                "id": "agent-1",
                "status": "completed_with_exceptions",
                "failed_attempts": [{"sha256": "preserved"}],
            },
            {"id": "baseline-1", "status": "completed_verified"},
        ],
    }
    source = tmp_path / "working.json"
    raw = json.dumps(value).encode()
    source.write_bytes(raw)
    receipt = {
        "uri": "s3://test/ledger.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    return source, receipt, value


def test_round_trip_preserves_all_entries_and_failures(tmp_path):
    source, receipt, value = fixture_ledger(tmp_path)
    export_ledger(source, tmp_path / "export", receipt)
    assert restore_ledger(tmp_path / "export") == value


def test_mismatched_backup_rejected_before_writing(tmp_path):
    source, receipt, _ = fixture_ledger(tmp_path)
    receipt["sha256"] = "incorrect"
    with pytest.raises(ValueError, match="receipt"):
        export_ledger(source, tmp_path / "export", receipt)
    assert not (tmp_path / "export").exists()


def test_tampered_result_fails_restore(tmp_path):
    source, receipt, _ = fixture_ledger(tmp_path)
    output = tmp_path / "export"
    export_ledger(source, output, receipt)
    (output / "records/agent-1.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        restore_ledger(output)


@pytest.mark.parametrize("ids", [["../escape"], ["duplicate", "duplicate"]])
def test_bad_identifiers_rejected_before_writing(tmp_path, ids):
    source, receipt, _ = fixture_ledger(
        tmp_path, [{"id": identifier, "status": "complete"} for identifier in ids]
    )
    with pytest.raises(ValueError):
        export_ledger(source, tmp_path / "export", receipt)
    assert not (tmp_path / "export").exists()
