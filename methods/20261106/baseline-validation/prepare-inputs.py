"""Restore frozen data and construct the authorized baseline lineage workflows."""
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from urllib.parse import urlparse

import boto3

from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.data.io import atomic_json, file_sha256
from yggdrisil_ecoli.vecoli import install_vecoli_adapter, map_finalists, select_finalists
from yggdrisil_ecoli.vecoli_results import _validate_manifest_provenance

ROOT = Path('/home/ubuntu/yggdrisil-baseline-validation-20261106')
if sys.platform != 'linux':
    raise RuntimeError('Native preparation requires remote Linux')
read = lambda p: json.loads(p.read_bytes())
spec = read(ROOT / 'protocol.json')
auth = read(ROOT / 'approval.json')
assert auth['paid_launch'] and auth['total_cap_usd'] == 30
assert auth['protocol_sha256'] == file_sha256(ROOT / 'protocol.json')
assert auth['manifest_sha256'] == file_sha256(ROOT / 'manifest.json')
for name, digest in read(ROOT / 'manifest.json')['files'].items():
    if file_sha256(ROOT / name) != digest:
        raise RuntimeError('Changed prepared input: ' + name)
if (ROOT / 'prepared.json').exists() or (ROOT / 'batch-progress.json').exists():
    raise RuntimeError('Workflow already prepared or started; preserve before retry')
plan = read(ROOT / 'restore-plan.json')
selected = read(ROOT / 'genotypes.json')['genotypes']
inputs = ROOT / 'inputs'
inputs.mkdir(exist_ok=True)
cache = ROOT / 'input-archives'
cache.mkdir(exist_ok=True)
s3 = boto3.client('s3', region_name='us-east-1')


def download(ref):
    uri = ref.get('uri', ref.get('s3_uri'))
    target = cache / (ref['sha256'] + '.tar.gz')
    location = urlparse(uri)
    if not target.exists():
        s3.download_file(location.netloc, location.path.lstrip('/'), str(target))
    if file_sha256(target) != ref['sha256']:
        raise RuntimeError('Restored archive hash mismatch: ' + uri)
    return target


