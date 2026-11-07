import hashlib
import json
import subprocess
from pathlib import Path

operation = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/literature-ablation-kimi-continuation-20261106')
prior_bytes = (operation / 'solver-monitoring.json').read_bytes()
payload = {
    'prior': json.loads(prior_bytes),
    'prior_sha256': hashlib.sha256(prior_bytes).hexdigest(),
    'snapshot': json.loads((operation / 'monitoring-summary.json').read_text()),
}
remote_code = '''
import hashlib, json, math, os, tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import boto3, wandb

r = Path('/home/ubuntu/yggdrisil-continuation-20261106')
os.environ.update(json.loads((r / 'credentials/common.json').read_text()))
s3 = boto3.client('s3', region_name='us-east-1')
api = wandb.Api(timeout=60)
def sha(data):
    return hashlib.sha256(data).hexdigest()
def get(ref):
    u = urlparse(ref['uri'])
    with s3.get_object(Bucket=u.netloc, Key=u.path.lstrip('/'))['Body'] as stream:
        data = stream.read()
    if sha(data) != ref['sha256'] or len(data) != ref['bytes']:
        raise RuntimeError('Remote content checksum/size mismatch: ' + ref['uri'])
    return data
def identity(x):
    return (x['seed'], x['genotype'], x['generation'], x['diagnostic_sha256'])
prior = {identity(x): x for x in payload['prior']['records']}
seen = set()
records = []
new_count = 0
for seed, job in sorted(payload['snapshot']['validation_jobs'].items()):
    index_ref = job['s3_index']
    index = json.loads(get(index_ref))
    progress = json.loads(get(index['files']['progress.json']))
    artifact_name = job['catalog']['data_backup']['wandb_artifact']
    artifact = None
    for genotype in progress['finalists']:
        for generation in genotype['generations']:
            for fallback in generation.get('solver_fallbacks', []):
                key = (seed, genotype['name'], generation['generation'], fallback['sha256'])
                if key in seen:
                    raise RuntimeError('Duplicate diagnostic identity')
                seen.add(key)
                if key in prior:
                    if not prior[key]['worker_s3_wandb_verified']:
                        raise RuntimeError('Prior audit is not verified')
                    records.append(prior[key])
                    continue
                p = Path(fallback['path'])
                data = p.read_bytes()
                if sha(data) != fallback['sha256']:
                    raise RuntimeError('Closed worker diagnostic changed')
                d = json.loads(data)
                if not d['model_unchanged'] or d['return_code'] != 0 or d['status'] != 'GLP_OPT: optimal':
                    raise RuntimeError('Unaccepted solver result')
                if d['tolerance_bound'] != 1e-7 or d['tolerance_dual'] != 1e-7:
                    raise RuntimeError('Solver tolerances changed')
                errors = [v['relative_error'] for v in d['kkt'].values()]
                if len(errors) != 4 or not all(math.isfinite(v) and 0 <= v <= 1e-7 for v in errors):
                    raise RuntimeError('KKT validation failed')
                lp_files = sorted(p.parent.glob(p.stem + '*.glp'))
                if len(lp_files) != 2:
                    raise RuntimeError('Expected exactly two LP snapshots')
                if artifact is None:
                    artifact = api.artifact(artifact_name)
                    if artifact.state != 'COMMITTED':
                        raise RuntimeError('W&B artifact not committed')
                with tempfile.TemporaryDirectory(prefix='solver-audit-', dir=r) as tmp:
                    for path in [p, *lp_files]:
                        worker = path.read_bytes()
                        suffix = '/nextflow/' + str(path).split('/nextflow/', 1)[1]
                        matches = [name for name in index['files'] if name.endswith(suffix)]
                        if len(matches) != 1:
                            raise RuntimeError('Expected one archive entry for ' + str(path))
                        name = matches[0]
                        if get(index['files'][name]) != worker:
                            raise RuntimeError('Worker/S3 mismatch')
                        saved = Path(artifact.get_entry(name).download(root=tmp))
                        if sha(saved.read_bytes()) != sha(worker):
                            raise RuntimeError('Worker/W&B mismatch')
                        if path.suffix == '.glp' and sha(worker) != d['original_lp_sha256']:
                            raise RuntimeError('LP changed during recovery')
                records.append({'seed': seed, 'genotype': genotype['name'],
                    'generation': generation['generation'], 'diagnostic_sha256': fallback['sha256'],
                    'original_lp_sha256': d['original_lp_sha256'],
                    'max_kkt_relative_error': max(errors), 's3_index': index_ref,
                    'wandb_artifact': artifact_name, 'worker_s3_wandb_verified': True})
                new_count += 1
if not set(prior).issubset(seen):
    raise RuntimeError('Previously verified diagnostic absent from current progress')
result = {'verified_at': datetime.now(timezone.utc).isoformat(),
    'progress_snapshot_at': payload['snapshot']['checked_at'], 'status': 'verified',
    'accepted_fallback_count': len(records), 'newly_download_verified': new_count,
    'prior_audit_sha256': payload['prior_sha256'],
    'prior_audit_verified_at': payload['prior']['verified_at'],
    'verification_scope': payload['prior']['verification_scope'],
    'records': records, 'interpretation': payload['prior']['interpretation']}
encoded = json.dumps(result, indent=2) + '\\n'
secrets = [v for p in (r/'credentials').glob('*.json') for k,v in json.loads(p.read_text()).items() if k.endswith('_KEY') and isinstance(v,str)]
if any(v in encoded for v in secrets):
    raise RuntimeError('Credential in audit metadata')
print(encoded, end='')
'''
command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes',
    '-o', 'HostKeyAlias=54.163.102.65', '-i', str(Path.home()/'.ssh/mcclain-phd-aws-dev.pem'),
    'ubuntu@100.53.103.11', '/home/ubuntu/yggdrisil-continuation-20261106/env/bin/python -']
script = 'payload = ' + repr(payload) + '\n' + remote_code
completed = subprocess.run(command, input=script, text=True, capture_output=True)
if completed.returncode:
    Path('/private/tmp/yggdrisil-solver-audit-error.log').write_text(completed.stderr)
    raise RuntimeError('Remote solver audit failed; inspect /private/tmp/yggdrisil-solver-audit-error.log')
result = json.loads(completed.stdout)
assert result['status'] == 'verified'
stamp = result['verified_at'].split('.')[0].replace('-', '').replace(':', '') + 'Z'
data = (json.dumps(result, indent=2) + '\n').encode()
prior_path = operation / ('solver-audit-' + payload['prior_sha256'] + '.json')
if prior_path.exists() and prior_path.read_bytes() != prior_bytes:
    raise RuntimeError('Prior audit archive changed')
prior_path.write_bytes(prior_bytes)
(operation / ('solver-monitoring-' + stamp + '.json')).write_bytes(data)
(operation / 'solver-monitoring.json').write_bytes(data)
print(json.dumps({k: v for k,v in result.items() if k != 'records'}))
