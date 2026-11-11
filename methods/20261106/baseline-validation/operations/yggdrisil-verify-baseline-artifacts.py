import concurrent.futures
import hashlib
import json
import netrc
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

OUT = Path('/private/tmp/yggdrisil-baseline-final-audit')
os.environ['WANDB_BASE_URL'] = 'https://api.forge.coreweave.com'
os.environ['WANDB_CACHE_DIR'] = str(OUT / 'wandb-cache')
os.environ['WANDB_API_KEY'] = netrc.netrc().authenticators('api.wandb.ai')[2]
import boto3
import wandb

read = lambda p: json.loads(p.read_bytes())
now = lambda: datetime.now(timezone.utc).isoformat()
index = read(OUT / 'final-index.json')
batch = read(OUT / 'metadata/batch-progress.json')
expected = {f['path']: f for f in index['files']}
s3 = boto3.client('s3', region_name='us-east-1')
owner = boto3.client('sts').get_caller_identity()['Account']
api = wandb.Api(timeout=120)
record = {'started_at': now(), 'experiment_id': batch['experiment_id'], 'jobs': {}}
log = OUT / 'artifact-verification.jsonl'

def fetch(ref):
    u = urlparse(ref['uri'])
    h, n = hashlib.sha256(), 0
    with s3.get_object(Bucket=u.netloc, Key=u.path.lstrip('/'), ExpectedBucketOwner=owner)['Body'] as body:
        while chunk := body.read(1024**2):
            h.update(chunk)
            n += len(chunk)
    assert h.hexdigest() == ref['sha256'] and n == ref['bytes'], ref['uri']

def verify_download(entry, ref, temporary):
    with tempfile.TemporaryDirectory(prefix='file-', dir=temporary) as folder:
        path = Path(entry.download(root=folder, skip_cache=True))
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        assert digest == ref['sha256'] and path.stat().st_size == ref['bytes'], entry.path
        return {'sha256': digest, 'bytes': ref['bytes'], 'wandb_digest': entry.digest}

with tempfile.TemporaryDirectory(prefix='wb-verify-', dir=OUT) as temporary:
    final = api.artifact('mcclain/yggdrisil/' + batch['experiment_id'] + '-final-data:latest')
    assert final.state == 'COMMITTED'
    assert set(final.manifest.entries) == {'data.tar.gz', 'index.json'}
    index_ref = {'sha256': hashlib.sha256((OUT/'final-index.json').read_bytes()).hexdigest(), 'bytes': (OUT/'final-index.json').stat().st_size}
    verify_download(final.get_entry('index.json'), index_ref, temporary)
    verify_download(final.get_entry('data.tar.gz'), index['archive'], temporary)
    record['final_artifact'] = {'name': final.qualified_name, 'state': final.state, 'digest': final.digest, 'archive': index['archive'], 'index': index_ref, 'full_archive_and_index_download_sha256_verified': True}
    print(json.dumps({'final_wandb_artifact_verified': final.qualified_name}), flush=True)
    for label in ['calibration', 'seed-101', 'seed-211', 'seed-307']:
        backup = batch['jobs'][label]['data_backup']
        path = OUT / (label+'-index.json')
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == backup['s3_checkpoint']['sha256']
        files = json.loads(data)['files']
        artifact = api.artifact(backup['wandb_artifact'])
        assert artifact.state == 'COMMITTED'
        assert set(artifact.manifest.entries) == set(files), label
        metadata_differences = []
        progress = read(OUT / 'metadata/runs' / label / 'progress.json')
        output_name = Path(progress['experiment_dir']).name
        compared = 0
        for name, ref in files.items():
            mapped = 'runs/'+label+'/'+name
            if name.startswith('output/'):
                mapped = 'runs/'+label+'/output/'+output_name+'/'+name.removeprefix('output/')
            if mapped in expected:
                if ref['sha256'] != expected[mapped]['sha256']:
                    assert name in {'experiments.json', 'events.jsonl'}, (label,name)
                    metadata_differences.append({'path': mapped, 'checkpoint_sha256': ref['sha256'], 'final_archive_sha256': expected[mapped]['sha256'], 'reason': 'Controller terminal checkpoint metadata is recorded before its own backup receipt is inserted.'})
                else:
                    compared += 1
        def verify(item):
            name, ref = item
            fetch(ref)
            verified = verify_download(artifact.get_entry(name), ref, temporary)
            return {'job': label, 'path': name, 'uri': ref['uri'], **verified}
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool, log.open('a') as journal:
            for item in pool.map(verify, files.items()):
                journal.write(json.dumps(item)+'\n')
                journal.flush()
                completed += 1
                if completed % 200 == 0:
                    print(json.dumps({'job': label, 'objects_verified': completed, 'of': len(files)}), flush=True)
        record['jobs'][label] = {'artifact': artifact.qualified_name, 'state': artifact.state, 'digest': artifact.digest, 'checkpoint': backup['s3_checkpoint'], 'objects': len(files), 'bytes': sum(f['bytes'] for f in files.values()), 'all_s3_full_get_and_wandb_downloads_sha256_verified': True, 'members_matching_final_archive': compared, 'metadata_differences': metadata_differences, 'progress_sha256': files['progress.json']['sha256']}
        (OUT/'artifact-verification-progress.json').write_text(json.dumps(record,indent=2)+'\n')
        print(json.dumps({'job_verified': label, 'objects': len(files)}), flush=True)
record.update(status='verified', completed_at=now())
(OUT/'artifact-verification.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps({'status': 'verified', 'jobs': list(record['jobs'])}), flush=True)
