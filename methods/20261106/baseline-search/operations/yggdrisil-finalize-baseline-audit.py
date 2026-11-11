"""Verify closed baseline data and recompute summaries without scientific reruns."""
import fcntl
import hashlib
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path('/home/ubuntu/yggdrisil-baselines-20261106')
BUNDLE = ROOT / 'bundle'
APP = BUNDLE / 'source/application'
sys.path[:0] = [str(APP), str(APP / 'src'), str(BUNDLE / 'source/framework/src')]
from scripts.collect_deletion_baselines import collect
from scripts.run_deletion_baseline import curves
from yggdrisil import SQLiteStateGraph
from yggdrisil_ecoli.analysis import summarize_run
from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets, screen
from yggdrisil_ecoli.data.io import atomic_json, file_sha256

import boto3
import wandb

read = lambda p: json.loads(p.read_bytes())
now = lambda: datetime.now(timezone.utc).isoformat()
out = ROOT / 'final-verification'
out.mkdir(exist_ok=True)
lock = (ROOT / 'batch.lock').open('a+')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
state = read(ROOT / 'batch-progress.json')
assert state['status'] == 'search_completed' and len(state['jobs']) == 10
assert all(j['status'] == 'completed_verified' and j['exit_code'] == 0 for j in state['jobs'].values())
processes = subprocess.check_output(['ps', '-eo', 'args'], text=True).splitlines()
assert not any('-m scripts.run_deletion_baseline' in p or 'python -u /home/ubuntu/yggdrisil-baselines-20261106/yggdrisil-baseline-supervisor' in p for p in processes)
os.environ.update(read(ROOT / 'credentials/tracking.json'))
s3 = boto3.client('s3', region_name='us-east-1')
owner = boto3.client('sts', region_name='us-east-1').get_caller_identity()['Account']
api = wandb.Api(timeout=120)
spec = read(BUNDLE / 'protocol.json')
approval = read(BUNDLE / 'approval.json')
assert approval['total_cap_usd'] == 25 and not approval['model_calls_allowed']
sources = read(BUNDLE / 'source-manifest.json')['files']
for name, digest in sources.items():
    assert file_sha256(BUNDLE / 'source' / name) == digest, name
for item in read(ROOT / 'metadata-repair.json')['metadata_headers']:
    assert file_sha256(ROOT / item['path']) == item['sha256']
for name, digest in read(ROOT / 'preflight.json')['operational_sources'].items():
    assert file_sha256(ROOT / name) == digest, name

def download(ref, path):
    uri = urlparse(ref['uri'])
    s3.download_file(uri.netloc, uri.path.lstrip('/'), str(path), ExtraArgs={'ExpectedBucketOwner': owner})
    assert file_sha256(path) == ref['sha256'] and path.stat().st_size == ref['bytes']