def extract(archive, name, target):
    with tarfile.open(archive) as tar:
        member = tar.getmember(name)
        if not member.isfile():
            raise RuntimeError('Expected a regular data member: ' + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(member) as source, target.open('wb') as dest:
            shutil.copyfileobj(source, dest)


native_inputs = download(plan['native_inputs'])
for name in ['manifest.json', 'workflow.json', 'inputs/simData.cPickle.gz']:
    extract(native_inputs, name, inputs / Path(name).name)
base, workflow = read(inputs / 'manifest.json'), read(inputs / 'workflow.json')
assert workflow['single_daughters'] and workflow['n_init_sims'] == 1
assert workflow['fail_at_max_duration']
sim = inputs / 'simData.cPickle'
with gzip.open(inputs / 'simData.cPickle.gz', 'rb') as src, sim.open('wb') as dest:
    shutil.copyfileobj(src, dest)
assert file_sha256(sim) == base['workflow']['sim_data_sha256']
genes_path = inputs / 'genes.parquet'
extract(download(plan['genes']['archive']), plan['genes']['path'], genes_path)
assert file_sha256(genes_path) == plan['genes']['sha256'] == base['registry']['sha256']
genes = load_genes(genes_path)
finalists = copy.deepcopy(base['finalists'][:2])
for actual, expected in zip(finalists, selected[:2], strict=True):
    for key in ['name', 'variant_index', 'state_id', 'deletion_count', 'deletion_set_sha256']:
        assert actual[key] == expected[key], key
sources = []
for candidate in selected[2:]:
    run = candidate['source_run_ids'][0]
    source = next(x for x in plan['source_graphs'] if x['run_id'] == run)
    graph = inputs / 'graphs' / (run + '.sqlite')
    extract(download(source['archive']), source['graph']['path'], graph)
    assert file_sha256(graph) == source['graph']['sha256']
    selection, frame = select_finalists(graph, count=1, deletion_band=1.0)
    assert frame.index[0] == candidate['state_id'], 'Finalist differs from frozen nomination'
    item = map_finalists(frame, genes).reset_index().to_dict('records')[0]
    assert sorted(item['deleted_gene_ids']) == candidate['deleted_gene_ids']
    assert item['deletion_set_sha256'] == candidate['deletion_set_sha256']
    item.update(name=candidate['name'], variant_index=candidate['variant_index'])
    finalists.append(item)
    sources.append({'selection': selection, 'backup': {'frozen_path': str(graph),
        'frozen_sha256': file_sha256(graph)}, 'source_run_id': run,
        'source_archive': source['archive'], 'source_index': source['index']})
native = ROOT / 'native-validated'
assert file_sha256(native / 'wholecell/utils/_netflow/nf_glpk.py') == '5ad8cd9ca727381032bb852b2b786465424598fbba1c261ac36741377db01686'
adapter = install_vecoli_adapter(native)
audit = ROOT / 'knockout-audit'
audit.mkdir(exist_ok=False)
shutil.copyfile(ROOT / 'audit-knockouts.py', audit / 'audit-knockouts.py')
manifest = copy.deepcopy(base)
manifest['finalists'] = finalists
manifest['workflow']['sim_data_path'] = str(sim)
atomic_json(audit / 'manifest.json', manifest)
atomic_json(audit / 'preflight.json', {'status': 'preparing'})
env = {**os.environ, 'PYTHONPATH': str(native), 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
for key in ['WANDB_API_KEY', 'OPENROUTER_API_KEY', 'EDISON_PLATFORM_API_KEY']:
    env.pop(key, None)
subprocess.run([str(native / '.venv/bin/python'), str(audit / 'audit-knockouts.py')],
               env=env, cwd=native, check=True)
finalists = read(audit / 'manifest.json')['finalists']
controller = ROOT / 'application/scripts/track_vecoli_experiment.py'
for seed in [None, *spec['seeds']]:
    calibration = seed is None
    seed = 101 if calibration else seed
    label = 'calibration' if calibration else f'seed-{seed}'
    folder = ROOT / 'runs' / label
    folder.mkdir(parents=True, exist_ok=False)
    for name in ['runscripts', 'ecoli']:
        (folder / name).symlink_to(native / name, target_is_directory=True)
    exp = spec['experiment_id'] + '-' + label
    config = copy.deepcopy(workflow)
    config.update(experiment_id=exp.replace('-', '_'), generations=2 if calibration else 20,
                  lineage_seed=seed, SIM_CPUS=5, SIM_MEM=4, sim_data_path=str(sim))
    config['emitter_arg']['out_dir'] = str(folder / 'output')
    config['variants'] = {} if calibration else {'yggdrisil_multi_gene_knockout': {
        'gene_ids': {'value': [x['vecoli_gene_ids'] for x in finalists[1:]]}}}
    config['skip_baseline'] = False
    atomic_json(folder / 'workflow.json', config)
    manifest = copy.deepcopy(base)
    manifest.update(experiment_id=exp, created_at=datetime.now(timezone.utc).isoformat(),
        purpose='Frozen non-LLM baseline validation with matched controls and numerical policy',
        source_graphs=sources, finalists=finalists[:1] if calibration else finalists)
    manifest.pop('application', None)
    manifest['parent_experiment_ids'] = spec['parent_experiment_ids']
    manifest['parent_data_artifact'] = 'mcclain/yggdrisil/deletion-baselines-20261106-final-data:v1'
    manifest['lineage'].update(seed=seed, max_generations=config['generations'])
    manifest['validation'].update(require_generations=config['generations'])
    manifest['controller_source'] = {'path': str(controller), 'sha256': file_sha256(controller)}
    manifest['execution'] = {'backend': 'Nextflow local executor on dedicated remote Linux EC2',
        'vecoli_python': str(native / '.venv/bin/python'),
        'nextflow_executable': str(ROOT / 'bin/nextflow'),
        'workflow_working_directory': str(folder),
        'max_parallel_cells': 1 if calibration else 3, 'paid_compute': True,
        'application_root': str(ROOT / 'application'), 'credential_directory': str(ROOT / 'credentials')}
    manifest['registry'] = {'path': str(genes_path), 'sha256': file_sha256(genes_path)}
    manifest['vecoli'].update(checkout=str(native), adapter_path=str(adapter),
        adapter_sha256=file_sha256(adapter),
        variant_knockout_audit_path=str(audit / 'variant-knockout-audit.json'),
        variant_knockout_audit_sha256=file_sha256(audit / 'variant-knockout-audit.json'),
        solver_patch_sha256=spec['solver_patch_sha256'],
        solver_after_sha256=file_sha256(native / 'wholecell/utils/_netflow/nf_glpk.py'))
    manifest['workflow'].update(config_path=str(folder / 'workflow.json'),
        config_sha256=file_sha256(folder / 'workflow.json'), experiment_id=config['experiment_id'],
        output_root=config['emitter_arg']['out_dir'], sim_data_path=str(sim), sim_data_sha256=file_sha256(sim))
    manifest['tracking'].update(run_id=hashlib.sha256(exp.encode()).hexdigest()[:12],
        run_name=exp, artifact_name=exp+'-data',
        s3_prefix='s3://' + spec['tracking']['s3_bucket'] + '/' + spec['tracking']['s3_prefix'] + label,
        tags=['baseline', 'lineage20', 'strict-solver-recovery', label])
    manifest['solver_policy'] = auth['solver_policy_scope'] + ' ' + spec['solver_policy']
    _validate_manifest_provenance(manifest)
    atomic_json(folder / 'manifest.json', manifest)
    atomic_json(folder / 'experiments.json', {'experiments': [
        {'id': exp, 'status': 'prepared', 'data_backup': {}}]})
atomic_json(ROOT / 'prepared.json', {'status': 'passed', 'prepared_at': datetime.now(timezone.utc).isoformat(),
    'genotypes': [{k: x[k] for k in ['name', 'state_id', 'variant_index', 'deletion_count', 'deletion_set_sha256']}
                  for x in finalists], 'source_graphs': sources,
    'manifest_sha256': {label: file_sha256(ROOT / 'runs' / label / 'manifest.json')
                       for label in ['calibration', 'seed-101', 'seed-211', 'seed-307']}})
print('Prepared 12 exact genotypes, 36 lineages and two-generation calibration.')
