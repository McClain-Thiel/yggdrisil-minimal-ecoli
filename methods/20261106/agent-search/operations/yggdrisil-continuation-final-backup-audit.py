import hashlib
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import boto3
import wandb

BASE = Path('/home/ubuntu/yggdrisil-continuation-20261106')
ROOT = BASE / 'validation'
PREFIX = 'yggdrisil/experiments/vecoli-kimi-continuation-20gen-3seed-20261106/'
BUCKET = 'phd-research-storage-1758274488'
read = lambda p: json.loads(p.read_bytes())
sha = lambda b: hashlib.sha256(b).hexdigest()
os.environ.update(read(BASE / 'credentials/common.json'))
s3 = boto3.client('s3', region_name='us-east-1')

def get(ref):
    u = urlparse(ref['uri'])
    with s3.get_object(Bucket=u.netloc, Key=u.path.lstrip('/'))['Body'] as body:
        data = body.read()
    assert sha(data) == ref['sha256'], ref['uri']
    if 'bytes' in ref:
        assert len(data) == ref['bytes'], ref['uri']
    return data

record = {'experiment_id': 'vecoli-kimi-continuation-20gen-3seed-20261106', 'started_at': datetime.now(timezone.utc).isoformat(), 'archive_only_restart': True, 'absolute_budget_deadline_unchanged': True}
backup = read(ROOT / 'final-backup.json')
index_ref = {'uri': backup['index_uri'], 'sha256': backup['index_sha256']}
index_bytes = get(index_ref)
index_ref['bytes'] = len(index_bytes)
index = json.loads(index_bytes)
expected = {x['path']: x for x in index['files']}
assert len(expected) == len(index['files']) == backup['file_count']
assert index['archive'] == {k: backup[k] for k in ('uri','sha256','bytes')}

class HashReader:
    def __init__(self, body):
        self.body = body
        self.digest = hashlib.sha256()
        self.count = 0
    def read(self, size=-1):
        data = self.body.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data

u = urlparse(backup['uri'])
body = s3.get_object(Bucket=u.netloc, Key=u.path.lstrip('/'))['Body']
stream = HashReader(body)
seen = set()
member_bytes = 0
mutable_differences = []
try:
    with tarfile.open(fileobj=stream, mode='r|gz') as archive:
        for member in archive:
            name = member.name
            path = PurePosixPath(name)
            assert member.isfile() and not path.is_absolute() and '..' not in path.parts, name
            assert name in expected and name not in seen, name
            assert path.suffix not in {'.py','.pyc','.sh','.nf','.patch','.diff'}, name
            seen.add(name)
            content = archive.extractfile(member)
            digest = hashlib.sha256()
            n = 0
            for chunk in iter(lambda: content.read(1024**2), b''):
                digest.update(chunk)
                n += len(chunk)
            exp = expected[name]
            assert n == exp['bytes'] == member.size and digest.hexdigest() == exp['sha256'], name
            local = ROOT / name
            with local.open('rb') as source:
                actual = hashlib.file_digest(source, 'sha256').hexdigest()
            if actual != exp['sha256']:
                if name == 'supervisor.log':
                    current = local.read_bytes()
                    assert sha(current[:n]) == exp['sha256'] and len(current) > n, 'Supervisor log changed rather than appended'
                    addition = current[n:].decode('utf-8')
                    assert addition.strip().startswith('Shutdown scheduled for '), addition
                    reason = 'Completion shutdown appended after final archive creation; original prefix SHA256 matches exactly'
                    extra = {'appended_text': addition, 'current_bytes': len(current)}
                elif name == 'batch-progress.json':
                    reason = 'Final backup reference added after archive creation'
                    extra = {'current_json': read(local)}
                else:
                    raise RuntimeError('Worker data differs from final archive: ' + name)
                mutable_differences.append({'path': name, 'archive_sha256': exp['sha256'], 'worker_sha256': actual, 'reason': reason, **extra})
            member_bytes += n
    while stream.read(1024**2):
        pass
finally:
    body.close()
assert seen == set(expected)
assert stream.count == backup['bytes'] and stream.digest.hexdigest() == backup['sha256']
record['final_archive'] = backup
record['final_index'] = index_ref
record['archive_verification'] = {'status': 'verified', 'members': len(seen), 'member_bytes': member_bytes, 'full_get_sha256': stream.digest.hexdigest(), 'worker_comparison': 'All members match; explicitly recorded final-batch metadata update only', 'metadata_differences': mutable_differences}
print(json.dumps({'archive_verified': len(seen), 'bytes': member_bytes}), flush=True)
api = wandb.Api(timeout=120)
record['wandb_artifacts'] = {}
for job in ('calibration','seed-101','seed-211','seed-307'):
    folder = ROOT / 'runs' / job
    entry = read(folder / 'experiments.json')['experiments'][0]
    checkpoint = entry['data_backup']['s3_checkpoint']
    files = json.loads(get(checkpoint))['files']
    artifact = api.artifact(entry['data_backup']['wandb_artifact'])
    assert artifact.state == 'COMMITTED'
    assert set(artifact.manifest.entries) == set(files), job
    from concurrent.futures import ThreadPoolExecutor
    def verify_object(ref):
        u = urlparse(ref['uri'])
        with s3.get_object(Bucket=u.netloc,Key=u.path.lstrip('/'))['Body'] as body:
            digest=hashlib.sha256();n=0
            while chunk:=body.read(1024**2):
                digest.update(chunk);n+=len(chunk)
        assert digest.hexdigest()==ref['sha256'] and n==ref['bytes'], ref['uri']
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(verify_object, files.values()))
    with tempfile.TemporaryDirectory(prefix='vecoli-final-audit-') as temporary:
        downloaded = Path(artifact.download(root=temporary))
        for name, ref in files.items():
            path = downloaded / name
            assert path.is_file() and path.stat().st_size == ref['bytes'], name
            with path.open('rb') as source:
                assert hashlib.file_digest(source, 'sha256').hexdigest() == ref['sha256'], name
            if name == 'progress.json' or name.startswith(('output/','daughters/','cells/','inputs/')):
                local = folder / name
                if local.exists():
                    with local.open('rb') as source:
                        assert hashlib.file_digest(source, 'sha256').hexdigest() == ref['sha256'], name
    record['wandb_artifacts'][job] = {'artifact': entry['data_backup']['wandb_artifact'], 'state': artifact.state, 'checkpoint': checkpoint, 'files_downloaded_and_sha256_verified': len(files), 'all_s3_objects_full_get_verified': True, 'bytes': sum(v['bytes'] for v in files.values()), 'progress_sha256': files['progress.json']['sha256']}
    print(json.dumps({'wandb_verified': job, 'files': len(files)}), flush=True)
record['completed_at'] = datetime.now(timezone.utc).isoformat()
record['status'] = 'verified'
(ROOT / 'final-backup-verification.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps({'status':'verified','final_archive':backup}),flush=True)
