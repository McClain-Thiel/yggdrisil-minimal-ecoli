"""Preserve final analysis and operational evidence separately from source code."""
import fcntl
import json
import os
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/home/ubuntu/yggdrisil-baselines-20261106')
BUNDLE = ROOT / 'bundle'
APP = BUNDLE / 'source/application'
sys.path[:0] = [str(APP), str(APP / 'src'), str(BUNDLE / 'source/framework/src')]
from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets, screen
from yggdrisil_ecoli.data.io import atomic_json, file_sha256
import boto3
import wandb

read = lambda p: json.loads(p.read_bytes())
out = ROOT / 'final-verification'
lock = (ROOT / 'batch.lock').open('a+')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
audit = read(out / 'audit.json')
analysis = read(out / 'analysis.json')
assert audit['status'] == 'independently_verified' and len(audit['artifacts']) == 11
assert analysis['jobs_completed'] == 10 and analysis['new_evaluated_states_total'] == 5000
os.environ.update(read(ROOT / 'credentials/tracking.json'))
secrets = configured_secrets(APP, ROOT / 'credentials')
spec = read(BUNDLE / 'protocol.json')
names = ['final-backup.json', 'metadata-repair.json', 'startup-attempt-audit.json',
         'launcher.json', 'supervisor.log', 'preflight.json', 'input-verification.json',
         'worker.json', 'batch-progress.json', 'offline-tests-attempt1.log',
         'offline-tests.log', 'supervisor-tests.log', 'supervisor-tests-retry1.log',
         'final-verification-attempt1.log', 'final-publication-attempt1.log',
         'publication-repair.json', 'final-verification/analysis.json',
         'final-verification/audit.json', 'final-verification/nominations.json']
sources = [(ROOT / name, name) for name in names if (ROOT / name).is_file()]
for path in sorted((ROOT / 'attempts/package-metadata-before-search').rglob('*')):
    if path.is_file() and path.suffix in ['.json', '.log']:
        sources.append((path, str(path.relative_to(ROOT))))
for required in ['final-backup.json', 'metadata-repair.json', 'startup-attempt-audit.json',
                 'final-verification/analysis.json', 'final-verification/audit.json',
                 'final-verification/nominations.json']:
    assert any(name == required for _, name in sources)
with wandb.init(entity='mcclain', project='yggdrisil', id='baseline-final-analysis-20261106-retry1',
    name=spec['experiment_id'] + '-verified-analysis', job_type='analysis',
    tags=['yggdrisil', 'baseline', 'analysis'], dir=str(ROOT),
    config={'protocol_sha256': file_sha256(BUNDLE / 'protocol.json'), 'model_calls': 0},
    settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False)) as run:
    archive = BaselineArchive(s3=boto3.client('s3', region_name='us-east-1'),
        wandb=wandb, tracked=run,
        owner=boto3.client('sts', region_name='us-east-1').get_caller_identity()['Account'],
        bucket=spec['tracking']['s3_bucket'], prefix=spec['tracking']['s3_prefix'],
        run_id='study', secrets=secrets)
    with tempfile.TemporaryDirectory(prefix='final-records-', dir=ROOT) as scratch:
        temp = Path(scratch)
        data = temp / 'data'
        data.mkdir()
        entries = []
        for source, name in sources:
            target = data / name
            target.parent.mkdir(parents=True, exist_ok=True)
            before = file_sha256(source)
            target.write_bytes(source.read_bytes())
            assert file_sha256(target) == before == file_sha256(source)
            screen(target, secrets)
            entries.append({'path': name, 'sha256': before, 'bytes': target.stat().st_size})
        tarpath = temp / 'records.tar.gz'
        with tarfile.open(tarpath, 'w:gz') as tar:
            for entry in entries:
                tar.add(data / entry['path'], arcname=entry['path'], recursive=False)
        archive_ref = archive.upload(tarpath, 'completion-records')
        index = temp / 'index.json'
        atomic_json(index, {'experiment_id': spec['experiment_id'], 'archive': archive_ref,
            'files': entries, 'analysis_sha256': file_sha256(out / 'analysis.json'),
            'audit_code_sha256': file_sha256(ROOT / 'yggdrisil-finalize-baseline-audit.py'),
            'publisher_code_sha256': file_sha256(Path(__file__))})
        screen(index, secrets)
        index_ref = archive.upload(index, 'completion-records')
        artifact = wandb.Artifact(spec['experiment_id'] + '-analysis', type='analysis')
        artifact.add_file(str(tarpath), name='records.tar.gz')
        artifact.add_file(str(index), name='index.json')
        for name in ['analysis.json', 'audit.json', 'nominations.json']:
            artifact.add_file(str(out / name), name=name)
        saved = run.log_artifact(artifact)
        saved.wait()
        remote = wandb.Api(timeout=120).artifact(saved.qualified_name)
        assert remote.state == 'COMMITTED'
        for name, expected in [('records.tar.gz', archive_ref['sha256']), ('index.json', index_ref['sha256']),
                               *[(n, file_sha256(out / n)) for n in ['analysis.json', 'audit.json', 'nominations.json']]]:
            path = Path(remote.get_entry(name).download(root=str(temp / 'verified')))
            assert file_sha256(path) == expected
        run.summary.update({'jobs_completed': 10, 'new_evaluated_states': 5000,
                            'unique_nominations': 10, 'baseline_validation_launched': False})
        record = {'published_at': datetime.now(timezone.utc).isoformat(),
            'status': 'verified', 'archive': archive_ref, 'index': index_ref,
            'members': len(entries), 'wandb_artifact': saved.qualified_name,
            'wandb_url': run.url, 'full_s3_get_verified': True,
            'wandb_download_verified': True, 'configured_secrets_screened': True,
            'source_and_credentials_excluded': True,
            'analysis_sha256': file_sha256(out / 'analysis.json'),
            'audit_sha256': file_sha256(out / 'audit.json'),
            'nominations_sha256': file_sha256(out / 'nominations.json')}
        atomic_json(out / 'publication.json', record)
        print(json.dumps(record), flush=True)
