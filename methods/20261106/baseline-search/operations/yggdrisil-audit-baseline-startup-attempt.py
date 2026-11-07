"""Verify the preserved pre-science startup failure independently."""
import hashlib
import json
import os
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import boto3
import wandb

root = Path('/home/ubuntu/yggdrisil-baselines-20261106')
attempt = root / 'attempts/package-metadata-before-search'
ref = json.loads((attempt / 'final-backup.json').read_text())
os.environ.update(json.loads((root / 'credentials/tracking.json').read_text()))
s3 = boto3.client('s3', region_name='us-east-1')
api = wandb.Api(timeout=120)

def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()

with tempfile.TemporaryDirectory(prefix='startup-audit-', dir=root) as tmp:
    directory = Path(tmp)
    for key, name in [('archive', 'data.tar.gz'), ('index', 'index.json')]:
        uri = urlparse(ref[key]['uri'])
        path = directory / name
        s3.download_file(uri.netloc, uri.path.lstrip('/'), str(path))
        assert digest(path) == ref[key]['sha256']
        assert path.stat().st_size == ref[key]['bytes']
    index = json.loads((directory / 'index.json').read_text())
    expected = {item['path']: item for item in index['files']}
    assert len(expected) == len(index['files']) == ref['members']
    assert index['archive'] == ref['archive']
    seen = set()
    with tarfile.open(directory / 'data.tar.gz', 'r:gz') as archive:
        for member in archive:
            assert member.isfile() and member.name in expected and member.name not in seen
            entry = expected[member.name]
            assert member.size == entry['bytes']
            with archive.extractfile(member) as handle:
                assert hashlib.file_digest(handle, 'sha256').hexdigest() == entry['sha256']
            seen.add(member.name)
    assert seen == set(expected)
    assert not any(name.endswith('graph.sqlite') for name in seen)
    artifact = api.artifact(ref['wandb_artifact'])
    assert artifact.state == 'COMMITTED'
    for key, name in [('archive', 'data.tar.gz'), ('index', 'index.json')]:
        path = Path(artifact.get_entry(name).download(root=str(directory / 'wandb')))
        assert digest(path) == ref[key]['sha256'] and path.stat().st_size == ref[key]['bytes']
record = {
    'verified_at': datetime.now(timezone.utc).isoformat(),
    'status': 'independently_verified', 'attempt': 'package-metadata-before-search',
    'members_verified': len(seen), 'scientific_graphs': 0,
    'archive': ref['archive'], 'index': ref['index'],
    'wandb_artifact': ref['wandb_artifact'], 'wandb_state': artifact.state,
    'full_s3_get_verified': True, 'all_member_names_sizes_sha256_verified': True,
    'wandb_download_verified': True,
}
(root / 'startup-attempt-audit.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps({'status': record['status'], 'members': len(seen)}))
