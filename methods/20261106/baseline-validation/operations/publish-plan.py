"""Publish data-only preparation and preserve both experiment ledger copies."""
import fcntl
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3

from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets, screen
from yggdrisil_ecoli.data.io import atomic_json, file_sha256

root = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil')
folder = root / 'runs/vecoli-deletion-baselines-20gen-3seed-20261106'
spec = json.loads((folder / 'protocol.json').read_bytes())
assert spec['authorization']['paid_launch'] is False
secrets = configured_secrets(root, root / 'runs/literature-ablation-kimi-continuation-20261106/credentials')
archive = BaselineArchive(s3=boto3.client('s3', region_name='us-east-1'),
    wandb=None, tracked=None, owner=boto3.client('sts').get_caller_identity()['Account'],
    bucket=spec['tracking']['s3_bucket'], prefix=spec['tracking']['s3_prefix'],
    run_id='study', secrets=secrets)
refs = {}
for name in ['protocol.json', 'genotypes.json', 'restore-plan.json', 'source-manifest.json',
             'native-extension-build-reference.json', 'pricing-and-input-verification.json',
             'manifest.json', 'checks.json']:
    path = folder / name
    digest = file_sha256(path)
    screen(path, secrets)
    refs[name] = archive.upload(path, 'preparation')
    assert digest == refs[name]['sha256'] == file_sha256(path)
with Path('/private/tmp/yggdrisil-migration.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    canonical = root / 'experiments.json'
    working = Path('/private/tmp/yggdrisil-migration-ledger.json')
    before = canonical.read_bytes()
    ledger = json.loads(before)
    assert json.loads(working.read_bytes()) == ledger, 'Ledger copies diverged'
    existing = [e for e in ledger['experiments'] if e['id'] == spec['experiment_id']]
    if existing and (len(existing) != 1
                     or existing[0]['status'] != 'prepared_pending_cost_approval'
                     or existing[0]['authorization']['paid_launch'] is not False
                     or existing[0]['protocol']['sha256'] != refs['protocol.json']['sha256']):
        raise RuntimeError('Existing validation differs or has advanced beyond preparation')
    (folder / 'attempts').mkdir(exist_ok=True)
    before_sha = hashlib.sha256(before).hexdigest()
    (folder / 'attempts' / f'ledger-before-{before_sha}.json').write_bytes(before)
    now = datetime.now(timezone.utc).isoformat()
    entry = {'id': spec['experiment_id'], 'status': 'prepared_pending_cost_approval',
             'created_at': now, 'parent_experiment_ids': spec['parent_experiment_ids'],
             'local_metadata': str(folder), 'protocol': refs['protocol.json'],
             'frozen_genotypes': refs['genotypes.json'], 'candidate_count': 10,
             'control_count': 2, 'seeds': spec['seeds'], 'target_generations': 20,
             'maximum_cell_simulations': 720, 'calibration_cell_simulations': 2,
             'execution_backend': 'remote_linux_only', 'instance_id': None,
             'remote_root': spec['execution']['remote_root'],
             'authorization': spec['authorization'], 'cost_proposal': spec['cost_proposal'],
             'solver_policy': spec['solver_policy'], 'solver_patch_sha256': spec['solver_patch_sha256'],
             'analysis_plan': spec['analysis'],
             'data_backup': {'status': 'verified_preparation_only', 'records': refs,
                             'full_s3_get_verified': True, 'credentials_and_source_excluded': True},
             'local_source_snapshot': str(folder / 'source'),
             'checks': json.loads((folder / 'checks.json').read_bytes())}
    if existing:
        entry['created_at'] = existing[0]['created_at']
        ledger['experiments'][ledger['experiments'].index(existing[0])] = entry
    else:
        ledger['experiments'].append(entry)
    baseline = next(e for e in ledger['experiments'] if e['id'] == 'deletion-baselines-20261106')
    assert baseline['status'] == 'completed_verified'
    baseline['held_out_validation'].update(status='prepared_pending_cost_approval',
                                          experiment_id=spec['experiment_id'])
    ledger['updated_at'] = now
    staged = folder / 'ledger-to-publish.json'
    atomic_json(staged, ledger)
    screen(staged, secrets)
    ledger_ref = archive.upload(staged, 'ledger')
    assert canonical.read_bytes() == before and json.loads(working.read_bytes()) == json.loads(before), 'Concurrent ledger change'
    atomic_json(canonical, ledger)
    atomic_json(working, ledger)
    publication = {'experiment_id': spec['experiment_id'], 'published_at': now,
                   'status': 'verified_preparation_only', 'paid_launch_authorized': False,
                   'records': refs, 'ledger': ledger_ref, 'full_configured_secret_scan': True,
                   'full_round_trip_verified': True}
    previous = folder / 'publication.json'
    if previous.exists():
        (folder / 'attempts' / f'publication-{file_sha256(previous)}.json').write_bytes(previous.read_bytes())
    atomic_json(previous, publication)
print(json.dumps({'status': publication['status'], 'records': len(refs), 'ledger': ledger_ref}))
