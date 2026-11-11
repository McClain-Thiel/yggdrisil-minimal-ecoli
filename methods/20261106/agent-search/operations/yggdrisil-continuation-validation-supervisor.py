"""Run the authorized control and three seed workflows, then preserve data."""
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlparse

import boto3

BASE = Path('/home/ubuntu/yggdrisil-continuation-20261106')
ROOT = BASE / 'validation'
PLAN = json.loads((BASE / 'protocol.json').read_text())
PROTOCOL = {'experiment_id': PLAN['validation']['experiment_id'], 'seeds': PLAN['validation']['seeds'], 'launch_proposal': {'data_destination': PLAN['tracking']['validation_s3_prefix']}}
DEADLINE = json.loads((BASE / 'deadlines.json').read_text())['science_unix']
ROOT.mkdir(exist_ok=True)
os.environ.update(json.loads((BASE / 'credentials/common.json').read_text()))
os.environ['PYTHONPATH'] = str(BASE / 'validation-application/src')+':'+str(BASE / 'validation-application')
lock = (ROOT / 'batch.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
if (ROOT / 'batch-progress.json').exists():
    raise RuntimeError('Batch already started; inspect the preserved attempt before resuming')
children = {}
logs = []
summary = {'experiment_id':PROTOCOL['experiment_id'], 'status':'waiting_for_search',
           'started_at':datetime.now(timezone.utc).isoformat(), 'jobs':{}}


def record():
    summary['updated_at'] = datetime.now(timezone.utc).isoformat()
    target = ROOT / 'batch-progress.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(summary, indent=2)+'\n')
    temporary.replace(target)


def launch(label):
    folder = ROOT / 'runs' / label
    if (folder / 'process.json').exists():
        raise RuntimeError('Refusing duplicate controller: ' + label)
    log = (folder / 'controller.log').open('ab')
    logs.append(log)
    command = [sys.executable, str(BASE / 'validation-application/scripts/track_vecoli_experiment.py'),
               '--manifest', str(folder / 'manifest.json'), '--catalog', str(folder / 'experiments.json')]
    process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    children[label] = process
    summary['jobs'][label] = {'pid':process.pid, 'status':'running', 'manifest':str(folder / 'manifest.json')}
    record()


def collect(label):
    process = children[label]
    if process.poll() is None:
        return False
    catalog = json.loads((ROOT / 'runs' / label / 'experiments.json').read_text())
    entry = catalog['experiments'][0]
    summary['jobs'][label].update(exit_code=process.returncode, status=entry['status'], data_backup=entry['data_backup'],
                                  wandb_run_urls=entry.get('wandb_run_urls', []))
    record()
    if entry['status'] not in {'completed', 'completed_with_nondivision'}:
        print(f'{label} completed with {entry["status"]}; independent lineages continue', flush=True)
    return True


def wait_for(labels):
    pending = set(labels)
    while pending:
        if time.time() > DEADLINE:
            raise TimeoutError('Experiment wall-time cap reached; preserve partial results')
        for label in list(pending):
            if collect(label):
                pending.remove(label)
        if pending:
            time.sleep(15)


def stop_children():
    for process in children.values():
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
    for label, process in children.items():
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            info = ROOT / 'runs' / label / 'process.json'
            if info.exists():
                pid = json.loads(info.read_text())['workflow_pid']
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    print('Workflow already stopped during forced cleanup:', pid, flush=True)
            process.kill()
            process.wait()


def final_backup():
    excluded_dirs = {'wandb-cache', 'wandb-data', 'backup-cache', 'backup-verification', 'wandb', '__pycache__'}
    excluded_names = {'.command.run', '.command.env', 'LOCK'}
    excluded_suffixes = {'.py', '.pyc', '.sh', '.nf', '.patch', '.diff'}
    files = [p for p in ROOT.glob('*') if p.is_file() and p.suffix in {'.json', '.log'}]
    files += [p for p in (ROOT/'inputs').rglob('*') if p.is_file() and p.suffix in {'.json','.sqlite','.cPickle','.gz'}]
    for directory, dirs, names in os.walk(ROOT / 'runs'):
        dirs[:] = [d for d in dirs if d not in excluded_dirs and not (Path(directory)/d).is_symlink()]
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or path.suffix in excluded_suffixes or name in excluded_names or name.startswith('.env'):
                continue
            files.append(path)
    secrets = [v.encode() for p in (BASE/'credentials').glob('*.json') for k,v in json.loads(p.read_text()).items() if k.endswith('_KEY') and isinstance(v,str)]
    assert len(secrets) == 4
    inventory = []
    archive = ROOT / 'final-data.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for path in sorted(set(files)):
            data = path.read_bytes()
            if any(secret in data for secret in secrets):
                raise RuntimeError('Credential detected in final data: ' + str(path))
            name = str(path.relative_to(ROOT))
            inventory.append({'path':name, 'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest()})
            tar.add(path, arcname=name, recursive=False)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    location = urlparse(PROTOCOL['launch_proposal']['data_destination'])
    key = location.path.strip('/') + '/final/' + digest + '.tar.gz'
    client = boto3.client('s3', region_name='us-east-1')
    client.upload_file(str(archive), location.netloc, key,
        ExtraArgs={'ServerSideEncryption':'AES256', 'Metadata':{'sha256':digest}})
    response = client.get_object(Bucket=location.netloc, Key=key)['Body']
    try:
        verified = hashlib.sha256()
        while chunk := response.read(1024**2):
            verified.update(chunk)
    finally:
        response.close()
    if verified.hexdigest() != digest:
        raise RuntimeError('Final archive round-trip mismatch; preserve the worker disk')
    index = {'archive':{'uri':f's3://{location.netloc}/{key}', 'sha256':digest, 'bytes':archive.stat().st_size},
             'files':inventory, 'verified_at':datetime.now(timezone.utc).isoformat(), 'summary':summary}
    data = (json.dumps(index, indent=2)+'\n').encode()
    index_sha = hashlib.sha256(data).hexdigest()
    index_key = location.path.strip('/') + '/final/' + index_sha + '.json'
    client.put_object(Bucket=location.netloc, Key=index_key, Body=data, ServerSideEncryption='AES256')
    with client.get_object(Bucket=location.netloc, Key=index_key)['Body'] as stream:
        if hashlib.sha256(stream.read()).hexdigest() != index_sha:
            raise RuntimeError('Final index round-trip mismatch')
    summary['final_backup'] = {'index_uri':f's3://{location.netloc}/{index_key}', 'index_sha256':index_sha,
                               **index['archive'], 'file_count':len(inventory)}
    record()
    (ROOT / 'final-backup.json').write_text(json.dumps(summary['final_backup'], indent=2)+'\n')


try:
    record()
    while not (BASE/'search-ended.json').exists():
        if time.time()>DEADLINE:raise TimeoutError('Science deadline while waiting for search')
        launcher=BASE/'search-launcher.json'
        if launcher.exists():
            pid=json.loads(launcher.read_text())['pid']; stat=Path(f'/proc/{pid}/stat')
            if not stat.exists() or stat.read_text().split()[2]=='Z':raise RuntimeError('Search controller exited without final checkpoint; preserve data')
        time.sleep(15)
    search=json.loads((BASE/'search-ended.json').read_text())
    if search['status']!='completed':raise RuntimeError('Search has failed arms; preserve results and notify before selecting validation genotypes')
    if not search['all_model_children_stopped']:raise RuntimeError('Search children still active')
    checks=json.loads((BASE/'validation-readiness.json').read_text());assert checks['status']=='passed'
    summary['status']='preparing_validation';record()
    with (ROOT/'input-preparation.log').open('xb') as log:
        subprocess.run([sys.executable,str(BASE/'yggdrisil-prepare-continuation-validation.py')],env={**os.environ,'PYTHONPATH':str(BASE/'application/src')},cwd=BASE,stdout=log,stderr=subprocess.STDOUT,check=True)
    summary['status']='calibrating';record()
    launch('calibration')
    wait_for(['calibration'])
    result = json.loads((ROOT / 'runs/calibration/results.json').read_text())
    if not result['all_reached_max_generations']:
        raise RuntimeError('MG1655 Linux control failed to divide twice')
    rss = []
    for trace in (ROOT / 'runs/calibration').glob('trace--*.csv'):
        for row in csv.DictReader(trace.open()):
            if row.get('peak_rss', '').isdigit():
                rss.append(int(row['peak_rss']))
    if not rss or max(rss) > 4 * 1024**3:
        raise RuntimeError('Linux memory calibration needs review before parallel dispatch')
    summary['calibration'] = {'divisions':2, 'peak_rss_bytes':max(rss), 'status':'passed'}
    summary['status'] = 'running'
    record()
    labels = [f'seed-{seed}' for seed in PROTOCOL['seeds']]
    for label in labels:
        launch(label)
    wait_for(labels)
    summary['status'] = ('completed' if all(j['status']=='completed' for j in summary['jobs'].values()) else 'completed_with_exceptions')
except BaseException as error:
    summary['status'] = 'failed'
    summary['error'] = repr(error)
    print('Remote validation stopped:', repr(error), flush=True)
finally:
    stop_children()
    for log in logs:
        log.close()
    record()
    try:
        final_backup()
    except BaseException as error:
        summary['backup_failure'] = repr(error)
        record()
        raise
    finally:
        subprocess.run(['sudo', 'shutdown', '-h', '+2'], check=True)
