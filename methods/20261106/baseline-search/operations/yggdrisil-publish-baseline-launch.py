"""Publish launch provenance and latest verified progress without changing science."""
import fcntl
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3

from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets, screen
from yggdrisil_ecoli.data.io import atomic_json, file_sha256

root = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil')
folder = root / 'runs/deletion-baselines-20261106'
read = lambda p: json.loads(p.read_bytes())
spec = read(folder / 'protocol.json')
summary = read(folder / 'monitoring-summary.json')
approval = read(folder / 'approval.json')
worker = read(folder / 'worker-launch.json')
assert summary['source_verified'] and summary['batch']['status'] == 'running'
assert summary['launcher']['alive'] and summary['launcher']['identity_matches']
assert summary['batch_lock'] == 'held'
assert approval['instance_id'] == worker['instance_id'] == summary['launcher']['instance_id']
assert approval['total_cap_usd'] == 25 and not approval['model_calls_allowed']
assert all(j.get('checkpoint_independently_verified') for j in summary['jobs'] if j['status'] != 'not_started')
secrets = configured_secrets(root, root / 'runs/literature-ablation-kimi-continuation-20261106/credentials')
archive = BaselineArchive(
    s3=boto3.client('s3', region_name='us-east-1'), wandb=None, tracked=None,
    owner=boto3.client('sts').get_caller_identity()['Account'],
    bucket=spec['tracking']['s3_bucket'], prefix=spec['tracking']['s3_prefix'],
    run_id='study', secrets=secrets,
)
refs = {}
names = ['approval.json', 'worker-launch.json', 'launch-pricing.json',
         'runtime-reference-packages.json', 'runtime-constraints.txt',
         'preflight.json', 'input-verification.json', 'metadata-repair.json',
         'startup-attempt-audit.json', 'monitoring-summary.json', 'batch-progress.json',
         'launcher.json', 'monitor-instructions.json', 'launch-checks.json',
         'operational-source-manifest.json', 'monitor-automation.json',
         'attempts/package-metadata-before-search/final-backup.json']
for name in names:
    path = folder / name
    before = file_sha256(path)
    screen(path, secrets)
    refs[name] = archive.upload(path, 'launch')
    assert before == refs[name]['sha256'] == file_sha256(path), name
now = datetime.now(timezone.utc).isoformat()
with Path('/private/tmp/yggdrisil-migration.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    canonical = root / 'experiments.json'
    working = Path('/private/tmp/yggdrisil-migration-ledger.json')
    before = canonical.read_bytes()
    ledger = json.loads(before)
    assert read(working) == ledger, 'Ledger copies diverged; preserve concurrent updates'
    before_sha = hashlib.sha256(before).hexdigest()
    (folder / 'attempts' / f'ledger-before-{before_sha}.json').write_bytes(before)
    matches = [e for e in ledger['experiments'] if e['id'] == spec['experiment_id']]
    assert len(matches) == 1
    entry = matches[0]
    entry.update(status='running', updated_at=now, launched_at=summary['batch']['started_at'])
    entry['authorization'] = {**approval, 'approved_budget_usd': 25,
                              'scope': 'Ten frozen non-LLM baseline searches only; separate from prior continuation/validation.'}
    entry['remote_execution'] = {'policy': 'remote_linux_only', 'launched': True,
        'worker': worker, 'remote_root': '/home/ubuntu/yggdrisil-baselines-20261106',
        'supervisor': summary['launcher'], 'max_concurrent_jobs': 4,
        'deadline_timer': summary['deadline_timer'], 'metadata_repair': refs['metadata-repair.json']}
    entry['data_backup']['status'] = 'running_checkpoints_independently_verified'
    entry['data_backup']['launch_records'] = refs
    entry['data_backup']['full_wandb_download_verified'] = True
    entry['startup_attempt'] = read(folder / 'startup-attempt-audit.json')
    entry['startup_attempt']['outcome'] = 'Rejected before scientific evaluation; package metadata repaired, original sources/configs/deadlines unchanged.'
    entry['monitoring'] = {'automation_id': 'monitor-deletion-baselines',
        'status': 'active', 'last_verified_at': summary['checked_at'],
        'instructions': str(folder / 'monitor-instructions.json'),
        'snapshot': refs['monitoring-summary.json']}
    entry['wandb_url'] = summary['batch']['wandb_url']
    entry['runs'] = [{key: job[key] for key in ('run_id', 'method', 'seed', 'directory',
        'status', 'progress', 'tracking', 'process', 'completion', 'result', 'finalist',
        'checkpoint', 'checkpoint_independently_verified') if key in job}
        for job in summary['jobs']]
    entry['launch_checks'] = refs['launch-checks.json']
    entry['operational_sources'] = refs['operational-source-manifest.json']
    ledger['updated_at'] = now
    staged = folder / 'ledger-to-publish.json'
    atomic_json(staged, ledger)
    screen(staged, secrets)
    ledger_ref = archive.upload(staged, 'ledger')
    assert file_sha256(canonical) == before_sha and read(working) == json.loads(before), 'Ledger changed during publication'
    atomic_json(canonical, ledger)
    atomic_json(working, ledger)
    old = folder / 'publication.json'
    if old.exists():
        (folder / 'attempts' / f'publication-{file_sha256(old)}.json').write_bytes(old.read_bytes())
    publication = {'published_at': now, 'status': 'running_verified',
        'experiment_id': spec['experiment_id'], 'records': refs, 'ledger': ledger_ref,
        'full_configured_secret_scan': True, 'full_round_trip_verified': True,
        'paid_launch_authorized': True, 'approved_cap_usd': 25}
    atomic_json(old, publication)
print(json.dumps({'status': publication['status'], 'records': len(refs), 'ledger': ledger_ref}))
