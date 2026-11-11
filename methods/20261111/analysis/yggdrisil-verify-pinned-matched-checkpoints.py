"""Audit explicit immutable checkpoint references; never launch scientific work."""
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import boto3

root = Path('/home/ubuntu/yggdrisil-depth-matched-20261111')
lock = (root / 'independent-checkpoint-verification.lock').open('a+')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
plan = json.loads(Path(sys.argv[1]).read_bytes())
if plan['experiment_id'] != 'vecoli-depth-matched-20261111':
    raise ValueError('Unexpected experiment in audit plan')
os.environ.update(json.loads((root / 'credentials/tracking.json').read_bytes()))
os.environ.pop('WANDB_SERVICE', None)
import wandb

s3 = boto3.client('s3', region_name='us-east-1')
owner = boto3.client('sts').get_caller_identity()['Account']
api = wandb.Api(timeout=120)


def download(ref, target):
    uri = urlparse(ref['uri'])
    if uri.scheme != 's3' or uri.netloc != 'phd-research-storage-1758274488' or not uri.path.startswith('/yggdrisil/experiments/vecoli-depth-matched-20261111/'):
        raise ValueError('Checkpoint outside approved experiment')
    body = s3.get_object(Bucket=uri.netloc, Key=uri.path.lstrip('/'), ExpectedBucketOwner=owner)['Body']
    checksum = hashlib.sha256()
    size = 0
    try:
        with target.open('wb') as stream:
            for block in body.iter_chunks(chunk_size=1024**2):
                checksum.update(block)
                size += len(block)
                stream.write(block)
    finally:
        body.close()
    if checksum.hexdigest() != ref['sha256'] or size != ref['bytes']:
        raise RuntimeError('S3 content mismatch')


def safe_name(name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe artifact member')


result = {'checked_at_unix': time.time(), 'plan_sha256': hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest(),
          'verification_host_role': 'primary', 'roles': {}}
for role, workflows in plan['roles'].items():
    report = {'checked_at_unix': result['checked_at_unix'], 'workflows': {}, 'status': 'verified_available_checkpoints'}
    result['roles'][role] = report
    for name, entry in workflows.items():
        backup = entry['data_backup']
        ref = backup['s3_checkpoint']
        artifact_name = backup['wandb_artifact']
        if not artifact_name.startswith('mcclain/yggdrisil/vecoli-depth-matched-20261111-'):
            raise ValueError('Unexpected W&B artifact')
        artifact = api.artifact(artifact_name)
        if artifact.state != 'COMMITTED':
            raise RuntimeError('W&B artifact not committed')
        for member_name in artifact.manifest.entries:
            safe_name(member_name)
        with tempfile.TemporaryDirectory(dir=root, prefix='checkpoint-audit-') as temp:
            directory = Path(temp)
            download(ref, directory / 'index.json')
            index = json.loads((directory / 'index.json').read_bytes())
            for member_name in index['files']:
                safe_name(member_name)
                if member_name not in artifact.manifest.entries:
                    raise RuntimeError('Indexed member absent from W&B: ' + member_name)
            # The SDK downloads concurrently into a new empty directory, bypassing its cache.
            downloaded = Path(artifact.download(root=str(directory / 'wandb'), skip_cache=True, allow_missing_references=False))
            if downloaded.resolve() != (directory / 'wandb').resolve():
                raise RuntimeError('Unexpected artifact download directory')
            total = 0
            for member_name, member in index['files'].items():
                download(member, directory / 'object')
                retrieved = downloaded / member_name
                if retrieved.is_symlink() or not retrieved.is_file():
                    raise RuntimeError('Unsafe or missing downloaded member: ' + member_name)
                checksum = hashlib.sha256()
                with retrieved.open('rb') as stream:
                    while block := stream.read(1024**2):
                        checksum.update(block)
                if checksum.hexdigest() != member['sha256'] or retrieved.stat().st_size != member['bytes']:
                    raise RuntimeError('W&B content mismatch: ' + member_name)
                total += member['bytes']
            report['workflows'][name] = {
                'catalog_status': entry['status'], 'verification_status': 'verified', 'checkpoint': ref,
                'wandb_artifact': artifact_name, 'wandb_state': 'COMMITTED', 'files': len(index['files']),
                'bytes': total, 'full_s3_get_verified': True, 'all_indexed_files_verified': True,
                'wandb_uncached_download_verified': True, 'verified_at_unix': time.time(),
                'snapshot_is_immutable': True, 'verification_host_role': 'primary',
            }
        destination = root / 'pinned-checkpoint-audit-latest.json'
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, indent=2) + '\n')
        temporary.replace(destination)
        print('Verified checkpoint: ' + name, file=sys.stderr, flush=True)
print(json.dumps(result), flush=True)
