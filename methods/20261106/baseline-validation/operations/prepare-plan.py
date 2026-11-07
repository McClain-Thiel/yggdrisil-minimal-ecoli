"""Freeze already selected baseline genotypes; no scientific execution or launch."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil')
FRAMEWORK = Path('/Users/mcclainthiel/Projects/PhD/yggdrisil')
EXPERIMENT = 'vecoli-deletion-baselines-20gen-3seed-20261106'
OUT = ROOT / 'runs' / EXPERIMENT
BASELINE = ROOT / 'runs/deletion-baselines-20261106'
PRIOR = ROOT / 'runs/literature-ablation-kimi-continuation-20261106'
ORIGINAL = ROOT / 'runs/storage-migration-20261105/remote-validation'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def identity(ids):
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r'b\d{4}', g) for g in ids):
        raise ValueError('Malformed or duplicated gene identifiers')
    ordered = sorted(ids)
    deletion_hash = sha(json.dumps(ordered, separators=(',', ':')).encode())
    state_hash = sha(json.dumps({'deleted_genes': ordered}, separators=(',', ':'),
                               sort_keys=True).encode())
    return state_hash, deletion_hash


def freeze_genotypes(nominations, catalog, start, controls):
    if (not catalog['finalist_selection_frozen']
            or catalog['status'] != 'search_completed_verified'
            or nominations['nominations'] != catalog['nominations']):
        raise ValueError('Nomination list differs from independently verified frozen catalog')
    selected = nominations['nominations']
    if len(selected) != 10 or len({n['state_id'] for n in selected}) != 10:
        raise ValueError('Expected all ten distinct preselected baseline genomes')
    expected_runs = {j['run_id'] for j in catalog['jobs']}
    actual_runs = [run for n in selected for run in n['source_run_ids']]
    if len(actual_runs) != 10 or set(actual_runs) != expected_runs:
        raise ValueError('Missing or duplicated baseline search seed')
    genotypes = []
    for control, ids in zip(controls, [[], start['deleted_gene_ids']], strict=True):
        state, digest = identity(ids)
        if (state != control['state_id'] or digest != control['deletion_set_sha256']
                or len(ids) != control['deletion_count']):
            raise ValueError('Control genotype differs from the agent validation')
        genotypes.append({**control, 'deleted_gene_ids': sorted(ids), 'role': 'control'})
    for index, nomination in enumerate(selected, 2):
        ids = nomination['deleted_gene_ids']
        state, digest = identity(ids)
        if state != nomination['state_id'] or len(ids) != nomination['genes_deleted']:
            raise ValueError('Nomination genotype identity or size mismatch')
        if not set(start['deleted_gene_ids']).issubset(ids):
            raise ValueError('Baseline does not preserve the common starting genotype')
        run = nomination['source_run_ids'][0]
        job = next(j for j in catalog['jobs'] if j['run_id'] == run)
        if (job['finalist']['state_id'] != state
                or job['checkpoint']['status'] != 'verified'
                or not job['checkpoint']['wandb_download_verified']):
            raise ValueError('Nomination is not the verified per-run finalist')
        genotypes.append({'name': run.removeprefix('deletion-baselines-20261106-'),
                          'role': 'baseline', 'variant_index': index,
                          'state_id': state, 'deletion_set_sha256': digest,
                          'deletion_count': len(ids), 'deleted_gene_ids': sorted(ids),
                          'source_run_ids': nomination['source_run_ids'],
                          'source_checkpoint': {k: job['checkpoint'][k] for k in
                                                ['archive', 'index', 'wandb_artifact']}})
    return genotypes


def prepare():
    if (OUT / 'manifest.json').exists():
        raise FileExistsError('Frozen preparation already exists; preserve before revising')
    pricing = read(OUT / 'pricing-and-input-verification.json')
    nomination_path = BASELINE / 'final/nominations.json'
    nomination_ref = pricing['verified_nomination_reference']
    if sha(nomination_path.read_bytes()) != nomination_ref['sha256']:
        raise ValueError('Nominations differ from verified private S3 copy')
    nominations = read(nomination_path)
    catalog_path = BASELINE / 'final/catalog.json'
    if sha(catalog_path.read_bytes()) != nominations['catalog_sha256']:
        raise ValueError('Frozen catalog hash changed')
    catalog = read(catalog_path)
    parent = read(ROOT / 'benchmarks/vecoli_20gen_remote.json')
    start = read(BASELINE / 'inputs/starting-genotype.json')
    genotypes = freeze_genotypes(nominations, catalog, start, parent['genotypes'][:2])
    ledger = read(ROOT / 'experiments.json')
    prior = next(e for e in ledger['experiments']
                 if e['id'] == 'vecoli-kimi-continuation-20gen-3seed-20261106')
    hourly = pricing['prices'][0]['usd_per_hour']
    if hourly * 24 >= 30:
        raise ValueError('Compute exceeds proposed budget')
    protocol = {
        'schema_version': 1, 'experiment_id': EXPERIMENT,
        'status': 'prepared_pending_cost_approval',
        'parent_experiment_ids': ['deletion-baselines-20261106', prior['id']],
        'selection': {'rule': nominations['selection_rule'],
                      'frozen_at': nominations['frozen_at'], 'reference': nomination_ref,
                      'baseline_genotypes': 10, 'control_genotypes': 2,
                      'candidate_substitutions': False, 'feedback_to_search': False},
        'seeds': [101, 211, 307], 'generations': 20, 'single_daughters': True,
        'baseline_lineages': 30, 'control_lineages': 6,
        'maximum_full_cell_simulations': 720, 'additional_calibration_cells': 2,
        'scientific_conditions': parent['scientific_conditions'],
        'native_runtime': parent['native_runtime'],
        'solver_policy': prior['solver_policy'],
        'solver_patch_sha256': prior['solver_patch_sha256'],
        'calibration': {'genotype': 'mg1655-control', 'seed': 101, 'generations': 2,
                        'require_finite_output': True, 'max_peak_rss_bytes': 4 * 1024**3,
                        'full_dispatch_requires_pass': True},
        'execution': {'platform': 'remote_linux_only', 'region': 'us-east-1',
                      'instance_type': 'm7i.4xlarge', 'vcpus': 16, 'memory_gib': 64,
                      'encrypted_scratch_gib': 200, 'scratch_type': 'gp3',
                      'seed_workflows': 3, 'max_parallel_cells_per_workflow': 3,
                      'max_parallel_cells_total': 9, 'native_threads_per_cell': 1,
                      'remote_root': '/home/ubuntu/yggdrisil-baseline-validation-20261106',
                      'worker_id': None, 'max_worker_hours': 24, 'science_hours': 23,
                      'deadlines': 'Set once from actual launch time; reserve last hour for preservation. Never reset on retry.',
                      'shared_worker': 'Leave all preexisting shared compute unchanged.'},
        'cost_proposal': {'total_cap_usd': 30, 'model_cost_usd': 0,
                          'verified_hourly_usd': hourly, 'max_compute_usd': hourly * 24,
                          'storage_transfer_request_reserve_usd': 30 - hourly * 24,
                          'estimated_wall_hours': [10, 14],
                          'estimate_basis': 'Scaled previous approximately 300-cell validation to 720 cells, same nine-cell concurrency; genotype runtime is uncertain.',
                          'pricing_record': 'pricing-and-input-verification.json'},
        'authorization': {'preparation': True, 'paid_launch': False,
                          'approved_budget_usd': None, 'model_calls_allowed': False,
                          'credential_transfer': 'Pending approval: W&B tracking key only over encrypted SSH; owner-only permissions; excluded from backups.'},
        'tracking': {'entity': 'mcclain', 'project': 'yggdrisil',
                     's3_bucket': 'phd-research-storage-1758274488',
                     's3_prefix': f'yggdrisil/experiments/{EXPERIMENT}/',
                     'monitor': 'Use approved scheduled monitoring; notify only completion, new failures, actionable stalls or required user action.'},
        'preservation': {'checkpoint': 'Closed cells only; full S3 GET SHA256 plus committed W&B artifact download verification; halt on backup failure.',
                         'credentials': 'Screen all locally configured historical secrets before metadata publication. Transfer only W&B tracking credential; remote scans all configured credentials. No source archives as data.',
                         'cleanup': 'Stop by hard deadline. Preserve disk on verification failure; terminate only dedicated worker and delete volume after independent final member audit.',
                         'ledger': 'Update experiments.json and working migration ledger with exact immutable S3/W&B refs, source hashes, failures, statuses and costs.'},
        'analysis': {'retain_all_lineages': True,
                     'outcomes': ['consecutive divisions', 'division times', 'birth/dry-mass trends',
                                  'exact target-transcript suppression', 'finite outputs',
                                  'solver-assisted cells', 'numerical warnings',
                                  'nondivision versus model/infrastructure failure'],
                     'denominators': ['4290-gene registry', '1915 current functional genes', '1197 shared functional genes'],
                     'comparison': 'Match solver policy, seeds and medium to prior agent validation. Search efficiency comparisons require common roots and equal evaluation counts.',
                     'interpretation': 'MG1655 expression-knockout model evidence; does not physically shorten the chromosome or establish laboratory viability.'},
        'remote_preflight_required': ['Verify exact restored input and source hashes',
            'Build native extensions using pinned native runtime without modifying scientific source',
            'Run exact-knockout audit on all 12 genotypes before dispatch',
            'Run offline controller, solver and backup integrity checks',
            'Verify bucket ownership, public-access blocks and W&B data round trips',
            'Verify persistent hard shutdown timer, process identities and exclusive locks'],
    }
    genes = next(x for x in read(BASELINE / 'restore-plan.json')
                 if x['path'] == 'data/processed/genes.parquet')
    restore = {'native_inputs': parent['input_archives'][0], 'genes': genes,
               'nominations': nomination_ref,
               'source_graphs': [{'run_id': j['run_id'],
                                  'archive': j['checkpoint']['archive'],
                                  'index': j['checkpoint']['index'],
                                  'graph': next(x for x in j['checkpoint']['files']
                                                if x['path'] == 'graph.sqlite')}
                                 for j in catalog['jobs']]}
    sources = {}
    for label, base in [('application', ROOT), ('framework', FRAMEWORK)]:
        for p in sorted((base / 'src').rglob('*')):
            if (p.is_file() and p.suffix in {'.py', '.txt', '.typed'}
                    and not any(x.endswith(('.egg-info', '.dist-info')) for x in p.parts)):
                sources[f'{label}/{p.relative_to(base)}'] = p
        sources[f'{label}/pyproject.toml'] = base / 'pyproject.toml'
    sources['application/scripts/track_vecoli_experiment.py'] = ROOT / 'scripts/track_vecoli_experiment.py'
    for name in ['test_vecoli.py', 'test_experiment_tracking.py', 'conftest.py']:
        sources[f'application/tests/{name}'] = ROOT / 'tests' / name
    for name in ['yggdrisil-prepare-validation-source.py', 'test_continuation_validation_guards.py']:
        sources[f'prior-operations/{name}'] = PRIOR / 'source' / name
    sources['solver-recovery-v2.patch'] = ORIGINAL / 'source/yggdrisil-glpk-recovery-v2.patch'
    sources['test-solver-recovery-v2.py'] = ORIGINAL / 'source/yggdrisil-test-solver-recovery-v2.py'
    sources['prepare-plan.py'] = Path(__file__)
    if sha(sources['solver-recovery-v2.patch'].read_bytes()) != protocol['solver_patch_sha256']:
        raise ValueError('Numerical policy changed relative to prior agent validation')
    hashes = {}
    for name, source in sources.items():
        data = source.read_bytes()
        target = OUT / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.resolve() != source.resolve():
            target.write_bytes(data)
        hashes[name] = sha(data)
    save(OUT / 'protocol.json', protocol)
    save(OUT / 'genotypes.json', {'experiment_id': EXPERIMENT, 'genotypes': genotypes})
    save(OUT / 'restore-plan.json', restore)
    save(OUT / 'source-manifest.json', {'files': hashes, 'source_uploaded_as_data': False})
    save(OUT / 'native-extension-build-reference.json', read(PRIOR / 'native-extension-repair.json'))
    names = ['protocol.json', 'genotypes.json', 'restore-plan.json', 'source-manifest.json',
             'native-extension-build-reference.json', 'pricing-and-input-verification.json']
    manifest = {'experiment_id': EXPERIMENT, 'prepared_at': datetime.now(timezone.utc).isoformat(),
                'status': 'prepared_pending_cost_approval', 'launch_authorized': False,
                'files': {name: sha((OUT / name).read_bytes()) for name in names},
                'data_policy': 'Source copies are local/deployment code only; never upload as data.'}
    save(OUT / 'manifest.json', manifest)
    print(json.dumps({'experiment_id': EXPERIMENT, 'genotypes': len(genotypes),
                      'lineages': 36, 'maximum_cells_including_calibration': 722,
                      'source_files': len(hashes), 'status': manifest['status']}))


if __name__ == '__main__':
    prepare()
