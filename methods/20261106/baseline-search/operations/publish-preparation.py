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
spec = json.loads((folder / 'protocol.json').read_text())
secrets = configured_secrets(root, root / 'runs/literature-ablation-kimi-continuation-20261106/credentials')
archive = BaselineArchive(
    s3=boto3.client('s3', region_name='us-east-1'), wandb=None, tracked=None,
    owner=boto3.client('sts').get_caller_identity()['Account'],
    bucket=spec['tracking']['s3_bucket'], prefix=spec['tracking']['s3_prefix'],
    run_id='study', secrets=secrets,
)
refs = {}
for name in ('protocol.json', 'manifest.json', 'source-manifest.json', 'jobs.json',
             'inputs/starting-genotype.json', 'checks.json', 'catalog.json',
             'deployment.json', 'pricing.json'):
    path = folder / name
    screen(path, secrets)
    refs[name] = archive.upload(path, 'preparation')
now = datetime.now(timezone.utc).isoformat()
entry = {
    'id': spec['experiment_id'], 'status': 'prepared_not_launched',
    'prepared_at': now, 'experiment_type': 'non_llm_deletion_baselines',
    'methods': spec['methods'], 'seeds': spec['seeds'], 'jobs': 10,
    'new_evaluated_states_per_job': spec['limits']['new_evaluated_states'],
    'starting_genotype': spec['starting_genotype'], 'growth_gates': spec['growth_gates'],
    'frozen_inputs': spec['inputs'], 'input_restore_index': spec['input_restore_index'],
    'evaluator_ids': spec['evaluator_ids'], 'analysis': spec['analysis'],
    'authorization': spec['authorization'], 'cost_proposal': spec['cost_proposal'],
    'remote_execution': {'policy': 'remote_linux_only', 'worker': None, 'launched': False},
    'local_metadata': str(folder), 'local_source_snapshot': str(folder / 'source'),
    'data_backup': {'status': 'verified_preparation_records', 'records': refs,
                    'full_s3_get_verified': True, 'credentials_and_source_excluded': True},
    'tests': {'passed': 41, 'deselected': 1, 'runtime': 'frozen source, synthetic evaluators'},
    'held_out_validation': spec['held_out_validation'],
}
with Path('/private/tmp/yggdrisil-migration.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    canonical = root / 'experiments.json'
    working = Path('/private/tmp/yggdrisil-migration-ledger.json')
    ledger = json.loads(canonical.read_text())
    if json.loads(working.read_text()) != ledger:
        raise RuntimeError('Ledger copies diverged; merge without losing concurrent monitor updates')
    before = canonical.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()
    (folder / 'attempts' / f'ledger-before-{before_sha}.json').write_bytes(before)
    existing = [e for e in ledger['experiments'] if e['id'] == spec['experiment_id']]
    if existing:
        raise RuntimeError('Baseline ledger entry already exists; review publication before retrying')
    ledger['experiments'].append(entry)
    ledger['updated_at'] = now
    staged = folder / 'ledger-to-publish.json'
    atomic_json(staged, ledger)
    screen(staged, secrets)
    ledger_ref = archive.upload(staged, 'ledger')
    if file_sha256(canonical) != before_sha or json.loads(working.read_text()) != json.loads(before):
        raise RuntimeError('Ledger changed during publication; preserve verified attempt and merge')
    atomic_json(canonical, ledger)
    atomic_json(working, ledger)
    publication = {'published_at': now, 'status': 'verified_preparation_only',
                   'experiment_id': spec['experiment_id'], 'records': refs,
                   'ledger': ledger_ref, 'full_configured_secret_scan': True,
                   'full_round_trip_verified': True, 'paid_launch_authorized': False}
    atomic_json(folder / 'publication.json', publication)
print(json.dumps({'status': publication['status'], 'records': len(refs), 'ledger': ledger_ref}))
