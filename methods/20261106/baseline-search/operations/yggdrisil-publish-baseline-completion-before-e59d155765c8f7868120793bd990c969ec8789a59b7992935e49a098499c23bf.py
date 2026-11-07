"""Publish verified final results and cleanup in both canonical ledger copies."""
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
out = folder / 'final'
read = lambda p: json.loads(p.read_bytes())
spec = read(folder / 'protocol.json')
analysis = read(out / 'analysis.json')
audit = read(out / 'audit.json')
cleanup = read(out / 'cleanup.json')
records = read(out / 'completion-record-publication.json')
assert cleanup['status'] == 'terminated_volume_deleted' and not cleanup['shared_instance_modified']
assert audit['status'] == 'independently_verified' and analysis['jobs_completed'] == 10
assert records['status'] == 'verified' and records['source_and_credentials_excluded']
for name in ['analysis', 'audit', 'nominations']:
    assert file_sha256(out / (name + '.json')) == records[name + '_sha256']
secrets = configured_secrets(root, root / 'runs/literature-ablation-kimi-continuation-20261106/credentials')
archive = BaselineArchive(s3=boto3.client('s3', region_name='us-east-1'),
    wandb=None, tracked=None, owner=boto3.client('sts').get_caller_identity()['Account'],
    bucket=spec['tracking']['s3_bucket'], prefix=spec['tracking']['s3_prefix'],
    run_id='study', secrets=secrets)
names = ['analysis.json', 'audit.json', 'nominations.json', 'cleanup.json',
         'completion-record-publication.json', 'final-backup.json', 'batch-progress.json',
         's3-member-audit.json', 'index.json', 'catalog.json', 'operational-source-manifest.json']
if (out / 'monitor-deletion.json').exists():
    names.append('monitor-deletion.json')
refs = {}
for name in names:
    path = out / name
    digest = file_sha256(path)
    screen(path, secrets)
    refs[name] = archive.upload(path, 'completion')
    assert digest == refs[name]['sha256'] == file_sha256(path)
with Path('/private/tmp/yggdrisil-migration.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    canonical = root / 'experiments.json'
    working = Path('/private/tmp/yggdrisil-migration-ledger.json')
    before = canonical.read_bytes()
    ledger = json.loads(before)
    assert read(working) == ledger, 'Ledger copies diverged; merge concurrent records before publication'
    digest = hashlib.sha256(before).hexdigest()
    (folder / 'attempts' / f'ledger-before-{digest}.json').write_bytes(before)
    matches = [e for e in ledger['experiments'] if e['id'] == spec['experiment_id']]
    assert len(matches) == 1
    entry = matches[0]
    now = datetime.now(timezone.utc).isoformat()
    entry.update(status='completed_verified', updated_at=now,
                 science_completed_at=read(out / 'batch-progress.json')['updated_at'])
    entry['summary'] = {k: analysis[k] for k in ['jobs_completed', 'new_evaluated_states_total',
        'model_calls', 'search_failures', 'unique_nominated_genotypes', 'method_summaries', 'limitations']}
    entry['results'] = refs['analysis.json']
    entry['runs'] = [{k: r[k] for k in r if k != 'checkpoint'} for r in analysis['rows']]
    entry['final_checkpoints'] = {r['run_id']: {k: r['checkpoint'][k] for k in
        ['archive', 'index', 'wandb_artifact', 'status', 'verified_at', 'member_count']}
        for r in analysis['rows']}
    entry['remote_execution']['worker'].update(status='terminated', public_ip=None,
        last_verified_at=cleanup['verified_at'], volume_status='deleted')
    entry['remote_execution']['supervisor'].update(alive=False, identity_matches=False)
    entry['cleanup'] = cleanup
    entry['data_backup'].update(status='completed_independently_verified',
        final_archive=audit['final_backup'], member_count=audit['member_count'],
        member_bytes=audit['member_bytes'], completion_records=records, final_records=refs,
        full_s3_get_verified=True, full_wandb_download_verified=True,
        all_final_member_names_sizes_sha256_verified=True, worker_graphs_compared=True)
    entry['nominations'] = {'count': 10, 'selection_frozen': True, 'record': refs['nominations.json'],
                            'validation_status': 'not_launched_not_authorized'}
    entry['held_out_validation']['status'] = 'nominated_not_authorized'
    entry['monitoring'].update(status='deleted' if 'monitor-deletion.json' in refs else 'completed_pending_monitor_removal',
        last_verified_at=audit['verified_at'], final_audit=refs['audit.json'], snapshot=refs['audit.json'])
    if 'monitor-deletion.json' in refs:
        entry['monitoring']['deletion_receipt'] = refs['monitor-deletion.json']
    entry['operational_sources'] = refs['operational-source-manifest.json']
    entry['cost'] = {'approved_cap_usd': 25, 'model_cost_usd': 0,
        'compute_upper_bound_usd': cleanup['compute_cost_upper_bound_usd'],
        'basis': cleanup['cost_method'], 'storage_requests_transfers': 'Additional; final AWS bill not yet settled.'}
    ledger['updated_at'] = now
    staged = folder / 'ledger-to-publish.json'
    atomic_json(staged, ledger)
    screen(staged, secrets)
    ledger_ref = archive.upload(staged, 'ledger')
    assert file_sha256(canonical) == digest and read(working) == json.loads(before), 'Concurrent ledger update during publication'
    atomic_json(canonical, ledger)
    atomic_json(working, ledger)
    previous = folder / 'publication.json'
    (folder / 'attempts' / f'publication-{file_sha256(previous)}.json').write_bytes(previous.read_bytes())
    publication = {'status': 'completed_verified', 'published_at': now,
        'experiment_id': spec['experiment_id'], 'records': refs, 'ledger': ledger_ref,
        'full_configured_secret_scan': True, 'full_round_trip_verified': True,
        'monitor_status': entry['monitoring']['status']}
    atomic_json(previous, publication)
    atomic_json(out / 'ledger-publication.json', publication)
print(json.dumps({'status': publication['status'], 'monitor': publication['monitor_status'], 'ledger': ledger_ref}))