def logical_database(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        h = hashlib.sha256(json.dumps(schema, separators=(',', ':')).encode())
        counts = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = len(conn.execute('PRAGMA table_info(' + quoted + ')').fetchall())
            count = 0
            for row in conn.execute('SELECT * FROM ' + quoted + ' ORDER BY ' + ','.join(str(i + 1) for i in range(columns))):
                h.update(json.dumps(row, separators=(',', ':'), ensure_ascii=True).encode() + b'\n')
                count += 1
            counts[name] = count
        return {'sha256': h.hexdigest(), 'table_rows': counts}

final_ref = read(ROOT / 'final-backup.json')
artifact_checks = []
graph_checks = []
rows = []
with tempfile.TemporaryDirectory(prefix='final-audit-', dir=ROOT) as scratch:
    temp = Path(scratch)
    download(final_ref['archive'], temp / 'final.tar.gz')
    download(final_ref['index'], temp / 'index.json')
    index = read(temp / 'index.json')
    expected = {e['path']: e for e in index['files']}
    assert len(expected) == len(index['files']) == final_ref['members']
    assert index['archive'] == final_ref['archive']
    seen = set()
    data = temp / 'data'
    data.mkdir()
    with tarfile.open(temp / 'final.tar.gz', 'r:gz') as tar:
        for member in tar:
            assert member.isfile() and member.name in expected and member.name not in seen
            target = (data / member.name).resolve()
            assert target.is_relative_to(data.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open('wb') as dest:
                while chunk := source.read(1024 * 1024):
                    dest.write(chunk)
            assert file_sha256(target) == expected[member.name]['sha256']
            assert target.stat().st_size == expected[member.name]['bytes']
            seen.add(member.name)
    assert seen == set(expected)
    for name in sorted(seen):
        original = ROOT / name if name in ['batch-progress.json', 'preflight.json', 'input-verification.json', 'worker.json'] else BUNDLE / name
        if name.endswith('/graph.sqlite'):
            archived = logical_database(data / name)
            actual = logical_database(original)
            assert archived == actual, name
            assert actual['table_rows']['states'] == 501 and actual['table_rows']['evaluations'] == 2505
            graph_checks.append({'path': name, **actual})
        elif name == 'batch-progress.json':
            current = read(original)
            archived = read(data / name)
            current.pop('final_backup', None)
            archived.pop('final_backup', None)
            current.pop('updated_at', None)
            archived.pop('updated_at', None)
            assert current == archived
        else:
            assert file_sha256(original) == expected[name]['sha256'], name
    assert len(graph_checks) == 10
    catalog = collect(BUNDLE)
    assert catalog == read(BUNDLE / 'catalog.json') == read(data / 'catalog.json')
    assert catalog['finalist_selection_frozen'] and len(catalog['nominations']) == 10
    mapping_ref = spec['analysis']['functional_mapping']
    download(mapping_ref, temp / 'mapping.json')
    mapping = read(temp / 'mapping.json')
    functional = mapping['current_functional_rows']
    shared = set(mapping['shared_functional_ecocyc_ids'])
    assert len(functional) == 1915 and len(shared) == 1197
    assert len({f['ecocyc_id'] for f in functional}) == 1915 and all(len(f['b_numbers']) == 1 for f in functional)
    all_refs = [('study-final', final_ref, 'data.tar.gz')]
    for job in catalog['jobs']:
        directory = BUNDLE / job['directory']
        graph_path = data / job['directory'] / 'graph.sqlite'
        report = read(directory / 'result.json')
        summary = summarize_run(graph_path)
        assert summary['deepest_viable_candidate'] == report['finalist']
        with SQLiteStateGraph(graph_path) as graph:
            recomputed = curves(graph, spec['evaluator_ids'], 1030)
        assert recomputed == read(directory / 'curves.json')
        assert len(recomputed) == 501 and sum(x['evaluation_complete'] for x in recomputed) == 501
        assert report['new_evaluated_states'] == 500 and report['stop_reason'] == 'max_states'
        assert report['model_calls'] == 0 and report['provider_spend_usd'] == 0
        assert max(x['best_additional_deletions'] for x in recomputed) == job['finalist']['genes_deleted'] - 1030
        f = job['finalist']
        deleted = set(f['deleted_gene_ids'])
        matched = {x['ecocyc_id'] for x in functional if deleted.intersection(x['b_numbers'])}
        values = {'registry': (len(deleted), 4290), 'current_functional': (len(matched), 1915),
                  'shared_functional': (len(matched & shared), 1197)}
        rows.append({'run_id': job['run_id'], 'method': job['kind'], 'seed': job['seed'],
            'evaluations': 500, 'stop_reason': report['stop_reason'],
            'state_id': f['state_id'], 'additional_deletions': len(deleted) - 1030,
            'denominators': {k: {'deleted': n, 'total': d, 'percent': 100 * n / d} for k, (n, d) in values.items()},
            'fba_growth_per_hour': f['growth_rate'],
            'rba_growth_floor_per_hour': f['evaluations']['resource_allocation']['growth_rate_floor_h'],
            'essentiality': f['evaluations']['essentiality'],
            'module_retention': f['evaluations']['module_retention'],
            'curves_recomputed_match': True, 'finalist_recomputed_match': True,
            'result_sha256': job['result_sha256'],
            'wandb_url': read(directory / 'tracking.json')['wandb_url'],
            'checkpoint': job['checkpoint']})
        all_refs.append((job['run_id'], job['checkpoint'], 'checkpoint.tar.gz'))
        print('Recomputed', job['run_id'], flush=True)
    for name, ref, archive_name in all_refs:
        artifact = api.artifact(ref['wandb_artifact'])
        assert artifact.state == 'COMMITTED'
        verify = temp / 'verify' / name
        verify.mkdir(parents=True, exist_ok=True)
        for category, filename in [('archive', archive_name), ('index', 'index.json')]:
            path = verify / ('s3-' + filename)
            download(ref[category], path)
            other = Path(artifact.get_entry(filename).download(root=str(verify / 'wandb')))
            assert file_sha256(other) == ref[category]['sha256'] and other.stat().st_size == ref[category]['bytes']
        artifact_checks.append({'run_id': name, 'artifact': ref['wandb_artifact'],
            'state': artifact.state, 'archive': ref['archive'], 'index': ref['index'],
            'full_s3_get_and_wandb_download_verified': True})
        print('Verified artifact', name, flush=True)

stats = {}
for method in spec['methods']:
    subset = [r for r in rows if r['method'] == method]
    stats[method] = {}
    for metric in ['registry', 'current_functional', 'shared_functional']:
        values = [r['denominators'][metric]['percent'] for r in subset]
        stats[method][metric] = {'n': len(values), 'mean_percent': statistics.mean(values),
            'sd_percent': statistics.stdev(values), 'min_percent': min(values), 'max_percent': max(values)}
analysis = {'experiment_id': spec['experiment_id'], 'analyzed_at': now(),
    'status': 'search_completed_independently_verified', 'jobs_completed': 10,
    'new_evaluated_states_total': 5000, 'model_calls': 0, 'search_failures': 0,
    'rows': rows, 'method_summaries': stats, 'functional_mapping': mapping_ref,
    'unique_nominated_genotypes': 10, 'nominations_frozen': True,
    'validation_status': 'not_launched_not_authorized',
    'interpretation': spec['analysis'],
    'limitations': ['Finite 500-evaluation budget, not convergence or a global minimum.',
        'FBA/RBA growth gates are model screens, not vEcoli or laboratory viability.',
        'No matched-budget LLM advantage is inferred from continuation endpoints; historical comparisons require common roots and overlapping actual evaluation counts.']}
audit = {'verified_at': now(), 'status': 'independently_verified', 'final_backup': final_ref,
    'member_count': len(seen), 'member_bytes': sum(x['bytes'] for x in index['files']),
    'all_member_names_sizes_sha256_verified': True, 'worker_files_verified': True,
    'sqlite_logical_comparisons': graph_checks, 'source_hashes_verified': len(sources),
    'metadata_headers_verified': 2, 'artifacts': artifact_checks,
    'mutable_metadata_exception': 'Only batch-progress final_backup reference and updated_at timestamp differ after archival; all other fields match.',
    'original_deadlines_preserved': approval}
atomic_json(out / 'analysis.json', analysis)
atomic_json(out / 'audit.json', audit)
atomic_json(out / 'nominations.json', {'experiment_id': spec['experiment_id'],
    'catalog_sha256': file_sha256(BUNDLE / 'catalog.json'), 'frozen_at': state['updated_at'],
    'selection_rule': spec['held_out_validation']['selection'], 'nominations': catalog['nominations'],
    'validation_status': 'not_launched_not_authorized'})
print(json.dumps({'status': analysis['status'], 'jobs': 10, 'nominations': 10, 'method_summaries': stats}))
