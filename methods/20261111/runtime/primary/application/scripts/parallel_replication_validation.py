"""Seed-sharded validation with verified S3 handoff and bounded remote workers."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path('/home/ubuntu/yggdrisil-replication-20261109')
VALID = ROOT / 'validation'
SEEDS = (101, 211, 307)


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def save(path: Path, value: Any) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()
    temp = path.with_name(path.name + '.tmp')
    temp.write_bytes(raw)
    temp.replace(path)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while data := stream.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


def check_assignments(plan: dict[str, Any]) -> None:
    workers = plan['workers']
    if sorted(x['seed'] for x in workers) != list(SEEDS):
        raise ValueError('Each frozen seed must be assigned exactly once')
    if len({x['instance_id'] for x in workers}) != 3:
        raise ValueError('Expected three distinct approved workers')
    if plan['additional_cap_usd'] != 100 or plan['maximum_cells_per_worker'] != 8:
        raise ValueError('Unapproved resource allocation')
    if plan['additional_maximum_vm_usd'] > 77.42:
        raise ValueError('Additional instance-hours exceed authorization')
    if plan['science_deadline_unix'] >= plan['stop_deadline_unix']:
        raise ValueError('No final-backup window')
    if plan['stop_deadline_unix'] > plan['original_stop_deadline_unix']:
        raise ValueError('Original deadline may not be extended')


def resource_config(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    result['SIM_CPUS'] = 2
    if result.get('SIM_MEM') != 4:
        raise ValueError('Frozen memory request changed')
    return result


def activate_paths() -> None:
    sys.path[:0] = [str(VALID / 'application'), str(VALID / 'application/src'), str(ROOT / 'application'), str(ROOT / 'framework/src')]


def worker_module() -> Any:
    spec = importlib.util.spec_from_file_location('validation_worker', VALID / 'worker-supervisor.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('Worker supervisor cannot be loaded')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def client() -> tuple[Any, str]:
    import boto3
    return boto3.client('s3', region_name='us-east-1'), boto3.client('sts').get_caller_identity()['Account']


def get_json(s3: Any, owner: str, uri: str, *, missing_ok: bool = False) -> Any:
    from botocore.exceptions import ClientError
    u = urlparse(uri)
    try:
        response = s3.get_object(Bucket=u.netloc, Key=u.path.lstrip('/'), ExpectedBucketOwner=owner)
    except ClientError as error:
        if missing_ok and error.response['Error']['Code'] == 'NoSuchKey':
            return None
        raise
    try:
        return json.loads(response['Body'].read())
    finally:
        response['Body'].close()


def put_json(s3: Any, owner: str, uri: str, value: Any) -> None:
    from yggdrisil_ecoli.baseline_storage import configured_secrets, screen
    raw = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()
    with tempfile.NamedTemporaryFile(dir=VALID, suffix='.json') as stream:
        stream.write(raw)
        stream.flush()
        screen(Path(stream.name), configured_secrets(VALID / 'application', VALID / 'credentials'))
        u = urlparse(uri)
        s3.put_object(Bucket=u.netloc, Key=u.path.lstrip('/'), ExpectedBucketOwner=owner, Body=raw, ServerSideEncryption='AES256', Metadata={'sha256': hashlib.sha256(raw).hexdigest()})
    if get_json(s3, owner, uri) != value:
        raise RuntimeError('S3 control-record round trip failed')


def download(s3: Any, owner: str, ref: dict[str, Any], target: Path) -> None:
    u = urlparse(ref['uri'])
    body = s3.get_object(Bucket=u.netloc, Key=u.path.lstrip('/'), ExpectedBucketOwner=owner)['Body']
    try:
        with target.open('wb') as out:
            while data := body.read(1024 * 1024):
                out.write(data)
    finally:
        body.close()
    if target.stat().st_size != ref['bytes'] or digest(target) != ref['sha256']:
        raise RuntimeError('Downloaded archive hash/length mismatch')


def inspect_members(packed: Path, index: dict[str, Any], destination: Path | None = None) -> None:
    expected = {x['path']: x for x in index['files']}
    if len(expected) != len(index['files']):
        raise ValueError('Duplicate indexed member')
    seen = set()
    with tarfile.open(packed) as archive:
        for member in archive:
            path = Path(member.name)
            if not member.isfile() or path.is_absolute() or '..' in path.parts or member.name not in expected or member.name in seen:
                raise ValueError('Unsafe or unexpected archive member')
            if any(x in path.parts for x in ('credentials', 'source', '.aws', '.ssh')) or path.suffix in ('.py', '.sh', '.pem') or path.name in ('.env', '.netrc'):
                raise ValueError('Source or credential member in data archive')
            ref = expected[member.name]
            data = archive.extractfile(member)
            if data is None:
                raise ValueError('Missing member stream')
            h = hashlib.sha256()
            count = 0
            target = destination / path if destination else None
            if target is not None:
                resolved = target.resolve()
                if not resolved.is_relative_to(destination.resolve()) or target.is_symlink():
                    raise ValueError('Archive path resolves outside destination')
                if target.exists() and digest(target) != ref['sha256']:
                    raise FileExistsError('Refusing to replace distinct existing data: ' + str(target))
                target.parent.mkdir(parents=True, exist_ok=True)
            with data:
                output = target.open('wb') if target is not None and not target.exists() else None
                try:
                    while block := data.read(1024 * 1024):
                        h.update(block)
                        count += len(block)
                        if output:
                            output.write(block)
                finally:
                    if output:
                        output.close()
            if h.hexdigest() != ref['sha256'] or count != ref['bytes']:
                raise RuntimeError('Archive member mismatch: ' + member.name)
            seen.add(member.name)
    if seen != set(expected):
        raise ValueError('Missing indexed archive members')


def verify_archive(s3: Any, owner: str, receipt: dict[str, Any], *, restore: bool = False) -> None:
    import wandb
    with tempfile.TemporaryDirectory(prefix='parallel-verify-', dir=VALID) as tmp:
        root = Path(tmp)
        packed, index_path = root / 'data.tar.gz', root / 'index.json'
        download(s3, owner, receipt['archive'], packed)
        download(s3, owner, receipt['index'], index_path)
        index = read(index_path)
        if index['archive'] != receipt['archive']:
            raise ValueError('Index/archive binding mismatch')
        # Validate the entire archive before restoring any member.
        inspect_members(packed, index)
        artifact = wandb.Api(timeout=120).artifact(receipt['wandb_artifact'])
        if artifact.state != 'COMMITTED':
            raise RuntimeError('W&B artifact is not committed')
        for name, ref in [('data.tar.gz', receipt['archive']), ('index.json', receipt['index'])]:
            fetched = Path(artifact.get_entry(name).download(root=str(root / 'wandb')))
            if digest(fetched) != ref['sha256'] or fetched.stat().st_size != ref['bytes']:
                raise RuntimeError('Independent W&B hash mismatch')
        if restore:
            inspect_members(packed, index, VALID)


def normalize_control_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    """Represent absent historical control references as JSON null, never NaN."""
    result = copy.deepcopy(manifest)
    for finalist in result['finalists']:
        if finalist.get('variant_index') != 1 or finalist.get('deletion_count') != 1030:
            continue
        for key in ('search_data_artifact', 'search_result'):
            value = finalist.get(key)
            if isinstance(value, float) and math.isnan(value):
                finalist[key] = None
                print('Normalized absent historical control reference: '+key, flush=True)
    return result


def prepare_assignments(plan: dict[str, Any]) -> None:
    from yggdrisil_ecoli.vecoli_results import _validate_manifest_provenance
    prepared = read(VALID / 'prepared.json')
    assignment_path = VALID / 'parallel-assignments.json'
    if assignment_path.exists():
        assignment = read(assignment_path)
        if assignment['workers'] != plan['workers'] or assignment['genotypes'] != prepared['genotypes'] or assignment['nominations_sha256'] != digest(ROOT / 'bundle/nominations.json') or assignment['manifest_sha256'] != prepared['manifest_sha256']:
            raise ValueError('Prepared assignment changed; refuse duplicate preparation')
        for label, expected in assignment['manifest_sha256'].items():
            path = VALID / 'runs' / label / 'manifest.json'
            if digest(path) != expected:
                raise ValueError('Prepared manifest changed: '+label)
            _validate_manifest_provenance(read(path))
        return
    for worker in plan['workers']:
        seed = worker['seed']
        folder = VALID / 'runs' / f'seed-{seed}'
        workflow = resource_config(read(folder / 'workflow.json'))
        save(folder / 'workflow.json', workflow)
        manifest = normalize_control_metadata(read(folder / 'manifest.json'))
        manifest['workflow']['config_sha256'] = digest(folder / 'workflow.json')
        manifest['tracking']['s3_prefix'] = plan['s3_prefix'] + f'seed-{seed}'
        manifest['execution'].update(max_parallel_cells=8, worker_instance_id=worker['instance_id'], parallel_assignment_seed=seed)
        save(folder / 'manifest.json', manifest)
        _validate_manifest_provenance(manifest)
        prepared['manifest_sha256'][f'seed-{seed}'] = digest(folder / 'manifest.json')
        label = f'calibration-worker-{seed}'
        target = VALID / 'runs' / label
        target.mkdir(exist_ok=False)
        original = VALID / 'runs/calibration'
        calibration = copy.deepcopy(read(original / 'manifest.json'))
        config = copy.deepcopy(read(original / 'workflow.json'))
        exp = calibration['experiment_id'] + f'-worker-{seed}' + plan.get('attempt_suffix', '')
        config.update(experiment_id=exp.replace('-', '_'))
        config['emitter_arg']['out_dir'] = str(target / 'output')
        save(target / 'workflow.json', config)
        calibration['experiment_id'] = exp
        calibration['workflow'].update(config_path=str(target / 'workflow.json'), config_sha256=digest(target / 'workflow.json'), experiment_id=config['experiment_id'], output_root=config['emitter_arg']['out_dir'])
        calibration['execution'].update(workflow_working_directory=str(target), worker_instance_id=worker['instance_id'])
        calibration['tracking'].update(run_id=hashlib.sha256(exp.encode()).hexdigest()[:16], run_name=exp, artifact_name=exp+'-data', s3_prefix=plan['s3_prefix']+'calibration/'+str(seed))
        calibration['purpose'] += '; per-worker calibration, excluded from independent scientific replicates'
        _validate_manifest_provenance(calibration)
        save(target / 'manifest.json', calibration)
        save(target / 'experiments.json', {'experiments': [{'id': exp, 'status': 'prepared', 'data_backup': {}}]})
        prepared['manifest_sha256'][label] = digest(target / 'manifest.json')
    save(VALID / 'prepared.json', prepared)
    assignments = {'workers': plan['workers'], 'nominations_sha256': digest(ROOT / 'bundle/nominations.json'), 'genotypes': prepared['genotypes'], 'manifest_sha256': prepared['manifest_sha256'], 'maximum_cells_per_worker': 8, 'maximum_cells_total': 24, 'scientific_lineages': len(prepared['genotypes']) * 3, 'calibrations_are_technical_controls': True}
    save(VALID / 'parallel-assignments.json', assignments)


def launch_leaf(seed: int) -> subprocess.Popen[bytes]:
    for label in (f'calibration-worker-{seed}', f'seed-{seed}'):
        folder = VALID / 'runs' / label
        for name in ('runscripts', 'ecoli'):
            link = folder / name
            expected = ROOT / 'native-validated' / name
            if link.is_symlink():
                if link.resolve() != expected.resolve():
                    raise ValueError('Unexpected native code link')
            elif not link.exists():
                link.symlink_to(expected, target_is_directory=True)
            else:
                raise FileExistsError('Non-symlink native code path')
    log = (VALID / f'worker-{seed}.log').open('xb')
    try:
        return subprocess.Popen([sys.executable, '-u', str(VALID / 'worker-supervisor.py')], cwd=VALID, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        log.close()


def worker_status(worker: dict[str, Any], phase: str, error: dict[str, str] | None = None) -> dict[str, Any]:
    seed = worker['seed']
    out = {'seed': seed, 'instance_id': worker['instance_id'], 'phase': phase, 'checked_at_unix': time.time(), 'error': error, 'workflows': {}}
    p = VALID / f'worker-{seed}-progress.json'
    if p.exists():
        out['study'] = read(p)
    for label in (f'calibration-worker-{seed}', f'seed-{seed}'):
        folder = VALID / 'runs' / label
        item = {}
        for name in ('progress.json', 's3-checkpoint.json', 'experiments.json'):
            p = folder / name
            if p.exists():
                value = read(p)
                item[name] = {'manifest': value.get('manifest'), 'verified_at': value.get('verified_at'), 'files': len(value.get('files', {}))} if name == 's3-checkpoint.json' else value
        out['workflows'][label] = item
    final = VALID / f'worker-{seed}-final-backup.json'
    if final.exists():
        out['final_backup'] = read(final)
    return out


def await_workers(s3: Any, owner: str, plan: dict[str, Any], state: dict[str, Any]) -> None:
    """Approval may arrive while searches finish; preserve the handoff until then."""
    while True:
        if time.time() >= plan['science_deadline_unix']:
            raise TimeoutError('Parallel workers were not approved and ready before the science deadline')
        ready = True
        for worker in plan['workers']:
            if worker['existing_worker']:
                continue
            result = get_json(s3, owner, plan['s3_prefix']+f"workers/{worker['seed']}/status.json", missing_ok=True)
            if result is None:
                ready = False
                continue
            if result['instance_id'] != worker['instance_id'] or result['seed'] != worker['seed']:
                raise ValueError('Ready-worker identity mismatch')
            if result['phase'] != 'ready_waiting_for_verified_finalists':
                raise RuntimeError('Unexpected existing worker state: '+result['phase'])
            if time.time()-result['checked_at_unix'] > 300:
                ready = False
        if ready:
            return
        state.update(status='waiting_for_tracking_approval_and_workers', updated_at_unix=time.time())
        save(VALID/'batch-progress.json', state)
        put_json(s3, owner, plan['s3_prefix']+'coordinator/status.json', state)
        print('Waiting for both approved tracking-enabled workers; no scientific dispatch yet', flush=True)
        time.sleep(60)


def run_remote_worker() -> None:
    plan = read(VALID / 'parallel-plan.json')
    check_assignments(plan)
    worker = read(VALID / 'parallel-worker.json')
    if worker not in plan['workers'] or worker['seed'] == 101:
        raise ValueError('Unapproved remote assignment')
    from scripts.run_search_replication import worker_identity
    if worker_identity() != worker['instance_id']:
        raise ValueError('Remote instance identity mismatch')
    with (VALID / 'dispatch.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (VALID / f"worker-{worker['seed']}-progress.json").exists():
            raise FileExistsError('Worker already dispatched; manual recovery required')
        s3, owner = client()
        status_uri = plan['s3_prefix'] + f"workers/{worker['seed']}/status.json"
        process = None
        try:
            while True:
                if time.time() >= plan['science_deadline_unix']:
                    raise TimeoutError('Parallel worker deadline reached before dispatch')
                dispatch = get_json(s3, owner, plan['s3_prefix'] + f"dispatch/{worker['seed']}.json", missing_ok=True)
                if dispatch is not None:
                    gate = get_json(s3, owner, plan['s3_prefix']+'dispatch-ready.json', missing_ok=True)
                    if gate is not None:
                        if gate['plan_sha256'] != digest(VALID / 'parallel-plan.json') or gate['seeds'] != list(SEEDS):
                            raise ValueError('Dispatch barrier does not match the frozen assignment')
                        break
                put_json(s3, owner, status_uri, worker_status(worker, 'ready_waiting_for_verified_finalists'))
                time.sleep(60)
            if dispatch['worker'] != worker or dispatch['plan_sha256'] != digest(VALID / 'parallel-plan.json'):
                raise ValueError('Dispatch belongs to another worker or plan')
            verify_archive(s3, owner, dispatch['preparation_backup'], restore=not plan.get('resume_saved_calibration', False))
            if plan.get('resume_saved_calibration'):
                if read(VALID / 'resume-backup-preflight.json')['status'] != 'passed':
                    raise RuntimeError('Saved worker backup not independently verified')
                prepare_assignments(plan)
            save(VALID / 'received-dispatch.json', dispatch)
            process = launch_leaf(worker['seed'])
            while process.poll() is None:
                if time.time() >= plan['science_deadline_unix']:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=max(1, plan['stop_deadline_unix'] - time.time() - 60))
                    break
                put_json(s3, owner, status_uri, worker_status(worker, 'running'))
                time.sleep(60)
            result = worker_status(worker, 'finished')
            if process.returncode != 0 or result.get('final_backup', {}).get('status') != 'verified':
                raise RuntimeError('Worker ended without a verified final backup')
            verify_archive(s3, owner, result['final_backup'])
            result['independent_final_member_audit'] = True
            put_json(s3, owner, status_uri, result)
        except BaseException as error:
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=max(1, plan['stop_deadline_unix'] - time.time() - 60))
            put_json(s3, owner, status_uri, worker_status(worker, 'action_required', {'type': type(error).__name__, 'message': str(error)}))
            raise
        finally:
            subprocess.run(['sudo', 'shutdown', '-h', '+2'], check=True)


def coordinate() -> None:
    import wandb

    from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets
    plan = read(VALID / 'parallel-plan.json')
    check_assignments(plan)
    with (VALID / 'batch.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (VALID / 'batch-progress.json').exists():
            raise FileExistsError('Validation coordinator already started')
        if read(VALID / 'preflight.json')['status'] != 'passed':
            raise ValueError('Frozen validation preparation did not pass')
        prepare_assignments(plan)
        s3, owner = client()
        helper = worker_module()
        secrets = configured_secrets(VALID / 'application', VALID / 'credentials')
        state = {'experiment_id': plan['experiment_id']+'-validation'+plan.get('attempt_suffix', ''), 'status': 'preparing_parallel_dispatch', 'workers': {}, 'maximum_parallel_cells': 24, 'started_at_unix': time.time()}
        local = None
        with wandb.init(entity='mcclain', project='yggdrisil', id=plan['experiment_id']+'-parallel-validation'+plan.get('attempt_suffix', ''), name=plan['experiment_id']+'-parallel-validation'+plan.get('attempt_suffix', ''), job_type='validation-coordinator', config=plan, dir=str(VALID), settings=wandb.Settings(disable_git=True, disable_code=True, save_code=False)) as tracked:
            archive = BaselineArchive(s3=s3, wandb=wandb, tracked=tracked, owner=owner, bucket='phd-research-storage-1758274488', prefix=plan['s3_prefix'].split('phd-research-storage-1758274488/',1)[1], run_id='coordinator', secrets=secrets)
            state['wandb_url'] = tracked.url
            save(VALID / 'batch-progress.json', state)
            try:
                prep = helper.final_backup({'experiment_id': state['experiment_id']+'-prepared-inputs', 'status': 'frozen_before_dispatch'}, archive, secrets, tracked, wandb, receipt_name='preparation-backup.json')
                verify_archive(s3, owner, prep)
                await_workers(s3, owner, plan, state)
                for worker in plan['workers']:
                    if worker['seed'] == 101:
                        continue
                    remote = get_json(s3, owner, plan['s3_prefix'] + f"workers/{worker['seed']}/status.json")
                    if remote['instance_id'] != worker['instance_id'] or remote['phase'] != 'ready_waiting_for_verified_finalists' or time.time()-remote['checked_at_unix'] > 300:
                        raise RuntimeError('Approved worker is not ready for dispatch')
                for worker in plan['workers']:
                    if worker['seed'] == 101:
                        continue
                    uri = plan['s3_prefix'] + f"dispatch/{worker['seed']}.json"
                    if get_json(s3, owner, uri, missing_ok=True) is not None:
                        raise FileExistsError('Duplicate remote dispatch refused')
                    put_json(s3, owner, uri, {'worker': worker, 'plan_sha256': digest(VALID / 'parallel-plan.json'), 'preparation_backup': prep, 'nominations_sha256': read(VALID / 'parallel-assignments.json')['nominations_sha256']})
                put_json(s3, owner, plan['s3_prefix']+'dispatch-ready.json', {'plan_sha256': digest(VALID / 'parallel-plan.json'), 'seeds': list(SEEDS), 'preparation_archive_sha256': prep['archive']['sha256']})
                local = launch_leaf(101)
                state.update(status='running', preparation_backup=prep)
                verified = set()
                while len(verified) < 3:
                    if time.time() >= plan['science_deadline_unix'] and local.poll() is None:
                        local.send_signal(signal.SIGTERM)
                    if time.time() >= plan['stop_deadline_unix']-60:
                        raise TimeoutError('Parallel final-backup deadline reached')
                    for worker in plan['workers']:
                        seed = worker['seed']
                        if seed == 101:
                            result = worker_status(worker, 'running' if local.poll() is None else 'finished')
                            if local.poll() not in (None, 0):
                                raise RuntimeError('Local validation worker failed operationally')
                        else:
                            result = get_json(s3, owner, plan['s3_prefix'] + f'workers/{seed}/status.json')
                        if result['instance_id'] != worker['instance_id'] or result['seed'] != seed:
                            raise ValueError('Worker status identity mismatch')
                        state['workers'][str(seed)] = result
                        if result['phase'] == 'action_required':
                            raise RuntimeError('Worker requires inspection: '+str(seed)+' '+str(result.get('error')))
                        if result['phase'] not in ('finished',) and time.time()-result['checked_at_unix'] > 900:
                            raise RuntimeError('Worker heartbeat stale: '+str(seed))
                        if result['phase'] == 'finished' and seed not in verified:
                            if result.get('final_backup', {}).get('status') != 'verified':
                                raise RuntimeError('Worker has no verified final archive')
                            verify_archive(s3, owner, result['final_backup'])
                            verified.add(seed)
                            result['coordinator_verified_final_members'] = True
                    state['updated_at_unix'] = time.time()
                    save(VALID / 'batch-progress.json', state)
                    put_json(s3, owner, plan['s3_prefix']+'coordinator/status.json', state)
                    tracked.log({'workers_verified_final': len(verified)})
                    if len(verified) < 3:
                        time.sleep(30)
                state['status'] = 'completed' if all(x['study']['status']=='completed' for x in state['workers'].values()) else 'completed_with_exceptions'
                save(VALID / 'batch-progress.json', state)
                save(VALID / 'parallel-results.json', state)
                final = helper.final_backup(state, archive, secrets, tracked, wandb)
                verify_archive(s3, owner, final)
                final['shard_archives'] = {k:v['final_backup'] for k,v in state['workers'].items()}
                final['all_shards_independently_verified'] = True
                save(VALID / 'final-backup.json', final)
            except BaseException as error:
                state.update(status='failed_preserving_worker_disks', error={'type': type(error).__name__, 'message': str(error)})
                save(VALID / 'batch-progress.json', state)
                put_json(s3, owner, plan['s3_prefix']+'coordinator/status.json', state)
                if local is not None and local.poll() is None:
                    local.send_signal(signal.SIGTERM)
                    local.wait(timeout=max(1, plan['stop_deadline_unix'] - time.time() - 60))
                raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--wait-dispatch', action='store_true')
    options = parser.parse_args()
    if sys.platform != 'linux':
        raise RuntimeError('Validation is remote Linux only')
    activate_paths()
    def interrupted(signum: int, frame: Any) -> None:
        raise InterruptedError('Parallel coordinator received signal '+str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    os.environ.update(read(VALID / 'credentials/tracking.json'))
    if options.wait_dispatch:
        run_remote_worker()
    else:
        coordinate()


if __name__ == '__main__':
    main()
